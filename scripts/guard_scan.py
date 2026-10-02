#!/usr/bin/env python3
"""Run Hermes's plugin scanner over this checkout, the way an update runs it.

`hermes plugins update` re-scans the plugin tree after it pulls, and a
``dangerous`` verdict disables the plugin on that gateway, push notifications
included. This script lets the repository find that out before a change is
merged, by calling the same two functions the update path calls:

    tools.plugin_guard.scan_plugin(tree)
    tools.plugin_guard.should_allow_plugin_install(result)

It does nothing else with the scanner: no policy of its own, no thresholds. The
gate is Hermes's own answer, and it passes only when that answer is a clean
``safe`` with the install allowed outright. ``caution`` fails too.

The scanner is Hermes's code, so it has to come from somewhere. Two sources
are scanned with, because the gateways run a fork and the fork tracks upstream:

* ``--pins FILE`` fetches each source at the commit the file names, so a CI run
  is reproducible. ``--latest`` fetches the branch named there instead, for the
  scheduled run that asks "would the newest scanner still pass this tree?".
* ``--scanner-root NAME=DIR`` uses a checkout you already have, for a local run.

Only ``tools/`` is fetched. The scanner modules use the standard library only,
and ``tools/__init__.py`` is documented to be free of side effects, so nothing
of Hermes itself needs to be installed. Each scanner runs in its own
interpreter, because the fork and upstream both define a package called
``tools``.

Exit status: 0 when every scanner says ``safe``; 1 when any of them does not,
or one could not be fetched or run (a gate that cannot run must not pass); 2
for a command line that makes no sense.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RESULT_PREFIX = "GUARD_SCAN_RESULT "
PINS_DEFAULT = Path(__file__).resolve().parent.parent / ".github" / "scanner-pins.json"
PLUGIN_DEFAULT = Path(__file__).resolve().parent.parent

# What the scanner itself never reads (tools/plugin_guard.py::EXCLUDED_DIRS). Used
# only to count what it will look at, never to decide anything.
SCANNER_EXCLUDED_DIRS = frozenset(
    {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox"}
)

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
# A fetch of tools/ takes seconds. A connection that stalls is cut after twenty
# seconds without data and tried again, rather than held until the job times out.
GIT_TIMEOUT = 90
GIT_STALL = ["-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=20"]
SCAN_TIMEOUT = 300
REPORT_LINE_LIMIT = 200


class GuardError(Exception):
    """A scanner could not be obtained or run. The gate fails; it never skips."""


class Source:
    """One scanner checkout: where it lives and what it is."""

    def __init__(self, name: str, root: Path, repo: str = "", commit: str = "") -> None:
        self.name = name
        self.root = root
        self.repo = repo
        self.commit = commit


# ---------------------------------------------------------------------------
# Getting the scanner
# ---------------------------------------------------------------------------


def load_pins(path: Path, latest: bool) -> list[dict]:
    """Read the pin file; return one ``{name, repo, ref}`` per scanner.

    Without ``latest`` the ref is the pinned 40-character commit, so a branch
    name or a tag can never stand in for one.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GuardError(f"cannot read the scanner pins at {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("scanners"), dict) or not data["scanners"]:
        raise GuardError(f"{path} has no 'scanners' object")
    out = []
    for name, entry in data["scanners"].items():
        if not NAME_RE.match(name):
            raise GuardError(f"scanner name {name!r} must be lower-case letters, digits and dashes")
        if not isinstance(entry, dict):
            raise GuardError(f"scanner {name!r} must be an object")
        repo = entry.get("repo")
        if not isinstance(repo, str) or not repo.startswith("https://"):
            raise GuardError(f"scanner {name!r}: 'repo' must be an https:// URL")
        if latest:
            ref = entry.get("latest")
            if not isinstance(ref, str) or not ref or ref.startswith("-"):
                raise GuardError(f"scanner {name!r}: 'latest' must name a branch")
        else:
            ref = entry.get("commit")
            if not isinstance(ref, str) or not SHA_RE.match(ref):
                raise GuardError(f"scanner {name!r}: 'commit' must be a full 40-character lower-case SHA")
        out.append({"name": name, "repo": repo, "ref": ref})
    return out


def _git(args: list[str], cwd: Path) -> str:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        done = subprocess.run(
            ["git", *GIT_STALL, *args], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GuardError(f"git {' '.join(args[:2])} failed: {exc}") from exc
    if done.returncode != 0:
        raise GuardError(f"git {' '.join(args[:2])} failed: {(done.stderr or done.stdout).strip()[-400:]}")
    return done.stdout.strip()


def prepare_dest(dest: Path) -> None:
    """Make ``dest`` an empty directory, so a rerun with the same workdir starts clean.

    Only a directory this script made (it holds a ``.git``) is cleared; anything
    else that is not empty is refused rather than deleted.
    """
    if dest.exists() and any(dest.iterdir()):
        if not (dest / ".git").is_dir():
            raise GuardError(f"{dest} is not empty and is not a previous scanner checkout; refusing to clear it")
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)


def fetch_scanner(name: str, repo: str, ref: str, dest: Path, attempts: int = 3, delay: float = 5.0) -> Source:
    """Fetch ``tools/`` of ``repo`` at ``ref`` into ``dest`` and return the Source.

    A shallow, blob-less, sparse fetch: a few megabytes and a few seconds. When
    ``ref`` is a full SHA the checked-out commit is compared with it, so a
    server that answers with something else is an error rather than a scan.
    """
    prepare_dest(dest)
    _git(["init", "-q", "."], dest)
    _git(["remote", "add", "origin", repo], dest)
    _git(["sparse-checkout", "set", "--cone", "tools"], dest)
    # The checkout is inside the retry as well: with a blob-less fetch it is the
    # step that downloads the files, and it can stall like the fetch can.
    for attempt in range(1, attempts + 1):
        try:
            _git(["fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", ref], dest)
            _git(["checkout", "-q", "-f", "--detach", "FETCH_HEAD"], dest)
            break
        except GuardError:
            if attempt == attempts:
                raise
            time.sleep(delay)
    commit = _git(["rev-parse", "HEAD"], dest)
    if SHA_RE.match(ref) and commit != ref:
        raise GuardError(f"{name}: asked for {ref} and got {commit}")
    if not (dest / "tools" / "plugin_guard.py").is_file():
        raise GuardError(f"{name}: {repo} at {commit[:12]} has no tools/plugin_guard.py")
    return Source(name, dest, repo, commit)


# ---------------------------------------------------------------------------
# The tree being scanned
# ---------------------------------------------------------------------------


def check_plugin_dir(plugin: Path) -> int:
    """Refuse a directory that is not this plugin; return how many files the scanner will see.

    ``scan_plugin`` answers "clean scan" for a path that does not exist, and for
    an empty one. A gate pointed at the wrong directory must not pass.
    """
    if not plugin.is_dir():
        raise GuardError(f"{plugin} is not a directory")
    if not (plugin / "plugin.yaml").is_file():
        raise GuardError(f"{plugin} has no plugin.yaml: this is not the plugin tree")
    count = 0
    for path in plugin.rglob("*"):
        parts = path.relative_to(plugin).parts
        if any(part in SCANNER_EXCLUDED_DIRS for part in parts):
            continue
        if path.is_file() or path.is_symlink():
            count += 1
    if count < 2:
        raise GuardError(f"{plugin} holds {count} file(s) the scanner would read; that cannot be the plugin")
    return count


# ---------------------------------------------------------------------------
# One scanner, in its own interpreter
# ---------------------------------------------------------------------------


def run_one(root: Path, plugin: Path) -> int:
    """Scan ``plugin`` with the scanner at ``root`` and print the report and one result line.

    Runs in a fresh ``python -I`` process, which ignores ``PYTHON*`` variables and
    the user's site directory but keeps the environment's own site-packages. What
    guarantees that the ``tools`` package in use is the fetched one is that
    ``root`` is put first on ``sys.path`` and that the module's file is then
    checked to live under ``root``: anything else is refused.
    """
    sys.path.insert(0, str(root))
    from tools import plugin_guard  # noqa: PLC0415 - must follow the sys.path edit

    loaded = Path(plugin_guard.__file__).resolve()
    if root.resolve() not in loaded.parents:
        print(f"refusing to continue: tools.plugin_guard came from {loaded}, not from {root}", file=sys.stderr)
        return 1

    result = plugin_guard.scan_plugin(plugin, source="hermie-plugin")
    allowed, reason = plugin_guard.should_allow_plugin_install(result)
    print(plugin_guard.format_scan_report(result))
    payload = {
        "verdict": str(result.verdict),
        "allowed": allowed,
        "reason": str(reason),
        "scanner_version": str(getattr(plugin_guard, "PLUGIN_SCANNER_VERSION", "unknown")),
        "findings": [
            {
                "severity": str(f.severity),
                "category": str(f.category),
                "pattern": str(f.pattern_id),
                "file": str(f.file),
                "line": int(f.line),
            }
            for f in result.findings
        ],
    }
    print(RESULT_PREFIX + json.dumps(payload, sort_keys=True))
    return 0 if (result.verdict == "safe" and allowed is True) else 1


def scan_with(source: Source, plugin: Path) -> dict:
    """Run one scanner in a child process and return its parsed result plus its report text."""
    cmd = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--run-one", str(source.root), str(plugin)]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=SCAN_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GuardError(f"{source.name}: the scanner could not be run: {exc}") from exc
    payload = None
    report_lines = []
    for line in done.stdout.splitlines():
        if line.startswith(RESULT_PREFIX):
            try:
                payload = json.loads(line[len(RESULT_PREFIX):])
            except ValueError:
                payload = None
        else:
            report_lines.append(line)
    if payload is None:
        detail = (done.stderr or done.stdout).strip()[-600:]
        raise GuardError(f"{source.name}: the scanner produced no result (exit {done.returncode}): {detail}")
    payload["report"] = "\n".join(report_lines)
    payload["exit"] = done.returncode
    return payload


def passes(result: dict) -> bool:
    """The whole gate: Hermes says ``safe``, allows it outright, and the child agrees."""
    return result.get("verdict") == "safe" and result.get("allowed") is True and result.get("exit") == 0


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def safe_text(text: str) -> str:
    """Make scanner output and error text harmless in a CI log.

    The report quotes lines of the tree under test, and on a pull request that
    tree is someone else's. Every line is indented behind a marker, so a line
    cannot begin with a workflow command; the two command spellings are also
    broken wherever they appear, and control characters (escape sequences
    included) become question marks. This makes the text safe to read in a log,
    not a guarantee about every way a runner could ever interpret a line.
    """
    cleaned = "".join(ch if ch == "\n" or (ch.isprintable() and ord(ch) < 0x110000) else "?" for ch in text)
    cleaned = cleaned.replace("::", ": :").replace("##[", "# #[")
    return "\n".join("| " + line for line in cleaned.splitlines())


def one_line(text: str) -> str:
    """``safe_text`` squeezed onto one line, for a table cell."""
    return " ".join(safe_text(text).replace("|", "/").split())[:200]


def severity_counts(findings: list[dict]) -> str:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    order = ["critical", "high", "medium", "low"]
    parts = [f"{counts[s]} {s}" for s in order if s in counts]
    parts += [f"{n} {s}" for s, n in sorted(counts.items()) if s not in order]
    return ", ".join(parts) if parts else "none"


def print_result(source: Source, result: dict) -> None:
    where = f"{source.repo} @ {source.commit[:12]}" if source.commit else str(source.root)
    status = "PASS" if passes(result) else "FAIL"
    print(f"\n=== scanner: {source.name} ({where}), {result['scanner_version']}  ->  {status}")
    report = safe_text(result["report"]).splitlines()
    for line in report[:REPORT_LINE_LIMIT]:
        print(line)
    if len(report) > REPORT_LINE_LIMIT:
        print(f"| ... {len(report) - REPORT_LINE_LIMIT} more line(s) not shown")
    print(f"verdict {result['verdict']}; findings: {severity_counts(result['findings'])}")


def write_summary(rows: list[tuple[Source, dict | None, str]], files: int, latest: bool) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    lines = [
        "### Plugin scanner" + (" (newest scanner, informational)" if latest else ""),
        "",
        f"{files} file(s) in the tree. Passes only on a verdict of `safe`.",
        "",
        "| Scanner | Commit | Version | Verdict | Findings |",
        "|---|---|---|---|---|",
    ]
    for source, result, error in rows:
        commit = f"`{source.commit[:12]}`" if source.commit else "local"
        if result is None:
            lines.append(f"| {source.name} | {commit} | - | could not run | {one_line(error)} |")
        else:
            verdict = result["verdict"] + ("" if passes(result) else " (fails)")
            lines.append(
                f"| {source.name} | {commit} | {result['scanner_version']} | {verdict}"
                f" | {severity_counts(result['findings'])} |"
            )
    try:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_scanner_roots(values: list[str]) -> list[Source]:
    out = []
    for value in values:
        name, sep, directory = value.partition("=")
        if not sep or not NAME_RE.match(name) or not directory:
            raise GuardError(f"--scanner-root wants NAME=DIR, got {value!r}")
        root = Path(directory).resolve()
        if not (root / "tools" / "plugin_guard.py").is_file():
            raise GuardError(f"{root} has no tools/plugin_guard.py")
        out.append(Source(name, root))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--plugin", type=Path, default=PLUGIN_DEFAULT, help="the tree to scan (default: this checkout)")
    parser.add_argument("--pins", type=Path, default=None, help=f"scanner pin file (default: {PINS_DEFAULT.name})")
    parser.add_argument("--latest", action="store_true", help="fetch each scanner's newest branch, not its pin")
    parser.add_argument("--workdir", type=Path, default=None, help="where to put fetched scanners (outside the plugin)")
    parser.add_argument("--scanner-root", action="append", default=[], metavar="NAME=DIR", help="use a checkout you have")
    parser.add_argument("--run-one", nargs=2, metavar=("ROOT", "PLUGIN"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.run_one:
        return run_one(Path(args.run_one[0]), Path(args.run_one[1]))

    plugin = args.plugin.resolve()
    try:
        files = check_plugin_dir(plugin)
        sources = parse_scanner_roots(args.scanner_root)
        if args.pins is not None or not sources:
            pins = load_pins(args.pins or PINS_DEFAULT, args.latest)
        else:
            pins = []
    except GuardError as exc:
        print(safe_text(f"guard-scan: {exc}"), file=sys.stderr)
        return 2
    if args.latest and args.scanner_root:
        print("guard-scan: --latest only applies to fetched scanners", file=sys.stderr)
        return 2

    workdir = (args.workdir or Path(tempfile.mkdtemp(prefix="guard-scan-"))).resolve()
    if workdir == plugin or plugin in workdir.parents or workdir in plugin.parents:
        print("guard-scan: the scanner workdir and the plugin tree must not contain one another", file=sys.stderr)
        return 2

    print(f"scanning {plugin} ({files} file(s) the scanner will read)")
    rows: list[tuple[Source, dict | None, str]] = []
    failed = False

    for pin in pins:
        try:
            sources.append(fetch_scanner(pin["name"], pin["repo"], pin["ref"], workdir / pin["name"]))
        except GuardError as exc:
            print(f"\n=== scanner: {pin['name']}  ->  FAIL\n" + safe_text(f"could not fetch it: {exc}"))
            rows.append((Source(pin["name"], workdir / pin["name"], pin["repo"]), None, str(exc)))
            failed = True

    for source in sources:
        try:
            result = scan_with(source, plugin)
        except GuardError as exc:
            print(f"\n=== scanner: {source.name}  ->  FAIL\n" + safe_text(str(exc)))
            rows.append((source, None, str(exc)))
            failed = True
            continue
        print_result(source, result)
        rows.append((source, result, ""))
        failed = failed or not passes(result)

    if not rows:
        print("\nguard-scan: no scanner was configured, so nothing was checked")
        failed = True

    write_summary(rows, files, args.latest)
    print("\nguard-scan: " + ("FAILED: the plugin would not install or update as `safe`" if failed else "every scanner says safe"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
