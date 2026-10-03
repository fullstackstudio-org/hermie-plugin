"""The bundled web client: checked once at load, advertised only when intact.

The check is the plugin's only claim about `dashboard/app/`. The dashboard serves
the files whatever this says, so what is tested here is the advert: present
exactly when every file matches `build.json`, absent (with one warning) otherwise,
and never a reason for the plugin not to load.
"""

import hashlib
import json
import logging
import os
import socket
import sys
from pathlib import Path

import pytest

import hermie_plugin
from hermie_plugin import contract, uimeta, web

from test_plugin import app_meta_with, gateway

ROOT = Path(__file__).resolve().parent.parent
COMMIT = "0123456789abcdef0123456789abcdef01234567"

FILES = {
    "index.html": b"<!doctype html><html><head><title>Hermie</title></head><body></body></html>\n",
    "assets/index-AbC123xy.js": b"export const a = 1;\n",
    "assets/index-Zz9_0-aa.css": b"body{margin:0}\n",
    "icons/icon.svg": b"<svg xmlns='http://www.w3.org/2000/svg'/>\n",
}


def manifest_for(files, **overrides):
    value = {
        "v": 1,
        "name": "hermie-web-client",
        "version": "0.2.0",
        "sourceRepo": "example-org/example-app",
        "sourceCommit": COMMIT,
        "files": {
            name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)} for name, data in files.items()
        },
        "totalBytes": sum(len(data) for data in files.values()),
    }
    value.update(overrides)
    return value


def write_manifest(app, value):
    (app / "build.json").write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="ascii")


def build(folder, files=None, **overrides):
    """A client build at `folder`, the way the app's build lays one out."""
    files = FILES if files is None else files
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    write_manifest(folder, manifest_for(files, **overrides))
    return folder


@pytest.fixture
def dashboard(tmp_path):
    folder = tmp_path / "dashboard"
    build(folder / "app")
    return folder


needs_symlinks = pytest.mark.skipif(not hasattr(os, "symlink") or sys.platform == "win32", reason="no symbolic links")


# -- verify -----------------------------------------------------------------


def test_an_intact_build_is_advertised_with_where_it_is_and_what_it_is(dashboard):
    result = web.verify(dashboard)

    assert result.ok and result.status == web.INTACT
    assert result.advert_block() == {
        "path": "/dashboard-plugins/hermie/app/index.html",
        "version": "0.2.0",
        "commit": COMMIT[:12],
        "files": 4,
        "bytes": sum(len(data) for data in FILES.values()),
    }


def test_one_byte_changed_is_refused_and_named(dashboard):
    path = dashboard / "app" / "assets" / "index-AbC123xy.js"
    data = bytearray(path.read_bytes())
    data[7] ^= 0x01  # same size, different content
    path.write_bytes(bytes(data))

    result = web.verify(dashboard)

    assert not result.ok and result.status == web.INVALID
    assert result.differing == 1
    assert result.advert_block() is None
    assert "assets/index-AbC123xy.js: its SHA-256 differs" in result.summary()
    assert result.summary().startswith("1 file differs")


def test_a_file_of_another_size_is_refused_without_hashing_it(dashboard, monkeypatch):
    (dashboard / "app" / "index.html").write_bytes(FILES["index.html"] + b"<script>x()</script>")
    hashed = []
    real = hashlib.sha256
    monkeypatch.setattr(web.hashlib, "sha256", lambda data=b"": hashed.append(data) or real(data))

    result = web.verify(dashboard)

    assert not result.ok and result.differing == 1
    assert "index.html" in result.summary()
    assert FILES["index.html"] not in hashed and len(hashed) == 3  # the other three only


def test_an_extra_file_is_refused(dashboard):
    (dashboard / "app" / "assets" / "extra.js").write_bytes(b"alert(1)\n")

    result = web.verify(dashboard)

    assert not result.ok and result.differing == 1
    assert "assets/extra.js: is not listed" in result.summary()


def test_a_hidden_extra_file_is_refused_too(dashboard):
    (dashboard / "app" / ".DS_Store").write_bytes(b"\x00\x00")

    assert not web.verify(dashboard).ok


def test_a_missing_file_is_refused(dashboard):
    (dashboard / "app" / "icons" / "icon.svg").unlink()

    result = web.verify(dashboard)

    assert not result.ok and result.differing == 1
    assert "icons/icon.svg: is listed in build.json but missing" in result.summary()


