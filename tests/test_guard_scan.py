"""`scripts/guard_scan.py`: the gate that runs Hermes's plugin scanner over this tree.

The script is a gate, so what is pinned here is the ways it must NOT pass: a
verdict that is anything but `safe`, a scanner that cannot be fetched or run, a
directory that is not the plugin, a scanner picked up from the wrong place. The
scanner is replaced by a few lines of Python that give the verdict a file in the
tree asks for; the real ones are exercised by the CI job itself, against the
pinned commits.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "guard_scan.py"

spec = importlib.util.spec_from_file_location("guard_scan_under_test", SCRIPT)
guard = importlib.util.module_from_spec(spec)
sys.modules["guard_scan_under_test"] = guard
spec.loader.exec_module(guard)

# A scanner with the two functions the update path calls and the same shapes. The
# verdict comes from a file called VERDICT in the tree it is given, so one fake
# serves every case.
FAKE_SCANNER = '''
from pathlib import Path

PLUGIN_SCANNER_VERSION = "fake-v1"
DEFAULT = "safe"


class Finding:
    def __init__(self, severity):
        self.severity = severity
        self.category = "test"
        self.pattern_id = "p"
        self.file = "f"
        self.line = 1
        self.match = "m"


class Result:
    pass


def _wanted(plugin_dir):
    marker = Path(plugin_dir) / "VERDICT"
    return marker.read_text().strip() if marker.is_file() else DEFAULT


def scan_plugin(plugin_dir, source=""):
    wanted = _wanted(plugin_dir)
    if wanted == "crash":
        raise RuntimeError("the scanner fell over")
    result = Result()
    result.verdict = {"safe-but-blocked": "safe"}.get(wanted, wanted)
    result.findings = [Finding(s) for s in {"caution": ["high"], "dangerous": ["critical", "high"]}.get(wanted, [])]
    result.wanted = wanted
    return result


def should_allow_plugin_install(result, force=False):
    if result.wanted == "safe-but-blocked":
        return False, "blocked for a reason of its own"
    if result.verdict == "safe":
        return True, "Allowed (clean scan)"
    if result.verdict == "caution":
        return None, "Requires confirmation"
    return False, "Blocked"


def format_scan_report(result):
    lines = ["Scan: tree  Verdict: " + result.verdict.upper()]
    if result.wanted == "inject":
        lines.append("::error::pretend workflow command")
    lines.append("escape \\x1b[31mred")
    return "\\n".join(lines)
'''


def make_scanner(directory, source=FAKE_SCANNER):
    (directory / "tools").mkdir(parents=True)
    (directory / "tools" / "__init__.py").write_text("")
    (directory / "tools" / "plugin_guard.py").write_text(source)
    return directory


@pytest.fixture
def plugin(tmp_path):
    tree = tmp_path / "plugin"
    tree.mkdir()
    (tree / "plugin.yaml").write_text("name: demo\n")
    (tree / "__init__.py").write_text("")
    return tree


@pytest.fixture
def scanners(tmp_path):
    return {name: make_scanner(tmp_path / f"scanner-{name}") for name in ("fork", "upstream")}


def run(plugin, scanners, *extra):
    argv = ["--plugin", str(plugin)]
    for name, root in scanners.items():
        argv += ["--scanner-root", f"{name}={root}"]
    return guard.main(argv + list(extra))


def verdict_is(plugin, wanted):
    (plugin / "VERDICT").write_text(wanted)


# -- the gate ----------------------------------------------------------------


def test_a_clean_tree_passes_with_both_scanners(plugin, scanners, capsys):
    assert run(plugin, scanners) == 0

    out = capsys.readouterr().out
    assert "scanner: fork" in out and "scanner: upstream" in out
    assert "every scanner says safe" in out


@pytest.mark.parametrize("wanted", ["caution", "dangerous", "safe-but-blocked", "crash"])
def test_anything_but_a_clean_safe_fails(plugin, scanners, wanted):
    verdict_is(plugin, wanted)

    assert run(plugin, scanners) == 1


def test_one_scanner_saying_no_is_enough_even_when_the_other_says_yes(plugin, tmp_path):
    strict = make_scanner(tmp_path / "strict", FAKE_SCANNER.replace('DEFAULT = "safe"', 'DEFAULT = "caution"'))
    lenient = make_scanner(tmp_path / "lenient")

    assert run(plugin, {"lenient": lenient, "strict": strict}) == 1


def test_a_scanner_that_cannot_import_fails_the_gate_rather_than_skipping_it(plugin, scanners):
    (scanners["upstream"] / "tools" / "plugin_guard.py").write_text("raise ImportError('nothing here')\n")

    assert run(plugin, scanners) == 1


def test_a_scanner_that_prints_no_result_fails(plugin, scanners):
    (scanners["fork"] / "tools" / "plugin_guard.py").write_text(
        FAKE_SCANNER.replace("def scan_plugin(plugin_dir, source=\"\"):",
                             "def scan_plugin(plugin_dir, source=\"\"):\n    import os\n    os._exit(0)")
    )

    assert run(plugin, scanners) == 1


def test_no_scanner_at_all_is_not_a_pass(plugin, tmp_path, monkeypatch):
    pins = tmp_path / "pins.json"
    pins.write_text(json.dumps({"scanners": {"x": {"repo": "https://example.test/x.git", "commit": "a" * 40}}}))
    monkeypatch.setattr(guard, "fetch_scanner", lambda *a, **k: (_ for _ in ()).throw(guard.GuardError("offline")))

    assert guard.main(["--plugin", str(plugin), "--pins", str(pins), "--workdir", str(tmp_path / "w")]) == 1


def test_a_scanner_that_cannot_be_fetched_fails_even_when_the_other_passes(plugin, scanners, tmp_path, monkeypatch):
    pins = tmp_path / "pins.json"
    pins.write_text(json.dumps({"scanners": {
        "good": {"repo": "https://example.test/a.git", "commit": "a" * 40},
        "bad": {"repo": "https://example.test/b.git", "commit": "b" * 40},
    }}))

    def fetch(name, repo, ref, dest, **kwargs):
        if name == "bad":
            raise guard.GuardError("connection reset")
        return guard.Source(name, scanners["fork"], repo, ref)

    monkeypatch.setattr(guard, "fetch_scanner", fetch)

    assert guard.main(["--plugin", str(plugin), "--pins", str(pins), "--workdir", str(tmp_path / "w")]) == 1


def test_the_scanner_is_the_one_asked_for_and_not_one_that_happens_to_be_importable(plugin, scanners, tmp_path, monkeypatch):
    """A `tools` package on PYTHONPATH must not stand in for the scanner under test."""
    decoy = make_scanner(tmp_path / "decoy")  # says `safe` for everything
    strict = scanners["fork"]
    (strict / "tools" / "plugin_guard.py").write_text(FAKE_SCANNER.replace('DEFAULT = "safe"', 'DEFAULT = "dangerous"'))
    monkeypatch.setenv("PYTHONPATH", str(decoy))

    assert run(plugin, {"fork": strict}) == 1


# -- the tree being scanned --------------------------------------------------


def test_a_directory_that_is_not_the_plugin_is_refused_not_scanned(tmp_path, scanners):
    """The real scanner calls an empty or missing directory a clean scan."""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run(empty, scanners) == 2
    assert run(tmp_path / "missing", scanners) == 2


def test_a_tree_with_a_manifest_and_nothing_else_is_refused(tmp_path, scanners):
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    (lonely / "plugin.yaml").write_text("name: demo\n")

    assert run(lonely, scanners) == 2


def test_the_repository_checkout_counts_as_the_plugin():
    assert guard.check_plugin_dir(ROOT) > 10


def test_scanners_may_not_live_inside_the_tree_they_scan(plugin, scanners):
    assert run(plugin, scanners, "--workdir", str(plugin / "scanners")) == 2


# -- the pins ----------------------------------------------------------------


def test_the_repositorys_own_pins_name_the_fork_and_upstream_at_full_commits():
    pins = guard.load_pins(guard.PINS_DEFAULT, latest=False)

    assert {p["name"] for p in pins} == {"fork", "upstream"}
    for pin in pins:
        assert pin["repo"].startswith("https://")
        assert guard.SHA_RE.match(pin["ref"]), "a pin is a commit, never a branch"


def test_the_nightly_run_reads_a_branch_for_each_scanner():
    pins = guard.load_pins(guard.PINS_DEFAULT, latest=True)

    assert {p["name"] for p in pins} == {"fork", "upstream"}
    assert all(not guard.SHA_RE.match(p["ref"]) for p in pins)


@pytest.mark.parametrize(
    "entry",
    [
        {"repo": "https://example.test/x.git", "commit": "main"},
        {"repo": "https://example.test/x.git", "commit": "abc123"},
        {"repo": "https://example.test/x.git", "commit": "A" * 40},
        {"repo": "http://example.test/x.git", "commit": "a" * 40},
        {"repo": "git@example.test:x.git", "commit": "a" * 40},
        {"commit": "a" * 40},
    ],
)
def test_a_pin_must_be_a_full_commit_of_an_https_repository(tmp_path, entry):
    pins = tmp_path / "pins.json"
    pins.write_text(json.dumps({"scanners": {"x": entry}}))

    with pytest.raises(guard.GuardError):
        guard.load_pins(pins, latest=False)


def test_a_missing_or_malformed_pin_file_is_an_error(tmp_path):
    with pytest.raises(guard.GuardError):
        guard.load_pins(tmp_path / "absent.json", latest=False)
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    with pytest.raises(guard.GuardError):
        guard.load_pins(broken, latest=False)
    empty = tmp_path / "empty.json"
    empty.write_text('{"scanners": {}}')
    with pytest.raises(guard.GuardError):
        guard.load_pins(empty, latest=False)


# -- fetching ----------------------------------------------------------------


def git(cwd, *args):
    done = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.test", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def remote(tmp_path):
    """A repository with the scanner's two files, reachable over file://."""
    repo = tmp_path / "remote"
    make_scanner(repo)
    (repo / "unrelated").mkdir()
    (repo / "unrelated" / "big.txt").write_text("not fetched\n")
    git(repo, "init", "-q", ".")
    git(repo, "config", "uploadpack.allowFilter", "true")
    git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "scanner")
    return repo, git(repo, "rev-parse", "HEAD")


