#!/usr/bin/env python3
"""Draft the release notes for the next Meadow release.

Lists what has changed since the last Full release, up to now, and writes it to DraftReleaseNote.md
as a starting point for the Notes in release.yaml.  The issues resolved and the pull requests merged
are grouped as:

- Operating System: Meadow.OS, Meadow-ESP32 and the Mono runtime,
- Meadow.Core: Meadow.Core, Meadow.Contracts, Meadow.Units and Meadow.Logging,
- Meadow.Foundation: Meadow.Foundation.

    ./create_draft_release_note.py                     since the last Full release, up to the branch heads
    ./create_draft_release_note.py --release-yaml      up to the commits pinned in release.yaml
    ./create_draft_release_note.py --since 2.999.51.1  since a given release or tag (--base is the same)

The last Full release is the newest published release in this repository that is not a pre-release.
Its operating system date is the date of the Meadow.OS commit it was built from.

Where a repository has an exact starting point it is used: the commit the release's manifest.json
gives, or else the release's tag in that repository (create_release.py tags the source commits of
each release); the runtime starts at the commit MONO_RUNTIME_COMMIT pins at the starting Meadow.OS
commit.  Every other repository (always Meadow.Core, Meadow.Contracts, Meadow.Units,
Meadow.Logging and Meadow.Foundation, which are not tagged with the releases) lists the commits since the operating
system date.  The runtime ends at the commit pinned in MONO_RUNTIME_COMMIT, because that is what
the release builds.

Requires: Python 3.9+, PyYAML, and the GitHub CLI (gh) logged in with access to the repositories.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import time
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
    parse_version,
    releases_repo_name,
    run_gh,
    say,
)

DEFAULT_OUTPUT = SCRIPT_DIR / "DraftReleaseNote.md"
# The groups of the note, each with its repositories in the order they are listed.
GROUPS = {
    "Operating System": ["Meadow.OS", "Meadow-ESP32", "runtime"],
    "Meadow.Core": ["Meadow.Core", "Meadow.Contracts", "Meadow.Units", "Meadow.Logging"],
    "Meadow.Foundation": ["Meadow.Foundation"],
}
# The libraries, which are not tagged with the releases; their changes always start at the date.
LIBRARY_REPOS = {
    "Meadow.Core": "WildernessLabs/Meadow.Core",
    "Meadow.Contracts": "WildernessLabs/Meadow.Contracts",
    "Meadow.Units": "WildernessLabs/Meadow.Units",
    "Meadow.Logging": "WildernessLabs/Meadow.Logging",
    "Meadow.Foundation": "WildernessLabs/Meadow.Foundation",
}
HIGHLIGHTS = "User to complete"
# Commits looked up per GraphQL request, and how often a failed request is tried.
GRAPHQL_BATCH = 40
GRAPHQL_TRIES = 3
# A list of commits longer than this is folded away in the note.
FOLD_AFTER = 20
# Pull requests that only move version.txt on are not news: the branches create_release.py makes
# for them, and the titles of the ones made by hand.
BUMP_BRANCH = re.compile(r"^meadow-\d+(?:\.\d+)+$")
BUMP_TITLE = re.compile(r"^bump (?:the )?version\b", re.IGNORECASE)
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
class Start:
    """Where the changes start: the release (or tag), its operating system date, and exact commits."""

    tag: str
    url: str
    kind: str
    os_date: str
    commits: dict[str, tuple[str, str]] = field(default_factory=dict)

    @property
    def description(self) -> str:
        """How the note names the starting point."""
        return f"{self.kind} [{self.tag}]({self.url})" if self.url else f"{self.kind} {self.tag}"


@dataclass
class Range:
    """The commits of one repository since the start: from a commit, or since a date."""

    name: str
    repo: str
    group: str
    head: str
    head_label: str
    base: str = ""
    base_label: str = ""
    since: str = ""
    commits: list[dict[str, Any]] = field(default_factory=list)
    behind: int = 0
    note: str = ""

    @property
    def commits_url(self) -> str:
        """The GitHub page listing the commits of the range."""
        if self.base:
            return f"https://github.com/{self.repo}/compare/{self.base}...{self.head}"
        return f"https://github.com/{self.repo}/commits/{self.head}?since={self.since[:10]}"


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
    """Everything the note lists, by range name."""

    issues: list[Issue]
    pull_requests: dict[str, list[PullRequest]]
    direct_commits: dict[str, list[dict[str, Any]]]


def is_version_bump(node: dict[str, Any]) -> bool:
    """Return True for a pull request that only moves the version on."""
    return bool(BUMP_BRANCH.match(node.get("headRefName") or "") or BUMP_TITLE.match(node.get("title") or ""))


# --------------------------------------------------------------------------------------------------
# Finding where the changes start.


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


def manifest_sources(manifest: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Return the repository (owner/name) and commit of each source a manifest gives a commit for."""
    found: dict[str, tuple[str, str]] = {}
    sources = manifest.get("sources")
    for name, source in (sources.items() if isinstance(sources, dict) else []):
        if isinstance(source, dict) and source.get("commit"):
            match = re.search(GITHUB_REPO, source.get("repository") or "")
            if match:
                found[name] = (match.group(1), str(source["commit"]))
    return found