@needs_symlinks
def test_a_listed_file_that_is_a_symlink_is_refused_even_when_its_bytes_match(dashboard, tmp_path):
    outside = tmp_path / "outside.js"
    outside.write_bytes(FILES["assets/index-AbC123xy.js"])
    target = dashboard / "app" / "assets" / "index-AbC123xy.js"
    target.unlink()
    os.symlink(outside, target)

    result = web.verify(dashboard)

    assert not result.ok
    assert "assets/index-AbC123xy.js: is a symbolic link" in result.summary()


@needs_symlinks
def test_a_linked_folder_is_refused_and_never_entered(dashboard, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "icon.svg").write_bytes(FILES["icons/icon.svg"])
    (dashboard / "app" / "icons" / "icon.svg").unlink()
    (dashboard / "app" / "icons").rmdir()
    os.symlink(elsewhere, dashboard / "app" / "icons")

    result = web.verify(dashboard)

    assert not result.ok
    assert "icons: is a symbolic link" in result.summary()


@needs_symlinks
def test_app_itself_as_a_symlink_is_refused(tmp_path):
    real = build(tmp_path / "real")
    (tmp_path / "dashboard").mkdir()
    os.symlink(real, tmp_path / "dashboard" / "app")

    result = web.verify(tmp_path / "dashboard")

    assert not result.ok and "symbolic link" in result.summary()


@needs_symlinks
def test_build_json_as_a_symlink_is_refused(dashboard, tmp_path):
    copy = tmp_path / "build.json"
    copy.write_bytes((dashboard / "app" / "build.json").read_bytes())
    (dashboard / "app" / "build.json").unlink()
    os.symlink(copy, dashboard / "app" / "build.json")

    assert "build.json is a symbolic link" in web.verify(dashboard).summary()


def test_an_extension_the_dashboard_does_not_serve_is_refused_listed_or_not(tmp_path):
    files = dict(FILES)
    files["notes.txt"] = b"hello\n"

    listed = web.verify(build(tmp_path / "a" / "dashboard" / "app", files).parent)
    assert not listed.ok and "notes.txt: the dashboard does not serve this extension" in listed.summary()

    unlisted = tmp_path / "b" / "dashboard"
    build(unlisted / "app")
    (unlisted / "app" / "plugin_api.py").write_bytes(b"print(1)\n")
    result = web.verify(unlisted)
    assert not result.ok and "plugin_api.py: the dashboard does not serve this extension" in " ".join(result.problems)


@pytest.mark.parametrize(
    "name", ["../plugin.yaml", "/etc/passwd", "assets/../../web.py", "./index.html", "assets//x.js", "a\\b.js", "build.json"]
)
def test_a_listed_name_outside_app_or_not_plain_is_refused(dashboard, name):
    value = manifest_for(FILES)
    value["files"][name] = {"sha256": "0" * 64, "bytes": 0}
    write_manifest(dashboard / "app", value)

    result = web.verify(dashboard)

    assert not result.ok and "not a plain path inside app/" in result.summary()


def test_an_absent_folder_is_not_an_error_and_advertises_nothing(tmp_path):
    (tmp_path / "dashboard").mkdir()

    result = web.verify(tmp_path / "dashboard")

    assert result.status == web.ABSENT and not result.ok
    assert result.advert_block() is None


def test_app_as_a_plain_file_is_refused(tmp_path):
    (tmp_path / "dashboard").mkdir()
    (tmp_path / "dashboard" / "app").write_bytes(b"")

    assert web.verify(tmp_path / "dashboard").status == web.INVALID


@pytest.mark.parametrize(
    "broken, reason",
    [
        (None, "build.json is missing"),
        (b"{not json", "not valid JSON"),
        (b"[]", "not a JSON object"),
        ({"v": 2}, '"v" is 2'),
        ({"v": True}, '"v" is True'),
        ({"name": "something-else"}, '"name"'),
        ({"version": "latest"}, '"version"'),
        ({"sourceRepo": "no-slash"}, '"sourceRepo"'),
        ({"sourceCommit": COMMIT[:12]}, '"sourceCommit"'),
        ({"sourceCommit": COMMIT.upper()}, '"sourceCommit"'),
        ({"totalBytes": 1}, '"totalBytes"'),
        ({"files": {}}, '"files"'),
    ],
)
def test_a_manifest_that_is_missing_or_malformed_is_refused(dashboard, broken, reason):
    path = dashboard / "app" / "build.json"
    if broken is None:
        path.unlink()
    elif isinstance(broken, bytes):
        path.write_bytes(broken)
    else:
        value = manifest_for(FILES)
        value.update(broken)
        write_manifest(dashboard / "app", value)

    result = web.verify(dashboard)

    assert not result.ok and result.advert_block() is None
    assert reason in result.summary()


