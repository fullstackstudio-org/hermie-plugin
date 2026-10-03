"""Every transport against the one push contract.

The contract is a file in the Hermie app's repository,
`contract/push/contract.json` (github.com/fullstackstudio-org/hermie). Every
sender and both generations of the app conform to it, so a payload this plugin
builds is checked against it here rather than against what this plugin happens
to believe.

`fixtures/push_contract.json` is a verbatim copy, so this suite runs on a bare
checkout. When the app repository is checked out next to this one — or
`HERMIE_APP_REPO` points at it — the copy is also compared with the original,
and a contract that moved on without this copy fails here first.
"""

import json
import os
import re
from pathlib import Path

import pytest

from hermie_plugin.push import events, expo
from hermie_plugin.push.cron import BY_TASK_ID, Cron

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "push_contract.json"
CONTRACT = json.loads(FIXTURE.read_text(encoding="utf-8"))

# Fields the data bag carries that the contract's field list does not describe.
# `preview` is the opt-in text, checked separately.
ENVELOPE = set()


def app_repo_contract():
    configured = os.environ.get("HERMIE_APP_REPO", "")
    candidates = [Path(configured)] if configured else []
    candidates.append(Path(__file__).resolve().parents[2] / "hermie")
    for root in candidates:
        path = root / "contract" / "push" / "contract.json"
        if path.is_file():
            return path
    return None


def test_the_copy_matches_the_app_repository():
    original = app_repo_contract()
    if original is None:
        pytest.skip("the app repository is not checked out next to this one")
    assert without_comments(json.loads(original.read_text(encoding="utf-8"))) == without_comments(CONTRACT)


def without_comments(value):
    """The contract minus its `$comment` notes, which are prose, not contract."""
    if isinstance(value, dict):
        return {key: without_comments(item) for key, item in value.items() if key != "$comment"}
    if isinstance(value, list):
        return [without_comments(item) for item in value]
    return value


def conformance_problems(data, *, allow_missing=()):
    """Everything about one data bag the contract would object to."""
    problems = []
    fields = {field["key"]: field for field in CONTRACT["data"]["fields"]}
    for key, field in fields.items():
        required = field.get("required") is True
        # `requiredWhen` is one condition; `alsoRequiredWhen` is more of them,
        # added beside it so the one a reader already understands is unchanged.
        for when in [field.get("requiredWhen"), *field.get("alsoRequiredWhen", [])]:
            if when and all(data.get(name) == value for name, value in when.items()):
                required = True
        if key not in data:
            if required and key not in allow_missing:
                problems.append(f"{key} is required")
            continue
        value = data[key]
        if value is None or value == "":
            problems.append(f"{key} is sent empty; optional fields are omitted instead")
            continue
        expected = {"string": str, "boolean": bool, "number": (int, float)}[field["type"]]
        if isinstance(value, bool) and field["type"] == "number":
            problems.append(f"{key} is a boolean, not a number")
        elif not isinstance(value, expected):
            problems.append(f"{key} is {type(value).__name__}, not {field['type']}")
        if "enum" in field and value not in field["enum"]:
            problems.append(f"{key}={value!r} is not one of {field['enum']}")
        if "const" in field and value != field["const"]:
            problems.append(f"{key}={value!r} is not {field['const']!r}")
        if "pattern" in field and not re.fullmatch(field["pattern"], str(value)):
            problems.append(f"{key}={value!r} does not match {field['pattern']}")
    for key in data:
        if key not in fields and key not in ENVELOPE and key != "preview":
            problems.append(f"{key} is not in the contract")
    return problems


# One of every notification this plugin raises, with everything a payload can
# carry filled in, so an unknown or mistyped field has somewhere to show up.
CRON = Cron(job_id="nightly", source=BY_TASK_ID)


def every_notification():
    return {
        "message": events.from_assistant_message(
            bot="scout", session_id="s1", turn_id="t1", assistant_response="hello", at=10
        ),
        "approval": events.from_approval(
            bot="scout", session_key="s1", description="rm", request_id="r1", turn_id="t1", at=10, cron=CRON
        ),
        "clarify": events.from_clarify(
            bot="scout", session_id="s1", tool_call_id="c1", question="which one?", at=10
        ),
        "turn_done": events.from_session_end(
            bot="scout", session_id="s1", turn_id="t1", completed=True, failed=False, interrupted=False, at=10
        ),
        "turn_failed": events.from_session_end(
            bot="scout", session_id="s1", turn_id="t1", completed=False, failed=True, interrupted=False, at=10
        ),
        "cron": events.from_cron_delivery(
            bot="scout", session_id="s1", turn_id="t1", assistant_response="report", at=10, cron=CRON
        ),
        "cron_done": events.from_session_end(
            bot="scout", session_id="s1", turn_id="t1", completed=True, failed=False, interrupted=False,
            at=10, cron=CRON,
        ),
        "cron_failed": events.from_session_end(
            bot="scout", session_id="s1", turn_id="t1", completed=False, failed=True, interrupted=False,
            at=10, cron=CRON,
        ),
    }


