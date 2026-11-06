#!/usr/bin/env python3
"""Draft the release notes for the next Meadow release.

Lists what has changed in Meadow.OS, Meadow-ESP32 and the Mono runtime since the last Meadow.OS
release, and writes it to DraftReleaseNote.md as a starting point for the Notes in release.yaml:

- the issues resolved, with their titles and the pull requests that fix them,
- the pull requests that resolve no issue,
- the commits that reached the branch without a pull request.

    ./generate_draft_release_note.py                     since the last release, up to the branch heads
    ./generate_draft_release_note.py --release-yaml      up to the commits pinned in release.yaml
    ./generate_draft_release_note.py --since 2.999.51.1  since a given tag (--base is the same)

The last release is the newest published release in this repository whose manifest.json comes from
Meadow.OS; the manifest holds the commit of each source repository.  The runtime is the commit
pinned in MONO_RUNTIME_COMMIT, so its changes are the ones between the pins.

--since names the tag to start from instead.  A release here with that tag is used through its
manifest.json; otherwise the tag must be in both Meadow.OS and Meadow-ESP32 (create_release.py tags
the source commits of every release), and the runtime starts at the commit pinned at the Meadow.OS tag.

Requires: Python 3.9+, PyYAML, and the GitHub CLI (gh) logged in with access to the repositories.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from create_release import (
    BUMP_BASE,
    ESP32_REPO,
    GITHUB_REPO,
    OS_REPO,
    OS_SOURCES,
    SCRIPT_DIR,
    GhError,
    ReleaseFileError,
    gh,
    gh_json,
    gh_raw,
    load_release_file,
    releases_repo_name,
    run_gh,
    say,
)

DEFAULT_OUTPUT = SCRIPT_DIR / "DraftReleaseNote.md"
# The order the repositories appear in the note, by the names manifest.json uses.
REPO_NAMES = ["Meadow.OS", "Meadow-ESP32", "runtime"]
# The generation manifest.json gives a release built from Meadow.OS.
OS_GENERATION = "3.0"
# Commits looked up per GraphQL request.
GRAPHQL_BATCH = 40
# Pull requests that only move version.txt on are not news: the branches create_release.py makes
# for them, and the titles of the ones made by hand.
BUMP_BRANCH = re.compile(r"^meadow-\d+(?:\.\d+)+$")
BUMP_TITLE = re.compile(r"^bump (?:the )?version\b", re.IGNORECASE)


def is_version_bump(node: dict[str, Any]) -> bool:
    """Return True for a pull request that only moves the version on."""
    return bool(BUMP_BRANCH.match(node.get("headRefName") or "") or BUMP_TITLE.match(node.get("title") or ""))
# The words GitHub uses to close an issue from a pull request, followed by the issue.
CLOSING_REF = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?\s+"
    r"(?:https://github\.com/(?P<url_repo>[\w.-]+/[\w.-]+)/issues/(?P<url_number>\d+)"
    r"|(?P<repo>[\w.-]+/[\w.-]+)?#(?P<number>\d+))",
    re.IGNORECASE,
)

PR_FIELDS = """
    number title url mergedAt headRefName
    author { login }
    body
    baseRepository { nameWithOwner }
    closingIssuesReferences(first: 25) { nodes { number title url state repository { nameWithOwner } } }
"""


@dataclass
class Range:
    """The commits of one repository between the last release and now."""

    name: str
    repo: str
    base: str
    head: str
    head_label: str
    commits: list[dict[str, Any]] = field(default_factory=list)
    behind: int = 0
    note: str = ""

    @property
    def compare_url(self) -> str:
        """The GitHub page comparing the two commits."""
        return f"https://github.com/{self.repo}/compare/{self.base}...{self.head}"


@dataclass
class PullRequest:
    """A merged pull request that brought commits into a range."""

    repo: str
    number: int
    title: str
    url: str
    author: str
    merged_at: str
    issues: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class Issue:
    """An issue closed by one or more of the pull requests."""

    repo: str
    number: int
    title: str
    url: str
    state: str
    fixed_by: list[PullRequest] = field(default_factory=list)


@dataclass
class Changes:
    """Everything the note lists."""

    issues: list[Issue]
    pull_requests: dict[str, list[PullRequest]]
    direct_commits: dict[str, list[dict[str, Any]]]


@dataclass
class Base:
    """Where the ranges start: a release here, or a tag in the source repositories."""

    tag: str
    url: str
    commits: dict[str, str]
    is_release: bool = True

    @property
    def description(self) -> str:
        """How the note names the starting point."""
        return f"{'Meadow release' if self.is_release else 'tag'} [{self.tag}]({self.url})"


# --------------------------------------------------------------------------------------------------
# Finding the two ends of each range.


def release_manifest(releases_repo: str, release: dict[str, Any]) -> dict[str, Any] | None:
    """Return a release's manifest.json, or None if it has none or it cannot be read."""
    for asset in release.get("assets", []):
        if asset["name"] == "manifest.json":
            try:
                raw = run_gh("api", "-H", "Accept: application/octet-stream",
                             f"repos/{releases_repo}/releases/assets/{asset['id']}")
                manifest = json.loads(raw)
            except (GhError, ValueError):
                return None
            return manifest if isinstance(manifest, dict) else None
    return None


