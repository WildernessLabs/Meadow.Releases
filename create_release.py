#!/usr/bin/env python3
"""Create a Meadow release in the Meadow.Releases repository.

Reads release.yaml, checks it against the source repositories, downloads the binaries built by
GitHub Actions for the pinned commits, stages the release assets (zips, manifest, sources) and
creates the GitHub Release, tags the source commits and moves the version on.  See README.md.

    ./create_release.py --dry-run          run every check and stage the assets, publish nothing
    ./create_release.py                    the same, then create the release (asks first)

Requires: Python 3.9+, PyYAML, and the GitHub CLI (gh) logged in with access to the repositories.
Nothing here depends on anything that is only available in CI, so a release manager can run it
from a laptop.
"""

from __future__ import annotations

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
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip install pyyaml")

SCRIPT_DIR = Path(__file__).resolve().parent

# Where each release source lives and which workflow builds it.  "Legacy" is not supported yet.
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

# Only runs of a push (or manual) trigger built the commit itself.  A pull request run builds the
# merge of the PR into its base, which is not the pinned commit.
RUN_EVENTS = ("push", "workflow_dispatch")

# The branch each repository's version bump pull request targets.
BUMP_BASE = {
    "WildernessLabs/Meadow.OS": "main",
    ESP32_REPO: "develop",
    "WildernessLabs/Meadow": "main",
}

OS_FILES = ["Meadow.OS.bin", "Meadow.OS.Update.bin", "Meadow.OS.Runtime.bin"]
ESP32_FILES = ["bootloader.bin", "partition-table.bin", "MeadowComms.bin"]
BCL_FOLDER = "meadow_assemblies"
DEFAULT_MIN_CLI_VERSION = "3.0.0"

# Old names for the release type, still accepted in release.yaml.
TYPE_NAMES = {"beta": "Beta", "internal": "Beta", "full": "Full", "customer": "Full"}

# owner/name from a GitHub URL; repository names can contain dots (Meadow.OS).
GITHUB_REPO = r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$"

# Fixed timestamp for zip entries, so the same inputs give the same zip.
ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)


class GhError(Exception):
    """A gh command failed; the message is gh's own error output."""


class ReleaseFileError(Exception):
    """release.yaml could not be read or is not in the expected shape."""


class Report:
    """Prints the outcome of each check and remembers the warnings and errors."""

    def __init__(self) -> None:
        """Start with no warnings and no errors."""
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def ok(self, message: str) -> None:
        """Report a check that passed."""
        say("  ok:    " + message)

    def warn(self, message: str) -> None:
        """Report a problem that does not stop the release."""
        self.warnings.append(message)
        say("  WARN:  " + message)

    def fail(self, message: str) -> None:
        """Report a problem that stops the release."""
        self.errors.append(message)
        say("  ERROR: " + message)

    def problem(self, message: str, is_error: bool) -> None:
        """Report a problem as an error or, when is_error is false, as a warning."""
        (self.fail if is_error else self.warn)(message)

    def finish(self) -> int:
        """Print the totals and return 1 if any check failed, otherwise 0."""
        say()
        if self.warnings:
            say(f"{len(self.warnings)} warning(s)")
        if self.errors:
            say(f"{len(self.errors)} error(s); nothing was published")
            for error in self.errors:
                say("  - " + error)
            return 1
        return 0


@dataclass
class ReleaseSpec:
    """The contents of release.yaml, all as stripped text ("" when absent)."""

    path: Path
    tag: str
    version: str
    description: str
    declared_type: str
    min_cli_version: str
    os_source: str
    os_commit: str
    esp32_commit: str


@dataclass
class Sources:
    """The two source repositories and commits a release is built from."""

    os_repo: str
    os_sha: str
    esp_sha: str
    version: list[int]
    tag: str = ""
    kind: str = ""
    runtime: dict[str, str] | None = None

    @property
    def version_text(self) -> str:
        """The version as dotted text."""
        return format_version(self.version)

    @property
    def targets(self) -> list[tuple[str, str]]:
        """Each source repository with the commit that is released."""
        return [(self.os_repo, self.os_sha), (ESP32_REPO, self.esp_sha)]


@dataclass
class Binaries:
    """Where the downloaded build artifacts are, and the runs that built them."""

    root: Path
    os_run: dict[str, Any]
    esp_run: dict[str, Any]
    os_files: dict[str, Path]
    esp_files: dict[str, Path]
    bcl_dir: Path
    bcl_files: list[Path]


@dataclass
class Staged:
    """The release assets that have been prepared, ready to publish."""

    out: Path
    assets: list[Path]
    notes: Path
    title: str


@dataclass
class BumpPlan:
    """What the version bump will do in one repository (see plan_bump)."""

    repo: str
    base: str
    branch: str
    new_version: str
    base_sha: str
    skip: str | None = None
    need_branch: bool = False
    need_commit: bool = False
    need_pr: bool = False


def say(message: str = "") -> None:
    """Print a line immediately, so progress shows while a long step runs."""
    print(message, flush=True)


def confirm(prompt: str, expected: str, assume_yes: bool) -> bool:
    """Return True if the release manager types `expected`; --yes skips the question."""
    return assume_yes or input(prompt).strip() == expected