def full_payload(note):
    return note.payload(preview=False, gateway_key="bf796761db84e312", session_kind="canonical")


@pytest.mark.parametrize("name", sorted(every_notification()))
def test_every_payload_conforms(name):
    note = every_notification()[name]
    data = full_payload(note)
    assert conformance_problems(data) == []


def test_a_clarify_seen_through_the_tool_hook_carries_no_request_id():
    """Its id is minted inside the gateway's blocking prompt, out of that hook's sight."""
    assert "requestId" not in full_payload(every_notification()["clarify"])


@pytest.mark.parametrize("example", CONTRACT["examples"]["list"], ids=lambda entry: entry["name"])
def test_every_example_in_the_contract_conforms_to_its_own_field_list(example):
    """The file's examples are held to the file's rules, whoever reads them."""
    data = example["data"]
    assert conformance_problems(data) == []
    # The category rule, and the per-method table that spells it out.
    methods = {entry["method"]: entry for entry in CONTRACT["requests"]["methods"]}
    wants_actions = data.get("type") == "request" and not data.get("clear") and (
        methods[data["method"]]["actions"] if data.get("method") in methods else False
    )
    assert ("category" in example) == bool(wants_actions)
    if data.get("method") in CONTRACT["category"]["notFor"]["methods"]:
        assert "category" not in example
    # A method that must never carry text carries none, with previews on or off.
    if data.get("method") in methods and not methods[data["method"]]["preview"]:
        assert "preview" not in data
    assert example.get("silent", False) == bool(data.get("clear"))


def test_the_methods_table_names_every_method_the_plugin_raises():
    assert [entry["method"] for entry in CONTRACT["requests"]["methods"]] == list(events.REQUEST_METHODS)
    field = next(f for f in CONTRACT["data"]["fields"] if f["key"] == "method")
    assert field["enum"] == list(events.REQUEST_METHODS)
    assert CONTRACT["category"]["notFor"]["methods"] == [
        method for method in events.REQUEST_METHODS if method != "approval"
    ]


def test_the_unfiltered_types_are_the_plugins_and_not_switches():
    assert list(CONTRACT["unfilteredTypes"].keys() - {"$comment"}) == list(events.UNFILTERED_TYPES)
    assert not set(events.UNFILTERED_TYPES) & set(CONTRACT["types"])
    assert next(f for f in CONTRACT["data"]["fields"] if f["key"] == "type")["enum"] == list(events.PAYLOAD_TYPES)
    assert CONTRACT["clear"]["reasons"] == list(events.CLEAR_REASONS)
    assert [c["id"] for c in CONTRACT["android"]["alsoChannels"]] == list(events.UNFILTERED_TYPES)


def test_an_approval_without_a_session_drops_the_session_and_keeps_the_rest():
    note = events.from_approval(
        bot="scout", session_key="", description="rm", request_id="r1", turn_id="t1", at=10
    )
    data = full_payload(note)
    assert "sessionId" not in data
    assert data["requestId"] == "r1"
    assert data["method"] == "approval"
    assert conformance_problems(data) == []
    assert expo_message(note)["categoryId"] == "hermie.request"


def test_an_approval_without_a_request_id_breaks_the_contract():
    """The rule the checker enforces, so a sender that loses the id is caught."""
    note = events.from_approval(
        bot="scout", session_key="s1", description="rm", request_id=None, turn_id="t1", at=10
    )
    assert conformance_problems(full_payload(note)) == ["requestId is required"]


def test_the_type_list_is_the_contracts():
    assert list(events.TYPES) == CONTRACT["types"]


def test_a_request_says_which_kind_of_request_it_is():
    notes = every_notification()
    assert full_payload(notes["approval"])["method"] == "approval"
    assert full_payload(notes["clarify"])["method"] == "clarify"
    assert "method" not in full_payload(notes["message"])


def test_the_request_id_travels_under_the_contracts_key():
    data = full_payload(every_notification()["approval"])
    assert data["requestId"] == "r1"
    assert "request" not in data


# -- Expo --------------------------------------------------------------------


def expo_message(note):
    payload = full_payload(note)
    title, body = note.rendered(preview=False)
    return expo.message_for("ExponentPushToken[a]", payload, title=title, body=body)


def test_an_approval_is_posted_under_the_contracts_category():
    message = expo_message(every_notification()["approval"])
    assert message["categoryId"] == CONTRACT["category"]["id"] == "hermie.request"


def test_only_an_approval_grows_buttons():
    """A clarify question has no answer a button could send; nor does a message."""
    for name, note in every_notification().items():
        if name != "approval":
            assert "categoryId" not in expo_message(note), name


