#!/usr/bin/env python3
"""Add historical firmware releases to the Meadow.Releases repository.

Each version is a folder in the layout the Meadow CLI keeps its downloaded firmware in:

    work/History/1.0.0.0/Meadow.OS.bin, Meadow.OS.Update.bin, Meadow.OS.Runtime.bin,
                         bootloader.bin, partition-table.bin, MeadowComms.bin,
                         build-info.json, meadow_assemblies/

For each version the script builds the same assets create_release.py makes (Meadow.OS.zip,
Meadow.Network.zip, manifest.json, and the sources where they can be found) and creates a GitHub
Release called after the version, marked "legacy" in its manifest and never the latest release.

    ./import_history.py --dry-run          check everything and stage the assets, publish nothing
    ./import_history.py                    the same, then create the releases (asks first)

What a historical release cannot have is described in README.md.  Anything that is known only to
the release manager (a description, the ESP32 source commit) goes in an optional history.yaml.
Release notes in Markdown go in an optional file beside the folder, named after it (work/History/1.0.0.0.md).

Requires: Python 3.9+, PyYAML, and the GitHub CLI (gh) logged in with write access to this repository.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import create_release as cr

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip install pyyaml")

# The repository the historical OS was built from.  Its commit is recorded in build-info.json.
LEGACY_REPO = "WildernessLabs/Meadow"
DEFAULT_HISTORY_DIR = cr.SCRIPT_DIR / "work" / "History"
INFO_FILE_NAME = "history.yaml"
NOTES_SUFFIX = ".md"
BUILD_INFO_FILE = "build-info.json"
REQUIRED_FILES = cr.OS_FILES + cr.ESP32_FILES

# The minimum CLI version of an old release is not recorded anywhere; 0.0.0 means any CLI.
DEFAULT_MIN_CLI_VERSION = "0.0.0"
DEFAULT_TYPE = "Full"


@dataclass
class HistoryRelease:
    """One historical release folder, with what is known about it."""

    version: str
    folder: Path
    description: str
    notes: str
    kind: str
    min_cli_version: str
    os_commit: str
    esp32_commit: str
    build_date: str
    files: dict[str, Path] = field(default_factory=dict)
    bcl_files: list[Path] = field(default_factory=list)

    @property
    def notes_file(self) -> Path:
        """The optional Markdown release notes beside the version folder, such as 1.0.0.0.md."""
        return notes_file(self.folder)

    @property
    def bcl_dir(self) -> Path:
        """The folder of managed assemblies in the release."""
        return self.folder / cr.BCL_FOLDER


def read_info(path: Path) -> dict[str, dict[str, Any]]:
    """Read the optional history.yaml: facts about each version that the folders do not hold.

    The file maps a version to any of Description, Type (Beta or Full), MinCLIVersion, OS.Commit
    and ESP32.Commit.  All values are read as text.
    """
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as stream:
        data = yaml.load(stream, Loader=yaml.BaseLoader)  # nosec - BaseLoader builds only text
    releases = data.get("Releases") if isinstance(data, dict) else None
    if not isinstance(releases, dict):
        raise cr.ReleaseFileError(f"{path} has no 'Releases' section")
    return {str(version): values for version, values in releases.items() if isinstance(values, dict)}


def find_versions(history_dir: Path, wanted: Sequence[str]) -> list[Path]:
    """Return the version folders in the history folder, oldest first.

    A folder counts if its name is a dotted numeric version.  If `wanted` is given only those
    versions are returned; asking for one that is not there raises ValueError.
    """
    folders = []
    for folder in history_dir.iterdir():
        if folder.is_dir():
            try:
                cr.parse_version(folder.name)
            except ValueError:
                continue
            folders.append(folder)
    if wanted:
        missing = [version for version in wanted if not (history_dir / version).is_dir()]
        if missing:
            raise ValueError(f"no such version folder in {history_dir}: {', '.join(missing)}")
        folders = [folder for folder in folders if folder.name in wanted]
    return sorted(folders, key=lambda folder: cr.parse_version(folder.name))


def notes_file(folder: Path) -> Path:
    """Return where the release notes for a version folder are kept: <folder>.md beside it."""
    return folder.with_name(folder.name + NOTES_SUFFIX)


def unmatched_notes(history_dir: Path) -> list[Path]:
    """Return the notes files named after a version that has no folder (probably a misnamed file)."""
    unmatched = []
    for path in history_dir.glob("*" + NOTES_SUFFIX):
        try:
            cr.parse_version(path.stem)
        except ValueError:
            continue
        if not (history_dir / path.stem).is_dir():
            unmatched.append(path)
    return sorted(unmatched, key=lambda path: cr.parse_version(path.stem))


def read_build_info(folder: Path) -> tuple[str, str]:
    """Return the OS commit and build date recorded in a folder's build-info.json ("" for any not recorded)."""
    path = folder / BUILD_INFO_FILE
    if not path.exists():
        return "", ""
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        return str(info["git"]["meadow"][0]), str(info.get("build-date", ""))
    except (ValueError, KeyError, IndexError, TypeError):
        return "", ""