@pytest.mark.parametrize(
    "raw", [b"[" * 100_000 + b"]" * 100_000, b'{"v": 1, "x": [[[[[]]]]]}'], ids=["recursion-bomb", "five-levels"]
)
def test_a_manifest_nested_too_deep_is_refused_before_it_is_parsed(dashboard, monkeypatch, raw):
    (dashboard / "app" / "build.json").write_bytes(raw)
    monkeypatch.setattr(web.json, "loads", lambda *a, **k: pytest.fail("parsed a manifest it should have refused"))

    result = web.verify(dashboard)

    assert not result.ok and f"nests deeper than {web.MAX_MANIFEST_DEPTH} levels" in result.summary()


def test_brackets_inside_strings_do_not_count_as_nesting():
    assert web._nesting(b'{"files": {"a[[[[.js": {"sha256": "\\"}}}"}}') == 3
    assert web._nesting(b'{"a": "\\"[[[[[[", "b": [1]}') == 2


def test_a_parser_that_recurses_too_far_is_a_manifest_problem_not_a_crash(dashboard, monkeypatch):
    def recurse(*args, **kwargs):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(web.json, "loads", recurse)

    result = web.verify(dashboard)

    assert not result.ok and "not valid JSON (RecursionError)" in result.summary()


needs_fifos = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes")


def _returns_within(seconds, call):
    """`call()`'s answer, or a failure when it has not returned in time (it would hang)."""
    import threading

    answer = []
    worker = threading.Thread(target=lambda: answer.append(call()), daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), "blocked: nothing is ever going to write to that pipe"
    return answer[0]


@needs_fifos
def test_opening_a_fifo_does_not_wait_for_a_writer(tmp_path):
    fifo = tmp_path / "pipe.js"
    os.mkfifo(fifo)

    assert _returns_within(5, lambda: web._read_exactly(fifo, 1)) is None


@needs_fifos
def test_a_fifo_where_a_listed_file_was_is_refused_without_hanging(dashboard):
    target = dashboard / "app" / "assets" / "index-AbC123xy.js"
    target.unlink()
    os.mkfifo(target)

    result = _returns_within(5, lambda: web.verify(dashboard))

    assert not result.ok and "assets/index-AbC123xy.js" in result.summary()


def test_an_entry_with_a_bad_hash_or_size_shape_is_refused(dashboard):
    value = manifest_for(FILES)
    value["files"]["index.html"] = {"sha256": "x" * 64, "bytes": True}
    write_manifest(dashboard / "app", value)

    assert 'index.html must hold exactly a "sha256" and a "bytes"' in web.verify(dashboard).summary()


def test_a_build_without_an_entry_document_is_refused(tmp_path):
    files = {k: v for k, v in FILES.items() if k != "index.html"}

    result = web.verify(build(tmp_path / "dashboard" / "app", files).parent)

    assert "does not list index.html" in result.summary()


# -- the bounds -----------------------------------------------------------------


def test_the_listing_stops_after_the_file_bound(dashboard, monkeypatch):
    many = dashboard / "app" / "many"
    many.mkdir()
    for index in range(web.MAX_FILES + 10):
        (many / f"f{index}.js").write_bytes(b"")
    read = []
    monkeypatch.setattr(web, "_read_exactly", lambda path, size: read.append(path))

    result = web.verify(dashboard)

    assert not result.ok and f"more than {web.MAX_FILES} files" in result.summary()
    assert [p.name for p in read] == ["build.json"], "nothing was hashed"


