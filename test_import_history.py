"""Unit tests for import_history.py.

These need no network: they build small version folders in a temporary directory.  Run them from
this directory with

    python3 -m unittest -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import create_release as cr
import import_history as ih


def make_version(root: Path, version: str, build_info: object = None, complete: bool = True) -> Path:
    """Create a version folder in the CLI layout under `root` and return it."""
    folder = root / version
    (folder / cr.BCL_FOLDER).mkdir(parents=True)
    names = ih.REQUIRED_FILES if complete else ih.REQUIRED_FILES[:-1]
    for name in names:
        (folder / name).write_bytes(name.encode())
    (folder / cr.BCL_FOLDER / "mscorlib.dll").write_bytes(b"dll")
    (folder / cr.BCL_FOLDER / ".DS_Store").write_bytes(b"junk")
    if build_info is not None:
        (folder / ih.BUILD_INFO_FILE).write_text(json.dumps(build_info), encoding="utf-8")
    return folder


def quiet_report() -> cr.Report:
    """Return a Report whose output is not printed."""
    with mock.patch.object(cr, "say"):
        return cr.Report()


class FindVersions(unittest.TestCase):
    """Finding the version folders."""

    def test_numeric_order_and_ignored_names(self) -> None:
        """Versions sort by number, not text, and folders that are not versions are ignored."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("1.0.0.10", "1.0.0.2", "1.0.1.0", "notes", "latest"):
                (root / name).mkdir()
            (root / "1.0.0.5").write_text("a file, not a folder", encoding="utf-8")
            found = [path.name for path in ih.find_versions(root, [])]
        self.assertEqual(found, ["1.0.0.2", "1.0.0.10", "1.0.1.0"])

    def test_wanted_versions(self) -> None:
        """Asking for versions returns only those, and an unknown one is an error."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("1.0.0.0", "1.0.0.1"):
                (root / name).mkdir()
            self.assertEqual([path.name for path in ih.find_versions(root, ["1.0.0.1"])], ["1.0.0.1"])
            with self.assertRaises(ValueError):
                ih.find_versions(root, ["9.9.9.9"])


class BuildInfo(unittest.TestCase):
    """Reading build-info.json."""

    def read(self, text: str | None) -> tuple[str, str]:
        """Write `text` as build-info.json in a temporary folder (none if text is None) and read it."""
        with tempfile.TemporaryDirectory() as folder:
            if text is not None:
                (Path(folder) / ih.BUILD_INFO_FILE).write_text(text, encoding="utf-8")
            return ih.read_build_info(Path(folder))

    def test_valid(self) -> None:
        """The commit and build date are returned."""
        info = '{"git": {"meadow": ["abc123", ""]}, "build-date": "2023-05-15 10:01:27"}'
        self.assertEqual(self.read(info), ("abc123", "2023-05-15 10:01:27"))

    def test_missing_or_broken(self) -> None:
        """A missing, unreadable or oddly shaped file gives empty values, not an error."""
        for text in (None, "not json", '{"git": {}}', '{"git": {"meadow": []}}'):
            self.assertEqual(self.read(text), ("", ""), msg=str(text))


class LoadRelease(unittest.TestCase):
    """Checking a version folder."""

    def test_complete_folder(self) -> None:
        """A complete folder loads; hidden files are not counted as assemblies."""
        with tempfile.TemporaryDirectory() as folder:
            make_version(Path(folder), "1.0.0.0", {"git": {"meadow": ["abc", ""]}, "build-date": "2023-05-15"})
            with mock.patch.object(cr, "say"):
                release = ih.load_release(Path(folder) / "1.0.0.0", {}, cr.Report())
        self.assertIsNotNone(release)
        assert release is not None
        self.assertEqual(release.os_commit, "abc")
        self.assertEqual([path.name for path in release.bcl_files], ["mscorlib.dll"])
        self.assertEqual((release.kind, release.min_cli_version), ("Full", ih.DEFAULT_MIN_CLI_VERSION))

    def test_missing_file(self) -> None:
        """A missing firmware file is an error and the folder is rejected."""
        report = quiet_report()
        with tempfile.TemporaryDirectory() as folder:
            make_version(Path(folder), "1.0.0.0", complete=False)
            with mock.patch.object(cr, "say"):
                self.assertIsNone(ih.load_release(Path(folder) / "1.0.0.0", {}, report))
        self.assertEqual(len(report.errors), 1)

    def test_overrides(self) -> None:
        """Facts from history.yaml win over the defaults and over build-info.json."""
        overrides = {"Description": "Release 1", "Type": "Beta", "MinCLIVersion": "1.0.0",
                     "OS": {"Commit": "os1"}, "ESP32": {"Commit": "esp1"}}
        with tempfile.TemporaryDirectory() as folder:
            make_version(Path(folder), "1.0.0.0", {"git": {"meadow": ["abc", ""]}})
            with mock.patch.object(cr, "say"):
                release = ih.load_release(Path(folder) / "1.0.0.0", overrides, cr.Report())
        assert release is not None
        self.assertEqual((release.description, release.kind, release.min_cli_version),
                         ("Release 1", "Beta", "1.0.0"))
        self.assertEqual((release.os_commit, release.esp32_commit), ("os1", "esp1"))

    def test_bad_type(self) -> None:
        """A Type that is not Beta or Full is an error."""
        report = quiet_report()
        with tempfile.TemporaryDirectory() as folder:
            make_version(Path(folder), "1.0.0.0")
            with mock.patch.object(cr, "say"):
                self.assertIsNone(ih.load_release(Path(folder) / "1.0.0.0", {"Type": "Gold"}, report))
        self.assertEqual(len(report.errors), 1)


class Notes(unittest.TestCase):
    """The optional <version>.md release notes beside each version folder."""

    def load(self, root: Path, version: str = "1.0.0.0") -> ih.HistoryRelease:
        """Load a complete version folder under `root`."""
        with mock.patch.object(cr, "say"):
            release = ih.load_release(make_version(root, version), {"Description": "Release 1"}, cr.Report())
        assert release is not None
        return release

    def test_notes_file_is_read(self) -> None:
        """The Markdown beside the folder becomes the notes, and goes after the description in the body."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "1.0.0.0.md").write_text("## v1.0.0.0\n\n- First release\n\n", encoding="utf-8")
            release = self.load(root)
        self.assertEqual(release.notes, "## v1.0.0.0\n\n- First release")
        self.assertTrue(ih.release_notes(release).startswith("Release 1\n\n## v1.0.0.0\n\n- First release\n\n"
                                                             "Historical release 1.0.0.0"))

    def test_no_notes_file(self) -> None:
        """Without a notes file the body is the description and where the release came from."""
        with tempfile.TemporaryDirectory() as folder:
            release = self.load(Path(folder))
        self.assertEqual(release.notes, "")
        self.assertTrue(ih.release_notes(release).startswith("Release 1\n\nHistorical release 1.0.0.0"))

    def test_unmatched_notes(self) -> None:
        """A notes file named after a version with no folder is found; other Markdown files are not."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "1.0.0.0").mkdir()
            for name in ("1.0.0.0.md", "1.10.2.0.md", "1.2.0.0.md", "README.md"):
                (root / name).write_text("notes", encoding="utf-8")
            self.assertEqual([path.name for path in ih.unmatched_notes(root)], ["1.2.0.0.md", "1.10.2.0.md"])


class Info(unittest.TestCase):
    """Reading history.yaml."""

    def test_missing_file_is_empty(self) -> None:
        """No history.yaml means no extra facts."""
        self.assertEqual(ih.read_info(Path("/nonexistent/history.yaml")), {})

    def test_read(self) -> None:
        """Versions are read as text and mapped to their facts."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.yaml"
            path.write_text("Releases:\n  1.0.0.0:\n    Description: One\n    ESP32:\n      Commit: 12345678\n",
                            encoding="utf-8")
            info = ih.read_info(path)
        self.assertEqual(info["1.0.0.0"]["ESP32"]["Commit"], "12345678")

    def test_no_releases_section(self) -> None:
        """A file without a Releases section is reported."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.yaml"
            path.write_text("other: 1\n", encoding="utf-8")
            with self.assertRaises(cr.ReleaseFileError):
                ih.read_info(path)


class Manifest(unittest.TestCase):
    """The manifest of a historical release."""

    def test_unknowns_are_null(self) -> None:
        """What is not known is null, and the release is marked legacy and imported."""
        with tempfile.TemporaryDirectory() as folder:
            make_version(Path(folder), "1.0.0.0", {"git": {"meadow": ["abc", ""]}, "build-date": "2023-05-15"})
            with mock.patch.object(cr, "say"):
                release = ih.load_release(Path(folder) / "1.0.0.0", {}, cr.Report())
            assert release is not None
            archive = Path(folder) / "Meadow.OS.zip"
            archive.write_bytes(b"zip")
            manifest = ih.build_manifest(release, [archive], [])
        self.assertEqual((manifest["generation"], manifest["imported"], manifest["tag"]), ("legacy", True, "1.0.0.0"))
        self.assertEqual(manifest["sources"]["Meadow.OS"]["commit"], "abc")
        self.assertIsNone(manifest["sources"]["Meadow-ESP32"]["commit"])
        self.assertIsNone(manifest["sources"]["runtime"])
        self.assertEqual(set(manifest["contents"]), set(ih.REQUIRED_FILES))
        self.assertIn("Meadow.OS.zip", manifest["files"])


if __name__ == "__main__":
    unittest.main()
