"""The web client this plugin carries, and whether it is what it says it is.

`dashboard/app/` holds a build of Hermie's browser client, made in the app
repository, together with that build's `build.json`: the repository and commit
it was built from, the client version, and the size and SHA-256 of every file.
The dashboard's own static route serves the folder at
`/dashboard-plugins/hermie/app/<file>`. This module adds no route and no
listener for it, and serves nothing itself.

What it does is check the folder once, at load, and tell the app about the
client only when the check passes:

- `build.json` parses and has the `v: 1` shape;
- every file it lists is there, with exactly the listed size and hash;
- nothing else is there: no unlisted file, no symbolic link anywhere, no file
  outside `app/`, no extension the dashboard's static route would not serve.

Passing adds the capability `web.client` and the advert block `web` (where the
client lives, its version, commit, file count and size). Failing adds neither
and logs one warning that says how many files differ. Neither outcome changes
what the dashboard serves: the files stay fetchable through its route whatever
this module decides. Withholding the advert is a courtesy to the app (it does
not offer a client that does not match its manifest), not an access control.

The check is bounded so a damaged or hostile tree cannot make a gateway's start
slow: it gives up after `MAX_FILES` files or `MAX_BYTES` bytes, and it reads a
file's bytes only after its size already matched. It never runs on a hook path.

This file imports nothing from the plugin package at module level, so
`scripts/import_web_client.py` can load it on its own and apply the very same
rules to a build before it is copied in.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

# Where the client sits inside the plugin's `dashboard/` folder, and the URL the
# dashboard serves its entry document at. The dashboard names a plugin's static
# files after the `name` in `dashboard/manifest.json`, which is `hermie`.
APP_DIR = "app"
ENTRY = "index.html"
ENTRY_PATH = "/dashboard-plugins/hermie/app/index.html"
MANIFEST_NAME = "build.json"
# This plugin's own `dashboard/` folder, the one the dashboard serves from.
DASHBOARD_DIR = Path(__file__).resolve().parent / "dashboard"
MANIFEST_FORMAT = 1
CLIENT_NAME = "hermie-web-client"

# The bounds. The client is about half a megabyte in a handful of files and the
# import script holds it to 80 files and 3 MB, so these are far above anything
# legitimate and only stop a tree that is not a client build at all.
MAX_FILES = 200
MAX_BYTES = 8_000_000
MAX_DEPTH = 8
MAX_MANIFEST_BYTES = 256_000
# build.json is three levels deep (the object, `files`, one entry). Anything
# deeper is refused before it is parsed: the parser recurses, and a file of
# nested brackets would otherwise end in a RecursionError rather than an answer.
MAX_MANIFEST_DEPTH = 4

# The extensions `serve_plugin_asset` in Hermes' dashboard (`hermes_cli/
# web_routers/dashboard_ui.py`) answers for. A file with any other extension
# would never be served, so it has no business in the folder.
ALLOWED_SUFFIXES = frozenset(
    {
        ".js",
        ".mjs",
        ".css",
        ".json",
        ".html",
        ".svg",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".woff2",
        ".woff",
        ".ttf",
        ".otf",
        ".map",
    }
)

# A listed name: relative, forward slashes, ASCII, and no segment that starts
# with a dot (which rules out `.`, `..` and hidden files in one go).
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*(?:/[A-Za-z0-9_][A-Za-z0-9._-]*)*")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
# A JSON string, so the brackets inside one are not counted as nesting.
_JSON_STRING = re.compile(rb'"(?:[^"\\]|\\.)*"')
_JSON_BRACKET = re.compile(rb"[\[\]{}]")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]{1,40})?")

# How many problems a result keeps word for word. The count is always exact.
_PROBLEMS_KEPT = 10

INTACT = "intact"
ABSENT = "absent"
INVALID = "invalid"


class Result:
    """What `verify` found.

    `status` is `intact`, `absent` (there is no `app/` at all) or `invalid`.
    `problems` holds the first few reasons word for word; `differing` counts the
    files that are changed, missing or unlisted, and is what the warning names.
    `manifest` is the parsed `build.json` when it could be read.
    """

    def __init__(
        self,
        status: str,
        problems: Tuple[str, ...] = (),
        differing: int = 0,
        manifest: Optional[Dict[str, Any]] = None,
        problem_count: Optional[int] = None,
    ) -> None:
        self.status = status
        self.problems = problems
        self.differing = differing
        self.manifest = manifest
        self.problem_count = len(problems) if problem_count is None else problem_count

    @property
    def ok(self) -> bool:
        return self.status == INTACT

    @property
    def files(self) -> int:
        """How many files `build.json` lists (it does not list itself)."""
        return len(self.manifest["files"]) if self.ok and self.manifest else 0

    @property
    def bytes(self) -> int:
        return int(self.manifest["totalBytes"]) if self.ok and self.manifest else 0

    def advert_block(self) -> Optional[Dict[str, Any]]:
        """The advert's `web` member, or None when the client is not intact."""
        if not self.ok or not self.manifest:
            return None
        return {
            "path": ENTRY_PATH,
            "version": self.manifest["version"],
            "commit": self.manifest["sourceCommit"][:12],
            "files": self.files,
            "bytes": self.bytes,
        }

    def summary(self) -> str:
        """One line for the log: what is wrong, and how much of it."""
        if self.ok:
            return f"{self.files} files, {self.bytes} bytes, commit {self.manifest['sourceCommit'][:12]}"
        if self.status == ABSENT:
            return f"there is no {APP_DIR}/ folder"
        shown = "; ".join(self.problems)
        more = self.problem_count - len(self.problems)
        if more > 0:
            shown += f"; and {more} more"
        if self.differing:
            files = "file differs" if self.differing == 1 else "files differ"
            return f"{self.differing} {files} from {MANIFEST_NAME} ({shown})"
        return shown

    def __repr__(self) -> str:
        return f"Result({self.status!r}, differing={self.differing}, problems={self.problem_count})"