def manifest_commits(manifest: dict[str, Any]) -> dict[str, str] | None:
    """Return each source repository's commit from a manifest, or None unless it is a Meadow.OS release."""
    sources = manifest.get("sources") or {}
    if manifest.get("generation") != OS_GENERATION or not isinstance(sources, dict):
        return None
    commits = {name: (sources.get(name) or {}).get("commit", "") for name in REPO_NAMES}
    return commits if all(commits.values()) else None


def release_base(releases_repo: str, release: dict[str, Any]) -> Base | None:
    """Return a release as the starting point, or None unless it has a Meadow.OS manifest.json."""
    manifest = release_manifest(releases_repo, release)
    commits = manifest_commits(manifest) if manifest else None
    return Base(release["tag_name"], release["html_url"], commits) if commits else None


def tag_commit(repo: str, tag: str) -> str | None:
    """Return the commit a tag points at in a repository (following an annotated tag), or None."""
    try:
        return str(gh_json(f"repos/{repo}/commits/refs/tags/{tag}")["sha"])
    except GhError:
        return None


def tagged_base(tag: str) -> Base:
    """Return the starting point given by a tag in Meadow.OS and Meadow-ESP32."""
    os_sha, esp_sha = tag_commit(OS_REPO, tag), tag_commit(ESP32_REPO, tag)
    missing = [repo for repo, sha in ((OS_REPO, os_sha), (ESP32_REPO, esp_sha)) if not sha]
    if missing or not (os_sha and esp_sha):
        sys.exit(f"'{tag}' is not a release with a Meadow.OS manifest.json here, nor a tag in {' or '.join(missing)}")
    try:
        runtime = runtime_pin(os_sha)["commit"]
    except GhError:
        sys.exit(f"{OS_REPO} has no MONO_RUNTIME_COMMIT at tag {tag}, so the runtime has no starting point")
    return Base(tag, f"https://github.com/{OS_REPO}/tree/{tag}",
                {"Meadow.OS": os_sha, "Meadow-ESP32": esp_sha, "runtime": runtime}, is_release=False)


def starting_point(releases_repo: str, tag: str | None) -> Base:
    """Return where the ranges start: `tag`, or the newest Meadow.OS release."""
    if tag:
        try:
            base = release_base(releases_repo, gh_json(f"repos/{releases_repo}/releases/tags/{tag}"))
        except GhError:
            base = None
        return base or tagged_base(tag)
    releases = [release for page in json.loads(gh("api", "--paginate", "--slurp",
                                                   f"repos/{releases_repo}/releases?per_page=100"))
                for release in page if not release["draft"]]
    releases.sort(key=lambda release: release.get("published_at") or release["created_at"], reverse=True)
    for release in releases:
        base = release_base(releases_repo, release)
        if base:
            return base
    sys.exit(f"no release in {releases_repo} has a Meadow.OS manifest.json; use --since <tag>")


def runtime_pin(os_sha: str) -> dict[str, str]:
    """Return the key=value lines of MONO_RUNTIME_COMMIT at a Meadow.OS commit."""
    pin: dict[str, str] = {}
    for line in gh_raw(OS_REPO, "MONO_RUNTIME_COMMIT", os_sha).splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            pin[key.strip()] = value.strip()
    if "commit" not in pin or not re.search(GITHUB_REPO, pin.get("repository", "")):
        sys.exit(f"MONO_RUNTIME_COMMIT at {OS_REPO}@{os_sha[:8]} has no repository or commit")
    return pin