def run_gh(*args: str) -> bytes:
    """Run gh and return its raw output; raise GhError if it fails."""
    result = subprocess.run(["gh", *args], capture_output=True, check=False)
    if result.returncode != 0:
        raise GhError(result.stderr.decode(errors="replace").strip())
    return result.stdout


def gh(*args: str) -> str:
    """Run gh and return its output as text."""
    return run_gh(*args).decode()


def gh_json(path: str) -> Any:
    """Call the GitHub API with gh and return the decoded JSON."""
    return json.loads(gh("api", path))


def gh_raw(repo: str, path: str, ref: str) -> str:
    """Return the text of a file in a repository at a branch, tag or commit."""
    return gh("api", "-H", "Accept: application/vnd.github.raw", f"repos/{repo}/contents/{path}?ref={ref}")


def git_output(*args: str) -> str:
    """Run git in this script's own checkout and return its output, stripped."""
    result = subprocess.run(["git", "-C", str(SCRIPT_DIR), *args], capture_output=True, text=True, check=False)
    return result.stdout.strip()


def sha256_of(path: Path) -> str:
    """Return the SHA-256 of a file as hex."""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_version(text: str) -> list[int]:
    """Split a dotted numeric version such as 2.999.51 into its parts; raise ValueError if it is not one."""
    parts = text.strip().split(".")
    if len(parts) < 3 or not all(part.isdigit() for part in parts):
        raise ValueError(f"'{text}' is not a dotted numeric version")
    return [int(part) for part in parts]


def format_version(parts: Sequence[int]) -> str:
    """Join version parts with dots."""
    return ".".join(str(part) for part in parts)


def release_type(y: int) -> str:
    """Return "Beta" for an odd y and "Full" for an even one."""
    return "Beta" if (y % 2) == 1 else "Full"


def next_y(y: int) -> int:
    """Return the y after a release: +2 from a beta (odd), +1 from a full release (even)."""
    return y + 2 if (y % 2) == 1 else y + 1


def first_line(text: str) -> str:
    """Return the first line of a text, stripped, or "" if it is blank."""
    return text.splitlines()[0].strip() if text.strip() else ""


def artifact_dir(root: Path, name: str) -> Path:
    """Return the folder an artifact is downloaded into (spaces become underscores)."""
    return root / name.replace(" ", "_")


def find_one(root: Path, name: str) -> Path | None:
    """Return the first file called `name` anywhere under `root`, or None."""
    found = sorted(root.rglob(name))
    return found[0] if found else None


def load_release_file(path: Path) -> ReleaseSpec:
    """Read release.yaml.  All values are read as text, so a hash such as 12345678 stays a string."""
    with open(path, encoding="utf-8") as stream:
        data = yaml.load(stream, Loader=yaml.BaseLoader)  # nosec - BaseLoader builds only text
    release = data.get("Release") if isinstance(data, dict) else None
    if not isinstance(release, dict):
        raise ReleaseFileError(f"{path} has no 'Release' section")

    def text(section: Any, key: str) -> str:
        return str(section.get(key, "")).strip() if isinstance(section, dict) else ""

    return ReleaseSpec(
        path=path,
        tag=text(release, "Tag"),
        version=text(release, "Version"),
        description=text(release, "Description"),
        declared_type=text(release, "Type"),
        min_cli_version=text(release, "MinCLIVersion") or DEFAULT_MIN_CLI_VERSION,
        os_source=text(release.get("OS"), "Source"),
        os_commit=text(release.get("OS"), "Commit"),
        esp32_commit=text(release.get("ESP32"), "Commit"),
    )


def validate_spec(spec: ReleaseSpec, report: Report) -> tuple[list[int], str] | None:
    """Check release.yaml itself.  Return (version parts, OS repository), or None after reporting."""
    for name, value in (("Version", spec.version), ("OS.Source", spec.os_source),
                        ("OS.Commit", spec.os_commit), ("ESP32.Commit", spec.esp32_commit)):
        if not value:
            report.fail(f"release.yaml has no {name}")
    if report.errors:
        return None

    # Version is major.minor.y, or major.minor.y.build.  The build number is derived from the OS
    # repository the same way build.sh derives it; when it is written here it is checked against
    # the derived value.
    try:
        version = parse_version(spec.version)
        if len(version) not in (3, 4):
            raise ValueError("expected major.minor.y or major.minor.y.build")
    except ValueError as error:
        report.fail(f"Version: {error}")
        return None

    os_repo = OS_SOURCES.get(spec.os_source.lower())
    if os_repo is None:
        report.fail(f"OS.Source '{spec.os_source}' is not supported (expected Rebase or Meadow.OS)")
        return None
    return version, os_repo


def check_type(declared: str, version: Sequence[int], report: Report) -> str:
    """Work out whether the release is a Beta or Full from y, and check any Type in release.yaml."""
    kind = release_type(version[2])
    if declared:
        normalised = TYPE_NAMES.get(declared.lower())
        if normalised is None:
            report.fail(f"Type '{declared}' is not Beta or Full")
        elif normalised != kind:
            report.fail(f"Type '{declared}' does not match version y={version[2]} "
                        f"({'odd' if kind == 'Beta' else 'even'}, so {kind})")
        elif declared.lower() in ("internal", "customer"):
            report.warn(f"Type '{declared}' is the old name; use {kind}")
    report.ok(f"y={version[2]} makes this a {kind} release")
    return kind