def version_key(release: dict[str, Any]) -> list[int]:
    """Return a release's version for sorting; a tag that is not a version sorts first."""
    try:
        return parse_version(release["tag_name"])
    except ValueError:
        return []


def last_full_release(releases_repo: str) -> dict[str, Any]:
    """Return the newest published release that is not a pre-release."""
    releases = [release for page in json.loads(gh("api", "--paginate", "--slurp",
                                                   f"repos/{releases_repo}/releases?per_page=100"))
                for release in page if not release["draft"] and not release["prerelease"]]
    if not releases:
        sys.exit(f"{releases_repo} has no Full release; use --since <tag>")
    return max(releases, key=version_key)


def tag_commit(repo: str, tag: str) -> str | None:
    """Return the commit a tag points at in a repository (following an annotated tag), or None."""
    try:
        return str(gh_json(f"repos/{repo}/commits/refs/tags/{tag}")["sha"])
    except GhError:
        return None


def commit_date(repo: str, sha: str) -> str:
    """Return the date a commit was committed, in UTC as GitHub gives it (2025-09-26T23:37:51Z)."""
    return str(gh_json(f"repos/{repo}/commits/{sha}")["commit"]["committer"]["date"])


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


def starting_point(releases_repo: str, tag: str | None) -> Start:
    """Return where the changes start: release or tag `tag`, or the last Full release."""
    release: dict[str, Any] | None
    if tag:
        try:
            release = gh_json(f"repos/{releases_repo}/releases/tags/{tag}")
        except GhError:
            release = None
    else:
        release = last_full_release(releases_repo)
    if release:
        tag = str(release["tag_name"])
        kind = "Meadow pre-release" if release["prerelease"] else "Meadow Full release"
        start = Start(tag, release["html_url"], kind, "")
    else:
        start = Start(str(tag), f"https://github.com/{OS_REPO}/tree/{tag}", "tag", "")
    manifest = release_manifest(releases_repo, release) if release else None
    sources = manifest_sources(manifest) if manifest else {}

    # The exact starting commits of the OS repositories: the release's own, or else its tag.
    for name, repo in (("Meadow.OS", OS_REPO), ("Meadow-ESP32", ESP32_REPO)):
        if name in sources and sources[name][0].lower() == repo.lower():
            start.commits[name] = (sources[name][1], f"release {start.tag}")
        else:
            sha = tag_commit(repo, start.tag)
            if sha:
                start.commits[name] = (sha, f"tag {start.tag}")
    if "runtime" in sources:
        start.commits["runtime"] = (sources["runtime"][1], f"release {start.tag}")
    elif "Meadow.OS" in start.commits:
        try:
            start.commits["runtime"] = (runtime_pin(start.commits["Meadow.OS"][0])["commit"],
                                        f"pinned at {start.tag}")
        except GhError:
            pass  # No MONO_RUNTIME_COMMIT then: the runtime starts at the date.

    # The operating system date: when the Meadow.OS commit of the release was made.
    if "Meadow.OS" in sources:
        start.os_date = commit_date(*sources["Meadow.OS"])
    elif "Meadow.OS" in start.commits:
        start.os_date = commit_date(OS_REPO, start.commits["Meadow.OS"][0])
    elif release:
        sys.exit(f"release {start.tag} has no manifest.json giving its operating system commit, so it has no date")
    else:
        sys.exit(f"'{start.tag}' is not a release in {releases_repo}, nor a tag in {OS_REPO}")
    return start


