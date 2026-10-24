"""Unit tests for create_release.py.

These cover the logic that needs no network: version rules, release.yaml handling, and the tag and
version bump decisions with the GitHub calls replaced.  Run them from this directory with

    python3 -m unittest -v
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import create_release as cr


def quiet_report() -> cr.Report:
    """Return a Report whose output is not printed."""
    with mock.patch.object(cr, "say"):
        return cr.Report()


class VersionRules(unittest.TestCase):
    """The small functions that implement the version rules."""

    def test_parse_version(self) -> None:
        """A dotted numeric version is split into integers."""
        self.assertEqual(cr.parse_version("2.999.51"), [2, 999, 51])
        self.assertEqual(cr.parse_version(" 3.0.1.412 \n"), [3, 0, 1, 412])

    def test_parse_version_rejects_bad_text(self) -> None:
        """Too few parts, or anything not a number, is an error."""
        for text in ("3.0", "3.0.beta", "3.0.1-beta5", ""):
            with self.assertRaises(ValueError, msg=text):
                cr.parse_version(text)

    def test_format_version(self) -> None:
        """Parts are joined with dots."""
        self.assertEqual(cr.format_version([3, 0, 1, 4]), "3.0.1.4")

    def test_release_type(self) -> None:
        """An odd y is a beta; an even y, including 0, is a full release."""
        self.assertEqual(cr.release_type(51), "Beta")
        self.assertEqual(cr.release_type(50), "Full")
        self.assertEqual(cr.release_type(0), "Full")

    def test_next_y(self) -> None:
        """After a beta y goes up by 2, after a full release by 1, so the next build is a beta."""
        self.assertEqual(cr.next_y(51), 53)
        self.assertEqual(cr.next_y(50), 51)
        self.assertEqual(cr.next_y(0), 1)

    def test_first_line(self) -> None:
        """Only the first line counts, stripped; blank text gives an empty string."""
        self.assertEqual(cr.first_line("2.999.51\nignored\n"), "2.999.51")
        self.assertEqual(cr.first_line("  \n"), "")


class ReleaseFile(unittest.TestCase):
    """Reading and checking release.yaml."""

    def load(self, text: str) -> cr.ReleaseSpec:
        """Write `text` to a temporary release.yaml and load it."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "release.yaml"
            path.write_text(text, encoding="utf-8")
            return cr.load_release_file(path)

    def test_values_stay_text(self) -> None:
        """A commit made only of digits is not turned into a number."""
        spec = self.load("Release:\n  Version: 2.999.51\n  OS:\n    Source: Rebase\n    Commit: 12345678\n"
                         "  ESP32:\n    Commit: abcdef01\n")
        self.assertEqual(spec.os_commit, "12345678")
        self.assertEqual(spec.version, "2.999.51")
        self.assertEqual(spec.min_cli_version, cr.DEFAULT_MIN_CLI_VERSION)

    def test_missing_release_section(self) -> None:
        """A file without a Release section is reported, not a traceback."""
        with self.assertRaises(cr.ReleaseFileError):
            self.load("something: else\n")

    def test_validate_reports_missing_fields(self) -> None:
        """Every missing field is named."""
        report = quiet_report()
        with mock.patch.object(cr, "say"):
            self.assertIsNone(cr.validate_spec(self.load("Release:\n  Version: 2.999.51\n"), report))
        self.assertEqual(len(report.errors), 3)

    def test_validate_accepts_three_or_four_parts(self) -> None:
        """The build number is optional in release.yaml."""
        for version, parts in (("2.999.51", 3), ("2.999.51.7", 4)):
            spec = self.load(f"Release:\n  Version: {version}\n  OS:\n    Source: Rebase\n    Commit: a\n"
                             "  ESP32:\n    Commit: b\n")
            checked = cr.validate_spec(spec, quiet_report())
            self.assertIsNotNone(checked)
            assert checked is not None
            self.assertEqual(len(checked[0]), parts)
            self.assertEqual(checked[1], "WildernessLabs/Meadow.OS")

    def test_validate_rejects_legacy_source(self) -> None:
        """Legacy releases are not supported yet."""
        spec = self.load("Release:\n  Version: 2.999.1\n  OS:\n    Source: Legacy\n    Commit: a\n"
                         "  ESP32:\n    Commit: b\n")
        report = quiet_report()
        with mock.patch.object(cr, "say"):
            self.assertIsNone(cr.validate_spec(spec, report))
        self.assertEqual(len(report.errors), 1)

    def test_type_must_agree_with_y(self) -> None:
        """A declared Type that contradicts y is an error; the old names only warn."""
        with mock.patch.object(cr, "say"):
            report = quiet_report()
            cr.check_type("Full", [2, 999, 51], report)
            self.assertEqual(len(report.errors), 1)
            report = quiet_report()
            self.assertEqual(cr.check_type("Internal", [2, 999, 51], report), "Beta")
            self.assertEqual((len(report.errors), len(report.warnings)), (0, 1))