def resolve_commit(repo: str, short: str, report: Report) -> str | None:
    """Return the full hash of a commit in a repository, or None after reporting that it is missing."""
    try:
        return str(gh_json(f"repos/{repo}/commits/{short}")["sha"])
    except GhError as error:
        report.fail(f"commit '{short}' not found in {repo}: {error}")
        return None


def version_at(repo: str, sha: str) -> tuple[str, int]:
    """Return version.txt and the build number at a commit, worked out as build.sh does.

    The build number is the number of commits since version.txt last changed.
    """
    text = first_line(gh_raw(repo, "version.txt", sha))
    origin = gh_json(f"repos/{repo}/commits?path=version.txt&sha={sha}&per_page=1")[0]["sha"]
    if origin == sha:
        return text, 0
    return text, int(gh_json(f"repos/{repo}/compare/{origin}...{sha}")["ahead_by"])


def check_sources(spec: ReleaseSpec, version: list[int], os_repo: str, report: Report) -> Sources | None:
    """Resolve both commits, derive the full version and the tag, and check the versions agree.

    Returns None, after reporting, when the checks cannot go on.
    """
    say()
    say("Source commits")
    os_sha = resolve_commit(os_repo, spec.os_commit, report)
    esp_sha = resolve_commit(ESP32_REPO, spec.esp32_commit, report)
    if not (os_sha and esp_sha):
        return None
    report.ok(f"{os_repo} {os_sha}")
    report.ok(f"{ESP32_REPO} {esp_sha}")

    os_version_text, os_build = version_at(os_repo, os_sha)
    esp_version_text, esp_build = version_at(ESP32_REPO, esp_sha)
    try:
        os_version = parse_version(os_version_text)
        esp_version = parse_version(esp_version_text)
    except ValueError as error:
        report.fail(f"version.txt: {error}")
        return None
    wanted = format_version(version[:3])
    if os_version[:3] != version[:3]:
        report.fail(f"{os_repo}@{os_sha[:8]} version.txt is {os_version_text}, but release.yaml says {wanted}")
        return None

    sources = Sources(os_repo, os_sha, esp_sha, os_version[:3] + [os_build])
    report.ok(f"{os_repo} version.txt {os_version_text}, {os_build} commits since it changed: "
              f"version {sources.version_text}")
    if len(spec.version.split(".")) == 4 and spec.version != sources.version_text:
        report.fail(f"release.yaml says version {spec.version} but the derived version is {sources.version_text}")
    if esp_version[:3] != sources.version[:3]:
        report.fail(f"{ESP32_REPO}@{esp_sha[:8]} version.txt is {esp_version_text}, expected {wanted}")
    else:
        report.ok(f"{ESP32_REPO} version.txt {esp_version_text} matches (its own build number is {esp_build})")

    # The tag is the version unless release.yaml names a different one.
    if not spec.tag or spec.tag == spec.version:
        sources.tag = sources.version_text
    else:
        sources.tag = spec.tag
        report.warn(f"Tag '{sources.tag}' is not the same as the version '{sources.version_text}'")
    report.ok(f"tag {sources.tag}")
    return sources


def runtime_pin(os_repo: str, os_sha: str, report: Report) -> dict[str, str] | None:
    """Read MONO_RUNTIME_COMMIT at the OS commit and check the pinned runtime commit and tree exist."""
    pin: dict[str, str] = {}
    for line in gh_raw(os_repo, "MONO_RUNTIME_COMMIT", os_sha).splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            pin[key.strip()] = value.strip()
    for key in ("repository", "commit", "tree"):
        if key not in pin:
            report.fail(f"MONO_RUNTIME_COMMIT at {os_sha[:8]} has no '{key}'")
            return None
    match = re.search(GITHUB_REPO, pin["repository"])
    if not match:
        report.fail(f"cannot read the runtime repository from '{pin['repository']}'")
        return None
    try:
        commit = gh_json(f"repos/{match.group(1)}/commits/{pin['commit']}")
    except GhError as error:
        report.fail(f"runtime commit {pin['commit']} not found in {match.group(1)}: {error}")
        return None
    if commit["commit"]["tree"]["sha"] != pin["tree"]:
        report.fail(f"runtime commit {pin['commit'][:8]} has a different tree than the pin ({pin['tree'][:8]})")
        return None
    report.ok(f"runtime pin {pin['commit'][:8]} exists and its tree matches")
    return {"repository": pin["repository"], "commit": pin["commit"], "tree": pin["tree"]}


def releases_repo_name(override: str | None) -> str:
    """Return owner/name of the Releases repository: --repo, or this checkout's origin."""
    if override:
        return override
    match = re.search(GITHUB_REPO, git_output("remote", "get-url", "origin"))
    if not match:
        sys.exit("cannot work out the Releases repository; use --repo owner/name")
    return match.group(1)


