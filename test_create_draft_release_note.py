"""Unit tests for create_draft_release_note.py.

These cover the logic that needs no network, with the GitHub calls replaced.  Run them from this
directory with

    python3 -m unittest -v
"""

import json
import unittest
from typing import Any
from unittest import mock

import create_draft_release_note as gd

OS = "WildernessLabs/Meadow.OS"
OS_URL = f"https://github.com/{OS}"
FOUNDATION = "WildernessLabs/Meadow.Foundation"
RELEASES = "WildernessLabs/Meadow.Releases"


def router(responses: dict[str, Any]) -> Any:
    """Return a gh_json that answers the given API paths and fails, as GitHub does, for any other."""
    def gh_json(path: str) -> Any:
        if path in responses:
            return responses[path]
        raise gd.GhError("Not Found")
    return gh_json


def commit(sha: str, message: str = "change", parents: int = 1, repo: str = OS) -> dict[str, Any]:
    """Return a commit as the compare and commits APIs give it."""
    return {"sha": sha, "html_url": f"https://github.com/{repo}/commit/{sha}", "parents": [{}] * parents,
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

    def test_sources(self) -> None:
        """Each source with a commit is given with its repository; sources without one are left out."""
        manifest = {"sources": {
            "Meadow.OS": {"repository": "https://github.com/WildernessLabs/Meadow", "commit": "abc"},
            "Meadow-ESP32": {"repository": "https://github.com/WildernessLabs/Meadow-ESP32", "commit": None},
            "runtime": None}}
        self.assertEqual(gd.manifest_sources(manifest), {"Meadow.OS": ("WildernessLabs/Meadow", "abc")})
        self.assertEqual(gd.manifest_sources({}), {})


class LastFullRelease(unittest.TestCase):
    """Choosing the last Full release."""

    def test_newest_full_release(self) -> None:
        """Pre-releases and drafts are ignored, and the highest version wins."""
        releases = [[{"tag_name": tag, "draft": draft, "prerelease": pre} for tag, draft, pre in (
            ("2.999.51.1", False, True), ("3.1.0.0", True, False), ("2.5.2.1", False, False),
            ("2.10.0.0", False, False), ("old-name", False, False))]]
        with mock.patch.object(gd, "gh", return_value=json.dumps(releases)):
            self.assertEqual(gd.last_full_release(RELEASES)["tag_name"], "2.10.0.0")

    def test_none(self) -> None:
        """With only pre-releases there is nothing to start from."""
        with mock.patch.object(gd, "gh", return_value=json.dumps([[{"tag_name": "3.0.0.0", "draft": False,
                                                                    "prerelease": True}]])), \
                self.assertRaises(SystemExit):
            gd.last_full_release(RELEASES)


class StartingPoint(unittest.TestCase):
    """Choosing the starting commit, or the date, of each repository."""

    @staticmethod
    def release(tag: str, prerelease: bool = False) -> dict[str, Any]:
        """Return a release as the API gives it."""
        return {"tag_name": tag, "html_url": f"https://example/{tag}", "prerelease": prerelease, "assets": []}

    @staticmethod
    def dated(sha: str, date: str) -> dict[str, Any]:
        """Return a commit with a committer date as the API gives it."""
        return {"sha": sha, "commit": {"committer": {"date": date}}}

    def test_legacy_full_release(self) -> None:
        """A Legacy release dates from its OS commit; the ESP32 starts at its tag, the rest at the date."""
        manifest = {"sources": {"Meadow.OS": {"repository": "https://github.com/WildernessLabs/Meadow",
                                              "commit": "legacy1"}}}
        responses = {"repos/WildernessLabs/Meadow/commits/legacy1": self.dated("legacy1", "2025-09-26T23:37:51Z"),
                     f"repos/{gd.ESP32_REPO}/commits/refs/tags/2.5.2.1": {"sha": "esp1"}}
        with mock.patch.object(gd, "last_full_release", return_value=self.release("2.5.2.1")), \
                mock.patch.object(gd, "release_manifest", return_value=manifest), \
                mock.patch.object(gd, "gh_json", side_effect=router(responses)):
            start = gd.starting_point(RELEASES, None)
        self.assertEqual(start.os_date, "2025-09-26T23:37:51Z")
        self.assertEqual(start.commits, {"Meadow-ESP32": ("esp1", "tag 2.5.2.1")})
        self.assertEqual(start.description, "Meadow Full release [2.5.2.1](https://example/2.5.2.1)")

    def test_meadow_os_release(self) -> None:
        """A Meadow.OS release gives every OS repository's commit through its manifest.json."""
        manifest = {"sources": {name: {"repository": f"https://github.com/{repo}", "commit": sha}
                                for name, repo, sha in (
            ("Meadow.OS", OS, "os1"), ("Meadow-ESP32", gd.ESP32_REPO, "esp1"),
            ("runtime", "WildernessLabs/runtime", "rt1"))}}
        responses = {f"repos/{RELEASES}/releases/tags/2.999.51.1": self.release("2.999.51.1", prerelease=True),
                     f"repos/{OS}/commits/os1": self.dated("os1", "2026-11-04T09:00:00Z")}
        with mock.patch.object(gd, "release_manifest", return_value=manifest), \
                mock.patch.object(gd, "gh_json", side_effect=router(responses)):
            start = gd.starting_point(RELEASES, "2.999.51.1")
        self.assertEqual(start.os_date, "2026-11-04T09:00:00Z")
        self.assertEqual({name: sha for name, (sha, _) in start.commits.items()},
                         {"Meadow.OS": "os1", "Meadow-ESP32": "esp1", "runtime": "rt1"})
        self.assertTrue(start.description.startswith("Meadow pre-release"))

    def test_source_tag(self) -> None:
        """A tag that is not a release starts at the tags, with the runtime pinned at the Meadow.OS tag."""
        responses = {f"repos/{OS}/commits/refs/tags/2.999.51.3": {"sha": "os1"},
                     f"repos/{gd.ESP32_REPO}/commits/refs/tags/2.999.51.3": {"sha": "esp1"},
                     f"repos/{OS}/commits/os1": self.dated("os1", "2026-10-01T00:00:00Z")}
        with mock.patch.object(gd, "gh_json", side_effect=router(responses)), \
                mock.patch.object(gd, "runtime_pin", return_value={"commit": "rt1"}) as pin:
            start = gd.starting_point(RELEASES, "2.999.51.3")
        pin.assert_called_once_with("os1")
        self.assertEqual((start.kind, start.os_date), ("tag", "2026-10-01T00:00:00Z"))
        self.assertEqual(start.commits["runtime"], ("rt1", "pinned at 2.999.51.3"))

    def test_unknown_tag(self) -> None:
        """A tag that is neither a release nor in Meadow.OS is refused."""
        with mock.patch.object(gd, "gh_json", side_effect=router({})), self.assertRaises(SystemExit):
            gd.starting_point(RELEASES, "nosuch")


class VersionBumps(unittest.TestCase):
    """Leaving the version bump pull requests out."""

    def test_bumps(self) -> None:
        """The script's branch and a hand-made bump title are both bumps; other changes are not."""
        self.assertTrue(gd.is_version_bump({"headRefName": "meadow-2.999.53", "title": "x"}))
        self.assertTrue(gd.is_version_bump({"headRefName": "b", "title": "Bump version from 2.999.51 to 2.999.53"}))
        self.assertFalse(gd.is_version_bump({"headRefName": "meadow-wifi", "title": "Bumper support"}))


class DropUpstream(unittest.TestCase):
    """Leaving out the upstream commits of a fork."""

    def test_fork(self) -> None:
        """Only the fork's own commits are kept, and the note says how many were left out."""
        span = gd.Range("runtime", "WildernessLabs/runtime", "Operating System", "head", "pin", since="2025",
                        commits=[commit("up1"), commit("own1"), commit("up2")], note="Unpinned.")
        compare = [{"commits": [{"sha": "own1"}, {"sha": "own0"}]}]
        details = {"fork": True, "parent": {"full_name": "dotnet/runtime", "default_branch": "main"}}
        with mock.patch.object(gd, "gh_json", return_value=details), \
                mock.patch.object(gd, "gh", return_value=json.dumps(compare)) as gh:
            gd.drop_upstream(span)
        self.assertIn("repos/dotnet/runtime/compare/main...WildernessLabs:runtime:head?per_page=100", gh.call_args[0])
        self.assertEqual([c["sha"] for c in span.commits], ["own1"])
        self.assertEqual(span.note, "Unpinned. 2 commits from upstream dotnet/runtime are not listed; "
                                    "only the 1 commits of the fork itself are.")

    def test_not_a_fork(self) -> None:
        """Other repositories keep every commit."""
        span = gd.Range("Meadow.OS", OS, "Operating System", "head", "main", commits=[commit("c1")])
        with mock.patch.object(gd, "gh_json", return_value={"fork": False}), mock.patch.object(gd, "gh") as gh:
            gd.drop_upstream(span)
        gh.assert_not_called()
        self.assertEqual(len(span.commits), 1)


class CollectAndRender(unittest.TestCase):
    """Sorting commits into issues, pull requests and direct commits, and writing the note."""

    def setUp(self) -> None:
        """Build a Meadow.OS range from a tag and a Meadow.Foundation range from the date."""
        self.os = gd.Range("Meadow.OS", OS, "Operating System", "b" * 40, "main", "a" * 40, "tag 2.5.2.1",
                           commits=[commit("c1"), commit("c2"), commit("c3"), commit("c4", "Direct [fix]"),
                                    commit("c5", "Merge branch", parents=2), commit("c6"), commit("c7")])
        self.foundation = gd.Range("Meadow.Foundation", FOUNDATION, "Meadow.Foundation", "f" * 40, "develop",
                                   since="2025-09-26T23:37:51Z",
                                   commits=[commit(f"d{i}", repo=FOUNDATION) for i in range(gd.FOLD_AFTER + 1)]
                                   + [commit("f1", repo=FOUNDATION)])
        self.prs = {
            OS: {
                "c1": [pull(10, "Fix the SPI bus", closing=[issue_ref(1, "SPI hangs")])],
                "c2": [pull(10, "Fix the SPI bus", closing=[issue_ref(1, "SPI hangs")])],
                "c3": [pull(11, "Tidy logging"), pull(99, "Unmerged", merged=None)],
                "c6": [pull(12, "Bump the version to 3.0.1", head="meadow-3.0.1")],
                # #1 is in both the GraphQL references and the body, so only #2 is looked up.
                "c7": [pull(13, "UART <fix>", body="Fixes #2 and fixes #1", closing=[issue_ref(1, "SPI hangs")])],
            },
            FOUNDATION: {
                # A driver fix that closes an issue filed against Meadow.OS lands in both groups.
                "f1": [pull(50, "New sensor driver", repo=FOUNDATION, closing=[issue_ref(1, "SPI hangs")]),
                       pull(51, "Docs", repo=FOUNDATION)],
            },
        }
        self.start = gd.Start("2.5.2.1", "https://example/2.5.2.1", "Meadow Full release", "2025-09-26T23:37:51Z")

    def collect(self) -> gd.Changes:
        """Run collect_changes with the GitHub lookups replaced."""
        issue_two = {"title": "UART drops bytes", "html_url": f"{OS_URL}/issues/2", "state": "open"}
        with mock.patch.object(gd, "associated_pull_requests", side_effect=lambda repo, shas: self.prs[repo]), \
                mock.patch.object(gd, "gh_json", return_value=issue_two) as gh_json, \
                mock.patch.object(gd, "say"):
            changes = gd.collect_changes([self.os, self.foundation])
        gh_json.assert_called_once_with(f"repos/{OS}/issues/2")
        return changes

    def test_collect(self) -> None:
        """Issues gather their pull requests; bumps, unmerged and merge commits are left out."""
        changes = self.collect()
        self.assertEqual([(issue.number, [pr.number for pr in issue.fixed_by]) for issue in changes.issues],
                         [(1, [10, 13, 50]), (2, [13])])
        self.assertEqual([pr.number for pr in changes.pull_requests["Meadow.OS"]], [10, 11, 13])
        self.assertEqual([c["sha"] for c in changes.direct_commits["Meadow.OS"]], ["c4"])
        self.assertEqual(len(changes.direct_commits["Meadow.Foundation"]), gd.FOLD_AFTER + 1)

    def render(self) -> str:
        """Return the note for the two ranges."""
        return gd.render(self.start, [self.os, self.foundation], self.collect(), "2026-11-07")

    def section(self, text: str, heading: str) -> str:
        """Return the text under a heading, up to the next heading of any level."""
        return text.split(f"{heading}\n", 1)[1].split("\n#", 1)[0]

    def test_headings(self) -> None:
        """The headings are in title case and in order, with Highlights for the user to complete."""
        text = self.render()
        headings = [line for line in text.splitlines() if line.startswith("#")]
        self.assertEqual(headings, ["# Draft Release Note", "## Highlights", "## Repository Overview",
                                    "## Issues Resolved", "### Operating System", "### Meadow.Core",
                                    "### Meadow.Foundation", "## Commits Without a Pull Request",
                                    "### Meadow.OS", "### Meadow.Foundation"])
        self.assertEqual(self.section(text, "## Highlights").strip(), "User to complete")
        self.assertIn("Changes since Meadow Full release [2.5.2.1](https://example/2.5.2.1) "
                      "(operating system date 2025-09-26) to 2026-11-07.", text)

    def test_overview(self) -> None:
        """The table shows a tag start as a commit and a date start as the operating system date."""
        overview = self.section(self.render(), "## Repository Overview")
        self.assertIn(f"| Operating System | [Meadow.OS]({OS_URL}) | [aaaaaaaa]({OS_URL}/commit/{'a' * 40}) "
                      f"(tag 2.5.2.1) |", overview)
        self.assertIn("| 2025-09-26 (operating system date) |", overview)
        self.assertIn(f"commits/{'f' * 40}?since=2025-09-26)", overview)

    def test_groups(self) -> None:
        """Each group lists its issues, with only its own fixes, then its pull requests without an issue."""
        text = self.render()
        system = self.section(text, "### Operating System")
        self.assertIn(f"- [Meadow.OS#1]({OS_URL}/issues/1) SPI hangs — fixed by "
                      f"[Meadow.OS#10]({OS_URL}/pull/10), [Meadow.OS#13]({OS_URL}/pull/13)\n", system)
        self.assertIn("UART drops bytes *(still open)*", system)
        self.assertIn(f"- [Meadow.OS#11]({OS_URL}/pull/11) Tidy logging *(pull request by @dev)*", system)
        self.assertNotIn("Bump the version", text)
        self.assertNotIn("Pull requests without an issue", text)
        self.assertEqual(self.section(text, "### Meadow.Core").strip(), "None.")
        foundation = self.section(text, "### Meadow.Foundation")
        self.assertIn(f"- [Meadow.OS#1]({OS_URL}/issues/1) SPI hangs — fixed by "
                      f"[Meadow.Foundation#50](https://github.com/{FOUNDATION}/pull/50)\n", foundation)
        self.assertIn("Docs *(pull request by @dev)*", foundation)

    def test_direct_commits(self) -> None:
        """Commits without a pull request are listed by repository, and a long list is folded away."""
        text = self.render().split("## Commits Without a Pull Request", 1)[1]
        self.assertIn("Direct \\[fix\\] (dev)", self.section(text, "### Meadow.OS"))
        self.assertIn(f"<details><summary>{gd.FOLD_AFTER + 1} commits</summary>", text)


class GraphQLRetries(unittest.TestCase):
    """Looking up pull requests when GitHub fails a query."""

    def test_retry_then_split(self) -> None:
        """A batch that keeps failing is split in half, and each half is looked up on its own."""
        def gh(*args: str) -> str:
            query = args[-1]
            if query.count("object(oid") > 1:
                raise gd.GhError("Something went wrong while executing your query")
            sha = query.split('oid: "')[1].split('"')[0]
            return json.dumps({"data": {"repository": {"c0": {"associatedPullRequests": {"nodes": [{"n": sha}]}}}}})
        with mock.patch.object(gd, "gh", side_effect=gh) as called, mock.patch.object(gd.time, "sleep"):
            found = gd.associated_pull_requests(OS, ["s1", "s2"])
        self.assertEqual(found, {"s1": [{"n": "s1"}], "s2": [{"n": "s2"}]})
        self.assertEqual(called.call_count, gd.GRAPHQL_TRIES + 2)

    def test_single_commit_fails(self) -> None:
        """A single commit that cannot be looked up is an error."""
        with mock.patch.object(gd, "gh", side_effect=gd.GhError("down")), mock.patch.object(gd.time, "sleep"), \
                self.assertRaises(gd.GhError):
            gd.associated_pull_requests(OS, ["s1"])