# --------------------------------------------------------------------------------------------------
# Finding where the changes end, and the commits in between.


def resolve(repo: str, ref: str) -> str:
    """Return the full hash of a branch, tag or (short) commit in a repository."""
    try:
        return str(gh_json(f"repos/{repo}/commits/{ref}")["sha"])
    except GhError as error:
        sys.exit(f"cannot find '{ref}' in {repo}: {error}")


def os_heads(use_release_yaml: bool, release_file: Path) -> dict[str, tuple[str, str, str]]:
    """Return the repository, end commit and a label for Meadow.OS and Meadow-ESP32."""
    if not use_release_yaml:
        return {name: (repo, resolve(repo, BUMP_BASE[repo]), BUMP_BASE[repo])
                for name, repo in (("Meadow.OS", OS_REPO), ("Meadow-ESP32", ESP32_REPO))}
    try:
        spec = load_release_file(release_file)
    except (OSError, ReleaseFileError) as error:
        sys.exit(f"cannot read {release_file}: {error}")
    if OS_SOURCES.get(spec.os_source.lower()) != OS_REPO:
        sys.exit(f"{release_file} releases from '{spec.os_source}'; only Meadow.OS releases can be drafted")
    if not (spec.os_commit and spec.esp32_commit):
        sys.exit(f"{release_file} must give both the OS and the ESP32 commit")
    return {"Meadow.OS": (OS_REPO, resolve(OS_REPO, spec.os_commit), release_file.name),
            "Meadow-ESP32": (ESP32_REPO, resolve(ESP32_REPO, spec.esp32_commit), release_file.name)}


def build_ranges(start: Start, heads: dict[str, tuple[str, str, str]]) -> list[Range]:
    """Return the range of every repository, in the order of GROUPS."""
    pin = runtime_pin(heads["Meadow.OS"][1])
    runtime_repo = re.search(GITHUB_REPO, pin["repository"]).group(1)  # type: ignore[union-attr]
    ends = {**heads, "runtime": (runtime_repo, pin["commit"], "MONO_RUNTIME_COMMIT")}
    for name, repo in LIBRARY_REPOS.items():
        branch = str(gh_json(f"repos/{repo}")["default_branch"])
        ends[name] = (repo, resolve(repo, branch), branch)

    ranges = []
    for group, names in GROUPS.items():
        for name in names:
            repo, head, label = ends[name]
            base, how = start.commits.get(name, ("", ""))
            ranges.append(Range(name, repo, group, head, label, base, how, "" if base else start.os_date))

    # Runtime work that has not been pinned yet is not in the release; say how much there is.
    branch = pin.get("branch")
    if branch:
        try:
            ahead = int(gh_json(f"repos/{runtime_repo}/compare/{pin['commit']}...{branch}")["ahead_by"])
        except (GhError, KeyError, ValueError):
            ahead = 0
        if ahead:
            runtime = next(span for span in ranges if span.name == "runtime")
            runtime.note = (f"{ahead} commit{'s' if ahead != 1 else ''} on the runtime branch `{branch}` "
                            f"are not pinned in MONO_RUNTIME_COMMIT yet, so they are not listed.")
    return ranges


