"""Unit tests for generate_draft_release_note.py.

These cover the logic that needs no network, with the GitHub calls replaced.  Run them from this
directory with

    python3 -m unittest -v
"""

import unittest
from typing import Any
from unittest import mock

import generate_draft_release_note as gd

OS = "WildernessLabs/Meadow.OS"


def commit(sha: str, message: str = "change", parents: int = 1) -> dict[str, Any]:
    """Return a commit as the compare API gives it."""
    return {"sha": sha, "html_url": f"https://github.com/{OS}/commit/{sha}", "parents": [{}] * parents,
            "author": {"login": "dev"}, "commit": {"message": message, "author": {"name": "Dev"}}}


def pull(number: int, title: str, body: str = "", closing: Any = (), head: str = "feature",
         repo: str = OS, merged: str | None = "2026-11-01T00:00:00Z") -> dict[str, Any]:
    """Return a pull request as the GraphQL query gives it."""
    return {"number": number, "title": title, "url": f"https://github.com/{repo}/pull/{number}",
            "mergedAt": merged, "headRefName": head, "author": {"login": "dev"}, "body": body,
            "baseRepository": {"nameWithOwner": repo},
            "closingIssuesReferences": {"nodes": list(closing)}}


def issue_ref(number: int, title: str, state: str = "CLOSED", repo: str = OS) -> dict[str, Any]:
    """Return a closing issue reference as the GraphQL query gives it."""
    return {"number": number, "title": title, "url": f"https://github.com/{repo}/issues/{number}",
            "state": state, "repository": {"nameWithOwner": repo}}


class ClosingRefs(unittest.TestCase):
    """Finding the issues a pull request closes from its text."""

    def test_keywords_and_forms(self) -> None:
        """Every closing keyword works, with a bare number, another repository or a URL."""
        text = ("Fixes #12, closes #13.\nResolved: other/repo#4\n"
                "fix https://github.com/WildernessLabs/Meadow_Issues/issues/99\nfixes #12")
        self.assertEqual(gd.closing_refs(text, OS),
                         [(OS, 12), (OS, 13), ("other/repo", 4), ("WildernessLabs/Meadow_Issues", 99)])

    def test_mentions_are_not_closes(self) -> None:
        """A plain mention, or a word that only contains a keyword, closes nothing."""
        self.assertEqual(gd.closing_refs("See #5; prefixes #6; related to #7", OS), [])
        self.assertEqual(gd.closing_refs("", OS), [])


class Manifest(unittest.TestCase):
    """Reading the source commits from a release's manifest.json."""

    def test_meadow_os_release(self) -> None:
        """A Meadow.OS manifest gives the commit of each repository."""
        manifest = {"generation": "3.0", "sources": {name: {"commit": name.lower()} for name in gd.REPO_NAMES}}
        self.assertEqual(gd.manifest_commits(manifest),
                         {"Meadow.OS": "meadow.os", "Meadow-ESP32": "meadow-esp32", "runtime": "runtime"})

    def test_other_releases(self) -> None:
        """Legacy releases, and manifests missing a commit, are not used."""
        sources = {name: {"commit": "abc"} for name in gd.REPO_NAMES}
        self.assertIsNone(gd.manifest_commits({"generation": "legacy", "sources": sources}))
        self.assertIsNone(gd.manifest_commits({"generation": "3.0", "sources": {**sources, "runtime": None}}))
        self.assertIsNone(gd.manifest_commits({"generation": "3.0"}))


class StartingPoint(unittest.TestCase):
    """Choosing where the ranges start from --since."""

    COMMITS = {"Meadow.OS": "os1", "Meadow-ESP32": "esp1", "runtime": "rt1"}

    def fake_gh_json(self, tags: dict[str, dict[str, str]]) -> Any:
        """Return a gh_json that knows no releases and the given tags (repo -> tag -> commit)."""
        def gh_json(path: str) -> dict[str, Any]:
            for repo, repo_tags in tags.items():
                for tag, sha in repo_tags.items():
                    if path == f"repos/{repo}/commits/refs/tags/{tag}":
                        return {"sha": sha}
            raise gd.GhError("Not Found")
        return gh_json

    def test_release_with_manifest(self) -> None:
        """A release here is used through its manifest.json."""
        release = {"tag_name": "3.0.0.1", "html_url": "https://example/3.0.0.1"}
        with mock.patch.object(gd, "gh_json", return_value=release), \
                mock.patch.object(gd, "release_manifest", return_value={"generation": "3.0", "sources": {
                    name: {"commit": sha} for name, sha in self.COMMITS.items()}}):
            base = gd.starting_point("WildernessLabs/Meadow.Releases", "3.0.0.1")
        self.assertEqual((base.tag, base.is_release, base.commits), ("3.0.0.1", True, self.COMMITS))
        self.assertIn("Meadow release [3.0.0.1]", base.description)

    def test_source_tags(self) -> None:
        """With no release, the tag in Meadow.OS and Meadow-ESP32 is used, and the runtime pin at the OS tag."""
        tags = {gd.OS_REPO: {"2.999.51.3": "os1"}, gd.ESP32_REPO: {"2.999.51.3": "esp1"}}
        with mock.patch.object(gd, "gh_json", side_effect=self.fake_gh_json(tags)), \
                mock.patch.object(gd, "runtime_pin", return_value={"commit": "rt1"}) as pin:
            base = gd.starting_point("WildernessLabs/Meadow.Releases", "2.999.51.3")
        pin.assert_called_once_with("os1")
        self.assertEqual((base.tag, base.is_release, base.commits), ("2.999.51.3", False, self.COMMITS))
        self.assertIn(f"tag [2.999.51.3](https://github.com/{gd.OS_REPO}/tree/2.999.51.3)", base.description)

    def test_tag_missing_from_a_repository(self) -> None:
        """A tag that is only in one source repository is refused, naming the other."""
        tags = {gd.OS_REPO: {"x": "os1"}}
        with mock.patch.object(gd, "gh_json", side_effect=self.fake_gh_json(tags)), \
                self.assertRaises(SystemExit) as stop:
            gd.starting_point("WildernessLabs/Meadow.Releases", "x")
        self.assertIn(gd.ESP32_REPO, str(stop.exception))
        self.assertNotIn(gd.OS_REPO, str(stop.exception))


