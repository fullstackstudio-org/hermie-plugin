#!/usr/bin/env python3
"""Put a build of Hermie's web client into `dashboard/app/`, or check the one there.

    python scripts/import_web_client.py --dist <app repo>/native/web/dist
    python scripts/import_web_client.py --check

The build comes from the app repository (`npm run client:build`), which writes
`build.json` beside the files: the repository and commit it was built from, the
client version, and the size and SHA-256 of every file. This script:

1. checks the build with the plugin's own rules (`web.verify_tree`, the check the
   plugin runs at load), so a build the plugin would refuse is never copied in;
2. holds it to the import limits, which are tighter than the load-time bounds and
   the same as the app's own bundle gate: at most 900 kB per file, 3 MB and 80
   files in all (`build.json` included, decimal units), text files ASCII only,
   no source maps, `build.json` in its canonical form;
3. copies exactly the listed files and `build.json` into a temporary folder beside
   `dashboard/app/`, checks that copy again, and swaps it in with renames, so a
   failed import leaves the old client where it was;
4. prints the version, commit, file count and size as `name=value` lines, for the
   pull request (and, with `--check`, for CI).

It makes no network request and does not commit. A bundle is merged only through a
pull request, where `web-bundle-verify` rebuilds the named commit and compares
every byte (see CONTRIBUTING.md).

`--check` applies the same rules and limits to the `dashboard/app/` already in the
tree and changes nothing. With no `dashboard/app/` at all it prints `present=false`
and succeeds: a checkout without a client is a valid plugin.

Exit status: 0 on success, 1 when the build is refused, 2 for a command line that
makes no sense.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import secrets
import shutil
import sys
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "dashboard"

MAX_FILE_BYTES = 900_000
MAX_TOTAL_BYTES = 3_000_000
MAX_FILES = 80
TEXT_SUFFIXES = (".js", ".mjs", ".css", ".html", ".json", ".svg")
STAGING_PREFIX = ".app-import-"
RETIRED_PREFIX = ".app-retired-"


def load_web():
    """`web.py` from this checkout, on its own: it imports nothing of the package."""
    spec = importlib.util.spec_from_file_location("hermie_web_verify", ROOT / "web.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical(manifest) -> str:
    """The form the app's build writes: sorted keys, two-space indent, one newline."""
    return json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def import_problems(web, folder: Path, result) -> List[str]:
    """What the import limits refuse in a build that already passed `verify_tree`."""
    problems: List[str] = []
    manifest = result.manifest
    names = sorted(manifest["files"]) + [web.MANIFEST_NAME]
    total = 0
    for name in names:
        path = folder.joinpath(*name.split("/"))
        data = path.read_bytes()
        total += len(data)
        if len(data) > MAX_FILE_BYTES:
            problems.append(f"{name}: {len(data)} bytes, over the {MAX_FILE_BYTES}-byte file limit")
        if name.endswith(".map"):
            problems.append(f"{name}: is a source map; maps do not belong in the bundle")
        if name.endswith(TEXT_SUFFIXES) and not data.isascii():
            problems.append(f"{name}: holds a non-ASCII byte; the build must escape non-ASCII text")
    if total > MAX_TOTAL_BYTES:
        problems.append(f"the bundle is {total} bytes, over the {MAX_TOTAL_BYTES}-byte limit")
    if len(names) > MAX_FILES:
        problems.append(f"the bundle has {len(names)} files, over the limit of {MAX_FILES}")
    raw = (folder / web.MANIFEST_NAME).read_bytes()
    if raw != canonical(manifest).encode("ascii"):
        problems.append(f"{web.MANIFEST_NAME}: is not in canonical form (sorted keys, two-space indent, one trailing newline)")
    return problems


def check(web, folder: Path) -> Optional[object]:
    """`verify_tree` plus the import limits; prints every problem, returns the result or None."""
    result = web.verify_tree(folder)
    if result.status == web.ABSENT:
        print(f"refused: {folder} does not exist", file=sys.stderr)
        return None
    if not result.ok:
        print(f"refused: {folder} does not match its {web.MANIFEST_NAME}:", file=sys.stderr)
        for problem in result.problems:
            print(f"  - {problem}", file=sys.stderr)
        more = result.problem_count - len(result.problems)
        if more > 0:
            print(f"  - and {more} more", file=sys.stderr)
        return None
    problems = import_problems(web, folder, result)
    if problems:
        print(f"refused: {folder} is over the import limits:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return None
    return result


def report(result) -> None:
    manifest = result.manifest
    print("present=true")
    print(f"version={manifest['version']}")
    print(f"commit={manifest['sourceCommit']}")
    print(f"repo={manifest['sourceRepo']}")
    print(f"files={result.files}")
    print(f"bytes={result.bytes}")


def copy_build(web, source: Path, staging: Path, result) -> None:
    """Exactly the listed files and `build.json`, byte for byte, nothing else."""
    staging.mkdir(mode=0o755)
    for name in sorted(result.manifest["files"]) + [web.MANIFEST_NAME]:
        target = staging.joinpath(*name.split("/"))
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        data = source.joinpath(*name.split("/")).read_bytes()
        with open(target, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(target, 0o644)


def install(staging: Path, app: Path) -> None:
    """Swap the checked copy in. The old client, if any, is removed last."""
    retired = None
    if os.path.lexists(app):
        retired = app.parent / (RETIRED_PREFIX + secrets.token_hex(4))
        os.rename(app, retired)
    try:
        os.rename(staging, app)
    except OSError:
        if retired is not None:
            os.rename(retired, app)
        raise
    if retired is not None:
        if retired.is_symlink() or not retired.is_dir():
            retired.unlink()
        else:
            shutil.rmtree(retired)


def run_import(web, dist: Path, dashboard: Path) -> int:
    # The folder may be reached through a link (a temporary directory on macOS
    # is); what is inside it may not.
    source = dist.resolve()
    result = check(web, source)
    if result is None:
        return 1

    staging = dashboard / (STAGING_PREFIX + secrets.token_hex(4))
    try:
        copy_build(web, source, staging, result)
        copied = check(web, staging)
        if copied is None:
            print("refused: the copy does not match the build; nothing was replaced", file=sys.stderr)
            return 1
        if copied.manifest != result.manifest:
            print("refused: build.json changed while it was copied; nothing was replaced", file=sys.stderr)
            return 1
        install(staging, dashboard / web.APP_DIR)
    finally:
        if os.path.lexists(staging):
            shutil.rmtree(staging)

    report(copied)
    print(f"imported into {dashboard / web.APP_DIR}; review and commit it yourself", file=sys.stderr)
    return 0


def run_check(web, dashboard: Path) -> int:
    app = dashboard / web.APP_DIR
    if not os.path.lexists(app):
        print("present=false")
        return 0
    result = check(web, app)
    if result is None:
        return 1
    report(result)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dist", type=Path, help="the client build to import (the app's native/web/dist)")
    mode.add_argument("--check", action="store_true", help="check dashboard/app/ in place and change nothing")
    parser.add_argument("--dashboard", type=Path, default=DASHBOARD, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    web = load_web()
    dashboard = args.dashboard.resolve()
    if not dashboard.is_dir():
        print(f"{dashboard} is not a folder", file=sys.stderr)
        return 2
    if args.check:
        return run_check(web, dashboard)
    if args.dist.resolve() == (dashboard / web.APP_DIR).resolve():
        print("--dist names dashboard/app itself; use --check to check it", file=sys.stderr)
        return 2
    return run_import(web, args.dist, dashboard)


if __name__ == "__main__":
    sys.exit(main())