def test_the_scanner_is_fetched_at_the_commit_asked_for_and_only_tools(remote, tmp_path):
    repo, sha = remote

    source = guard.fetch_scanner("fork", repo.as_uri(), sha, tmp_path / "got", attempts=1, delay=0)

    assert source.commit == sha
    assert (source.root / "tools" / "plugin_guard.py").is_file()
    assert not (source.root / "unrelated").exists(), "only tools/ is needed, and only tools/ is checked out"


def test_a_commit_the_server_does_not_have_is_an_error(remote, tmp_path):
    repo, _ = remote

    with pytest.raises(guard.GuardError):
        guard.fetch_scanner("fork", repo.as_uri(), "f" * 40, tmp_path / "got", attempts=1, delay=0)


def test_a_repository_without_the_scanner_is_an_error(tmp_path):
    repo = tmp_path / "other"
    repo.mkdir()
    (repo / "README").write_text("nothing to see\n")
    git(repo, "init", "-q", ".")
    git(repo, "config", "uploadpack.allowFilter", "true")
    git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "x")
    sha = git(repo, "rev-parse", "HEAD")

    with pytest.raises(guard.GuardError, match="plugin_guard"):
        guard.fetch_scanner("fork", repo.as_uri(), sha, tmp_path / "got", attempts=1, delay=0)