def load_commits(span: Range) -> None:
    """Fill in the commits of a range, oldest first."""
    if span.base:
        if span.base == span.head:
            return
        pages = json.loads(gh("api", "--paginate", "--slurp",
                              f"repos/{span.repo}/compare/{span.base}...{span.head}?per_page=100"))
        span.behind = int(pages[0].get("behind_by", 0)) if pages else 0
        found = [commit for page in pages for commit in page.get("commits", [])]
    else:
        pages = json.loads(gh("api", "--paginate", "--slurp",
                              f"repos/{span.repo}/commits?sha={span.head}&since={span.since}&per_page=100"))
        found = [commit for page in pages for commit in page][::-1]
    seen: set[str] = set()
    for commit in found:
        if commit["sha"] not in seen:
            seen.add(commit["sha"])
            span.commits.append(commit)
    drop_upstream(span)


def drop_upstream(span: Range) -> None:
    """Leave out the commits of a fork that came from its upstream (dotnet/runtime for the runtime).

    Moving a fork onto a newer upstream brings in thousands of upstream commits; only the fork's own
    commits, the ones not on the upstream's default branch, are listed.
    """
    details = gh_json(f"repos/{span.repo}")
    parent = details.get("parent") if details.get("fork") else None
    if not parent or not span.commits:
        return
    owner, name = span.repo.split("/")
    pages = json.loads(gh("api", "--paginate", "--slurp", f"repos/{parent['full_name']}/compare/"
                          f"{parent['default_branch']}...{owner}:{name}:{span.head}?per_page=100"))
    own = {commit["sha"] for page in pages for commit in page.get("commits", [])}
    kept = [commit for commit in span.commits if commit["sha"] in own]
    dropped = len(span.commits) - len(kept)
    if dropped:
        span.commits = kept
        span.note = " ".join(filter(None, [
            span.note, f"{dropped} commit{'s' if dropped != 1 else ''} from upstream {parent['full_name']} "
                       f"are not listed; only the {len(kept)} commits of the fork itself are."]))


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


