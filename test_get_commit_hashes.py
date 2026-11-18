"""Unit tests for get_commit_hashes.py.

These need no network: gh's output is given as text.  Run them from this directory with

    python3 -m unittest -v
"""

from __future__ import annotations

import argparse
import json
import unittest

import get_commit_hashes as gch


def pull(number: int, merged_at: str | None, commit: str | None = "a" * 40) -> dict[str, object]:
    """Return one pull request as gh pr list --json prints it."""
    return {
        "number": number,
        "title": f"Title {number}",
        "headRefName": f"branch-{number}",
        "mergedAt": merged_at,
        "mergeCommit": {"oid": commit} if commit else None,
    }


class ParsePullRequestsTests(unittest.TestCase):
    """parse_pull_requests orders by merge date and keeps the newest."""

    def test_sorted_by_merge_date_not_list_order(self) -> None:
        output = json.dumps([
            pull(1, "2026-11-10T10:00:00Z"),
            pull(3, "2026-11-18T10:00:00Z"),
            pull(2, "2026-11-15T10:00:00Z"),
        ])
        self.assertEqual([p.number for p in gch.parse_pull_requests(output, 2)], [3, 2])

    def test_skips_unmerged_and_handles_missing_merge_commit(self) -> None:
        output = json.dumps([pull(1, None), pull(2, "2026-11-18T10:00:00Z", commit=None)])
        pulls = gch.parse_pull_requests(output, 5)
        self.assertEqual(len(pulls), 1)
        self.assertEqual(pulls[0].commit, "?" * 40)

    def test_summary(self) -> None:
        pulls = gch.parse_pull_requests(json.dumps([pull(645, "2026-11-18T13:37:27Z")]), 1)
        self.assertEqual(pulls[0].summary(), f"{'a' * 40} 2026-11-18 #645 Title 645 (branch-645)")


class GitHubUrlTests(unittest.TestCase):
    """GITHUB_URL takes owner/name from the usual forms of origin."""

    def test_forms(self) -> None:
        for url in (
            "git@github.com:WildernessLabs/Meadow-ESP32.git",
            "https://github.com/WildernessLabs/Meadow-ESP32.git",
            "https://github.com/WildernessLabs/Meadow-ESP32",
        ):
            match = gch.GITHUB_URL.search(url)
            self.assertIsNotNone(match, url)
            assert match is not None
            self.assertEqual(match.group("name"), "WildernessLabs/Meadow-ESP32")


class PositiveIntegerTests(unittest.TestCase):
    """The count must be a whole number greater than zero."""

    def test_values(self) -> None:
        self.assertEqual(gch.positive_integer("10"), 10)
        for text in ("0", "-1", "five"):
            with self.assertRaises(argparse.ArgumentTypeError):
                gch.positive_integer(text)


if __name__ == "__main__":
    unittest.main()