def has_write_access(repo: str) -> bool:
    """Return True if the logged-in GitHub user can push to a repository."""
    permissions = gh_json(f"repos/{repo}").get("permissions", {})
    return bool(permissions.get("push") or permissions.get("admin"))


def check_releases_repo(repo: str, tag: str, dry_run: bool, report: Report) -> str | None:
    """Check the Releases repository.  Return the commit the tag will point at (the one holding release.yaml)."""
    try:
        writable = has_write_access(repo)
    except GhError as error:
        report.fail(f"cannot read {repo}: {error}")
        return None
    if writable:
        report.ok(f"write access to {repo}")
    else:
        report.fail(f"your GitHub login does not have write access to {repo}")

    for kind, path in (("tag", f"repos/{repo}/git/ref/tags/{tag}"), ("release", f"repos/{repo}/releases/tags/{tag}")):
        try:
            gh_json(path)
            report.fail(f"a {kind} named '{tag}' already exists in {repo}")
        except GhError:
            report.ok(f"no {kind} named '{tag}' in {repo}")

    if git_output("status", "--porcelain", "--", "release.yaml"):
        report.problem("release.yaml has uncommitted changes; commit and push it before a real release "
                       "(the tag points at the commit holding it)", not dry_run)
    head = git_output("rev-parse", "HEAD")
    if not git_output("branch", "-r", "--contains", head):
        report.problem("the current commit of the Releases repository is not pushed", not dry_run)
    return head


# Tagging the source commits.  After the release the pinned commit in each source repository gets
# the release tag (an annotated tag), so the release can be found from the source.  An existing
# tag on the same commit is left alone; a tag on a different commit is an error, because tags are
# never moved.

def tag_state(repo: str, tag: str, sha: str, report: Report) -> str | None:
    """Return 'create', 'exists' (already on sha), or None after reporting a tag on another commit."""
    try:
        ref = gh_json(f"repos/{repo}/git/ref/tags/{tag}")["object"]
    except GhError:
        return "create"
    target = ref["sha"]
    if ref["type"] == "tag":
        target = gh_json(f"repos/{repo}/git/tags/{target}")["object"]["sha"]
    if target == sha:
        return "exists"
    report.fail(f"{repo} already has a tag '{tag}' on a different commit ({target[:8]}, not {sha[:8]}); "
                f"tags are not moved")
    return None


def do_tag(repo: str, tag: str, sha: str, releases_repo: str, report: Report) -> None:
    """Create an annotated tag on a commit in a source repository, unless it is already there."""
    if tag_state(repo, tag, sha, report) == "exists":
        report.ok(f"{repo}: tag {tag} is already on {sha[:8]}")
        return
    message = f"Meadow release {tag}\n\nhttps://github.com/{releases_repo}/releases/tag/{tag}\n"
    created = json.loads(gh("api", "-X", "POST", f"repos/{repo}/git/tags", "-f", f"tag={tag}",
                            "-f", f"message={message}", "-f", f"object={sha}", "-f", "type=commit"))
    gh("api", "-X", "POST", f"repos/{repo}/git/refs", "-f", f"ref=refs/tags/{tag}", "-f", f"sha={created['sha']}")
    report.ok(f"{repo}: tagged {sha[:8]} as {tag}")


def check_source_tags(sources: Sources, dry_run: bool, report: Report) -> None:
    """Check each source repository can take the release tag and that we can write to it."""
    say()
    say("Source repository tags and access")
    for repo, sha in sources.targets:
        state = tag_state(repo, sources.tag, sha, report)
        if state == "create":
            report.ok(f"{repo}: tag {sources.tag} will be created on {sha[:8]}")
        elif state == "exists":
            report.ok(f"{repo}: tag {sources.tag} is already on {sha[:8]}")
        if has_write_access(repo):
            report.ok(f"write access to {repo} (for the tag and the version bump)")
        else:
            report.problem(f"your GitHub login does not have write access to {repo}, "
                           f"which the tag and the version bump need", not dry_run)


# Version bump.  After a release the version.txt of the source repositories moves on: y goes up by
# 2 from a beta (odd) and by 1 from a full release (even), so the next build is a beta.  The change
# goes on a branch meadow-<new version> and a pull request to the base branch in BUMP_BASE.  Every
# step checks what already exists, so the bump is safe to run again.

def plan_bump(repo: str, released: str, new_version: str) -> BumpPlan:
    """Work out which steps of the version bump a repository still needs."""
    base = BUMP_BASE[repo]
    branch = f"meadow-{new_version}"
    plan = BumpPlan(repo, base, branch, new_version,
                    base_sha=gh_json(f"repos/{repo}/git/ref/heads/{base}")["object"]["sha"])
    current = first_line(gh_raw(repo, "version.txt", base))
    try:
        gh_json(f"repos/{repo}/git/ref/heads/{branch}")
        branch_exists = True
    except GhError:
        branch_exists = False
    pull_request = gh("pr", "list", "-R", repo, "--head", branch, "--state", "all",
                      "--json", "url", "--jq", ".[0].url").strip()
    if current == new_version and not branch_exists:
        plan.skip = f"{base} is already at {new_version}"
    elif current not in (released, new_version):
        plan.skip = f"{base} version.txt is {current}, expected {released}; not bumping"
    elif pull_request:
        plan.skip = f"pull request already exists: {pull_request}"
    else:
        on_branch = first_line(gh_raw(repo, "version.txt", branch)) if branch_exists else current
        plan.need_branch = not branch_exists
        plan.need_commit = on_branch != new_version
        plan.need_pr = True
    return plan