def load_release(folder: Path, overrides: dict[str, Any], report: cr.Report) -> HistoryRelease | None:
    """Read a version folder and check it holds the files the CLI needs.  Returns None, after reporting, if not."""
    version = folder.name
    os_commit, build_date = read_build_info(folder)
    os_section = overrides.get("OS")
    esp32_section = overrides.get("ESP32")
    release = HistoryRelease(
        version=version,
        folder=folder,
        description=str(overrides.get("Description", "")).strip() or f"Meadow {version}",
        notes="",
        kind=str(overrides.get("Type", "")).strip() or DEFAULT_TYPE,
        min_cli_version=str(overrides.get("MinCLIVersion", "")).strip() or DEFAULT_MIN_CLI_VERSION,
        os_commit=(str(os_section.get("Commit", "")).strip() if isinstance(os_section, dict) else "") or os_commit,
        esp32_commit=str(esp32_section.get("Commit", "")).strip() if isinstance(esp32_section, dict) else "",
        build_date=build_date,
    )
    if release.notes_file.is_file():
        release.notes = release.notes_file.read_text(encoding="utf-8").strip()
        report.ok(f"{version}: release notes from {release.notes_file.name}")
    if release.kind not in ("Beta", "Full"):
        report.fail(f"{version}: Type '{release.kind}' is not Beta or Full")
        return None
    for name in REQUIRED_FILES:
        if (folder / name).is_file():
            release.files[name] = folder / name
        else:
            report.fail(f"{version}: {name} is missing")
    # Hidden files (such as the .DS_Store macOS leaves in folders) are not part of a release.
    release.bcl_files = sorted(path for path in release.bcl_dir.rglob("*")
                               if path.is_file() and not path.name.startswith("."))
    if not any(path.suffix == ".dll" for path in release.bcl_files):
        report.fail(f"{version}: {cr.BCL_FOLDER} holds no .dll files")
    if len(release.files) != len(REQUIRED_FILES):
        return None
    report.ok(f"{version}: {len(release.files)} firmware files and {len(release.bcl_files)} assemblies")
    return release


def check_sources(release: HistoryRelease, report: cr.Report) -> None:
    """Report what is known about the sources of a release, and check the commits recorded exist."""
    if release.os_commit:
        try:
            cr.gh_json(f"repos/{LEGACY_REPO}/commits/{release.os_commit}")
            report.ok(f"{release.version}: OS commit {release.os_commit[:8]} exists in {LEGACY_REPO}")
        except cr.GhError:
            report.warn(f"{release.version}: OS commit {release.os_commit} is not in {LEGACY_REPO}; "
                        f"its sources will not be saved")
            release.os_commit = ""
    else:
        report.warn(f"{release.version}: no OS commit (no build-info.json); its sources will not be saved")
    if release.esp32_commit:
        try:
            cr.gh_json(f"repos/{cr.ESP32_REPO}/commits/{release.esp32_commit}")
            report.ok(f"{release.version}: ESP32 commit {release.esp32_commit[:8]} exists in {cr.ESP32_REPO}")
        except cr.GhError:
            report.warn(f"{release.version}: ESP32 commit {release.esp32_commit} is not in {cr.ESP32_REPO}")
            release.esp32_commit = ""
    else:
        report.warn(f"{release.version}: the ESP32 source commit is unknown; add it to {INFO_FILE_NAME} to save it")


def already_published(repo: str, tag: str) -> bool:
    """Return True if the Releases repository already has a release or a tag called `tag`."""
    for path in (f"repos/{repo}/releases/tags/{tag}", f"repos/{repo}/git/ref/tags/{tag}"):
        try:
            cr.gh_json(path)
            return True
        except cr.GhError:
            continue
    return False