def test_a_second_run_into_the_same_workdir_starts_clean(remote, tmp_path):
    repo, sha = remote
    dest = tmp_path / "got"

    first = guard.fetch_scanner("fork", repo.as_uri(), sha, dest, attempts=1, delay=0)
    again = guard.fetch_scanner("fork", repo.as_uri(), sha, dest, attempts=1, delay=0)

    assert first.commit == again.commit == sha


def test_a_directory_that_is_not_a_previous_checkout_is_not_cleared(remote, tmp_path):
    repo, sha = remote
    dest = tmp_path / "precious"
    dest.mkdir()
    (dest / "notes.txt").write_text("mine\n")

    with pytest.raises(guard.GuardError, match="refusing to clear"):
        guard.fetch_scanner("fork", repo.as_uri(), sha, dest, attempts=1, delay=0)
    assert (dest / "notes.txt").exists()


def test_a_checkout_that_stalls_is_tried_again_with_the_fetch(remote, tmp_path, monkeypatch):
    repo, sha = remote
    real = guard._git
    failures = []

    def flaky(args, cwd):
        if args[0] == "checkout" and not failures:
            failures.append(args)
            raise guard.GuardError("git checkout failed: the connection stalled")
        return real(args, cwd)

    monkeypatch.setattr(guard, "_git", flaky)

    source = guard.fetch_scanner("fork", repo.as_uri(), sha, tmp_path / "got", attempts=2, delay=0)

    assert failures and source.commit == sha
    assert (source.root / "tools" / "plugin_guard.py").is_file()


