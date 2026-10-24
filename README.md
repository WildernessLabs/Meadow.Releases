# Meadow.Releases

Releases of the Meadow firmware.  A release pulls together the binaries built from two
repositories:

- [Meadow.OS](https://github.com/WildernessLabs/Meadow.OS): the OS, the Mono runtime and the
  managed assemblies (`Meadow.OS.bin`, `Meadow.OS.Update.bin`, `Meadow.OS.Runtime.bin`,
  `meadow_assemblies/`).
- [Meadow-ESP32](https://github.com/WildernessLabs/Meadow-ESP32): the ESP32 firmware
  (`bootloader.bin`, `partition-table.bin`, `MeadowComms.bin`).

The releases are published as GitHub Releases in this repository, in the layout the Meadow CLI
(`meadow firmware download`) expects.

A release is made by running `create_release.py` from a laptop, by a release manager.  The script
does the work; the release manager decides which commits go in.

## What you need

- Python 3.9 or later and PyYAML (`pip install pyyaml`).
- The [GitHub CLI](https://cli.github.com/) (`gh`), logged in (`gh auth login`) with write access to
  this repository, and to Meadow.OS and Meadow-ESP32 (the script tags commits and opens pull requests
  there).
- A clone of this repository.  The script does not need clones of the other two.

## The release file

`release.yaml` says what to release.  Edit it, commit it and push it before a real release:

```yaml
Release:
    Tag: 2.999.51
    Version: 2.999.51
    Description: Beta 1, NuttX 12, Managed Code
    Type: Beta
    OS:
        Source: Rebase
        Commit: <full Meadow.OS commit hash>
    ESP32:
        Commit: <full Meadow-ESP32 commit hash>
```

| Field | Meaning |
|-------|---------|
| `Version` | `major.minor.y`, for example `2.999.51`.  The script adds the build number (see below). You may write all four parts, in which case the script checks them. |
| `Tag` | Optional.  Leave it out, or make it the same as `Version`, and the tag is the full version, for example `2.999.51.3`. |
| `Description` | Optional text for the release title and notes. |
| `Type` | Optional, `Beta` or `Full`.  It is worked out from `y`, and the script stops if it disagrees. |
| `OS.Source` | `Rebase` or `Meadow.OS` (the Meadow.OS repository).  `Legacy` is not supported yet. |
| `OS.Commit` | The Meadow.OS commit to release. |
| `ESP32.Commit` | The Meadow-ESP32 commit to release. |

Use full 40-character hashes.  The script resolves shorter ones, but a short hash can be ambiguous.

### Version numbers

A version is `major.minor.y.build`, for example `3.0.1.412`.

- `major.minor.y` comes from `version.txt` in the repository.  Meadow.OS and Meadow-ESP32 must have
  the same `major.minor.y` at the commits you pick.
- `build` is worked out from the Meadow.OS commit exactly as `build.sh` works it out: the number of
  commits since `version.txt` last changed.  You never type it.  The script also checks that the
  binary was stamped with the same number.
- If `y` is odd the release is a **Beta** and is published as a GitHub pre-release.  If `y` is even
  (or 0) it is a **Full** release.
- After a release the version moves on: odd `y` goes up by 2, even `y` by 1, so the next build is a
  beta.  The script does this for you (see "The version bump").

## Choosing the commits

The commit must be one that GitHub Actions built when it was merged.  The script finds the build by
the commit hash and downloads its binaries, so a commit with no successful build cannot be released.

- Meadow.OS builds on pushes to `main` (and `MergeManagedCode`): the workflow `Build OS and RT`.
- Meadow-ESP32 builds on pushes to `develop`: the workflow `Build`.

Pull request builds do not count, because they build the merge of the pull request, not the commit.
Build artifacts expire after 90 days, so release soon after the merge.

### The last 5 merge commits

A repository that merges with merge commits shows them with `--merges`.  Fetch first so you see
what is on GitHub:

```bash
git -C <Meadow.OS clone> fetch origin
git -C <Meadow.OS clone> log origin/main --merges -5 --format='%H %ad %s' --date=short

git -C <Meadow-ESP32 clone> fetch origin
git -C <Meadow-ESP32 clone> log origin/develop --merges -5 --format='%H %ad %s' --date=short
```

### The last 5 squash (or any) commits

A squash merge leaves one ordinary commit, which `--merges` does not list.  To see everything that
landed on the branch, whichever way it was merged, follow the first parent only:

```bash
git -C <Meadow.OS clone> log origin/main --first-parent -5 --format='%H %ad %s' --date=short

git -C <Meadow-ESP32 clone> log origin/develop --first-parent -5 --format='%H %ad %s' --date=short
```

Both commands print the full hash (`%H`), the date and the subject.

Without a clone, GitHub can list the last 5 merge commits (commits with two parents).  It cannot
separate squash commits from the commits inside a merged pull request, so for squash merges use a
clone and `--first-parent`:

```bash
gh api 'repos/WildernessLabs/Meadow.OS/commits?sha=main&per_page=50' \
    --jq '[.[] | select((.parents | length) > 1)][:5][] | "\(.sha) \(.commit.message | split("\n")[0])"'
gh api 'repos/WildernessLabs/Meadow-ESP32/commits?sha=develop&per_page=50' \
    --jq '[.[] | select((.parents | length) > 1)][:5][] | "\(.sha) \(.commit.message | split("\n")[0])"'
```

### Which commits were built

Pick a commit from the lists above that also appears here with a `success` result:

```bash
gh run list -R WildernessLabs/Meadow.OS --workflow "Build OS and RT" --event push --branch main -L 10 \
    --json headSha,conclusion,createdAt --jq '.[] | "\(.headSha) \(.conclusion) \(.createdAt)"'

gh run list -R WildernessLabs/Meadow-ESP32 --workflow Build --event push --branch develop -L 10 \
    --json headSha,conclusion,createdAt --jq '.[] | "\(.headSha) \(.conclusion) \(.createdAt)"'
```

## Making a release

1. Choose the commits (above) and put them in `release.yaml`, with the `Version` (`major.minor.y`)
   and a `Description`.
2. Commit and push `release.yaml`.  The tag points at the commit holding it.
3. Do a dry run.  It runs every check and builds the release assets, but publishes nothing and
   changes nothing on GitHub:

   ```bash
   ./create_release.py --dry-run
   ```

   Read the output.  Every line is `ok`, `WARN` or `ERROR`; an error stops the release.  The staged
   assets are left in `work/<tag>/<timestamp>/release/` so you can inspect them.  Use
   `--skip-sources` to skip the large source archives while testing.
4. When the dry run is clean, make the release:

   ```bash
   ./create_release.py
   ```

   It repeats all the checks, prints a summary and asks you to type the tag to confirm.  Then it
   creates the GitHub Release (a beta is a pre-release), tags the source commits, and does the version
   bump.

Each run works in a new timestamped folder under `work/`, which git ignores.  Delete old ones when
you no longer need them.

### What the script checks

- The release file is complete and the version is valid.
- `Type` (if given) matches `y`.
- Both commits exist, and the Meadow.OS and Meadow-ESP32 versions match `release.yaml`.
- The build number derived from the Meadow.OS commit matches any build number you typed.
- The Mono runtime pinned by `MONO_RUNTIME_COMMIT` at the OS commit exists, with the right tree.
- Your login can write to this repository, and the tag and release do not already exist.
- `release.yaml` is committed and pushed (a warning in a dry run, an error in a real one).
- You can write to Meadow.OS and Meadow-ESP32, and neither has the release tag on a different commit.
- A successful push build exists for each commit, and its artifacts have not expired.
- The binaries came from the commits: the OS `build-info.json` and the build stamp in
  `Meadow.OS.Update.bin`, the build hash in `MeadowComms.bin`, and the ESP32 `.md5` files.

### What a release contains

| Asset | Contents |
|-------|----------|
| `Meadow.OS.zip` | `Meadow.OS.bin`, `Meadow.OS.Update.bin`, `Meadow.OS.Runtime.bin`, `meadow_assemblies/` |
| `Meadow.Network.zip` | `bootloader.bin`, `partition-table.bin`, `MeadowComms.bin` and their `.md5` files |
| `Meadow.Debug.zip` | ELF and map files for the OS, bootloader and ESP32 |
| `manifest.json` | Generated: version, type, the SHA-256 of every file, and the full source commits |
| `release.yaml` | The release file that was used |
| `sources-*.tar.gz` | The Meadow.OS and Meadow-ESP32 sources at the released commits |

## Tagging the source commits

After the release is published the script tags the released commit in each source repository with
the release tag, for example `2.999.51.3`, so a release can be found from the source.  The tag is an
annotated tag whose message links to the release.  Tags are created in Meadow.OS and Meadow-ESP32,
not in Meadow-ESP32's other branches or in the runtime.

- A tag that already exists on the same commit is left alone.
- A tag that exists on a different commit is an error: tags are never moved.  The dry run reports
  it before anything is published.
- Tagging happens before the version bump.  The tags are on branch commits, so they do not start the
  build workflows, which run on branch pushes only.  (A commit that is tagged is reported with its
  tag instead of its branch name in the build info of any later build of it.)

If tagging fails after the release was published, the release stands.  Fix the cause and repeat just
the tagging:

```bash
./create_release.py --tag-only --dry-run      # see what it would do
./create_release.py --tag-only                # do it (asks you to type the tag)
```

## The version bump

After the release the script moves `version.txt` on in both source repositories, so the next build
is a beta:

| Repository | Base branch |
|------------|-------------|
| Meadow.OS | `main` |
| Meadow-ESP32 | `develop` |
| Meadow (Legacy, not used yet) | `main` |

For each one it creates a branch `meadow-<new version>` from the base branch, commits the new
`version.txt`, and opens a pull request.  A repository is skipped, with a message, if its base
branch is already at the new version, its `version.txt` is not what was expected, or the pull
request already exists.  Review and merge the pull requests as normal.

The bump is safe to repeat.  If it fails after the release was published, the release stands.  Fix
the cause and run just the bump again:

```bash
./create_release.py --bump-only --dry-run     # see what it would do
./create_release.py --bump-only               # do it (asks you to type 'bump')
```

## If something goes wrong

| Message | Cause and fix |
|---------|---------------|
| `no successful 'Build ...' push run for ...` | The commit was never built on merge.  Choose a commit from the "Which commits were built" lists. |
| `artifact ... has expired` | The build is more than 90 days old.  Rebuild the commit, or release a newer one. |
| `release.yaml says version ... but the derived version is ...` | You typed a build number that does not match.  Leave it out (`major.minor.y`). |
| `version.txt is ..., but release.yaml says ...` | The commit is on a different version line from the one you asked for. |
| `Type ... does not match version` | An odd `y` is a Beta and an even `y` is a Full release. |
| `... already has a tag ... on a different commit` | A source repository already has the release tag on another commit.  Tags are not moved; check the version and the commits. |
| `a tag named ... already exists` | That version was already released.  Releases are never replaced; choose a new commit and version. |
| `release.yaml has uncommitted changes` | Commit and push it before a real release. |
| `Meadow.OS.Update.bin is not stamped with build ...` | The CI build counted a different build number.  Do not release that commit. |

## Not done yet

- `OS.Source: Legacy` is not supported, and old releases have not been imported from S3.
- The Meadow CLI still reads the old S3 location.  It must be changed to download from these
  releases and to check the SHA-256 values in `manifest.json`.
- Tag and release creation is not yet restricted to release managers on GitHub.  This repository
  needs a tag ruleset limiting who can create, move or delete release tags, review (CODEOWNERS) of
  `release.yaml` and the script, and GitHub's immutable releases setting.