def build_manifest(release: HistoryRelease, zips: Sequence[Path], extra: Sequence[Path]) -> dict[str, Any]:
    """Return manifest.json for a historical release.

    It has the same shape as a new release's manifest, with generation "legacy", and says what is
    unknown (the ESP32 commit, the runtime) as null.
    """
    return {
        "tag": release.version,
        "version": release.version,
        "generation": "legacy",
        "type": release.kind,
        "description": release.description,
        "minCLIVersion": release.min_cli_version,
        "imported": True,
        "buildDate": release.build_date,
        "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": {path.name: {"sha256": cr.sha256_of(path)} for path in [*zips, *extra]},
        "contents": {name: {"sha256": cr.sha256_of(path)} for name, path in release.files.items()},
        "sources": {
            "Meadow.OS": {"repository": f"https://github.com/{LEGACY_REPO}",
                          "commit": release.os_commit or None},
            "Meadow-ESP32": {"repository": f"https://github.com/{cr.ESP32_REPO}",
                             "commit": release.esp32_commit or None},
            "runtime": None,
        },
        "builds": None,
    }


def stage_release(release: HistoryRelease, work: Path, skip_sources: bool, report: cr.Report) -> cr.Staged:
    """Build the assets of a historical release in <work>/release."""
    out = work / "release"
    out.mkdir(parents=True, exist_ok=True)
    os_zip = out / "Meadow.OS.zip"
    net_zip = out / "Meadow.Network.zip"
    cr.write_zip(os_zip, [(name, release.files[name]) for name in cr.OS_FILES]
                 + [(f"{cr.BCL_FOLDER}/{path.relative_to(release.bcl_dir).as_posix()}", path)
                    for path in release.bcl_files])
    cr.write_zip(net_zip, [(name, release.files[name]) for name in cr.ESP32_FILES])
    zips = [os_zip, net_zip]
    for archive in zips:
        report.ok(f"{release.version}: {archive.name} ({archive.stat().st_size} bytes)")

    # build-info.json is kept as its own asset: it is the record of where the build came from.
    extra = []
    if (release.folder / BUILD_INFO_FILE).is_file():
        build_info = out / BUILD_INFO_FILE
        build_info.write_bytes((release.folder / BUILD_INFO_FILE).read_bytes())
        extra.append(build_info)

    archives: list[Path] = []
    if skip_sources:
        report.warn(f"{release.version}: source archives skipped (--skip-sources)")
    else:
        if release.os_commit:
            archives.append(cr.download_sources(LEGACY_REPO, release.os_commit, out, "Meadow", report))
        if release.esp32_commit:
            archives.append(cr.download_sources(cr.ESP32_REPO, release.esp32_commit, out, "Meadow-ESP32", report))
        for archive in archives:
            report.ok(f"{release.version}: {archive.name} ({archive.stat().st_size} bytes)")

    manifest_path = out / "manifest.json"
    manifest_path.write_text(json.dumps(build_manifest(release, zips, extra), indent=2) + "\n", encoding="utf-8")
    report.ok(f"{release.version}: manifest.json")

    notes = out / "notes.md"
    notes.write_text(release_notes(release), encoding="utf-8")
    return cr.Staged(out, [*zips, manifest_path, *extra, *archives], notes, release.description)


def release_notes(release: HistoryRelease) -> str:
    """Return the Markdown body of a historical release: the description, any notes, then where it came from."""
    parts = [part for part in (release.description, release.notes) if part]
    parts.append(f"Historical release {release.version}, imported from a saved copy of the firmware.")
    parts.append(f"- Built: {release.build_date or 'unknown'}\n"
                 f"- OS source: {LEGACY_REPO} {release.os_commit or 'unknown'}\n"
                 f"- ESP32 source: {release.esp32_commit or 'unknown'}")
    return "\n\n".join(parts) + "\n"