def test_an_approval_without_a_request_id_grows_no_buttons():
    note = events.from_approval(
        bot="scout", session_key="s1", description="rm", request_id=None, turn_id="t1", at=10
    )
    assert "categoryId" not in expo_message(note)


def test_every_android_channel_is_the_type_name():
    channels = {channel["type"]: channel["id"] for channel in CONTRACT["android"]["channels"]}
    for note in every_notification().values():
        message = expo_message(note)
        assert message["channelId"] == channels[message["data"]["type"]]


# -- Web Push ----------------------------------------------------------------


def web_push_payload(tmp_path, monkeypatch, note):
    """Deliver *note* to one browser subscription and hand back what was encrypted."""
    pytest.importorskip("cryptography")
    import yaml

    import hermie_plugin
    import hermie_plugin.push as push_pkg
    from hermie_plugin import uimeta
    from hermie_plugin.state import FileStore

    home = tmp_path / "hermes"
    home.mkdir()
    row = {
        "v": 1, "transport": "webpush", "endpoint": "https://push.example/x",
        "keys": {"p256dh": "a", "auth": "b"}, "platform": "web",
        "types": {name: True for name in events.TYPES}, "updatedAt": 1,
    }
    (home / "profile.yaml").write_text(
        yaml.safe_dump({"ui_meta": {"hermie-app": {"push": {"registrations": {"w1": row}}}}})
    )
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)
    module = push_pkg.PushModule(hermie_plugin.Runtime(object(), home=home, store=FileStore(tmp_path / "s.json")))

    sent = []
    monkeypatch.setattr(module, "vapid_key", lambda: object())
    monkeypatch.setattr(
        push_pkg.webpush, "send",
        lambda key, endpoint, p256dh, auth, payload, contact="": sent.append(payload)
        or push_pkg.webpush.Result(status=201),
    )
    assert module.deliver(note) == 1
    return sent[0]


def test_web_push_carries_the_payload_where_the_service_worker_reads_it(tmp_path, monkeypatch):
    """`{title, body, data}` — the shape the app's service worker opens."""
    sent = web_push_payload(tmp_path, monkeypatch, every_notification()["approval"])

    assert set(sent) == {"title", "body", "data"}
    assert sent["title"] == "scout"
    assert sent["data"]["type"] == "request"
    assert sent["data"]["requestId"] == "r1"
    assert conformance_problems(sent["data"]) == []


# The app's service worker, `public/hermie-push-sw.js` in the Hermie app's
# repository (unchanged since the 0.1.9 release), ported line for line: what
# its `push` handler shows, and what its `notificationclick` handler hands the
# app. The real file is also run under Node below when it can be found.


def service_worker_shows(payload):
    payload = payload if isinstance(payload, dict) else {}
    title = payload["title"] if isinstance(payload.get("title"), str) and payload["title"] else "Hermie"
    body = payload["body"] if isinstance(payload.get("body"), str) else ""
    data = payload["data"] if isinstance(payload.get("data"), dict) else {}
    needs_input = data.get("type") == "request"
    bot = data["bot"] if isinstance(data.get("bot"), str) and data["bot"] else ""
    if not bot:
        tag = "hermie"
    else:
        session = next(
            (data[key] for key in ("sessionId", "session") if isinstance(data.get(key), str) and data[key]), ""
        )
        tag = f"hermie:{bot}:{session}" if session else f"hermie:{bot}"
    return {
        "title": title,
        "options": {
            "body": body,
            "data": data,
            "tag": tag,
            "requireInteraction": needs_input,
            "actions": [
                {"action": "hermie.request.allow", "title": "Allow"},
                {"action": "hermie.request.deny", "title": "Deny"},
            ]
            if needs_input
            else [],
        },
    }


def service_worker_tap(shown, action):
    return {"actionIdentifier": action or "default", "data": shown["options"]["data"]}


def test_the_service_worker_turns_an_approval_into_a_notification_with_buttons(tmp_path, monkeypatch):
    sent = web_push_payload(tmp_path, monkeypatch, every_notification()["approval"])
    shown = service_worker_shows(sent)

    assert shown["title"] == "scout"
    assert shown["options"]["body"] == "Needs your approval"
    # The worker tags by `sessionId`, and an approval no longer carries one: the
    # hook names the conversation by its stored key, which is not the runtime
    # id the app compares, so it travels as `sessionKey` and this worker, which
    # does not read that, tags the approval by the bot alone.
    assert shown["options"]["tag"] == "hermie:scout"
    assert shown["options"]["requireInteraction"] is True
    assert [action["action"] for action in shown["options"]["actions"]] == [
        "hermie.request.allow",
        "hermie.request.deny",
    ]

    tap = service_worker_tap(shown, "hermie.request.allow")
    assert tap["actionIdentifier"] == "hermie.request.allow"
    # What the app's `pushTapOf` reads to answer: the bot and the request.
    assert tap["data"]["bot"] == "scout"
    assert tap["data"]["requestId"] == "r1"
    assert "sessionId" not in tap["data"]
    assert tap["data"]["sessionKey"] == "s1"


