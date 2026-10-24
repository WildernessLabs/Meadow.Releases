#!/usr/bin/env python3
"""
Create a Meadow release in the Meadow.Releases repository.

Reads release.yaml, checks it against the source repositories, downloads the binaries built by
GitHub Actions for the pinned commits, stages the release assets (zips, manifest, sources) and
creates the GitHub Release, tags the source commits and moves the version on.  See README.md.

    ./create_release.py --dry-run          run every check and stage the assets, publish nothing
    ./create_release.py                    the same, then create the release (asks first)

Requires: Python 3.9+, PyYAML, and the GitHub CLI (gh) logged in with access to the repositories.
Nothing here depends on anything that is only available in CI, so a release manager can run it
from a laptop.
"""

import argparse
import base64
import datetime
import hashlib
import json
import re
import struct
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip install pyyaml")

SCRIPT_DIR = Path(__file__).resolve().parent

#
#   Where each release source lives and which workflow builds it.  "Legacy" is not supported yet.
#
OS_SOURCES = {
    "rebase": "WildernessLabs/Meadow.OS",
    "meadow / rebase": "WildernessLabs/Meadow.OS",
    "meadow.os": "WildernessLabs/Meadow.OS",
}
OS_WORKFLOW = "Build OS and RT"
OS_ARTIFACTS = ["Meadow.OS", "meadow_assemblies", "Meadow.OS Debug Symbols"]
ESP32_REPO = "WildernessLabs/Meadow-ESP32"
ESP32_WORKFLOW = "Build"
ESP32_ARTIFACTS = ["network", "build"]

#
#   Only runs of a push (or manual) trigger built the commit itself.  A pull request run builds
#   the merge of the PR into its base, which is not the pinned commit.
#
RUN_EVENTS = ("push", "workflow_dispatch")

#
#   The branch each repository's version bump pull request targets.
#
BUMP_BASE = {
    "WildernessLabs/Meadow.OS": "main",
    ESP32_REPO: "develop",
    "WildernessLabs/Meadow": "main",
}

OS_FILES = ["Meadow.OS.bin", "Meadow.OS.Update.bin", "Meadow.OS.Runtime.bin"]
ESP32_FILES = ["bootloader.bin", "partition-table.bin", "MeadowComms.bin"]
BCL_FOLDER = "meadow_assemblies"

#
#   owner/name from a GitHub URL; repository names can contain dots (Meadow.OS).
#
GITHUB_REPO = r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$"

errors = []
warnings = []


class GhError(Exception):
    pass


def say(message=""):
    print(message, flush=True)


def fail(message):
    errors.append(message)
    say("  ERROR: " + message)


def warn(message):
    warnings.append(message)
    say("  WARN:  " + message)


def ok(message):
    say("  ok:    " + message)


def gh(*args, binary=False):
    result = subprocess.run(["gh", *args], capture_output=True)
    if result.returncode != 0:
        raise GhError(result.stderr.decode(errors="replace").strip())
    return result.stdout if binary else result.stdout.decode()


def gh_json(path):
    return json.loads(gh("api", path))


def gh_raw(repo, path, ref):
    return gh("api", "-H", "Accept: application/vnd.github.raw", f"repos/{repo}/contents/{path}?ref={ref}")


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_version(text):
    parts = text.strip().split(".")
    if len(parts) < 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"'{text}' is not a dotted numeric version")
    return [int(p) for p in parts]


def release_type(y):
    return "Beta" if (y % 2) == 1 else "Full"


def next_y(y):
    return y + 2 if (y % 2) == 1 else y + 1


#
#   Source repository checks
#
def resolve_commit(repo, short):
    try:
        return gh_json(f"repos/{repo}/commits/{short}")["sha"]
    except GhError as e:
        fail(f"commit '{short}' not found in {repo}: {e}")
        return None


def version_at(repo, sha):
    """Return (version.txt text, build number) at a commit, matching build.sh."""
    text = gh_raw(repo, "version.txt", sha).splitlines()[0].strip()
    origin = gh_json(f"repos/{repo}/commits?path=version.txt&sha={sha}&per_page=1")[0]["sha"]
    if origin == sha:
        return text, 0
    return text, gh_json(f"repos/{repo}/compare/{origin}...{sha}")["ahead_by"]


