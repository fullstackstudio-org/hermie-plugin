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

# Fields the data bag carries that the contract's field list does not describe:
# the envelope (shape version, the second, the dedupe id) that every sender has
# always put on the wire. `preview` is the opt-in text, checked separately.
ENVELOPE = {"v", "at", "eventId"}


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
        when = field.get("requiredWhen")
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
        expected = {"string": str, "boolean": bool}[field["type"]]
        if not isinstance(value, expected):
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


# A clarify question's request id is minted inside the gateway's blocking
# prompt and is never visible to a hook (see `events.from_clarify`), so that one
# notification cannot carry the `requestId` the contract asks of every
# `request`. The app opens the chat and finds the open question itself.
KNOWN_GAPS = {"clarify": ("requestId",)}


@pytest.mark.parametrize("name", sorted(every_notification()))
def test_every_payload_conforms(name):
    note = every_notification()[name]
    data = full_payload(note)
    assert conformance_problems(data, allow_missing=KNOWN_GAPS.get(name, ())) == []


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


def test_web_push_carries_the_payload_where_the_service_worker_reads_it(tmp_path, monkeypatch):
    """`{title, body, data}` — the shape the app's service worker opens."""
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
        "types": {"request": True}, "updatedAt": 1,
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
    assert module.deliver(every_notification()["approval"]) == 1

    assert set(sent[0]) == {"title", "body", "data"}
    assert sent[0]["title"] == "scout"
    assert sent[0]["data"]["type"] == "request"
    assert sent[0]["data"]["requestId"] == "r1"
    assert conformance_problems(sent[0]["data"]) == []


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
    assert conformance_problems(message["data"], allow_missing=KNOWN_GAPS.get(name, ())) == []
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