def resolve(repo: str, ref: str) -> str:
    """Return the full hash of a branch, tag or (short) commit in a repository."""
    try:
        return str(gh_json(f"repos/{repo}/commits/{ref}")["sha"])
    except GhError as error:
        sys.exit(f"cannot find '{ref}' in {repo}: {error}")


def head_commits(use_release_yaml: bool, release_file: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Return the commit each range ends at for Meadow.OS and Meadow-ESP32, and a label for each."""
    if not use_release_yaml:
        os_branch, esp_branch = BUMP_BASE[OS_REPO], BUMP_BASE[ESP32_REPO]
        return ({"Meadow.OS": resolve(OS_REPO, os_branch), "Meadow-ESP32": resolve(ESP32_REPO, esp_branch)},
                {"Meadow.OS": os_branch, "Meadow-ESP32": esp_branch})
    try:
        spec = load_release_file(release_file)
    except (OSError, ReleaseFileError) as error:
        sys.exit(f"cannot read {release_file}: {error}")
    if OS_SOURCES.get(spec.os_source.lower()) != OS_REPO:
        sys.exit(f"{release_file} releases from '{spec.os_source}'; only Meadow.OS releases can be drafted")
    if not (spec.os_commit and spec.esp32_commit):
        sys.exit(f"{release_file} must give both the OS and the ESP32 commit")
    return ({"Meadow.OS": resolve(OS_REPO, spec.os_commit), "Meadow-ESP32": resolve(ESP32_REPO, spec.esp32_commit)},
            {"Meadow.OS": release_file.name, "Meadow-ESP32": release_file.name})


def build_ranges(start: dict[str, str], heads: dict[str, str], labels: dict[str, str]) -> list[Range]:
    """Return the range of each repository; the runtime runs between the two pinned commits."""
    pin = runtime_pin(heads["Meadow.OS"])
    runtime_repo = re.search(GITHUB_REPO, pin["repository"]).group(1)  # type: ignore[union-attr]
    ranges = [
        Range("Meadow.OS", OS_REPO, start["Meadow.OS"], heads["Meadow.OS"], labels["Meadow.OS"]),
        Range("Meadow-ESP32", ESP32_REPO, start["Meadow-ESP32"], heads["Meadow-ESP32"], labels["Meadow-ESP32"]),
        Range("runtime", runtime_repo, start["runtime"], pin["commit"], "MONO_RUNTIME_COMMIT"),
    ]
    # Runtime work that has not been pinned yet is not in the release; say how much there is.
    branch = pin.get("branch")
    if branch:
        try:
            ahead = int(gh_json(f"repos/{runtime_repo}/compare/{pin['commit']}...{branch}")["ahead_by"])
        except (GhError, KeyError, ValueError):
            ahead = 0
        if ahead:
            ranges[2].note = (f"{ahead} commit{'s' if ahead != 1 else ''} on the runtime branch `{branch}` "
                              f"are not pinned in MONO_RUNTIME_COMMIT yet, so they are not listed.")
    return ranges


def load_commits(span: Range) -> None:
    """Fill in the commits reachable from the head but not from the base, oldest first."""
    if span.base == span.head:
        return
    pages = json.loads(gh("api", "--paginate", "--slurp",
                          f"repos/{span.repo}/compare/{span.base}...{span.head}?per_page=100"))
    span.behind = int(pages[0].get("behind_by", 0)) if pages else 0
    seen: set[str] = set()
    for page in pages:
        for commit in page.get("commits", []):
            if commit["sha"] not in seen:
                seen.add(commit["sha"])
                span.commits.append(commit)


# --------------------------------------------------------------------------------------------------
# Pull requests and issues.


def closing_refs(text: str, repo: str) -> list[tuple[str, int]]:
    """Return the issues a pull request's text closes with a keyword such as "Fixes #12", in order."""
    found: list[tuple[str, int]] = []
    for match in CLOSING_REF.finditer(text or ""):
        target = (match.group("url_repo") or match.group("repo") or repo,
                  int(match.group("url_number") or match.group("number")))
        if target not in found:
            found.append(target)
    return found


def associated_pull_requests(repo: str, shas: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """Return the pull requests GitHub associates with each commit, looked up in batches."""
    owner, name = repo.split("/")
    result: dict[str, list[dict[str, Any]]] = {}
    for start in range(0, len(shas), GRAPHQL_BATCH):
        batch = shas[start:start + GRAPHQL_BATCH]
        fields = "\n".join(
            f'c{i}: object(oid: "{sha}") {{ ... on Commit {{ oid '
            f"associatedPullRequests(first: 10) {{ nodes {{ {PR_FIELDS} }} }} }} }}"
            for i, sha in enumerate(batch))
        query = f'query {{ repository(owner: "{owner}", name: "{name}") {{ {fields} }} }}'
        data = json.loads(gh("api", "graphql", "-f", f"query={query}"))["data"]["repository"]
        for i, sha in enumerate(batch):
            node = data.get(f"c{i}") or {}
            result[sha] = (node.get("associatedPullRequests") or {}).get("nodes") or []
    return result


def fetch_issue(repo: str, number: int, cache: dict[tuple[str, int], dict[str, Any] | None]) -> dict[str, Any] | None:
    """Return an issue from the REST API, or None if it is missing or is a pull request."""
    key = (repo, number)
    if key not in cache:
        try:
            issue = gh_json(f"repos/{repo}/issues/{number}")
            cache[key] = None if "pull_request" in issue else issue
        except GhError:
            cache[key] = None
    return cache[key]


def is_merge_commit(commit: dict[str, Any]) -> bool:
    """Return True for a merge commit, which says nothing about a change by itself."""
    return len(commit.get("parents", [])) > 1


def collect_changes(ranges: Iterable[Range]) -> Changes:
    """Sort every commit into issues fixed, pull requests without an issue and direct commits."""
    issues: dict[tuple[str, int], Issue] = {}
    pull_requests: dict[str, list[PullRequest]] = {}
    direct_commits: dict[str, list[dict[str, Any]]] = {}
    issue_cache: dict[tuple[str, int], dict[str, Any] | None] = {}

    for span in ranges:
        say(f"{span.name}: looking up the pull requests of {len(span.commits)} commits")
        by_commit = associated_pull_requests(span.repo, [commit["sha"] for commit in span.commits])
        known: dict[int, PullRequest] = {}
        direct: list[dict[str, Any]] = []
        for commit in span.commits:
            merged = [pr for pr in by_commit.get(commit["sha"], [])
                      if pr.get("mergedAt") and (pr.get("baseRepository") or {}).get("nameWithOwner") == span.repo]
            if not merged:
                if not is_merge_commit(commit):
                    direct.append(commit)
                continue
            for node in merged:
                if node["number"] in known or is_version_bump(node):
                    continue
                pr = PullRequest(span.repo, node["number"], node["title"], node["url"],
                                 (node.get("author") or {}).get("login", ""), node["mergedAt"])
                known[pr.number] = pr
                linked = [(ref["repository"]["nameWithOwner"], ref["number"], ref)
                          for ref in (node.get("closingIssuesReferences") or {}).get("nodes") or []]
                for repo, number in closing_refs(f"{node['title']}\n{node.get('body') or ''}", span.repo):
                    if all((repo.lower(), number) != (other.lower(), n) for other, n, _ in linked):
                        rest = fetch_issue(repo, number, issue_cache)
                        if rest:
                            linked.append((repo, number, {"title": rest["title"], "url": rest["html_url"],
                                                          "state": rest["state"].upper()}))
                for repo, number, ref in linked:
                    issue = issues.setdefault((repo, number), Issue(repo, number, ref["title"], ref["url"],
                                                                    ref["state"]))
                    issue.fixed_by.append(pr)
                    pr.issues.append((repo, number))
        pull_requests[span.name] = sorted(known.values(), key=lambda pr: pr.merged_at)
        direct_commits[span.name] = direct

    ordered = sorted(issues.values(), key=lambda issue: (issue.repo.lower(), issue.number))
    return Changes(ordered, pull_requests, direct_commits)


# --------------------------------------------------------------------------------------------------
# Writing the note.


def escape(text: str) -> str:
    """Escape the characters that would turn an issue or commit title into Markdown markup."""
    return re.sub(r"([\\\[\]<>*])", r"\\\1", text.strip())


def short_repo(repo: str) -> str:
    """Return a repository's name without the owner when it is one of WildernessLabs'."""
    return repo.split("/", 1)[1] if repo.lower().startswith("wildernesslabs/") else repo


def pr_link(pr: PullRequest) -> str:
    """Return a link to a pull request such as [Meadow.OS#12](url)."""
    return f"[{short_repo(pr.repo)}#{pr.number}]({pr.url})"


def render(start: Base, ranges: Sequence[Range], changes: Changes, today: str) -> str:
    """Return the Markdown of the draft release note."""
    lines = [
        "# Draft release note",
        "",
        f"Changes since {start.description}, generated {today}.",
        "This is a draft: edit it into the Notes in release.yaml.",
        "",
        "| Repository | From | To | Commits |",
        "| --- | --- | --- | --- |",
    ]
    for span in ranges:
        base = f"https://github.com/{span.repo}/commit/"
        lines.append(f"| [{span.name}](https://github.com/{span.repo}) "
                     f"| [{span.base[:8]}]({base}{span.base}) ({start.tag}) "
                     f"| [{span.head[:8]}]({base}{span.head}) ({span.head_label}) "
                     f"| [{len(span.commits)}]({span.compare_url}) |")
    notes = [span.note for span in ranges if span.note]
    notes += [f"{span.name}: the new commit is not a descendant of the release commit ({span.behind} commits "
              f"of the release are not on it); the history may have been rewritten." for span in ranges if span.behind]
    if notes:
        lines += [""] + [f"> {note}" for note in notes]

    lines += ["", "## Issues resolved", ""]
    for issue in changes.issues:
        state = " *(still open)*" if issue.state.upper() == "OPEN" else ""
        fixes = ", ".join(pr_link(pr) for pr in issue.fixed_by)
        lines.append(f"- [{short_repo(issue.repo)}#{issue.number}]({issue.url}) {escape(issue.title)}{state} "
                     f"— fixed by {fixes}")
    if not changes.issues:
        lines.append("None.")

    lines += ["", "## Pull requests without an issue", ""]
    without = {name: [pr for pr in prs if not pr.issues] for name, prs in changes.pull_requests.items()}
    if not any(without.values()):
        lines.append("None.")
    for span in ranges:
        if without.get(span.name):
            lines += [f"### {span.name}", ""]
            lines += [f"- {pr_link(pr)} {escape(pr.title)}" + (f" (@{pr.author})" if pr.author else "")
                      for pr in without[span.name]]
            lines.append("")

    direct = {span.name: changes.direct_commits.get(span.name, []) for span in ranges}
    if any(direct.values()):
        lines += ["", "## Commits without a pull request", ""]
        for span in ranges:
            if direct[span.name]:
                lines += [f"### {span.name}", ""]
                for commit in direct[span.name]:
                    headline = commit["commit"]["message"].splitlines()[0] if commit["commit"]["message"] else ""
                    author = (commit.get("author") or {}).get("login") or commit["commit"]["author"]["name"]
                    lines.append(f"- [{commit['sha'][:8]}]({commit['html_url']}) {escape(headline)} ({author})")
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Read the command line."""
    parser = argparse.ArgumentParser(description="Draft the release notes for the next Meadow release.")
    parser.add_argument("--since", "--base", dest="since", metavar="TAG",
                        help="the tag to start from: a release in this repository, or a tag in both Meadow.OS and "
                             "Meadow-ESP32 (default: the newest Meadow.OS release in this repository)")
    parser.add_argument("--release-yaml", action="store_true",
                        help="end at the commits in release.yaml instead of the branch heads")
    parser.add_argument("--release-file", type=Path, default=SCRIPT_DIR / "release.yaml",
                        help="the release file read by --release-yaml (default: release.yaml)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help="where to write the note (default: DraftReleaseNote.md)")
    parser.add_argument("--repo", help="the Releases repository as owner/name (default: this checkout's origin)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Write the draft release note."""
    args = parse_args(argv)
    try:
        releases_repo = releases_repo_name(args.repo)
        start = starting_point(releases_repo, args.since)
        say(f"starting from {'release' if start.is_release else 'tag'} {start.tag}")
        heads, labels = head_commits(args.release_yaml, args.release_file)
        ranges = build_ranges(start.commits, heads, labels)
        for span in ranges:
            load_commits(span)
            say(f"{span.name}: {span.base[:8]}...{span.head[:8]} ({span.head_label}), {len(span.commits)} commits")
        changes = collect_changes(ranges)
    except GhError as error:
        say(f"gh failed: {error}")
        return 1
    today = datetime.date.today().isoformat()
    args.output.write_text(render(start, ranges, changes, today), encoding="utf-8")
    prs = sum(len(prs) for prs in changes.pull_requests.values())
    say(f"wrote {args.output}: {len(changes.issues)} issues, {prs} pull requests, "
        f"{sum(len(c) for c in changes.direct_commits.values())} commits without a pull request")
    return 0


if __name__ == "__main__":
    sys.exit(main())