def publish_release(release: HistoryRelease, staged: cr.Staged, repo: str, target: str) -> None:
    """Create the GitHub Release for a historical version.  It is never marked as the latest release."""
    command = ["release", "create", release.version, *map(str, staged.assets), "-R", repo,
               "--title", staged.title, "--notes-file", str(staged.notes), "--target", target, "--latest=false"]
    if release.kind == "Beta":
        command.append("--prerelease")
    cr.say(cr.gh(*command).strip())


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Read the command line."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("history_dir", nargs="?", default=str(DEFAULT_HISTORY_DIR),
                        help="folder holding one sub-folder per version (default work/History)")
    parser.add_argument("--version", action="append", default=[], metavar="VERSION",
                        help="import only this version (may be repeated); default is every version found")
    parser.add_argument("--info", help=f"facts about the versions (default <history folder>/{INFO_FILE_NAME})")
    parser.add_argument("--dry-run", action="store_true",
                        help="run all checks and stage the assets, but publish nothing")
    parser.add_argument("--skip-existing", action="store_true",
                        help="skip a version that already has a release or tag instead of reporting an error")
    parser.add_argument("--skip-sources", action="store_true", help="do not download the source archives")
    parser.add_argument("--work-dir", help="where to stage; each run uses a new timestamped folder")
    parser.add_argument("--repo", help="the Releases repository (default: this checkout's origin)")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation before publishing")
    return parser.parse_args(argv)


def select_releases(folders: Sequence[Path], info: dict[str, dict[str, Any]], repo: str,
                    args: argparse.Namespace, report: cr.Report) -> list[HistoryRelease]:
    """Check each version folder and return the releases that are ready to stage."""
    releases: list[HistoryRelease] = []
    for folder in folders:
        cr.say()
        cr.say(f"Version {folder.name}")
        if args.skip_existing and already_published(repo, folder.name):
            report.ok(f"{folder.name}: already published, skipped")
            continue
        release = load_release(folder, info.get(folder.name, {}), report)
        if release is not None and cr.check_new_release(repo, folder.name, report):
            check_sources(release, report)
            releases.append(release)
    return releases


def publish_all(staged: Sequence[tuple[HistoryRelease, cr.Staged]], repo: str, target: str) -> int:
    """Create the GitHub Releases, oldest first.  Stops at the first failure."""
    for release, assets in staged:
        try:
            publish_release(release, assets, repo, target)
        except cr.GhError as error:
            cr.say(f"  ERROR: publishing {release.version} failed: {error}")
            cr.say("  Earlier versions are published.  Run again with --skip-existing to carry on.")
            return 1
    return 0


def conclude(staged: Sequence[tuple[HistoryRelease, cr.Staged]], repo: str, target: str,
             args: argparse.Namespace) -> int:
    """Stop after staging for --dry-run, otherwise ask for confirmation and publish."""
    cr.say()
    cr.say(f"{len(staged)} release(s) ready for {repo}, target commit {target[:8]}")
    if args.dry_run or not staged:
        cr.say("DRY RUN: nothing was published." if args.dry_run else "nothing to publish")
        return 0
    if not cr.confirm(f"Type 'import' to publish {len(staged)} release(s): ", "import", args.yes):
        cr.say("not confirmed, nothing published")
        return 1
    return publish_all(staged, repo, target)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the script and return its exit status: 0 for success, 1 if a check failed or nothing was confirmed."""
    args = parse_args(argv)
    report = cr.Report()
    history_dir = Path(args.history_dir)
    try:
        folders = find_versions(history_dir, args.version)
        info = read_info(Path(args.info) if args.info else history_dir / INFO_FILE_NAME)
    except (OSError, ValueError, cr.ReleaseFileError, yaml.YAMLError) as error:
        cr.say(f"cannot read the history: {error}")
        return 1
    if not folders:
        cr.say(f"no version folders found in {history_dir}")
        return 1

    repo = cr.releases_repo_name(args.repo)
    cr.say(f"{'DRY RUN: ' if args.dry_run else ''}importing {len(folders)} historical release(s) "
           f"from {history_dir} into {repo}")
    if not args.version:
        for path in unmatched_notes(history_dir):
            report.warn(f"{path.name} has no version folder called {path.stem}; it is not used")
    releases = select_releases(folders, info, repo, args, report)

    cr.say()
    cr.say("Releases repository")
    target = cr.check_head_pushed(args.dry_run, report)
    if report.errors:
        return report.finish()

    staged: list[tuple[HistoryRelease, cr.Staged]] = []
    for release in releases:
        cr.say()
        cr.say(f"Staging {release.version}")
        work = cr.new_work_dir(args.work_dir, f"history-{release.version}")
        cr.say(f"  work directory: {work}")
        staged.append((release, stage_release(release, work, args.skip_sources, report)))
    if report.finish():
        return 1

    return conclude(staged, repo, target, args)


if __name__ == "__main__":
    sys.exit(main())
