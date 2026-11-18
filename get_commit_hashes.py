#!/usr/bin/env python3
"""List the last pull requests merged in each of the operating system repositories.

The repositories are clones that sit beside this one (siblings of Meadow.Releases).  For each one
the script finds the GitHub repository from the clone's origin, then asks GitHub for the last N
pull requests merged into its default branch (for example main or develop) and prints, newest
first, the commit at the top of the branch after the merge, the merge date, the pull request number
and its title.  A clone that is not there is reported and skipped.

GitHub is asked, not git, because a pull request merged with "Rebase and merge" (or "Squash and
merge") leaves no merge commit, so `git log --merges` does not see it.  The commit printed is the
one GitHub Actions built when the pull request was merged, the one to pin in release.yaml.

    ./get_commit_hashes.py       the last 5 pull requests merged in each repository
    ./get_commit_hashes.py 10    the last 10

Requires: Python 3.9+, git, and the GitHub CLI (gh) logged in with access to the repositories.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPOSITORIES = ("Rebase", "Meadow.OS", "Legacy", "Meadow", "Meadow-ESP32")
DEFAULT_COUNT = 5
# gh lists pull requests by when they were last updated.  A pull request is updated when it is
# merged (and may be again later, by a comment), so the last N merged are among the last N updated
# unless a later update pushes some down; ask for a margin and sort by the merge date.
MINIMUM_FETCH = 30
GITHUB_URL = re.compile(r"github\.com[:/](?P<name>[^/]+/[^/]+?)(?:\.git)?/?$")


class CommandError(Exception):
    """A git or gh command failed."""


@dataclass(frozen=True)
class MergedPullRequest:
    """A pull request that was merged into the default branch."""

    number: int
    title: str
    branch: str
    merged_at: str
    commit: str

    def summary(self) -> str:
        """Return 'hash date #number title (branch)'."""
        return f"{self.commit} {self.merged_at[:10]} #{self.number} {self.title} ({self.branch})"


def run(*command: str) -> str:
    """Run a command and return its output, raising CommandError if it fails."""
    try:
        result = subprocess.run(list(command), capture_output=True, text=True, check=False)
    except FileNotFoundError as error:
        raise CommandError(f"{command[0]} is not installed") from error
    if result.returncode != 0:
        raise CommandError(result.stderr.strip() or f"{' '.join(command)} failed")
    return result.stdout


def github_repository(clone: Path) -> str:
    """Return the GitHub repository (owner/name) that the clone's origin points at."""
    url = run("git", "-C", str(clone), "remote", "get-url", "origin").strip()
    match = GITHUB_URL.search(url)
    if match is None:
        raise CommandError(f"origin is not a GitHub repository: {url}")
    return match.group("name")


def default_branch(repository: str) -> str:
    """Return the name of the repository's default branch on GitHub."""
    return run("gh", "repo", "view", repository, "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name").strip()


def parse_pull_requests(output: str, count: int) -> list[MergedPullRequest]:
    """Return the `count` most recently merged pull requests from gh's JSON output, newest first."""
    entries: list[dict[str, Any]] = json.loads(output)
    pulls = [
        MergedPullRequest(
            number=entry["number"],
            title=entry["title"],
            branch=entry["headRefName"],
            merged_at=entry["mergedAt"],
            commit=(entry.get("mergeCommit") or {}).get("oid", "?" * 40),
        )
        for entry in entries
        if entry.get("mergedAt")
    ]
    pulls.sort(key=lambda pull: pull.merged_at, reverse=True)
    return pulls[:count]


def merged_pull_requests(repository: str, branch: str, count: int) -> list[MergedPullRequest]:
    """Return the last `count` pull requests merged into `branch`, newest first."""
    output = run(
        "gh", "pr", "list", "--repo", repository, "--state", "merged", "--base", branch,
        "--search", "sort:updated-desc", "--limit", str(max(MINIMUM_FETCH, count * 3)),
        "--json", "number,title,headRefName,mergedAt,mergeCommit",
    )
    return parse_pull_requests(output, count)


def report(clone: Path, count: int) -> bool:
    """Print the last pull requests merged in the clone's repository; return False on an error."""
    print(f"== {clone.name}")
    if not (clone / ".git").exists():
        print("   not found, skipped")
        return True
    try:
        repository = github_repository(clone)
        branch = default_branch(repository)
        pulls = merged_pull_requests(repository, branch, count)
    except CommandError as error:
        print(f"   error: {error}")
        return False
    print(f"   {repository} {branch}")
    for pull in pulls:
        print(f"   {pull.summary()}")
    if not pulls:
        print("   no merged pull requests")
    return True


def positive_integer(text: str) -> int:
    """Parse a whole number greater than zero, for argparse."""
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be a whole number greater than 0, not {text!r}")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    """List the merged pull requests; the exit code is 1 if any clone could not be read."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "count", nargs="?", type=positive_integer, default=DEFAULT_COUNT,
        help=f"number of merged pull requests per repository (default {DEFAULT_COUNT})",
    )
    arguments = parser.parse_args(argv)

    parent = Path(__file__).resolve().parent.parent
    succeeded = True
    for index, name in enumerate(REPOSITORIES):
        if index:
            print()
        succeeded &= report(parent / name, arguments.count)
    return 0 if succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