def test_the_flat_payload_this_plugin_used_to_send_opened_nothing():
    """Why the shape changed: the worker found no bot, no tag and no buttons."""
    data = full_payload(every_notification()["approval"])
    shown = service_worker_shows({**data, "title": "scout", "body": "Needs your approval"})
    assert shown["options"]["tag"] == "hermie"
    assert shown["options"]["actions"] == []
    assert shown["options"]["data"] == {}


def app_repo_service_worker():
    contract_path = app_repo_contract()
    if contract_path is None:
        return None
    root = contract_path.parents[2]
    for relative in ("expo/hermie/public/hermie-push-sw.js", "apps/hermie/public/hermie-push-sw.js"):
        if (root / relative).is_file():
            return root / relative
    return None


NODE_HARNESS = r"""
const fs = require('fs'), vm = require('vm')
const [source, payloadJson, action] = process.argv.slice(1)
const handlers = {}, shown = [], posted = [], waits = []
const self = {
  addEventListener: (type, handler) => { handlers[type] = handler },
  skipWaiting () {},
  clients: { claim () { return Promise.resolve() } },
  registration: {
    scope: 'https://app.example/',
    showNotification: (title, options) => { shown.push({ title, options }); return Promise.resolve() }
  }
}
const clients = {
  matchAll: async () => [{ focus: () => Promise.resolve(), postMessage: message => posted.push(message) }],
  openWindow: async () => {}
}
vm.runInNewContext(fs.readFileSync(source, 'utf8'), { self, clients, encodeURIComponent, JSON })
;(async () => {
  handlers.push({ data: { json: () => JSON.parse(payloadJson) }, waitUntil: p => waits.push(p) })
  await Promise.all(waits)
  handlers.notificationclick({
    action, notification: { data: shown[0].options.data, close () {} }, waitUntil: p => waits.push(p)
  })
  await Promise.all(waits)
  process.stdout.write(JSON.stringify({ shown: shown[0], posted: posted[0] }))
})()
"""


def test_the_real_service_worker_agrees_with_the_port(tmp_path, monkeypatch):
    import shutil
    import subprocess

    source = app_repo_service_worker()
    node = shutil.which("node")
    if source is None or node is None:
        pytest.skip("needs the app repository checked out next to this one, and Node")
    sent = web_push_payload(tmp_path, monkeypatch, every_notification()["approval"])
    result = subprocess.run(
        [node, "-e", NODE_HARNESS, str(source), json.dumps(sent), "hermie.request.allow"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    real = json.loads(result.stdout)
    port = service_worker_shows(sent)
    assert real["shown"] == port
    assert real["posted"] == {"source": "hermie-push", "response": service_worker_tap(port, "hermie.request.allow")}


# -- the relay ---------------------------------------------------------------


def relay_entry(note):
    from hermie_plugin.push import relay
    from hermie_plugin.push.registrations import registration_of

    row = registration_of(
        "i1",
        {
            "v": 1, "transport": "relay", "relay": "https://push.hermie.dev", "handle": "h_1",
            "secret": "s", "platform": "ios", "types": {}, "preview": True, "updatedAt": 1,
        },
    )
    payload = note.payload(preview=row.may_preview, gateway_key="bf796761db84e312", session_kind="canonical")
    title, body = note.rendered(preview=row.may_preview)
    return relay.message_for(row, payload, title=title, body=body, gateway_key="bf796761db84e312")


@pytest.mark.parametrize("name", sorted(every_notification()))
def test_every_relay_message_carries_a_conforming_data_bag_and_no_text(name):
    note = every_notification()[name]
    message = relay_entry(note)["message"]
    assert conformance_problems(message["data"]) == []
    assert "preview" not in message["data"]
    assert (message["title"], message["body"]) == (note.title, note.body)


def test_the_relay_is_told_the_contracts_category_for_an_approval_only():
    for name, note in every_notification().items():
        message = relay_entry(note)["message"]
        if name == "approval":
            assert message["category"] == CONTRACT["category"]["id"]
        else:
            assert "category" not in message, name


@pytest.mark.parametrize("name", ["cron", "cron_done", "cron_failed", "approval"])
def test_a_job_id_is_carried_and_never_shown(name):
    """The contract: `jobId` is carried, never shown — on any transport."""
    note = every_notification()[name]
    assert full_payload(note)["jobId"] == "nightly"
    for preview in (False, True):
        title, body = note.rendered(preview=preview)
        assert "nightly" not in title and "nightly" not in body