def bump_plans(version: Sequence[int], os_repo: str) -> list[BumpPlan]:
    """Plan the version bump in the OS and ESP32 repositories after releasing `version`."""
    released = format_version(version[:3])
    new_version = f"{version[0]}.{version[1]}.{next_y(version[2])}"
    return [plan_bump(repo, released, new_version) for repo in (os_repo, ESP32_REPO)]


def describe_bump(plan: BumpPlan) -> None:
    """Print what a bump plan will do."""
    say(f"  {plan.repo}: version.txt -> {plan.new_version}, branch {plan.branch}, PR to {plan.base}")
    if plan.skip:
        say(f"    skipped: {plan.skip}")
        return
    steps = [text for needed, text in (
        (plan.need_branch, f"create branch from {plan.base} {plan.base_sha[:8]}"),
        (plan.need_commit, "commit the new version.txt"),
        (plan.need_pr, "open the pull request")) if needed]
    say("    would: " + ", ".join(steps))


def do_bump(plan: BumpPlan, tag: str, report: Report) -> None:
    """Carry out a bump plan: create the branch, commit the new version.txt and open the pull request."""
    if plan.skip:
        say(f"  {plan.repo}: skipped: {plan.skip}")
        return
    if plan.need_branch:
        gh("api", "-X", "POST", f"repos/{plan.repo}/git/refs", "-f", f"ref=refs/heads/{plan.branch}",
           "-f", f"sha={plan.base_sha}")
        report.ok(f"{plan.repo}: created branch {plan.branch}")
    if plan.need_commit:
        blob = gh_json(f"repos/{plan.repo}/contents/version.txt?ref={plan.branch}")["sha"]
        raw = gh_raw(plan.repo, "version.txt", plan.branch)
        new_raw = raw.replace(first_line(raw), plan.new_version, 1)
        gh("api", "-X", "PUT", f"repos/{plan.repo}/contents/version.txt",
           "-f", f"message=Bump the version to {plan.new_version} after release {tag}",
           "-f", "content=" + base64.b64encode(new_raw.encode()).decode(),
           "-f", f"sha={blob}", "-f", f"branch={plan.branch}")
        report.ok(f"{plan.repo}: committed version.txt {plan.new_version}")
    if plan.need_pr:
        url = gh("pr", "create", "-R", plan.repo, "--base", plan.base, "--head", plan.branch,
                 "--title", f"Bump the version to {plan.new_version}",
                 "--body", f"Moves version.txt on after release {tag}.").strip()
        report.ok(f"{plan.repo}: opened {url}")