def runtime_pin(os_repo, os_sha):
    text = gh_raw(os_repo, "MONO_RUNTIME_COMMIT", os_sha)
    pin = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            pin[key.strip()] = value.strip()
    for key in ("repository", "commit", "tree"):
        if key not in pin:
            fail(f"MONO_RUNTIME_COMMIT at {os_sha[:8]} has no '{key}'")
            return None
    match = re.search(GITHUB_REPO, pin["repository"])
    if not match:
        fail(f"cannot read the runtime repository from '{pin['repository']}'")
        return None
    try:
        commit = gh_json(f"repos/{match.group(1)}/commits/{pin['commit']}")
    except GhError as e:
        fail(f"runtime commit {pin['commit']} not found in {match.group(1)}: {e}")
        return None
    if commit["commit"]["tree"]["sha"] != pin["tree"]:
        fail(f"runtime commit {pin['commit'][:8]} has a different tree than the pin ({pin['tree'][:8]})")
        return None
    ok(f"runtime pin {pin['commit'][:8]} exists and its tree matches")
    return {"repository": pin["repository"], "commit": pin["commit"], "tree": pin["tree"]}


def find_run(repo, workflow, sha):
    runs = gh_json(f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100")["workflow_runs"]
    runs = [r for r in runs if r["name"] == workflow and r["conclusion"] == "success" and r["event"] in RUN_EVENTS]
    if not runs:
        fail(f"no successful '{workflow}' push run for {repo}@{sha[:8]}; the commit must be one that was "
             f"built on merge (a pull request run does not count)")
        return None
    return max(runs, key=lambda r: r["created_at"])


def download_artifacts(repo, run, names, dest):
    available = {a["name"]: a for a in gh_json(f"repos/{repo}/actions/runs/{run['id']}/artifacts?per_page=100")["artifacts"]}
    good = True
    for name in names:
        if name not in available:
            fail(f"run {run['id']} of {repo} has no artifact '{name}'")
            good = False
        elif available[name]["expired"]:
            fail(f"artifact '{name}' of run {run['id']} of {repo} has expired; the commit must be rebuilt")
            good = False
    if not good:
        return False
    for name in names:
        target = dest / name.replace(" ", "_")
        target.mkdir(parents=True, exist_ok=True)
        gh("run", "download", str(run["id"]), "-R", repo, "-n", name, "-D", str(target))
        ok(f"downloaded artifact '{name}' ({run['html_url']})")
    return True


def find_one(root, name):
    found = sorted(root.rglob(name))
    return found[0] if found else None


#
#   Staging
#
def write_zip(path, entries):
    """entries: list of (arcname, source path).  Fixed timestamps keep the zip reproducible."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for arcname, source in sorted(entries):
            info = zipfile.ZipInfo(arcname, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, Path(source).read_bytes())


def download_sources(repo, sha, dest, label):
    path = dest / f"sources-{label}-{sha[:8]}.tar.gz"
    path.write_bytes(gh("api", f"repos/{repo}/tarball/{sha}", binary=True))
    try:
        with tarfile.open(path):
            pass
    except tarfile.TarError as e:
        fail(f"source archive for {repo}@{sha[:8]} is not a valid tar file: {e}")
    return path


def releases_repo_name(override):
    if override:
        return override
    url = subprocess.run(["git", "-C", str(SCRIPT_DIR), "remote", "get-url", "origin"],
                         capture_output=True, text=True).stdout.strip()
    match = re.search(GITHUB_REPO, url)
    if not match:
        sys.exit("cannot work out the Releases repository; use --repo owner/name")
    return match.group(1)


def check_releases_repo(repo, tag, dry_run):
    """Return the commit the tag will point at (the commit holding release.yaml)."""
    try:
        info = gh_json(f"repos/{repo}")
    except GhError as e:
        fail(f"cannot read {repo}: {e}")
        return None
    permissions = info.get("permissions", {})
    if not (permissions.get("push") or permissions.get("admin")):
        fail(f"your GitHub login does not have write access to {repo}")
    else:
        ok(f"write access to {repo}")

    for kind, path in (("tag", f"repos/{repo}/git/ref/tags/{tag}"), ("release", f"repos/{repo}/releases/tags/{tag}")):
        try:
            gh_json(path)
            fail(f"a {kind} named '{tag}' already exists in {repo}")
        except GhError:
            ok(f"no {kind} named '{tag}' in {repo}")

    def git(*args):
        return subprocess.run(["git", "-C", str(SCRIPT_DIR), *args], capture_output=True, text=True).stdout.strip()

    problem = fail if not dry_run else warn
    if git("status", "--porcelain", "--", "release.yaml"):
        problem("release.yaml has uncommitted changes; commit and push it before a real release "
                "(the tag points at the commit holding it)")
    head = git("rev-parse", "HEAD")
    if not git("branch", "-r", "--contains", head):
        problem("the current commit of the Releases repository is not pushed")
    return head


#
#   Tagging the source commits.  After the release the pinned commit in each source repository gets
#   the release tag (an annotated tag), so the release can be found from the source.  An existing
#   tag on the same commit is left alone; a tag on a different commit is an error, because tags
#   are never moved.
#
def tag_state(repo, tag, sha):
    """Return 'create', 'exists' (already on sha), or None (after reporting an error)."""
    try:
        ref = gh_json(f"repos/{repo}/git/ref/tags/{tag}")["object"]
    except GhError:
        return "create"
    target = ref["sha"]
    if ref["type"] == "tag":
        target = gh_json(f"repos/{repo}/git/tags/{target}")["object"]["sha"]
    if target == sha:
        return "exists"
    fail(f"{repo} already has a tag '{tag}' on a different commit ({target[:8]}, not {sha[:8]}); tags are not moved")
    return None


def do_tag(repo, tag, sha, releases_repo):
    if tag_state(repo, tag, sha) == "exists":
        ok(f"{repo}: tag {tag} is already on {sha[:8]}")
        return
    message = f"Meadow release {tag}\n\nhttps://github.com/{releases_repo}/releases/tag/{tag}\n"
    created = json.loads(gh("api", "-X", "POST", f"repos/{repo}/git/tags", "-f", f"tag={tag}",
                            "-f", f"message={message}", "-f", f"object={sha}", "-f", "type=commit"))
    gh("api", "-X", "POST", f"repos/{repo}/git/refs", "-f", f"ref=refs/tags/{tag}", "-f", f"sha={created['sha']}")
    ok(f"{repo}: tagged {sha[:8]} as {tag}")


def check_push_access(repo, dry_run):
    permissions = gh_json(f"repos/{repo}").get("permissions", {})
    if permissions.get("push") or permissions.get("admin"):
        ok(f"write access to {repo} (for the tag and the version bump)")
    else:
        (warn if dry_run else fail)(f"your GitHub login does not have write access to {repo}, "
                                     f"which the tag and the version bump need")


#
#   Version bump.  After a release the version.txt of the source repositories moves on: y goes up
#   by 2 from a beta (odd) and by 1 from a full release (even), so the next build is a beta.  The
#   change goes on a branch meadow-<new version> and a pull request to the base branch in
#   BUMP_BASE.  Every step checks what already exists, so the bump is safe to run again.
#
def first_line(text):
    return text.splitlines()[0].strip() if text.strip() else ""


def plan_bump(repo, released, new_version):
    base = BUMP_BASE[repo]
    branch = f"meadow-{new_version}"
    plan = {"repo": repo, "base": base, "branch": branch, "new_version": new_version, "skip": None,
            "need_branch": False, "need_commit": False, "need_pr": False, "pr": None}
    plan["base_sha"] = gh_json(f"repos/{repo}/git/ref/heads/{base}")["object"]["sha"]
    current = first_line(gh_raw(repo, "version.txt", base))
    try:
        gh_json(f"repos/{repo}/git/ref/heads/{branch}")
        branch_exists = True
    except GhError:
        branch_exists = False
    pr = gh("pr", "list", "-R", repo, "--head", branch, "--state", "all", "--json", "url", "--jq", ".[0].url").strip()
    plan["pr"] = pr or None
    if current == new_version and not branch_exists:
        plan["skip"] = f"{base} is already at {new_version}"
    elif current not in (released, new_version):
        plan["skip"] = f"{base} version.txt is {current}, expected {released}; not bumping"
    elif pr:
        plan["skip"] = f"pull request already exists: {pr}"
    else:
        on_branch = first_line(gh_raw(repo, "version.txt", branch)) if branch_exists else current
        plan["need_branch"] = not branch_exists
        plan["need_commit"] = on_branch != new_version
        plan["need_pr"] = True
    return plan


def describe_bump(plan):
    say(f"  {plan['repo']}: version.txt -> {plan['new_version']}, branch {plan['branch']}, PR to {plan['base']}")
    if plan["skip"]:
        say(f"    skipped: {plan['skip']}")
        return
    steps = [text for needed, text in ((plan["need_branch"], f"create branch from {plan['base']} {plan['base_sha'][:8]}"),
                                       (plan["need_commit"], "commit the new version.txt"),
                                       (plan["need_pr"], "open the pull request")) if needed]
    say("    would: " + ", ".join(steps))


def do_bump(plan, tag):
    repo, branch = plan["repo"], plan["branch"]
    if plan["skip"]:
        say(f"  {repo}: skipped: {plan['skip']}")
        return
    if plan["need_branch"]:
        gh("api", "-X", "POST", f"repos/{repo}/git/refs", "-f", f"ref=refs/heads/{branch}", "-f", f"sha={plan['base_sha']}")
        ok(f"{repo}: created branch {branch}")
    if plan["need_commit"]:
        info = gh_json(f"repos/{repo}/contents/version.txt?ref={branch}")
        raw = gh_raw(repo, "version.txt", branch)
        new_raw = raw.replace(first_line(raw), plan["new_version"], 1)
        gh("api", "-X", "PUT", f"repos/{repo}/contents/version.txt",
           "-f", f"message=Bump the version to {plan['new_version']} after release {tag}",
           "-f", "content=" + base64.b64encode(new_raw.encode()).decode(),
           "-f", f"sha={info['sha']}", "-f", f"branch={branch}")
        ok(f"{repo}: committed version.txt {plan['new_version']}")
    if plan["need_pr"]:
        url = gh("pr", "create", "-R", repo, "--base", plan["base"], "--head", branch,
                 "--title", f"Bump the version to {plan['new_version']}",
                 "--body", f"Moves version.txt on after release {tag}.").strip()
        ok(f"{repo}: opened {url}")


def bump_plans(version, os_repo):
    released = ".".join(str(p) for p in version[:3])
    new_version = f"{version[0]}.{version[1]}.{next_y(version[2])}"
    return [plan_bump(repo, released, new_version) for repo in (os_repo, ESP32_REPO)]


#
#   Main
#
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("release_file", nargs="?", default=str(SCRIPT_DIR / "release.yaml"))
    parser.add_argument("--dry-run", action="store_true",
                        help="run all checks and stage the assets, but publish and push nothing")
    parser.add_argument("--work-dir", help="where to download and stage; each run uses a new timestamped folder inside it (default work/<tag> beside this script)")
    parser.add_argument("--skip-sources", action="store_true", help="do not download the source archives")
    parser.add_argument("--repo", help="the Releases repository (default: this checkout's origin)")
    parser.add_argument("--tag-only", action="store_true",
                        help="only tag the source commits with the release tag; use to repeat tagging "
                             "that failed after the release was published")
    parser.add_argument("--bump-only", action="store_true",
                        help="only do the version bump (branch, commit, pull request) for the versions in release.yaml; "
                             "use to repeat a bump that failed after the release was published")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation before publishing")
    args = parser.parse_args()

    with open(args.release_file) as f:
        release = yaml.load(f, Loader=yaml.BaseLoader)["Release"]

    tag_text = release.get("Tag", "").strip()
    version_text = release.get("Version", "").strip()
    description = release.get("Description", "").strip()
    os_source = release.get("OS", {}).get("Source", "").strip()
    os_short = release.get("OS", {}).get("Commit", "").strip()
    esp_short = release.get("ESP32", {}).get("Commit", "").strip()

    say(f"{'DRY RUN: ' if args.dry_run else ''}release file {args.release_file}")
    say("")

    say("Release file")
    for name, value in (("Version", version_text), ("OS.Source", os_source),
                        ("OS.Commit", os_short), ("ESP32.Commit", esp_short)):
        if not value:
            fail(f"release.yaml has no {name}")
    if errors:
        return finish(args)

    #
    #   Version is major.minor.y, or major.minor.y.build.  The build number is derived from the
    #   OS repository the same way build.sh derives it (commits since version.txt last changed);
    #   when it is written here it is checked against the derived value.
    #
    try:
        version = parse_version(version_text)
        if len(version) not in (3, 4):
            raise ValueError("expected major.minor.y or major.minor.y.build")
    except ValueError as e:
        fail(f"Version: {e}")
        return finish(args)

    os_repo = OS_SOURCES.get(os_source.lower())
    if os_repo is None:
        fail(f"OS.Source '{os_source}' is not supported (expected Rebase or Meadow.OS)")
        return finish(args)

    if args.bump_only:
        say("")
        say("Version bump")
        plans = bump_plans(version, os_repo)
        for plan in plans:
            describe_bump(plan)
        if args.dry_run or not any(not plan["skip"] for plan in plans):
            say("DRY RUN: nothing was changed." if args.dry_run else "nothing to do")
            return 0
        if not args.yes and input("Type 'bump' to make these changes: ").strip() != "bump":
            say("not confirmed, nothing changed")
            return 1
        for plan in plans:
            do_bump(plan, version_text)
        return 0

    kind = release_type(version[2])
    declared = release.get("Type", "").strip()
    if declared:
        normalised = {"beta": "Beta", "internal": "Beta", "full": "Full", "customer": "Full"}.get(declared.lower())
        if normalised is None:
            fail(f"Type '{declared}' is not Beta or Full")
        elif normalised != kind:
            fail(f"Type '{declared}' does not match version y={version[2]} ({'odd' if kind == 'Beta' else 'even'}, so {kind})")
        elif declared.lower() in ("internal", "customer"):
            warn(f"Type '{declared}' is the old name; use {kind}")
    ok(f"y={version[2]} makes this a {kind} release")

    say("")
    say("Source commits")
    os_sha = resolve_commit(os_repo, os_short)
    esp_sha = resolve_commit(ESP32_REPO, esp_short)
    if not (os_sha and esp_sha):
        return finish(args)
    ok(f"{os_repo} {os_sha}")
    ok(f"{ESP32_REPO} {esp_sha}")

    os_version_text, os_build = version_at(os_repo, os_sha)
    esp_version_text, esp_build = version_at(ESP32_REPO, esp_sha)
    try:
        os_v = parse_version(os_version_text)
        esp_v = parse_version(esp_version_text)
    except ValueError as e:
        fail(f"version.txt: {e}")
        return finish(args)
    if os_v[:3] != version[:3]:
        fail(f"{os_repo}@{os_sha[:8]} version.txt is {os_version_text}, but release.yaml says "
             f"{'.'.join(map(str, version[:3]))}")
        return finish(args)
    version = os_v[:3] + [os_build]
    version_text = ".".join(str(p) for p in version)
    ok(f"{os_repo} version.txt {os_version_text}, {os_build} commits since it changed: version {version_text}")
    given = release.get("Version", "").strip()
    if len(given.split(".")) == 4 and given != version_text:
        fail(f"release.yaml says version {given} but the derived version is {version_text}")
    if esp_v[:3] != version[:3]:
        fail(f"{ESP32_REPO}@{esp_sha[:8]} version.txt is {esp_version_text}, expected {'.'.join(map(str, version[:3]))}")
    else:
        ok(f"{ESP32_REPO} version.txt {esp_version_text} matches (its own build number is {esp_build})")

    #
    #   The tag is the version unless release.yaml names a different one.
    #
    if not tag_text or tag_text == given:
        tag = version_text
    else:
        tag = tag_text
        warn(f"Tag '{tag}' is not the same as the version '{version_text}'")
    ok(f"tag {tag}")

    say("")
    say("Source repository tags and access")
    tag_targets = [(os_repo, os_sha), (ESP32_REPO, esp_sha)]
    for repo, sha in tag_targets:
        state = tag_state(repo, tag, sha)
        if state == "create":
            ok(f"{repo}: tag {tag} will be created on {sha[:8]}")
        elif state == "exists":
            ok(f"{repo}: tag {tag} is already on {sha[:8]}")
        check_push_access(repo, args.dry_run)

    if args.tag_only:
        if errors:
            return finish(args)
        if args.dry_run:
            say("DRY RUN: nothing was changed.")
            return 0
        if not args.yes and input(f"Type the tag '{tag}' to tag the source commits: ").strip() != tag:
            say("not confirmed, nothing changed")
            return 1
        for repo, sha in tag_targets:
            do_tag(repo, tag, sha, releases_repo_name(args.repo))
        return 0

    releases_repo = releases_repo_name(args.repo)
    say("")
    say(f"Releases repository ({releases_repo})")
    target_commit = check_releases_repo(releases_repo, tag, args.dry_run)

    runtime = runtime_pin(os_repo, os_sha)

    say("")
    say("Binaries built by GitHub Actions")
    #
    #   Each run works in a new directory (never reusing or deleting an old one), because
    #   gh refuses to download over existing files.
    #
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    work = (Path(args.work_dir) if args.work_dir else SCRIPT_DIR / "work" / tag) / stamp
    work.mkdir(parents=True)
    say(f"  work directory: {work}")
    os_run = find_run(os_repo, OS_WORKFLOW, os_sha)
    esp_run = find_run(ESP32_REPO, ESP32_WORKFLOW, esp_sha)
    if not (os_run and esp_run):
        return finish(args)
    ok(f"OS run {os_run['id']} {os_run['html_url']}")
    ok(f"ESP32 run {esp_run['id']} {esp_run['html_url']}")

    dl = work / "download"
    if not (download_artifacts(os_repo, os_run, OS_ARTIFACTS, dl / "os")
            and download_artifacts(ESP32_REPO, esp_run, ESP32_ARTIFACTS, dl / "esp32")):
        return finish(args)

    os_root = dl / "os"
    esp_root = dl / "esp32" / "network"
    os_files = {}
    for name in OS_FILES + ["build-info.json"]:
        found = find_one(os_root / "Meadow.OS", name)
        if found is None:
            fail(f"{name} is not in the Meadow.OS artifact")
        else:
            os_files[name] = found
    esp_files = {}
    for name in ESP32_FILES + [n + ".md5" for n in ESP32_FILES]:
        found = find_one(esp_root, name)
        if found is None:
            fail(f"{name} is not in the ESP32 'network' artifact")
        else:
            esp_files[name] = found
    if len(os_files) != len(OS_FILES) + 1 or len(esp_files) != len(ESP32_FILES) * 2:
        return finish(args)
    bcl_dir = os_root / "meadow_assemblies"
    bcl_files = sorted(p for p in bcl_dir.rglob("*") if p.is_file())
    if not any(p.suffix == ".dll" for p in bcl_files):
        fail("the meadow_assemblies artifact contains no .dll files")
        return finish(args)

    #
    #   The binaries must come from the pinned commits.  The run was found by commit; the OS
    #   binaries carry their commit in build-info.json, and the ESP32 binary the last eight
    #   characters of it as the build hash (a little-endian 32-bit number).
    #
    built = json.loads(os_files["build-info.json"].read_text())["git"]["meadow"][0]
    if built != os_sha:
        fail(f"build-info.json says the OS was built from {built}, not {os_sha}")
    else:
        ok("build-info.json commit matches the pinned OS commit")
    #
    #   The version stamped into the OS (CONFIG_VERSION_BUILD, "y.build" followed by the build
    #   date) must equal the derived one; a mismatch means the CI build used a different count.
    #
    stamp = f"{version[2]}.{version[3]} ".encode()
    if stamp in os_files["Meadow.OS.Update.bin"].read_bytes():
        ok(f"Meadow.OS.Update.bin is stamped with build {version[2]}.{version[3]}")
    else:
        fail(f"Meadow.OS.Update.bin is not stamped with build {version[2]}.{version[3]}; "
             f"the CI build used a different build number than {version_text}")
    if struct.pack("<I", int(esp_sha[-8:], 16)) in esp_files["MeadowComms.bin"].read_bytes():
        ok("MeadowComms.bin contains the pinned ESP32 build hash")
    else:
        fail(f"MeadowComms.bin does not contain the build hash {esp_sha[-8:]} of the pinned ESP32 commit")
    for name in ESP32_FILES:
        wanted = esp_files[name + ".md5"].read_text().split()[0].lower()
        actual = hashlib.md5(esp_files[name].read_bytes()).hexdigest()
        if wanted != actual:
            fail(f"{name} does not match its .md5 file")
    ok("ESP32 binaries checked against their .md5 files")

    say("")
    say("Staging the release assets")
    out = work / "release"
    out.mkdir(parents=True, exist_ok=True)
    os_zip = out / "Meadow.OS.zip"
    net_zip = out / "Meadow.Network.zip"
    dbg_zip = out / "Meadow.Debug.zip"
    write_zip(os_zip, [(n, os_files[n]) for n in OS_FILES]
              + [(f"{BCL_FOLDER}/{p.relative_to(bcl_dir).as_posix()}", p) for p in bcl_files])
    write_zip(net_zip, [(n, esp_files[n]) for n in esp_files])
    debug = [(f"os/{p.relative_to(dl / 'os' / 'Meadow.OS_Debug_Symbols').as_posix()}", p)
             for p in (dl / "os" / "Meadow.OS_Debug_Symbols").rglob("*") if p.is_file()]
    debug += [(f"esp32/{p.relative_to(dl / 'esp32' / 'build').as_posix()}", p)
              for p in (dl / "esp32" / "build").rglob("*") if p.is_file() and p.suffix in (".elf", ".map")]
    write_zip(dbg_zip, debug)
    for z in (os_zip, net_zip, dbg_zip):
        ok(f"{z.name} ({z.stat().st_size} bytes)")

    sources = []
    if not args.skip_sources:
        sources.append(download_sources(os_repo, os_sha, out, "Meadow.OS"))
        sources.append(download_sources(ESP32_REPO, esp_sha, out, "Meadow-ESP32"))
        for s in sources:
            ok(f"{s.name} ({s.stat().st_size} bytes)")
    else:
        warn("source archives skipped (--skip-sources)")

    manifest = {
        "tag": tag,
        "version": version_text,
        "generation": "3.0",
        "type": kind,
        "description": description,
        "minCLIVersion": release.get("MinCLIVersion", "3.0.0"),
        "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": {z.name: {"sha256": sha256_of(z)} for z in (os_zip, net_zip, dbg_zip)},
        "contents": {**{n: {"sha256": sha256_of(os_files[n])} for n in OS_FILES},
                     **{n: {"sha256": sha256_of(esp_files[n])} for n in ESP32_FILES}},
        "sources": {
            "Meadow.OS": {"repository": f"https://github.com/{os_repo}", "commit": os_sha},
            "Meadow-ESP32": {"repository": f"https://github.com/{ESP32_REPO}", "commit": esp_sha},
            "runtime": runtime,
        },
        "builds": {"Meadow.OS": os_run["html_url"], "Meadow-ESP32": esp_run["html_url"]},
    }
    manifest_path = out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    ok("manifest.json")
    yaml_copy = out / "release.yaml"
    yaml_copy.write_bytes(Path(args.release_file).read_bytes())

    notes = out / "notes.md"
    notes.write_text(
        f"{description}\n\nVersion {version_text} ({kind})\n\n"
        f"- Meadow.OS {os_sha}\n- Meadow-ESP32 {esp_sha}\n"
        f"- runtime {runtime['commit'] if runtime else 'unknown'}\n")

    assets = [os_zip, net_zip, dbg_zip, manifest_path, yaml_copy] + sources
    title = f"{description} ({version_text})" if description else version_text

    say("")
    say("Version bump after the release")
    plans = bump_plans(version, os_repo)
    for plan in plans:
        describe_bump(plan)

    if finish(args, final=False):
        return 1

    say("")
    say(f"Release {tag} ({kind}) in {releases_repo}: {len(assets)} assets, target commit {(target_commit or '')[:8]}")
    if args.dry_run:
        say("DRY RUN: nothing was published or pushed.  Staged assets are in " + str(out))
        return 0

    if not args.yes and input(f"Type the tag '{tag}' to publish this release: ").strip() != tag:
        say("not confirmed, nothing published")
        return 1

    command = ["release", "create", tag, *map(str, assets), "-R", releases_repo, "--title", title,
               "--notes-file", str(notes), "--target", target_commit]
    command += ["--prerelease", "--latest=false"] if kind == "Beta" else ["--latest"]
    say(gh(*command).strip())

    say("")
    say("Tagging the source commits")
    try:
        for repo, sha in tag_targets:
            do_tag(repo, tag, sha, releases_repo)
    except GhError as e:
        say(f"  ERROR: tagging failed after the release was published: {e}")
        say("  The release stands.  Fix the cause and run again with --tag-only (and --bump-only).")
        return 1

    say("")
    say("Version bump")
    try:
        for plan in plans:
            do_bump(plan, tag)
    except GhError as e:
        say(f"  ERROR: the version bump failed after the release was published: {e}")
        say("  The release stands.  Fix the cause and run again with --bump-only.")
        return 1
    return 0


def finish(args, final=True):
    say("")
    if warnings:
        say(f"{len(warnings)} warning(s)")
    if errors:
        say(f"{len(errors)} error(s); nothing was published")
        for e in errors:
            say("  - " + e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