def test_a_manifest_listing_more_than_the_file_bound_is_refused_unread(tmp_path, monkeypatch):
    files = {f"assets/f{index}.js": b"" for index in range(web.MAX_FILES + 1)}
    files["index.html"] = b""
    folder = tmp_path / "dashboard"
    app = folder / "app"
    app.mkdir(parents=True)
    write_manifest(app, manifest_for(files))

    result = web.verify(folder)

    assert not result.ok and f"over the limit of {web.MAX_FILES}" in result.summary()


def test_a_manifest_promising_more_than_the_byte_bound_is_refused_before_any_file_is_read(dashboard, monkeypatch):
    value = manifest_for(FILES)
    value["files"]["index.html"]["bytes"] = web.MAX_BYTES
    value["totalBytes"] = sum(entry["bytes"] for entry in value["files"].values())
    write_manifest(dashboard / "app", value)
    read = []
    real = web._read_exactly
    monkeypatch.setattr(web, "_read_exactly", lambda path, size: read.append(path.name) or real(path, size))

    result = web.verify(dashboard)

    assert not result.ok and f"over the {web.MAX_BYTES}-byte limit" in result.summary()
    assert read == ["build.json"]


def test_a_tree_nested_past_the_depth_bound_is_refused(dashboard):
    deep = dashboard / "app"
    for level in range(web.MAX_DEPTH + 1):
        deep = deep / f"d{level}"
    deep.mkdir(parents=True)

    result = web.verify(dashboard)

    assert not result.ok and "nested deeper" in result.summary()


def test_the_warning_counts_everything_but_keeps_only_a_few_reasons(dashboard):
    for index in range(15):
        (dashboard / "app" / f"extra{index}.js").write_bytes(b"")

    result = web.verify(dashboard)

    assert result.differing == 15 and len(result.problems) == 10
    assert result.summary().startswith("15 files differ") and "and 5 more" in result.summary()


# -- loading -------------------------------------------------------------------


@pytest.fixture
def loaded(tmp_path, monkeypatch):
    """Load the whole plugin against a dashboard folder of the test's choosing."""

    def load(dashboard_dir, settings=None):
        home, ctx = gateway(tmp_path / "gw", app_meta=app_meta_with(), settings=settings)
        monkeypatch.setattr(uimeta, "hermes_home", lambda: home)
        monkeypatch.setattr(web, "DASHBOARD_DIR", dashboard_dir)
        hermie_plugin.register(ctx)
        return uimeta.read_key(uimeta.PLUGIN_KEY, home), ctx

    return load


def test_an_intact_client_puts_the_capability_and_the_block_in_the_advert(dashboard, loaded):
    advert, _ = loaded(dashboard)

    assert contract.CAP_WEB_CLIENT in contract.read_capabilities(advert)
    assert advert["modules"]["web"] == "on"
    assert advert["web"] == web.verify(dashboard).advert_block()


def test_a_changed_client_is_not_advertised_and_says_so_once(dashboard, loaded, caplog):
    (dashboard / "app" / "index.html").write_bytes(b"x" * len(FILES["index.html"]))

    with caplog.at_level(logging.INFO, logger="hermie_plugin"):
        advert, _ = loaded(dashboard)

    assert contract.CAP_WEB_CLIENT not in contract.read_capabilities(advert)
    assert "web" not in advert
    assert advert["modules"]["web"] == "on"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "web client" in r.getMessage()]
    assert len(warnings) == 1
    assert "1 file differs" in warnings[0].getMessage()