def find_run(repo: str, workflow: str, sha: str, report: Report) -> dict[str, Any] | None:
    """Return the newest successful push run of a workflow for a commit, or None after reporting."""
    all_runs: list[dict[str, Any]] = gh_json(f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100")["workflow_runs"]
    runs = [run for run in all_runs
            if run["name"] == workflow and run["conclusion"] == "success" and run["event"] in RUN_EVENTS]
    if not runs:
        report.fail(f"no successful '{workflow}' push run for {repo}@{sha[:8]}; the commit must be one that was "
                    f"built on merge (a pull request run does not count)")
        return None
    return max(runs, key=lambda run: run["created_at"])


def download_artifacts(repo: str, run: dict[str, Any], names: Sequence[str], dest: Path, report: Report) -> bool:
    """Download the named artifacts of a run into `dest`.

    Returns False, after reporting, if any is missing or expired.
    """
    available = {artifact["name"]: artifact
                 for artifact in gh_json(f"repos/{repo}/actions/runs/{run['id']}/artifacts?per_page=100")["artifacts"]}
    usable = True
    for name in names:
        if name not in available:
            report.fail(f"run {run['id']} of {repo} has no artifact '{name}'")
            usable = False
        elif available[name]["expired"]:
            report.fail(f"artifact '{name}' of run {run['id']} of {repo} has expired; the commit must be rebuilt")
            usable = False
    if not usable:
        return False
    for name in names:
        target = artifact_dir(dest, name)
        target.mkdir(parents=True, exist_ok=True)
        gh("run", "download", str(run["id"]), "-R", repo, "-n", name, "-D", str(target))
        report.ok(f"downloaded artifact '{name}' ({run['html_url']})")
    return True


def new_work_dir(work_dir: str | None, tag: str) -> Path:
    """Create a new folder for this run.  An old one is never reused, because gh will not download over files."""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    work = (Path(work_dir) if work_dir else SCRIPT_DIR / "work" / tag) / stamp
    work.mkdir(parents=True)
    return work


def fetch_binaries(sources: Sources, work: Path, report: Report) -> Binaries | None:
    """Find the CI runs for the pinned commits, download their artifacts and locate the files.

    Returns None, after reporting, when the binaries cannot be had.
    """
    os_run = find_run(sources.os_repo, OS_WORKFLOW, sources.os_sha, report)
    esp_run = find_run(ESP32_REPO, ESP32_WORKFLOW, sources.esp_sha, report)
    if not (os_run and esp_run):
        return None
    report.ok(f"OS run {os_run['id']} {os_run['html_url']}")
    report.ok(f"ESP32 run {esp_run['id']} {esp_run['html_url']}")

    root = work / "download"
    if not (download_artifacts(sources.os_repo, os_run, OS_ARTIFACTS, root / "os", report)
            and download_artifacts(ESP32_REPO, esp_run, ESP32_ARTIFACTS, root / "esp32", report)):
        return None

    os_dir = artifact_dir(root / "os", "Meadow.OS")
    esp_dir = artifact_dir(root / "esp32", "network")
    os_files: dict[str, Path] = {}
    for name in OS_FILES + ["build-info.json"]:
        found = find_one(os_dir, name)
        if found is None:
            report.fail(f"{name} is not in the Meadow.OS artifact")
        else:
            os_files[name] = found
    esp_files: dict[str, Path] = {}
    for name in ESP32_FILES + [n + ".md5" for n in ESP32_FILES]:
        found = find_one(esp_dir, name)
        if found is None:
            report.fail(f"{name} is not in the ESP32 'network' artifact")
        else:
            esp_files[name] = found
    if len(os_files) != len(OS_FILES) + 1 or len(esp_files) != len(ESP32_FILES) * 2:
        return None

    bcl_dir = artifact_dir(root / "os", "meadow_assemblies")
    bcl_files = sorted(path for path in bcl_dir.rglob("*") if path.is_file())
    if not any(path.suffix == ".dll" for path in bcl_files):
        report.fail("the meadow_assemblies artifact contains no .dll files")
        return None
    return Binaries(root, os_run, esp_run, os_files, esp_files, bcl_dir, bcl_files)


def verify_binaries(binaries: Binaries, sources: Sources, report: Report) -> None:
    """Check the downloaded binaries really are builds of the pinned commits.

    The run was found by commit.  As well, the OS binaries carry their commit in build-info.json and
    the build number as "y.build" (CONFIG_VERSION_BUILD, followed by the build date), and the ESP32
    binary carries the last eight characters of its commit as a little-endian 32-bit build hash.
    """
    built = json.loads(binaries.os_files["build-info.json"].read_text(encoding="utf-8"))["git"]["meadow"][0]
    if built != sources.os_sha:
        report.fail(f"build-info.json says the OS was built from {built}, not {sources.os_sha}")
    else:
        report.ok("build-info.json commit matches the pinned OS commit")

    build_stamp = f"{sources.version[2]}.{sources.version[3]}"
    if f"{build_stamp} ".encode() in binaries.os_files["Meadow.OS.Update.bin"].read_bytes():
        report.ok(f"Meadow.OS.Update.bin is stamped with build {build_stamp}")
    else:
        report.fail(f"Meadow.OS.Update.bin is not stamped with build {build_stamp}; "
                    f"the CI build used a different build number than {sources.version_text}")

    build_hash = sources.esp_sha[-8:]
    if struct.pack("<I", int(build_hash, 16)) in binaries.esp_files["MeadowComms.bin"].read_bytes():
        report.ok("MeadowComms.bin contains the pinned ESP32 build hash")
    else:
        report.fail(f"MeadowComms.bin does not contain the build hash {build_hash} of the pinned ESP32 commit")

    for name in ESP32_FILES:
        wanted = binaries.esp_files[name + ".md5"].read_text(encoding="utf-8").split()[0].lower()
        actual = hashlib.md5(binaries.esp_files[name].read_bytes()).hexdigest()  # nosec - matches the build's .md5
        if wanted != actual:
            report.fail(f"{name} does not match its .md5 file")
    report.ok("ESP32 binaries checked against their .md5 files")


def write_zip(path: Path, entries: Sequence[tuple[str, Path]]) -> None:
    """Write a zip of (name in the zip, source file) entries, in a fixed order with fixed timestamps."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for arcname, source in sorted(entries):
            info = zipfile.ZipInfo(arcname, date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, source.read_bytes())


def download_sources(repo: str, sha: str, dest: Path, label: str, report: Report) -> Path:
    """Download the sources of a repository at a commit as a tar.gz and check the archive is readable."""
    path = dest / f"sources-{label}-{sha[:8]}.tar.gz"
    path.write_bytes(run_gh("api", f"repos/{repo}/tarball/{sha}"))
    try:
        with tarfile.open(path):
            pass
    except tarfile.TarError as error:
        report.fail(f"source archive for {repo}@{sha[:8]} is not a valid tar file: {error}")
    return path


def stage_zips(binaries: Binaries, out: Path, report: Report) -> list[Path]:
    """Write Meadow.OS.zip, Meadow.Network.zip and Meadow.Debug.zip into `out`."""
    os_zip = out / "Meadow.OS.zip"
    net_zip = out / "Meadow.Network.zip"
    dbg_zip = out / "Meadow.Debug.zip"
    write_zip(os_zip, [(name, binaries.os_files[name]) for name in OS_FILES]
              + [(f"{BCL_FOLDER}/{path.relative_to(binaries.bcl_dir).as_posix()}", path)
                 for path in binaries.bcl_files])
    write_zip(net_zip, list(binaries.esp_files.items()))
    os_symbols = artifact_dir(binaries.root / "os", "Meadow.OS Debug Symbols")
    esp_build = artifact_dir(binaries.root / "esp32", "build")
    write_zip(dbg_zip, [(f"os/{path.relative_to(os_symbols).as_posix()}", path)
                        for path in os_symbols.rglob("*") if path.is_file()]
              + [(f"esp32/{path.relative_to(esp_build).as_posix()}", path)
                 for path in esp_build.rglob("*") if path.is_file() and path.suffix in (".elf", ".map")])
    zips = [os_zip, net_zip, dbg_zip]
    for archive in zips:
        report.ok(f"{archive.name} ({archive.stat().st_size} bytes)")
    return zips


def stage_sources(sources: Sources, out: Path, skip: bool, report: Report) -> list[Path]:
    """Download the source archives of both repositories into `out`, unless `skip` is set."""
    if skip:
        report.warn("source archives skipped (--skip-sources)")
        return []
    archives = [download_sources(sources.os_repo, sources.os_sha, out, "Meadow.OS", report),
                download_sources(ESP32_REPO, sources.esp_sha, out, "Meadow-ESP32", report)]
    for archive in archives:
        report.ok(f"{archive.name} ({archive.stat().st_size} bytes)")
    return archives


def stage_assets(spec: ReleaseSpec, sources: Sources, binaries: Binaries, work: Path,
                 skip_sources: bool, report: Report) -> Staged:
    """Build the zips, source archives, manifest and release notes in <work>/release."""
    out = work / "release"
    out.mkdir(parents=True, exist_ok=True)
    zips = stage_zips(binaries, out, report)
    archives = stage_sources(sources, out, skip_sources, report)

    manifest = build_manifest(spec, sources, binaries, zips)
    manifest_path = out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    report.ok("manifest.json")
    release_copy = out / "release.yaml"
    release_copy.write_bytes(spec.path.read_bytes())

    runtime = sources.runtime
    notes = out / "notes.md"
    notes.write_text(
        f"{spec.description}\n\nVersion {sources.version_text} ({sources.kind})\n\n"
        f"- Meadow.OS {sources.os_sha}\n- Meadow-ESP32 {sources.esp_sha}\n"
        f"- runtime {runtime['commit'] if runtime else 'unknown'}\n", encoding="utf-8")
    title = f"{spec.description} ({sources.version_text})" if spec.description else sources.version_text
    return Staged(out, zips + [manifest_path, release_copy] + archives, notes, title)


def build_manifest(spec: ReleaseSpec, sources: Sources, binaries: Binaries,
                   zips: Sequence[Path]) -> dict[str, Any]:
    """Return the contents of manifest.json: version, hashes of every file, and the source commits."""
    return {
        "tag": sources.tag,
        "version": sources.version_text,
        "generation": "3.0",
        "type": sources.kind,
        "description": spec.description,
        "minCLIVersion": spec.min_cli_version,
        "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": {archive.name: {"sha256": sha256_of(archive)} for archive in zips},
        "contents": {**{name: {"sha256": sha256_of(binaries.os_files[name])} for name in OS_FILES},
                     **{name: {"sha256": sha256_of(binaries.esp_files[name])} for name in ESP32_FILES}},
        "sources": {
            "Meadow.OS": {"repository": f"https://github.com/{sources.os_repo}", "commit": sources.os_sha},
            "Meadow-ESP32": {"repository": f"https://github.com/{ESP32_REPO}", "commit": sources.esp_sha},
            "runtime": sources.runtime,
        },
        "builds": {"Meadow.OS": binaries.os_run["html_url"], "Meadow-ESP32": binaries.esp_run["html_url"]},
    }


def run_bump_only(spec: ReleaseSpec, version: Sequence[int], os_repo: str, args: argparse.Namespace,
                  report: Report) -> int:
    """Handle --bump-only: plan the version bump and, unless --dry-run, carry it out."""
    say()
    say("Version bump")
    plans = bump_plans(version, os_repo)
    for plan in plans:
        describe_bump(plan)
    if args.dry_run or all(plan.skip for plan in plans):
        say("DRY RUN: nothing was changed." if args.dry_run else "nothing to do")
        return 0
    if not confirm("Type 'bump' to make these changes: ", "bump", args.yes):
        say("not confirmed, nothing changed")
        return 1
    for plan in plans:
        do_bump(plan, spec.version, report)
    return 0


def run_tag_only(sources: Sources, args: argparse.Namespace, report: Report) -> int:
    """Handle --tag-only: tag the source commits and, unless --dry-run, create the tags."""
    if report.errors:
        return report.finish()
    if args.dry_run:
        say("DRY RUN: nothing was changed.")
        return 0
    if not confirm(f"Type the tag '{sources.tag}' to tag the source commits: ", sources.tag, args.yes):
        say("not confirmed, nothing changed")
        return 1
    for repo, sha in sources.targets:
        do_tag(repo, sources.tag, sha, releases_repo_name(args.repo), report)
    return 0


def publish(sources: Sources, staged: Staged, plans: Sequence[BumpPlan], releases_repo: str,
            target_commit: str, report: Report) -> int:
    """Create the GitHub Release, then tag the source commits and bump the versions.

    The release is created first.  If tagging or the bump then fails the release stands, and the
    --tag-only and --bump-only options repeat the failed step.
    """
    command = ["release", "create", sources.tag, *map(str, staged.assets), "-R", releases_repo,
               "--title", staged.title, "--notes-file", str(staged.notes), "--target", target_commit]
    command += ["--prerelease", "--latest=false"] if sources.kind == "Beta" else ["--latest"]
    say(gh(*command).strip())

    say()
    say("Tagging the source commits")
    try:
        for repo, sha in sources.targets:
            do_tag(repo, sources.tag, sha, releases_repo, report)
    except GhError as error:
        say(f"  ERROR: tagging failed after the release was published: {error}")
        say("  The release stands.  Fix the cause and run again with --tag-only (and --bump-only).")
        return 1

    say()
    say("Version bump")
    try:
        for plan in plans:
            do_bump(plan, sources.tag, report)
    except GhError as error:
        say(f"  ERROR: the version bump failed after the release was published: {error}")
        say("  The release stands.  Fix the cause and run again with --bump-only.")
        return 1
    return 0


def make_release(spec: ReleaseSpec, sources: Sources, args: argparse.Namespace, report: Report) -> int:
    """Check the Releases repository, fetch and verify the binaries, stage the assets and (unless --dry-run) publish."""
    releases_repo = releases_repo_name(args.repo)
    say()
    say(f"Releases repository ({releases_repo})")
    target_commit = check_releases_repo(releases_repo, sources.tag, args.dry_run, report)
    sources.runtime = runtime_pin(sources.os_repo, sources.os_sha, report)

    say()
    say("Binaries built by GitHub Actions")
    work = new_work_dir(args.work_dir, sources.tag)
    say(f"  work directory: {work}")
    binaries = fetch_binaries(sources, work, report)
    if binaries is None:
        return report.finish()
    verify_binaries(binaries, sources, report)

    say()
    say("Staging the release assets")
    staged = stage_assets(spec, sources, binaries, work, args.skip_sources, report)

    say()
    say("Version bump after the release")
    plans = bump_plans(sources.version, sources.os_repo)
    for plan in plans:
        describe_bump(plan)

    if report.finish():
        return 1

    say()
    say(f"Release {sources.tag} ({sources.kind}) in {releases_repo}: {len(staged.assets)} assets, "
        f"target commit {(target_commit or '')[:8]}")
    if args.dry_run:
        say("DRY RUN: nothing was published or pushed.  Staged assets are in " + str(staged.out))
        return 0
    if not confirm(f"Type the tag '{sources.tag}' to publish this release: ", sources.tag, args.yes):
        say("not confirmed, nothing published")
        return 1
    return publish(sources, staged, plans, releases_repo, target_commit or "", report)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Read the command line."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("release_file", nargs="?", default=str(SCRIPT_DIR / "release.yaml"))
    parser.add_argument("--dry-run", action="store_true",
                        help="run all checks and stage the assets, but publish and push nothing")
    parser.add_argument("--work-dir",
                        help="where to download and stage; each run uses a new timestamped folder inside it "
                             "(default work/<tag> beside this script)")
    parser.add_argument("--skip-sources", action="store_true", help="do not download the source archives")
    parser.add_argument("--repo", help="the Releases repository (default: this checkout's origin)")
    parser.add_argument("--tag-only", action="store_true",
                        help="only tag the source commits with the release tag; use to repeat tagging "
                             "that failed after the release was published")
    parser.add_argument("--bump-only", action="store_true",
                        help="only do the version bump (branch, commit, pull request) for the versions in "
                             "release.yaml; use to repeat a bump that failed after the release was published")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation before publishing")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the script and return its exit status: 0 for success, 1 if a check failed or nothing was confirmed."""
    args = parse_args(argv)
    report = Report()
    try:
        spec = load_release_file(Path(args.release_file))
    except (OSError, ReleaseFileError, yaml.YAMLError) as error:
        say(f"cannot read {args.release_file}: {error}")
        return 1

    say(f"{'DRY RUN: ' if args.dry_run else ''}release file {args.release_file}")
    say()
    say("Release file")
    checked = validate_spec(spec, report)
    if checked is None:
        return report.finish()
    version, os_repo = checked

    if args.bump_only:
        return run_bump_only(spec, version, os_repo, args, report)

    kind = check_type(spec.declared_type, version, report)
    sources = check_sources(spec, version, os_repo, report)
    if sources is None:
        return report.finish()
    sources.kind = kind

    check_source_tags(sources, args.dry_run, report)
    if args.tag_only:
        return run_tag_only(sources, args, report)
    return make_release(spec, sources, args, report)


if __name__ == "__main__":
    sys.exit(main())