class _Problems:
    """Collects reasons, keeping the first few and counting them all."""

    def __init__(self) -> None:
        self.kept: List[str] = []
        self.count = 0
        self.differing = 0

    def add(self, text: str, *, differs: bool = False) -> None:
        self.count += 1
        if differs:
            self.differing += 1
        if len(self.kept) < _PROBLEMS_KEPT:
            self.kept.append(text)

    def result(self, manifest: Optional[Dict[str, Any]] = None) -> Result:
        if not self.count:
            return Result(INTACT, manifest=manifest)
        return Result(INVALID, tuple(self.kept), self.differing, manifest, self.count)


def _suffix(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    return "." + base.rsplit(".", 1)[-1].lower() if "." in base else ""


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _nesting(raw: bytes) -> int:
    """How deeply `raw` nests arrays and objects, without parsing it."""
    depth = deepest = 0
    for bracket in _JSON_BRACKET.findall(_JSON_STRING.sub(b"", raw)):
        if bracket in (b"[", b"{"):
            depth += 1
            deepest = max(deepest, depth)
        else:
            depth -= 1
    return deepest


def _read_manifest(app: Path, problems: _Problems) -> Optional[Dict[str, Any]]:
    """`build.json`, parsed and checked for shape, or None after a problem."""
    path = app / MANIFEST_NAME
    try:
        info = path.lstat()
    except FileNotFoundError:
        problems.add(f"{MANIFEST_NAME} is missing")
        return None
    except OSError as exc:
        problems.add(f"{MANIFEST_NAME} cannot be read ({exc.strerror or exc})")
        return None
    if stat.S_ISLNK(info.st_mode):
        problems.add(f"{MANIFEST_NAME} is a symbolic link")
        return None
    if not stat.S_ISREG(info.st_mode):
        problems.add(f"{MANIFEST_NAME} is not a regular file")
        return None
    if info.st_size > MAX_MANIFEST_BYTES:
        problems.add(f"{MANIFEST_NAME} is {info.st_size} bytes, over the {MAX_MANIFEST_BYTES}-byte limit")
        return None
    try:
        raw = _read_exactly(path, info.st_size)
        if raw is None:
            problems.add(f"{MANIFEST_NAME} changed while it was read")
            return None
        if _nesting(raw) > MAX_MANIFEST_DEPTH:
            problems.add(f"{MANIFEST_NAME} nests deeper than {MAX_MANIFEST_DEPTH} levels")
            return None
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        problems.add(f"{MANIFEST_NAME} is not valid JSON ({exc.__class__.__name__})")
        return None

    if not isinstance(manifest, dict):
        problems.add(f"{MANIFEST_NAME} is not a JSON object")
        return None
    version = manifest.get("v")
    if not _is_count(version) or version != MANIFEST_FORMAT:
        problems.add(f'{MANIFEST_NAME}: "v" is {version!r}, this plugin reads {MANIFEST_FORMAT}')
        return None

    before = problems.count
    if manifest.get("name") != CLIENT_NAME:
        problems.add(f'{MANIFEST_NAME}: "name" must be "{CLIENT_NAME}"')
    if not isinstance(manifest.get("version"), str) or not _VERSION.fullmatch(manifest["version"]):
        problems.add(f'{MANIFEST_NAME}: "version" must be a semantic version')
    if not isinstance(manifest.get("sourceRepo"), str) or not _REPO.fullmatch(manifest["sourceRepo"]):
        problems.add(f'{MANIFEST_NAME}: "sourceRepo" must be "<owner>/<name>"')
    if not isinstance(manifest.get("sourceCommit"), str) or not _COMMIT.fullmatch(manifest["sourceCommit"]):
        problems.add(f'{MANIFEST_NAME}: "sourceCommit" must be a 40-character lower-case commit hash')
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        problems.add(f'{MANIFEST_NAME}: "files" must be a non-empty object')
        return None
    if len(files) > MAX_FILES:
        problems.add(f"{MANIFEST_NAME} lists {len(files)} files, over the limit of {MAX_FILES}")
        return None

    total = 0
    for name, entry in files.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name == MANIFEST_NAME:
            problems.add(f"{MANIFEST_NAME} lists {name!r}, which is not a plain path inside {APP_DIR}/")
            continue
        if _suffix(name) not in ALLOWED_SUFFIXES:
            problems.add(f"{name}: the dashboard does not serve this extension")
        if (
            not isinstance(entry, dict)
            or set(entry) != {"sha256", "bytes"}
            or not isinstance(entry["sha256"], str)
            or not _SHA256.fullmatch(entry["sha256"])
            or not _is_count(entry["bytes"])
        ):
            problems.add(f'{MANIFEST_NAME}: {name} must hold exactly a "sha256" and a "bytes"')
            continue
        total += entry["bytes"]
    if ENTRY not in files:
        problems.add(f"{MANIFEST_NAME} does not list {ENTRY}")
    if not _is_count(manifest.get("totalBytes")) or manifest["totalBytes"] != total:
        problems.add(f'{MANIFEST_NAME}: "totalBytes" is not the sum of the listed sizes ({total})')
    if total > MAX_BYTES:
        problems.add(f"{MANIFEST_NAME} lists {total} bytes, over the {MAX_BYTES}-byte limit")
    return manifest if problems.count == before else None


def _walk(app: Path, problems: _Problems) -> Optional[Dict[str, int]]:
    """Every regular file under `app`, as `{relative name: size}`.

    Never follows a symbolic link; a link anywhere is a problem in itself, and
    so is anything that is neither a file nor a folder. Stops, with a problem
    and None, once it has seen more than `MAX_FILES` entries or gone deeper than
    `MAX_DEPTH`, so a huge tree costs at most that much listing.
    """
    found: Dict[str, int] = {}
    seen = 0
    stack: List[Tuple[Path, str, int]] = [(app, "", 0)]
    while stack:
        folder, prefix, depth = stack.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    seen += 1
                    if seen > MAX_FILES + 1:  # + build.json
                        problems.add(f"{APP_DIR}/ holds more than {MAX_FILES} files; not looked at further")
                        return None
                    name = prefix + entry.name
                    if entry.is_symlink():
                        problems.add(f"{name}: is a symbolic link", differs=True)
                    elif entry.is_dir(follow_symlinks=False):
                        if depth + 1 > MAX_DEPTH:
                            problems.add(f"{APP_DIR}/ is nested deeper than {MAX_DEPTH} folders; not looked at further")
                            return None
                        stack.append((Path(entry.path), name + "/", depth + 1))
                    elif entry.is_file(follow_symlinks=False):
                        found[name] = entry.stat(follow_symlinks=False).st_size
                    else:
                        problems.add(f"{name}: is not a regular file", differs=True)
        except OSError as exc:
            problems.add(f"{prefix or APP_DIR + '/'}: cannot be listed ({exc.strerror or exc})")
            return None
    return found


def _read_exactly(path: Path, size: int) -> Optional[bytes]:
    """`size` bytes of `path`, or None when it holds more or fewer.

    Opened without following a symbolic link where the platform can refuse one,
    and checked to be a regular file on the open descriptor, so a file swapped
    for a link between the listing and the read is not followed out of `app/`.
    Opened non-blocking as well: opening a FIFO for reading otherwise waits for
    a writer, and that wait would be the gateway's start. A regular file reads
    the same either way, and a FIFO is refused by the check that follows.
    """
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks: List[bytes] = []
        remaining = size + 1  # one byte more tells a grown file from an exact one
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    return data if len(data) == size else None


def verify_tree(app: Union[str, Path]) -> Result:
    """Check one client build folder (`dist/`, or `dashboard/app/`) in place.

    `app` itself must be a real folder, not a link to one. The caller that
    wants to accept a linked folder (a build directory on a path through a
    symbolic link) resolves it first.
    """
    app = Path(app)
    try:
        info = app.lstat()
    except FileNotFoundError:
        return Result(ABSENT)
    except OSError as exc:
        return Result(INVALID, (f"{APP_DIR}/ cannot be read ({exc.strerror or exc})",))
    if stat.S_ISLNK(info.st_mode):
        return Result(INVALID, (f"{APP_DIR}/ is a symbolic link",))
    if not stat.S_ISDIR(info.st_mode):
        return Result(INVALID, (f"{APP_DIR}/ is not a folder",))

    problems = _Problems()
    manifest = _read_manifest(app, problems)
    present = _walk(app, problems)
    if manifest is None or present is None:
        return problems.result()

    listed: Dict[str, Dict[str, Any]] = manifest["files"]
    present.pop(MANIFEST_NAME, None)

    for name in sorted(set(present) - set(listed)):
        problems.add(f"{name}: is not listed in {MANIFEST_NAME}", differs=True)
        if _suffix(name) not in ALLOWED_SUFFIXES:
            problems.add(f"{name}: the dashboard does not serve this extension")

    root = os.path.realpath(app)
    for name in sorted(listed):
        entry = listed[name]
        if name not in present:
            problems.add(f"{name}: is listed in {MANIFEST_NAME} but missing", differs=True)
            continue
        if present[name] != entry["bytes"]:
            problems.add(f"{name}: {present[name]} bytes, {MANIFEST_NAME} says {entry['bytes']}", differs=True)
            continue
        path = app.joinpath(*name.split("/"))
        # The listing never descended through a link, so this holds already;
        # checked again because it is the one property that matters most.
        if os.path.commonpath([root, os.path.realpath(path)]) != root:
            problems.add(f"{name}: lies outside {APP_DIR}/", differs=True)
            continue
        try:
            data = _read_exactly(path, entry["bytes"])
        except OSError as exc:
            problems.add(f"{name}: cannot be read ({exc.strerror or exc})", differs=True)
            continue
        if data is None:
            problems.add(f"{name}: changed while it was read, or is not a regular file", differs=True)
        elif hashlib.sha256(data).hexdigest() != entry["sha256"]:
            problems.add(f"{name}: its SHA-256 differs from {MANIFEST_NAME}", differs=True)

    return problems.result(manifest)


def verify(dashboard_dir: Union[str, Path]) -> Result:
    """Check `<dashboard_dir>/app/` against its own `build.json`."""
    return verify_tree(Path(dashboard_dir) / APP_DIR)


class WebModule:
    """What the advert says about the bundled client: everything or nothing."""

    def __init__(self, result: Result, capability: str) -> None:
        self.result = result
        self._capability = capability

    def capabilities(self) -> List[str]:
        return [self._capability] if self.result.ok else []

    def advert_block(self) -> Optional[Dict[str, Any]]:
        return self.result.advert_block()


def register(ctx: Any, runtime: Any, dashboard_dir: Optional[Path] = None) -> WebModule:
    """Verify the bundle once, at load. No hook, no route, no listener.

    Called only while `modules.web` is on. A missing `app/` (an older checkout,
    a partial clone) is not an error: the plugin loads and advertises no client.
    A folder that does not match its manifest is logged once, as a warning.
    """
    from .contract import CAP_WEB_CLIENT

    folder = dashboard_dir if dashboard_dir is not None else DASHBOARD_DIR
    try:
        result = verify(folder)
    except Exception as exc:  # a bug here must not stop push from loading
        result = Result(INVALID, (f"the check itself failed ({exc.__class__.__name__})",))

    if result.ok:
        logger.info("hermie: web client %s: %s", result.manifest["version"], result.summary())
    elif result.status == ABSENT:
        logger.info("hermie: no web client in dashboard/%s/; not advertising one", APP_DIR)
    else:
        logger.warning("hermie: not advertising the web client: %s", result.summary())
    return WebModule(result, CAP_WEB_CLIENT)
