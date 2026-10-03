# Contributing

## Running the tests

```
python -m pytest --rootdir=tests tests
```

A few tests skip rather than fail when what they need is not there, so a clean
run on a bare checkout is not a full run. Install what CI installs and the skips
left are the ones that can only run elsewhere:

```
pip install -r .github/requirements-ci.txt   # pytest, FastAPI, httpx, cryptography, PyYAML at Hermes's locked versions
python -m pytest --rootdir=tests tests -q -rs
```

`-rs` lists every skip with its reason. With that file installed there are six,
for reasons of three kinds, and `ci.yml` fails if there is any other:

- `hermes is not importable here`, `needs Hermes itself`: these need a real Hermes
  (see "Validating against a real Hermes" below).
- `the app repository is not checked out next to this one`, and the one that needs
  Node as well: set `HERMIE_APP_REPO` or check the app out beside this repository.
- `the relay's source is not checked out here`: set `HERMIE_RELAY_REPO`.

On a bare checkout, with neither FastAPI nor `cryptography`, there are eleven.
That is expected too. A skip count above what is described here, once the
requirements are installed, is the shape worth investigating.

The versions in `.github/requirements-ci.txt` are the ones Hermes locks, because
the plugin runs inside Hermes and the tests should meet what the gateways meet.
FastAPI is a *Hermes runtime* dependency, not one of this plugin's:
`python_dependencies` in the manifest is empty and stays empty. The routes only
ever run inside a process that already has it.

`--rootdir=tests` is not optional. The repo root is the plugin package, because
that is how Hermes loads a plugin, so it has an `__init__.py`; without the flag
pytest walks up from `tests/`, finds it, and tries to import the root as a module
called `__init__`.

## The scanner gate

Hermes scans a plugin's whole tree when it is installed, and again after every
`hermes plugins update`. A `dangerous` verdict (any critical finding) blocks the
install and, after an update, **switches the plugin off** on that gateway. A
`caution` verdict (any high finding) asks for confirmation at install and prints
the report on every update. `scripts/guard_scan.py` runs that scan, with the two
functions the update path calls (`tools.plugin_guard.scan_plugin` and
`should_allow_plugin_install`), and `guard-scan` in CI fails on anything but
`safe`. It scans the checkout as an install would see it: every file except
`.git`, caches and virtual environments, so the tests, the docs, the workflows,
the scripts and the web client bundle in `dashboard/app/` are all in it.

It needs no Hermes install. The scanner is a few standard-library modules under
`tools/` in the Hermes repository, so the script fetches only that directory (a
shallow, sparse fetch of a few megabytes) at the commit pinned in
`.github/scanner-pins.json`, once for the fork the gateways run and once for
upstream. Each runs in its own interpreter, because both define a package called
`tools`. It needs Python 3.11 or newer and `git`.

Run it yourself:

```
python scripts/guard_scan.py                          # the pinned scanners, as CI does
python scripts/guard_scan.py --latest                 # their newest branches, as the daily run does
python scripts/guard_scan.py --scanner-root fork=/path/to/hermes-agent   # a checkout you already have
```

Read the report the way an operator would: `HIGH` and `CRITICAL` are what the
gate is about, `MEDIUM` and `LOW` are listed and do not fail it. The test
directory is scanned too, with findings stepped down one level, so a fixture that
holds a hostile string is a `MEDIUM` note, not a failure. Build a string like that
at run time when a test needs it rather than writing it out, and never put an
invisible or direction-changing character in a source file literally: write it as
an escape (`\u202e`), which says what it means and cannot be lost to an editor.

When the scan goes red:

1. Read which file and line, and which pattern. Fix the plugin if the finding is
   real, or reword it if it is prose that trips a pattern.
2. Do not move the pins to make a finding go away. A pin moves for a reason of its
   own, in a pull request of its own.

To move a pin, take the new commit from the fork's or upstream's `main` (`git ls-remote
<repo> refs/heads/main`), put it in `.github/scanner-pins.json`, and open a pull
request that changes nothing else. `guard-scan` runs against the new scanner on
that pull request, and its report is the review. When the daily run turns red
and the pinned one is green, a newer scanner has a stricter rule: fix the tree
first, then move the pin.

## Importing a web client build

`dashboard/app/` is generated: a build of the app repository's `native/web`,
copied in by a script, never edited by hand. To import one:

```
# in the app repository, at a commit that is on its main
git switch --detach <commit on main>
npm ci && npm run client:build              # writes native/web/dist/ and dist/build.json

# here, on a branch of its own
python scripts/import_web_client.py --dist <app repo>/native/web/dist
```