def test_without_an_app_folder_the_plugin_loads_and_advertises_no_client(tmp_path, loaded, caplog):
    (tmp_path / "dashboard").mkdir()

    with caplog.at_level(logging.INFO, logger="hermie_plugin"):
        advert, ctx = loaded(tmp_path / "dashboard")

    assert contract.CAP_WEB_CLIENT not in contract.read_capabilities(advert)
    assert "web" not in advert
    assert contract.CAP_PUSH_EXPO in contract.read_capabilities(advert), "everything else still loads"
    assert "post_llm_call" in ctx.hooks
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_switched_off_the_client_is_neither_checked_nor_advertised(dashboard, loaded, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("modules.web is off: nothing to check")

    monkeypatch.setattr(web, "verify", refuse)

    advert, _ = loaded(dashboard, settings={"modules.web": False})

    assert advert["modules"]["web"] == "off"
    assert contract.CAP_WEB_CLIENT not in contract.read_capabilities(advert)
    assert "web" not in advert


def test_a_check_that_breaks_does_not_stop_the_plugin_from_loading(dashboard, loaded, monkeypatch):
    monkeypatch.setattr(web, "verify", lambda folder: 1 / 0)

    advert, _ = loaded(dashboard)

    assert contract.CAP_WEB_CLIENT not in contract.read_capabilities(advert)
    assert contract.CAP_PUSH_EXPO in contract.read_capabilities(advert)


def test_the_check_runs_once_at_load_and_never_on_a_hook(dashboard, loaded, monkeypatch):
    calls = []
    real = web.verify
    monkeypatch.setattr(web, "verify", lambda folder: calls.append(folder) or real(folder))

    _, ctx = loaded(dashboard)
    assert len(calls) == 1

    for name in list(ctx.hooks):
        for callback in ctx.hooks[name]:
            try:
                callback(session_id="s", turn_id="t", tool_name="clarify", args={}, platform="cli")
            except Exception:
                pass
    assert len(calls) == 1


# -- the build in this repository ----------------------------------------------


def test_the_committed_client_is_intact():
    """What `register` will find on a gateway that installs this tree.

    A client is committed, so a vanished or damaged `dashboard/app/` fails here.
    """
    result = web.verify(ROOT / "dashboard")

    assert result.status == web.INTACT, result.summary()
    manifest = json.loads((ROOT / "dashboard" / "app" / "build.json").read_text())
    assert result.advert_block()["commit"] == manifest["sourceCommit"][:12]


# Everything the dashboard may serve from this plugin outside `app/`. The static
# route serves any file under `dashboard/` with an allowed extension, but the
# check at load and `web-bundle-verify` cover `app/` only, so anything new here
# has to be added on purpose, in review.
DASHBOARD_OUTSIDE_APP = {"manifest.json", "plugin_api.py", "dist/index.js"}


def test_nothing_but_the_known_files_lives_under_dashboard_outside_app():
    folder = ROOT / "dashboard"
    found = set()
    for path in folder.rglob("*"):
        relative = path.relative_to(folder)
        if relative.parts[0] == web.APP_DIR or "__pycache__" in relative.parts or path.is_dir():
            continue
        found.add(relative.as_posix())

    assert found == DASHBOARD_OUTSIDE_APP


def test_the_entry_path_is_where_the_dashboard_serves_this_plugin():
    name = json.loads((ROOT / "dashboard" / "manifest.json").read_text())["name"]

    assert web.ENTRY_PATH == f"/dashboard-plugins/{name}/{web.APP_DIR}/{web.ENTRY}"


def test_web_py_stands_alone_so_the_import_script_applies_the_same_rules():
    source = (ROOT / "web.py").read_text(encoding="utf-8")
    module_level = [line for line in source.splitlines() if line.startswith(("import ", "from "))]

    assert not [line for line in module_level if line.startswith("from .")]


# -- the import script -------------------------------------------------------------


@pytest.fixture
def importer():
    import importlib.util

    spec = importlib.util.spec_from_file_location("import_web_client", ROOT / "scripts" / "import_web_client.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def offline(monkeypatch):
    """The script makes no network request; any attempt fails the test."""

    def refuse(*args, **kwargs):
        raise AssertionError("the import script opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def run(importer, capsys, *argv):
    code = importer.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_import_puts_the_build_in_place_and_prints_what_it_is(importer, tmp_path, capsys, offline):
    dist = build(tmp_path / "dist")
    target = tmp_path / "plugin" / "dashboard"
    target.mkdir(parents=True)

    code, out, _ = run(importer, capsys, "--dist", str(dist), "--dashboard", str(target))

    assert code == 0
    assert out.splitlines() == [
        "present=true",
        "version=0.2.0",
        f"commit={COMMIT}",
        "repo=example-org/example-app",
        "files=4",
        f"bytes={sum(len(d) for d in FILES.values())}",
    ]
    assert web.verify(target).ok
    assert sorted(p.name for p in target.iterdir()) == ["app"], "no staging folder left behind"


def test_import_replaces_an_older_build_and_copies_only_listed_files(importer, tmp_path, capsys, offline):
    target = tmp_path / "dashboard"
    build(target / "app", {"index.html": b"old\n", "assets/old-1.js": b"old\n"})
    dist = build(tmp_path / "dist")
    (dist / "dist-notes").mkdir()  # an unlisted folder with nothing in it is copied by no one

    code, _, _ = run(importer, capsys, "--dist", str(dist), "--dashboard", str(target))

    assert code == 0
    assert not (target / "app" / "assets" / "old-1.js").exists()
    assert not (target / "app" / "dist-notes").exists()
    assert (target / "app" / "index.html").read_bytes() == FILES["index.html"]


@pytest.mark.parametrize(
    "spoil, reason",
    [
        (lambda d: (d / "index.html").write_bytes(b"changed"), "does not match"),
        (lambda d: (d / "stray.js").write_bytes(b""), "not listed"),
        (lambda d: build(d, {**FILES, "assets/big.js": b"x" * 900_001}), "file limit"),
        (lambda d: build(d, {**FILES, "assets/word.js": "const s = 'é';\n".encode()}), "non-ASCII"),
        (lambda d: build(d, {**FILES, "assets/index.js.map": b"{}"}), "source map"),
        (lambda d: build(d, {**FILES, **{f"assets/c{i}.js": b"" for i in range(80)}}), "over the limit of 80"),
        (lambda d: build(d, {**FILES, **{f"assets/c{i}.js": b"x" * 800_000 for i in range(4)}}), "3000000-byte limit"),
        (
            lambda d: (d / "build.json").write_text(json.dumps(manifest_for(FILES)), encoding="ascii"),
            "canonical form",
        ),
    ],
)
def test_import_refuses_a_build_and_leaves_the_old_one_alone(importer, tmp_path, capsys, offline, spoil, reason):
    target = tmp_path / "dashboard"
    old = {"index.html": b"old\n"}
    build(target / "app", old)
    dist = build(tmp_path / "dist")
    spoil(dist)

    code, out, err = run(importer, capsys, "--dist", str(dist), "--dashboard", str(target))

    assert code == 1 and out == ""
    assert reason in err
    assert (target / "app" / "index.html").read_bytes() == old["index.html"]
    assert sorted(p.name for p in target.iterdir()) == ["app"]


def test_import_of_a_folder_that_does_not_exist_is_refused(importer, tmp_path, capsys, offline):
    (tmp_path / "dashboard").mkdir()

    code, _, err = run(importer, capsys, "--dist", str(tmp_path / "nowhere"), "--dashboard", str(tmp_path / "dashboard"))

    assert code == 1 and "does not exist" in err


@needs_symlinks
def test_import_accepts_a_dist_reached_through_a_link(importer, tmp_path, capsys, offline):
    dist = build(tmp_path / "real-dist")
    os.symlink(dist, tmp_path / "dist")
    (tmp_path / "dashboard").mkdir()

    code, _, _ = run(importer, capsys, "--dist", str(tmp_path / "dist"), "--dashboard", str(tmp_path / "dashboard"))

    assert code == 0
    assert not (tmp_path / "dashboard" / "app").is_symlink()


def test_check_says_present_false_without_a_client(importer, tmp_path, capsys, offline):
    (tmp_path / "dashboard").mkdir()

    assert run(importer, capsys, "--check", "--dashboard", str(tmp_path / "dashboard")) == (0, "present=false\n", "")


def test_check_refuses_a_changed_client_in_place(importer, dashboard, capsys, offline):
    (dashboard / "app" / "extra.css").write_bytes(b"")

    code, out, err = run(importer, capsys, "--check", "--dashboard", str(dashboard))

    assert code == 1 and out == "" and "extra.css" in err


def test_check_refuses_a_nesting_bomb_without_a_traceback(importer, dashboard, capsys, offline):
    (dashboard / "app" / "build.json").write_bytes(b"[" * 100_000 + b"]" * 100_000)

    code, out, err = run(importer, capsys, "--check", "--dashboard", str(dashboard))

    assert code == 1 and out == "" and "nests deeper" in err and "Traceback" not in err


def test_the_committed_client_passes_the_import_limits(importer, capsys, offline):
    code, out, err = run(importer, capsys, "--check")

    assert code == 0, err
    assert out.startswith("present=true\n")


def test_the_command_line_needs_exactly_one_mode(importer, capsys):
    with pytest.raises(SystemExit) as raised:
        importer.main([])
    assert raised.value.code == 2
    with pytest.raises(SystemExit):
        importer.main(["--check", "--dist", "x"])