def pull_requests_of(repo: str, batch: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """Look up the pull requests of a batch of commits in one GraphQL request.

    GitHub sometimes fails a large query ("Something went wrong while executing your query"), so a
    failed request is tried again, and then split in half.
    """
    owner, name = repo.split("/")
    fields = "\n".join(
        f'c{i}: object(oid: "{sha}") {{ ... on Commit {{ oid '
        f"associatedPullRequests(first: 10) {{ nodes {{ {PR_FIELDS} }} }} }} }}"
        for i, sha in enumerate(batch))
    query = f'query {{ repository(owner: "{owner}", name: "{name}") {{ {fields} }} }}'
    for attempt in range(GRAPHQL_TRIES):
        try:
            data = json.loads(gh("api", "graphql", "-f", f"query={query}"))["data"]["repository"]
            break
        except GhError:
            if attempt + 1 < GRAPHQL_TRIES:
                time.sleep(2 ** attempt)
    else:
        if len(batch) == 1:
            raise GhError(f"cannot look up the pull requests of {repo}@{batch[0][:8]}")
        half = len(batch) // 2
        return {**pull_requests_of(repo, batch[:half]), **pull_requests_of(repo, batch[half:])}
    return {sha: ((data.get(f"c{i}") or {}).get("associatedPullRequests") or {}).get("nodes") or []
            for i, sha in enumerate(batch)}


def associated_pull_requests(repo: str, shas: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """Return the pull requests GitHub associates with each commit, looked up in batches."""
    result: dict[str, list[dict[str, Any]]] = {}
    for start in range(0, len(shas), GRAPHQL_BATCH):
        result.update(pull_requests_of(repo, shas[start:start + GRAPHQL_BATCH]))
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
    """Sort every commit into issues fixed, pull requests and direct commits."""
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


def overview(ranges: Sequence[Range]) -> list[str]:
    """Return the Repository Overview table, and any notes about the ranges."""
    lines = ["| Group | Repository | From | To | Commits |", "| --- | --- | --- | --- | --- |"]
    for span in ranges:
        commit_url = f"https://github.com/{span.repo}/commit/"
        start = (f"[{span.base[:8]}]({commit_url}{span.base}) ({span.base_label})" if span.base
                 else f"{span.since[:10]} (operating system date)")
        lines.append(f"| {span.group} | [{span.name}](https://github.com/{span.repo}) | {start} "
                     f"| [{span.head[:8]}]({commit_url}{span.head}) ({span.head_label}) "
                     f"| [{len(span.commits)}]({span.commits_url}) |")
    notes = [f"{span.name}: {span.note}" for span in ranges if span.note]
    notes += [f"{span.name}: the new commit is not a descendant of the starting commit ({span.behind} commits "
              f"of the start are not on it); the history may have been rewritten." for span in ranges if span.behind]
    if notes:
        lines += [""] + [f"> {note}" for note in notes]
    return lines


def group_changes(group: str, ranges: Sequence[Range], changes: Changes) -> list[str]:
    """Return the list of one group: its issues resolved, then its pull requests without an issue."""
    repos = {span.repo for span in ranges if span.group == group}
    lines = []
    for issue in changes.issues:
        fixes = [pr for pr in issue.fixed_by if pr.repo in repos]
        if fixes:
            state = " *(still open)*" if issue.state.upper() == "OPEN" else ""
            lines.append(f"- [{short_repo(issue.repo)}#{issue.number}]({issue.url}) {escape(issue.title)}{state} "
                         f"— fixed by {', '.join(pr_link(pr) for pr in fixes)}")
    for name in GROUPS[group]:
        for pr in changes.pull_requests.get(name, []):
            if not pr.issues:
                author = f" by @{pr.author}" if pr.author else ""
                lines.append(f"- {pr_link(pr)} {escape(pr.title)} *(pull request{author})*")
    return lines or ["None."]


def direct_commit_lines(commits: Sequence[dict[str, Any]]) -> list[str]:
    """Return the list of commits made without a pull request, folded away when it is long."""
    lines = []
    for commit in commits:
        message = commit["commit"]["message"] or ""
        headline = message.splitlines()[0] if message else ""
        author = (commit.get("author") or {}).get("login") or commit["commit"]["author"]["name"]
        lines.append(f"- [{commit['sha'][:8]}]({commit['html_url']}) {escape(headline)} ({author})")
    if len(lines) > FOLD_AFTER:
        return [f"<details><summary>{len(lines)} commits</summary>", "", *lines, "", "</details>"]
    return lines


def render(start: Start, ranges: Sequence[Range], changes: Changes, today: str) -> str:
    """Return the Markdown of the draft release note."""
    lines = [
        "# Draft Release Note",
        "",
        f"Changes since {start.description} (operating system date {start.os_date[:10]}) to {today}.",
        "This is a draft: edit it into the Notes in release.yaml.",
        "",
        "## Highlights",
        "",
        HIGHLIGHTS,
        "",
        "## Repository Overview",
        "",
        *overview(ranges),
        "",
        "## Issues Resolved",
    ]
    for group in GROUPS:
        lines += ["", f"### {group}", "", *group_changes(group, ranges, changes)]

    with_direct = [span for span in ranges if changes.direct_commits.get(span.name)]
    if with_direct:
        lines += ["", "## Commits Without a Pull Request"]
        for span in with_direct:
            lines += ["", f"### {span.name}", "", *direct_commit_lines(changes.direct_commits[span.name])]
    return "\n".join(lines).rstrip() + "\n"


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Read the command line."""
    parser = argparse.ArgumentParser(description="Draft the release notes for the next Meadow release.")
    parser.add_argument("--since", "--base", dest="since", metavar="TAG",
                        help="the release or tag to start from (default: the last Full release in this repository)")
    parser.add_argument("--release-yaml", action="store_true",
                        help="end Meadow.OS and Meadow-ESP32 at the commits in release.yaml instead of the "
                             "branch heads")
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
        say(f"starting from {start.kind} {start.tag}, operating system date {start.os_date}")
        ranges = build_ranges(start, os_heads(args.release_yaml, args.release_file))
        for span in ranges:
            load_commits(span)
            begin = f"{span.base[:8]} ({span.base_label})" if span.base else f"since {span.since}"
            say(f"{span.name}: {begin} to {span.head[:8]} ({span.head_label}), {len(span.commits)} commits")
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