def test_a_fetched_scanner_runs_end_to_end(remote, plugin, tmp_path):
    repo, sha = remote
    source = guard.fetch_scanner("fork", repo.as_uri(), sha, tmp_path / "got", attempts=1, delay=0)

    assert guard.passes(guard.scan_with(source, plugin))
    verdict_is(plugin, "caution")
    assert not guard.passes(guard.scan_with(source, plugin))


# -- what reaches the CI log -------------------------------------------------


def test_the_report_cannot_start_a_workflow_command(plugin, scanners, capsys):
    verdict_is(plugin, "inject")

    run(plugin, scanners)

    out = capsys.readouterr().out
    assert "pretend workflow command" in out
    for line in out.splitlines():
        assert not line.lstrip().startswith("::"), line


def test_a_command_is_broken_wherever_it_appears_in_a_line():
    out = guard.safe_text("quoted ::error::x and ##[error]y")

    assert "::" not in out and "##[" not in out


def test_the_scanners_own_error_output_is_made_harmless_too(plugin, scanners, capsys):
    (scanners["fork"] / "tools" / "plugin_guard.py").write_text(
        "import sys\nprint('::error::from stderr', file=sys.stderr)\nsys.exit(3)\n"
    )

    assert run(plugin, scanners) == 1

    out = capsys.readouterr().out
    assert "from stderr" in out
    assert "::" not in out
    for line in out.splitlines():
        assert not line.startswith("::"), line


def test_control_characters_in_a_report_are_replaced():
    assert "\x1b" not in guard.safe_text("a\x1b[31mred")
    assert guard.safe_text("one\ntwo") == "| one\n| two"


def test_the_step_summary_is_written_when_the_runner_asks(plugin, scanners, tmp_path, monkeypatch):
    target = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
    verdict_is(plugin, "caution")

    run(plugin, scanners)

    text = target.read_text()
    assert "| fork |" in text and "| upstream |" in text
    assert "caution (fails)" in text


# -- the early warning for the one finding this tree has had ------------------


def invisible_characters(root):
    """`path:line U+XXXX` for every invisible or direction-changing character in a text file under `root`, tests excluded.

    Walks the text character by character and counts only `\\n` itself:
    `str.splitlines` also splits on U+2028, U+2029 and U+0085, so a line-based
    walk could never see the two separators it most needs to find.
    """
    import unicodedata

    skipped = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "tests"}
    suffixes = {".py", ".md", ".yml", ".yaml", ".json", ".js", ".txt", ".css", ".html"}
    found = []
    for path in sorted(root.rglob("*")):
        parts = path.relative_to(root).parts
        if skipped & set(parts) or not path.is_file() or path.suffix not in suffixes:
            continue
        line = 1
        for char in path.read_text(encoding="utf-8"):
            if char == "\n":
                line += 1
            elif unicodedata.category(char) in {"Cf", "Zl", "Zp"}:
                found.append(f"{'/'.join(parts)}:{line} U+{ord(char):04X}")
    return found


def test_no_file_outside_tests_holds_an_invisible_or_direction_changing_character():
    """The scanner reads one of these in a source file as a `caution` verdict, which
    is the whole plugin flagged on every gateway's update; an editor can also
    silently turn one into something else. Write it as an escape instead."""
    assert invisible_characters(ROOT) == []


@pytest.mark.parametrize("char", ["\u2028", "\u2029", "\u202e", "\u200b", "\ufeff"], ids=lambda c: f"U+{ord(c):04X}")
def test_the_walk_finds_a_planted_character_and_names_its_line(tmp_path, char):
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text(f"a = 1\nb = 'x{char}y'\nc = 3\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "fixture.py").write_text(f"'{char}'\n", encoding="utf-8")

    assert invisible_characters(tmp_path) == [f"bad.py:2 U+{ord(char):04X}"]


def test_the_line_number_counts_newlines_only(tmp_path):
    (tmp_path / "bad.py").write_text("a\u2028b\nc\u2029d\ne\u2028\n", encoding="utf-8")

    assert invisible_characters(tmp_path) == ["bad.py:1 U+2028", "bad.py:2 U+2029", "bad.py:3 U+2028"]