class Reporting(unittest.TestCase):
    """The Report summary."""

    def test_finish_status(self) -> None:
        """Finishing returns 1 only when there is an error."""
        with mock.patch.object(cr, "say"):
            report = cr.Report()
            report.warn("careful")
            self.assertEqual(report.finish(), 0)
            report.fail("broken")
            self.assertEqual(report.finish(), 1)

    def test_problem_is_error_or_warning(self) -> None:
        """A problem is reported as an error or a warning, as asked."""
        with mock.patch.object(cr, "say"):
            report = cr.Report()
            report.problem("a", True)
            report.problem("b", False)
        self.assertEqual((report.errors, report.warnings), (["a"], ["b"]))


class Zips(unittest.TestCase):
    """Zip files are reproducible."""

    def test_same_inputs_same_zip(self) -> None:
        """Writing the same files twice, in any order, gives identical bytes."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "a.bin").write_bytes(b"alpha")
            (root / "b.bin").write_bytes(b"beta")
            cr.write_zip(root / "one.zip", [("b.bin", root / "b.bin"), ("a.bin", root / "a.bin")])
            cr.write_zip(root / "two.zip", [("a.bin", root / "a.bin"), ("b.bin", root / "b.bin")])
            self.assertEqual((root / "one.zip").read_bytes(), (root / "two.zip").read_bytes())


class Tagging(unittest.TestCase):
    """Deciding whether a source commit needs the release tag."""

    SHA = "a" * 40

    def state(self, ref: object) -> object:
        """Return tag_state with the tag lookup replaced by `ref` (an exception means no tag)."""
        def fake_json(path: str) -> object:
            if isinstance(ref, Exception):
                raise ref
            if "git/tags/" in path:
                return {"object": {"sha": self.SHA}}
            return {"object": ref}
        with mock.patch.object(cr, "gh_json", side_effect=fake_json), mock.patch.object(cr, "say"):
            return cr.tag_state("o/r", "1.2.3.4", self.SHA, cr.Report())

    def test_no_tag(self) -> None:
        """A missing tag is created."""
        self.assertEqual(self.state(cr.GhError("Not Found")), "create")

    def test_lightweight_tag_on_commit(self) -> None:
        """A tag already on the commit is left alone."""
        self.assertEqual(self.state({"type": "commit", "sha": self.SHA}), "exists")

    def test_annotated_tag_on_commit(self) -> None:
        """An annotated tag is followed to the commit it points at."""
        self.assertEqual(self.state({"type": "tag", "sha": "t" * 40}), "exists")

    def test_tag_on_other_commit(self) -> None:
        """A tag on a different commit is an error and is never moved."""
        self.assertIsNone(self.state({"type": "commit", "sha": "b" * 40}))


class VersionBump(unittest.TestCase):
    """Deciding what the version bump still has to do."""

    def plan(self, base_version: str, branch_exists: bool, pull_request: str = "",
             branch_version: str = "") -> cr.BumpPlan:
        """Return plan_bump for Meadow.OS with the GitHub calls replaced."""
        def fake_json(path: str) -> object:
            if "git/ref/heads/main" in path:
                return {"object": {"sha": "c" * 40}}
            if branch_exists:
                return {"object": {"sha": "d" * 40}}
            raise cr.GhError("Not Found")

        def fake_raw(_repo: str, _path: str, ref: str) -> str:
            return (branch_version if ref.startswith("meadow-") else base_version) + "\n"

        with mock.patch.object(cr, "gh_json", side_effect=fake_json), \
                mock.patch.object(cr, "gh_raw", side_effect=fake_raw), \
                mock.patch.object(cr, "gh", return_value=pull_request):
            return cr.plan_bump("WildernessLabs/Meadow.OS", "2.999.51", "2.999.53")

    def test_everything_needed(self) -> None:
        """At the released version with no branch: branch, commit and pull request."""
        plan = self.plan("2.999.51", False)
        self.assertIsNone(plan.skip)
        self.assertEqual((plan.need_branch, plan.need_commit, plan.need_pr), (True, True, True))
        self.assertEqual((plan.base, plan.branch), ("main", "meadow-2.999.53"))

    def test_already_bumped(self) -> None:
        """If the base branch already has the new version nothing is done."""
        self.assertIsNotNone(self.plan("2.999.53", False).skip)

    def test_unexpected_version(self) -> None:
        """A version.txt that is neither the old nor the new version is left alone."""
        self.assertIn("expected", self.plan("2.999.99", False).skip or "")

    def test_pull_request_exists(self) -> None:
        """An existing pull request means the bump is done."""
        self.assertIn("already exists", self.plan("2.999.51", True, "https://example/pull/1").skip or "")

    def test_resume_after_branch_created(self) -> None:
        """A branch with no commit yet only needs the commit and the pull request."""
        plan = self.plan("2.999.51", True, branch_version="2.999.51")
        self.assertEqual((plan.need_branch, plan.need_commit, plan.need_pr), (False, True, True))

    def test_resume_after_commit(self) -> None:
        """A branch that already has the new version only needs the pull request."""
        plan = self.plan("2.999.51", True, branch_version="2.999.53")
        self.assertEqual((plan.need_branch, plan.need_commit, plan.need_pr), (False, False, True))


if __name__ == "__main__":
    unittest.main()