The script checks the build with the plugin's own load-time rules (`web.py`) and
the import limits (900 kB per file, 3 MB and 80 files in all, ASCII text, no
source maps, a canonical `build.json`), replaces `dashboard/app/` through a
temporary folder and a rename, and prints `version`, `commit`, `files` and
`bytes`. It makes no network request and commits nothing. Paste its output into
the pull request, bump the version (see "Releasing"), add a changelog entry
naming the client build, and fill in the bundle checklist in the pull request
template. `python scripts/import_web_client.py --check` checks the folder in
place and changes nothing.

Before you push, run the scanner over the tree with the bundle in it
(`python scripts/guard_scan.py`): the client's text (its English strings
included) is scanned like every other file, and a phrase the scanner reads as a
prompt injection turns the verdict to `caution`. Such a phrase is fixed in the
app repository and the build imported again; the bundle here is never edited.

What `web-bundle-verify` does in CI, you can do by hand: clone the app
repository, check out the commit `build.json` names, confirm
`git merge-base --is-ancestor <commit> origin/main`, run `npm ci && npm run
client:build` with the Node version in its `.nvmrc`, and
`diff -r native/web/dist <this repo>/dashboard/app`. No output is a pass.

## Validating against a real Hermes

The two checks that matter, both of which run the real discovery path:

```
hermes plugins validate /path/to/hermie-plugin   # the catalog admission gate
hermes plugins doctor   /path/to/hermie-plugin   # imports it and calls register()
```

The tests that skip without Hermes are worth running there by hand at least
once per change to the memory routes: one checks that `/api/plugins` is absent
from core's own public-path allowlist (which is what puts these routes behind
the auth gate at all), one checks that the entry delimiter this plugin believes
in still matches `MemoryStore`'s, and one that the two file names the raw route
reads are the two the store writes.

`doctor` catches the things unit tests cannot: a hook name that does not exist,
a callback without `**kwargs`, and drift between `provides_hooks` in the manifest
and what `register()` actually registers.

## House rules

- **Every hook callback takes `**kwargs`.** Hermes inspects a callback's
  signature and passes only the parameters it declares, so a narrow signature
  silently stops receiving fields that are added later.
- **Never block in a hook.** They sit on the agent's own path, and `pre_tool_call`
  fails *closed* — a slow callback blocks a tool. Build a decision, hand it to
  the queue, return.
- **Never write the `hermie-app` ui_meta key.** It belongs to the app, which
  holds a compare-and-swap revision for it. `uimeta.write_key` refuses it.
- **A capability is claimed only when it can be honoured on this gateway.** Not
  when the code supports it somewhere.
- **Reaching into Hermes internals goes through `Runtime`.** Modules take the
  runtime, not `ctx`, so the places this plugin depends on Hermes stay
  countable — and so the fake in `tests/test_plugin.py` stays small. If that
  fake has to grow, say why in the pull request.
- **State changes need a migration.** Bump `STATE_VERSION`, add the entry to
  `MIGRATIONS`, and add a test. A state file from an unknown future version is
  left alone, never overwritten.

## Commits

Conventional commits, present tense, describing the behaviour that changed
rather than the files that moved.

## Releasing

`main` deploys itself to both gateways within about fifteen minutes of a push,
so a release here is a version bump on code that is already live, not a
shipping step. Cutting one:

1. Bump the version in the three places it lives: `version` in `plugin.yaml`,
   `PLUGIN_VERSION` in `contract.py` and `version` in `dashboard/manifest.json`.
   They must always agree; a test checks it.
2. Give `CHANGELOG.md` a real section for the new version, dated the day of
   the release (`## 0.8.2 — 2026-10-01`), above the previous one. Move each
   `Unreleased` entry that is going out under it, grouped under `### Added`,
   `### Changed`, `### Fixed` as it already is.
3. Commit only those version fields plus the changelog, as its own
   commit: `chore(release): <version>`.
4. Tag that commit as an annotated tag: `git tag -a v<version> -m "Hermie
   plugin <version>"`.
5. Open the release commit as a pull request and merge it once `test` and
   `guard-scan` are green (`main` takes no direct pushes: see "Branch protection" in
   the README), then push the tag: `git push --tags`.
6. Run the checks before any of the above lands, not after:
   `.venv/bin/pytest --rootdir=tests tests`, `python scripts/guard_scan.py`,
   `hermes plugins validate .`, `hermes plugins doctor .`. The first two are what
   CI runs; the last two need a real Hermes.
7. Create the GitHub release from the tag: `gh release create v<version>
   --title "<version>" --notes "<notes>"`. Write the notes in plain
   sentences — what changed, the way you'd tell a colleague, not a list of
   commit subjects or file names. Three to six bullets is usually right.
   End with a link to the commit comparison for anyone who wants the detail:
   `https://github.com/fullstackstudio-org/hermie-plugin/compare/v<previous>...v<version>`