class VersionBumps(unittest.TestCase):
    """Leaving the version bump pull requests out."""

    def test_bumps(self) -> None:
        """The script's branch and a hand-made bump title are both bumps; other changes are not."""
        self.assertTrue(gd.is_version_bump({"headRefName": "meadow-2.999.53", "title": "x"}))
        self.assertTrue(gd.is_version_bump({"headRefName": "b", "title": "Bump version from 2.999.51 to 2.999.53"}))
        self.assertFalse(gd.is_version_bump({"headRefName": "meadow-wifi", "title": "Bumper support"}))


class CollectAndRender(unittest.TestCase):
    """Sorting commits into issues, pull requests and direct commits, and writing the note."""

    def setUp(self) -> None:
        """Build one Meadow.OS range whose commits cover each case."""
        self.span = gd.Range("Meadow.OS", OS, "a" * 40, "b" * 40, "main",
                             commits=[commit("c1"), commit("c2"), commit("c3"), commit("c4", "Direct [fix]"),
                                      commit("c5", "Merge branch", parents=2), commit("c6"), commit("c7")])
        self.prs = {
            "c1": [pull(10, "Fix the SPI bus", closing=[issue_ref(1, "SPI hangs")])],
            "c2": [pull(10, "Fix the SPI bus", closing=[issue_ref(1, "SPI hangs")])],
            "c3": [pull(11, "Tidy logging"), pull(99, "Unmerged", merged=None)],
            "c6": [pull(12, "Bump the version to 3.0.1", head="meadow-3.0.1")],
            # #1 is in both the GraphQL references and the body, so only #2 is looked up.
            "c7": [pull(13, "UART <fix>", body="Fixes #2 and fixes #1", closing=[issue_ref(1, "SPI hangs")])],
        }

    def collect(self) -> gd.Changes:
        """Run collect_changes with the GitHub lookups replaced."""
        issue_two = {"title": "UART drops bytes", "html_url": f"https://github.com/{OS}/issues/2", "state": "open"}
        with mock.patch.object(gd, "associated_pull_requests", return_value=self.prs), \
                mock.patch.object(gd, "gh_json", return_value=issue_two) as gh_json, \
                mock.patch.object(gd, "say"):
            changes = gd.collect_changes([self.span])
        gh_json.assert_called_once_with(f"repos/{OS}/issues/2")
        return changes

    def test_collect(self) -> None:
        """Issues gather their pull requests; bumps, unmerged and merge commits are left out."""
        changes = self.collect()
        self.assertEqual([(issue.number, [pr.number for pr in issue.fixed_by]) for issue in changes.issues],
                         [(1, [10, 13]), (2, [13])])
        self.assertEqual([pr.number for pr in changes.pull_requests["Meadow.OS"]], [10, 11, 13])
        self.assertEqual([c["sha"] for c in changes.direct_commits["Meadow.OS"]], ["c4"])

    def test_render(self) -> None:
        """The note links issues to their fixes and lists what has no issue or no pull request."""
        release = gd.Base("3.0.0.1", "https://example/3.0.0.1", {})
        text = gd.render(release, [self.span], self.collect(), "2026-11-06")
        self.assertIn(f"- [Meadow.OS#1]({OS_URL}/issues/1) SPI hangs — fixed by "
                      f"[Meadow.OS#10]({OS_URL}/pull/10), [Meadow.OS#13]({OS_URL}/pull/13)", text)
        self.assertIn("UART drops bytes *(still open)*", text)
        self.assertIn(f"- [Meadow.OS#11]({OS_URL}/pull/11) Tidy logging (@dev)", text)
        self.assertNotIn("UART \\<fix\\>", text.split("## Pull requests without an issue")[1])
        self.assertIn("Direct \\[fix\\] (dev)", text)
        self.assertNotIn("Bump the version", text)

    def test_render_empty(self) -> None:
        """With nothing changed the sections say so."""
        release = gd.Base("3.0.0.1", "https://example/3.0.0.1", {})
        text = gd.render(release, [self.span], gd.Changes([], {}, {}), "2026-11-06")
        self.assertEqual(text.count("None."), 2)
        self.assertNotIn("Commits without a pull request", text)


OS_URL = f"https://github.com/{OS}"

if __name__ == "__main__":
    unittest.main()
