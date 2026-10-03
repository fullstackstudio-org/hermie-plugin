"""The pushes for every kind of request, their clearing, and the security notice.

What each notification SAYS is pinned twice: as the examples in the shared push
contract (`contract/push/contract.json` in the app's repository, copied to
`fixtures/push_contract.json`), which every sender and app generation can read,
and here, where the table below builds the same examples and the test compares
them. A payload that changes shape fails in both places or in neither.

The rest is who is told, and when: the audience rules, the hooks only some
gateways have, and the promise that a gateway without them behaves exactly as
it did before.
"""

import json
from pathlib import Path

import pytest
import yaml

import hermie_plugin
import hermie_plugin.push as push_pkg
from hermie_plugin import contract, uimeta
from hermie_plugin.push import events, expo, relay
from hermie_plugin.push.cron import BY_TASK_ID, Cron
from hermie_plugin.push.registrations import read_sections, registration_of
from hermie_plugin.state import FileStore

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "push_contract.json"
CONTRACT = json.loads(FIXTURE.read_text(encoding="utf-8"))

AT = 1790000000
KEY = "bf796761db84e312"
STORED = "20261003_101500_a1b2c3"
RUNTIME = "8a1b2c3d"
RID = "srq-0123456789ab"
USER = "self-hosted:11111111-2222-3333-4444-555555555555"


# -- the examples ----------------------------------------------------------------


def request(method, **extra):
    return events.from_server_request(
        bot="scout", method=method, request_id=RID, session_id=RUNTIME, session_key=STORED, at=AT, **extra
    )


def confirm(level):
    return events.from_confirm(
        bot="scout", request_id=RID, level=level, session_id=RUNTIME, session_key=STORED, user_id=USER, at=AT
    )


def passkey(change):
    return events.from_passkey_change(
        bot="scout", change=change, user_id=USER, credential={"id": "Y3JlZA", "name": "Pocket", "rp_id": "x.test"}, at=AT
    )


# name -> (notification, preview). Every one is built for the canonical session kind
# and the contract's pinned gateway key.
BUILDERS = {
    "approval": (
        lambda: events.from_approval(
            bot="scout", session_key=STORED, description="rm -rf build", request_id="appr-1", turn_id="t1", at=AT
        ),
        False,
    ),
    "approval_runtime_session": (
        lambda: events.from_approval(
            bot="scout", session_key=STORED, description="rm -rf build", request_id="appr-1", turn_id="t1", at=AT,
            runtime_session_id=RUNTIME,
        ),
        False,
    ),
    "clarify_without_request_id": (
        lambda: events.from_clarify(bot="scout", session_id=STORED, tool_call_id="call-1", question="which one?", at=AT),
        False,
    ),
    "clarify": (lambda: request("clarify"), False),
    "secret": (lambda: request("secret"), True),
    "sudo": (lambda: request("sudo"), True),
    "vault_unlock_prompt": (lambda: request("vault.unlock_prompt"), True),
    "vault_code": (lambda: request("vault.code"), True),
    "vault_save_login": (lambda: request("vault.save_login"), True),
    "confirm_passkey": (lambda: confirm("passkey"), True),
    "confirm_plain": (lambda: confirm("plain"), True),
    "clear_approval_answered": (
        lambda: events.clear_approval(
            bot="scout", session_key=STORED, description="rm -rf build", request_id="appr-1", turn_id="t1",
            choice="once", at=AT,
        ),
        False,
    ),
    "clear_clarify_timeout": (
        lambda: events.clear_clarify(bot="scout", session_id=STORED, tool_call_id="call-1", reason="timeout", at=AT),
        False,
    ),
    "clear_secret_cancelled": (
        lambda: events.clear_server_request(
            bot="scout", method="secret", request_id=RID, reason="cancelled", session_id=RUNTIME,
            session_key=STORED, at=AT, user_id=USER,
        ),
        False,
    ),
    "background_complete": (
        lambda: events.from_background_complete(
            bot="scout", session_id=RUNTIME, session_key=STORED, task_id="bg-7", at=AT
        ),
        False,
    ),
    "security_added": (lambda: passkey("added"), False),
    "security_added_preview": (lambda: passkey("added"), True),
    "security_revoked": (lambda: passkey("revoked"), False),
}


def example_of(name):
    build, preview = BUILDERS[name]
    note = build()
    shown = preview and name.endswith("_preview")
    data = note.payload(preview=shown, gateway_key=KEY, session_kind="canonical" if note.kind_session else "")
    title, body = note.rendered(preview=shown)
    out = {"name": name, "data": data, "title": title, "body": body}
    category = events.category_for(data)
    if category:
        out["category"] = category
    if note.clear:
        out["silent"] = True
    return out


def test_the_contract_carries_every_example_this_plugin_builds():
    published = {entry["name"]: entry for entry in CONTRACT["examples"]["list"]}
    assert set(published) == set(BUILDERS)
    for name in BUILDERS:
        assert published[name] == example_of(name), name


# -- what a request says ------------------------------------------------------------


@pytest.mark.parametrize("method", events.SECURE_INPUT_METHODS)
def test_a_secure_input_says_which_kind_and_nothing_about_it(method):
    note = request(method)
    for preview in (False, True):
        data = note.payload(preview=preview, gateway_key=KEY)
        title, body = note.rendered(preview=preview)
        assert (title, body) == ("scout", events.SECURE_INPUT_BODY[method])
        assert "preview" not in data
        assert data["method"] == method and data["requestId"] == RID and data["type"] == "request"
    assert events.category_for(note.payload(preview=True)) == ""


def test_a_secure_input_without_a_request_id_is_not_sent():
    """A bell for something the app cannot look up is worse than none."""
    for rid in ("", None):
        assert events.from_server_request(
            bot="scout", method="secret", request_id=rid, session_id=RUNTIME, session_key=STORED, at=AT
        ) is None


@pytest.mark.parametrize("method", ["approval", "confirm", "window.read", "tour", "", None, "SECRET"])
def test_only_the_kinds_with_a_sentence_are_raised_from_the_generic_hook(method):
    assert request(method) is None


def test_a_clarify_with_its_id_travels_with_it():
    data = request("clarify").payload(preview=False)
    assert data["method"] == "clarify" and data["requestId"] == RID
    assert events.category_for(data) == ""


@pytest.mark.parametrize("level", ["plain", "passkey"])
def test_a_confirmation_has_no_category_and_no_text_at_either_level(level):
    note = confirm(level)
    data = note.payload(preview=True, gateway_key=KEY)
    assert data["method"] == "confirm" and data["level"] == level and data["requestId"] == RID
    assert "preview" not in data
    assert events.category_for(data) == ""
    # Nothing a button could answer: it only ever opens the app.
    assert set(data) <= {f["key"] for f in CONTRACT["data"]["fields"]}


def test_an_unknown_confirmation_level_is_worded_as_the_strictest():
    data = confirm("biometric").payload(preview=False)
    assert data["level"] == "passkey"
    assert confirm("biometric").body == "Confirm this in the app"


def test_a_confirmation_without_a_request_id_is_not_sent():
    assert events.from_confirm(
        bot="scout", request_id="", level="passkey", session_id=RUNTIME, session_key=STORED, user_id=USER, at=AT
    ) is None


def test_a_passkey_confirmation_bound_to_nobody_is_not_sent_and_a_plain_one_may_be():
    for level, sent in (("passkey", False), ("plain", True), ("biometric", False)):
        note = events.from_confirm(
            bot="scout", request_id=RID, level=level, session_id=RUNTIME, session_key=STORED, user_id="", at=AT
        )
        assert (note is not None) is sent, level


def test_a_request_carries_the_runtime_id_and_its_stored_one_apart():
    data = confirm("passkey").payload(preview=False)
    assert data["sessionId"] == RUNTIME
    assert data["sessionKey"] == STORED


def test_an_approval_never_passes_its_stored_key_off_as_the_runtime_id():
    """The hook names the conversation by its stored key; the app's open approval
    is filed under the live id, so `sessionId` is that or it is absent."""
    data = BUILDERS["approval"][0]().payload(preview=False)
    assert "sessionId" not in data
    assert data["sessionKey"] == STORED
    live = BUILDERS["approval_runtime_session"][0]().payload(preview=False)
    assert live["sessionId"] == RUNTIME and live["sessionKey"] == STORED


def test_a_request_reads_its_sessions_kind_from_the_stored_id():
    note = confirm("passkey")
    assert note.kind_session == STORED
    assert events.from_assistant_message(
        bot="scout", session_id="s9", turn_id="t", assistant_response="hi", at=AT
    ).kind_session == "s9"


# -- background.complete ------------------------------------------------------------


def test_a_finished_background_task_is_a_finished_turn_with_a_marker():
    note = BUILDERS["background_complete"][0]()
    data = note.payload(preview=False)
    assert data["type"] == "turn_done" and data["event"] == "background.complete"
    # The stored id, as for every type but `request`.
    assert data["sessionId"] == STORED and "sessionKey" not in data
    assert note.type in events.TYPES and note.type not in events.NEVER_SUPPRESSED


def test_a_background_task_without_any_session_is_not_sent():
    assert events.from_background_complete(
        bot="scout", session_id="", session_key="", task_id="t", at=AT
    ) is None


# -- the clearing push ----------------------------------------------------------------


def test_a_clear_names_the_notification_it_withdraws():
    raised = BUILDERS["approval"][0]()
    cleared = BUILDERS["clear_approval_answered"][0]()
    assert cleared.extra["replaces"] == raised.event_id
    assert cleared.extra["clear"] is True and cleared.extra["reason"] == "answered"
    assert cleared.extra["requestId"] == "appr-1" and cleared.extra["method"] == "approval"
    assert cleared.event_id != raised.event_id, "the same id would be dropped as already sent"
    assert cleared.clear and events.category_for(cleared.payload(preview=False)) == ""


def test_a_clarify_clear_finds_a_question_that_never_had_an_id():
    raised = BUILDERS["clarify_without_request_id"][0]()
    cleared = BUILDERS["clear_clarify_timeout"][0]()
    assert cleared.extra["replaces"] == raised.event_id
    assert "requestId" not in cleared.extra


def test_a_server_request_and_its_clear_agree_on_the_id():
    raised = request("sudo")
    cleared = events.clear_server_request(
        bot="scout", method="sudo", request_id=RID, reason="answered", session_id=RUNTIME, session_key=STORED, at=AT
    )
    assert cleared.extra["replaces"] == raised.event_id


@pytest.mark.parametrize(
    "choice,reason",
    [
        ("once", "answered"), ("session", "answered"), ("always", "answered"), ("deny", "answered"),
        ("timeout", "timeout"), ("cancelled", "cancelled"), ("notify_failed", "cancelled"),
        ("transport_error", "cancelled"), ("", "cancelled"), (None, "cancelled"),
    ],
)
def test_an_approvals_choice_becomes_one_of_three_reasons(choice, reason):
    assert events.approval_clear_reason(choice) == reason
    assert reason in events.CLEAR_REASONS


def test_a_smart_decision_clears_nothing_because_nothing_was_raised():
    assert events.clear_approval(
        bot="b", session_key="s", description="d", request_id="r", turn_id="t", choice="smart_approve", at=AT
    ) is None


@pytest.mark.parametrize(
    "status,result,reason",
    [
        ("ok", json.dumps({"answer": "blue"}), "answered"),
        ("ok", json.dumps({"answers": {"q1": "a"}, "timed_out": True}), "timeout"),
        ("ok", "not json at all", "answered"),
        ("error", "", "cancelled"),
        (None, None, "answered"),
    ],
)
def test_a_clarifys_end_is_read_from_what_the_tool_call_reported(status, result, reason):
    assert events.clarify_clear_reason(status, result) == reason


@pytest.mark.parametrize("why,reason", [("answered", "answered"), ("resolved", "answered"), ("timeout", "timeout"),
                                       ("interrupted", "cancelled"), ("shutdown", "cancelled"), ("", "cancelled")])
def test_a_gateways_word_for_how_a_request_ended_is_folded_into_three(why, reason):
    note = events.clear_server_request(
        bot="b", method="secret", request_id=RID, reason=why, session_id=RUNTIME, session_key=STORED, at=AT
    )
    assert note.extra["reason"] == reason


def test_a_clear_for_a_request_without_an_id_is_not_sent():
    assert events.clear_server_request(
        bot="b", method="secret", request_id="", reason="answered", session_id=RUNTIME, session_key=STORED, at=AT
    ) is None


# -- the security notice ---------------------------------------------------------------


def test_the_lock_screen_says_that_a_passkey_changed_and_never_which():
    for change, word in (("added", "added"), ("revoked", "removed")):
        note = passkey(change)
        title, body = note.rendered(preview=False)
        assert (title, body) == ("scout", f"A passkey was {word}")
        data = note.payload(preview=False, gateway_key=KEY)
        assert data["type"] == "security" and data["change"] == change
        assert "preview" not in data
        for text in (title, body, json.dumps(data)):
            assert "Pocket" not in text and "Y3JlZA" not in text and "x.test" not in text


def test_the_credentials_name_appears_only_as_preview_text():
    note = passkey("added")
    title, body = note.rendered(preview=True)
    assert title == "scout" and "Pocket" in body and body.endswith("was added")
    assert note.payload(preview=True)["preview"] == body
    assert "Pocket" not in json.dumps(note.payload(preview=False))


def test_a_passkey_change_for_nobody_is_not_sent():
    """A security notice for nobody would be for everybody."""
    assert events.from_passkey_change(
        bot="b", change="added", user_id="", credential={"name": "n"}, at=AT
    ) is None
    assert events.from_passkey_change(bot="b", change="exploded", user_id=USER, credential={}, at=AT) is None


def test_a_passkey_without_a_name_has_no_preview_text():
    note = events.from_passkey_change(bot="b", change="added", user_id=USER, credential={"id": "x"}, at=AT)
    assert note.text == "" and note.rendered(preview=True) == ("b", "A passkey was added")


def test_two_changes_of_one_credential_at_different_times_are_two_facts():
    one = events.from_passkey_change(bot="b", change="added", user_id=USER, credential={"id": "x"}, at=1)
    two = events.from_passkey_change(bot="b", change="added", user_id=USER, credential={"id": "x"}, at=2)
    assert one.event_id != two.event_id


# -- who is told -----------------------------------------------------------------------


def row(**overrides):
    entry = {
        "v": 1, "transport": "expo", "token": "ExponentPushToken[abcdefghijklmnopqrstuv]", "platform": "ios",
        "types": {}, "preview": False, "updatedAt": 1789957143,
    }
    entry.update(overrides)
    return entry


def section(*, rows, mutes=None, seen=None, per_bot=None):
    """`rows` is `[(user id, installation id, row)]`; the legacy bag is user id ''."""
    by_user = {}
    for user_id, installation_id, value in rows:
        by_user.setdefault(user_id, {"v": 1, "push": {"registrations": {}, "seen": {}}})
        by_user[user_id]["push"]["registrations"][installation_id] = value
    for user_id, bag in by_user.items():
        if mutes and user_id in mutes:
            bag["mutes"] = mutes[user_id]
        if seen:
            bag["push"]["seen"].update(seen)
        if per_bot and user_id in per_bot:
            bag["push"]["perBot"] = per_bot[user_id]
    return read_sections(sorted(by_user.items()))


def told(note, sec, *, now=1000.0, enabled=events.TYPES, preview="device"):
    found = events.recipients(
        note, sec, now=now, attached_window_seconds=90, enabled_types=tuple(enabled), gateway_preview=preview,
        retired=lambda *_: False,
    )
    return sorted(registration.installation_id for registration, _ in found)


ALL_ON = {name: True for name in events.TYPES}


def test_a_security_notice_ignores_every_switch_a_mute_and_an_open_chat():
    sec = section(
        rows=[(USER, "phone", row(types={})), (USER, "tablet", row(types={name: False for name in events.TYPES}))],
        mutes={USER: {"scout": 0}},
        seen={"phone": {"bot": "scout", "at": 999}},
    )
    note = passkey("added")
    # Not one of the switches is on, the bot is muted for ever, and one device is
    # reading this very chat — and it is still told, on both.
    assert told(note, sec, enabled=()) == ["phone", "tablet"]


def test_a_security_notice_reaches_only_the_devices_of_that_person():
    sec = section(
        rows=[
            (USER, "mine", row()),
            ("self-hosted:someone-else", "theirs", row()),
            ("", "legacy", row()),
        ]
    )
    assert told(passkey("added"), sec) == ["mine"]


def test_a_security_notice_still_respects_a_retired_device_and_the_preview_ceiling():
    sec = section(rows=[(USER, "phone", row(preview=True))])
    found = events.recipients(
        passkey("added"), sec, now=1.0, attached_window_seconds=90, enabled_types=(), gateway_preview="never",
        retired=lambda *_: False,
    )
    assert [(r.installation_id, preview) for r, preview in found] == [("phone", False)]
    assert events.recipients(
        passkey("added"), sec, now=1.0, attached_window_seconds=90, enabled_types=(), gateway_preview="device",
        retired=lambda installation_id, _: installation_id == "phone",
    ) == []


def test_a_passkey_level_confirmation_is_for_the_bound_person_alone():
    sec = section(
        rows=[
            (USER, "mine", row(types=ALL_ON)),
            ("self-hosted:someone-else", "theirs", row(types=ALL_ON)),
            ("", "legacy", row(types=ALL_ON)),
        ]
    )
    assert told(confirm("passkey"), sec) == ["mine"]


def test_a_plain_confirmation_still_reaches_a_device_that_names_nobody():
    sec = section(
        rows=[
            (USER, "mine", row(types=ALL_ON)),
            ("self-hosted:someone-else", "theirs", row(types=ALL_ON)),
            ("", "legacy", row(types=ALL_ON)),
        ]
    )
    assert told(confirm("plain"), sec) == ["legacy", "mine"]


def test_a_confirmation_is_a_request_so_the_request_switch_and_a_mute_still_apply():
    sec = section(
        rows=[(USER, "on", row(types=ALL_ON)), (USER, "off", row(types={**ALL_ON, "request": False}))]
    )
    assert told(confirm("passkey"), sec) == ["on"]
    muted = section(rows=[(USER, "on", row(types=ALL_ON))], mutes={USER: {"scout": 0}})
    assert told(confirm("passkey"), muted) == []


def test_a_clearing_push_goes_only_to_a_device_that_asked_for_one():
    sec = section(
        rows=[(USER, "new", row(types=ALL_ON, clears=True)), (USER, "old", row(types=ALL_ON))]
    )
    assert told(BUILDERS["clear_approval_answered"][0](), sec) == ["new"]
    assert told(BUILDERS["approval"][0](), sec) == ["new", "old"]


def test_a_row_says_it_clears_only_with_a_true_boolean():
    for value in ("true", 1, "yes", None):
        parsed = registration_of("i", row(clears=value))
        assert parsed is not None and parsed.clears is False
    assert registration_of("i", row(clears=True)).clears is True
    assert registration_of("i", row()).clears is False


def test_a_background_task_follows_the_finished_turn_switch_and_an_open_chat():
    note = BUILDERS["background_complete"][0]()
    sec = section(
        rows=[(USER, "off", row(types=ALL_ON)), (USER, "off2", row(types={**ALL_ON, "turn_done": False}))],
        seen={"off": {"bot": "scout", "at": 999}},
    )
    assert told(note, sec) == []
    sec = section(rows=[(USER, "on", row(types=ALL_ON)), (USER, "off", row(types={**ALL_ON, "turn_done": False}))])
    assert told(note, sec) == ["on"]


# -- delivery -----------------------------------------------------------------------------


def module_with(tmp_path, monkeypatch, rows, *, settings=None):
    import yaml as _yaml

    home = tmp_path / "hermes"
    home.mkdir(exist_ok=True)
    bags = {}
    for user_id, installation_id, value in rows:
        bags.setdefault(user_id, {"v": 1, "push": {"registrations": {}}})["push"]["registrations"][
            installation_id
        ] = value
    meta = {(f"hermie-app:{user_id}" if user_id else "hermie-app"): bag for user_id, bag in bags.items()}
    (home / "profile.yaml").write_text(_yaml.safe_dump({"ui_meta": meta}))
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    class Ctx:
        profile_name = "scout"
        state = None

        def get_config(self, key, default=None):
            return (settings or {}).get(key, default)

    module = push_pkg.PushModule(hermie_plugin.Runtime(Ctx(), home=home, store=FileStore(tmp_path / "state.json")))
    return module


def capture_expo(monkeypatch):
    sent = []
    monkeypatch.setattr(
        expo, "send",
        lambda batch: sent.extend(batch) or [expo.Ticket(token=m["to"], status="ok", receipt_id="r") for m in batch],
    )
    return sent


def test_an_expo_clear_is_a_silent_data_message(tmp_path, monkeypatch):
    module = module_with(tmp_path, monkeypatch, [(USER, "p", row(types=ALL_ON, clears=True))])
    sent = capture_expo(monkeypatch)
    assert module.deliver(BUILDERS["clear_approval_answered"][0]()) == 1
    message = sent[0]
    assert set(message) == {"to", "data", "_contentAvailable", "priority", "ttl"}
    assert message["_contentAvailable"] is True
    assert message["data"]["clear"] is True and message["data"]["requestId"] == "appr-1"
    assert "title" not in message and "body" not in message and "sound" not in message
    assert "categoryId" not in message


def test_a_relay_row_gets_no_clear_until_the_relay_can_carry_one(tmp_path, monkeypatch):
    relay_row = {
        "v": 1, "transport": "relay", "relay": "https://push.hermie.dev", "handle": "h_1", "secret": "s",
        "platform": "ios", "types": ALL_ON, "clears": True, "updatedAt": 1,
    }
    module = module_with(tmp_path, monkeypatch, [(USER, "p", relay_row)])
    posted = []
    monkeypatch.setattr(relay, "send", lambda origin, entries, **kw: posted.append(entries) or [])
    assert relay.CAN_CLEAR is False
    assert module.deliver(BUILDERS["clear_approval_answered"][0]()) == 0
    assert posted == []
    # ... while the request it would clear does reach it.
    module.deliver(BUILDERS["approval"][0]())
    assert len(posted) == 1


def test_a_relay_row_is_sent_the_secure_input_with_no_category_and_no_text(tmp_path, monkeypatch):
    relay_row = {
        "v": 1, "transport": "relay", "relay": "https://push.hermie.dev", "handle": "h_1", "secret": "s",
        "platform": "ios", "types": ALL_ON, "preview": True, "updatedAt": 1,
    }
    module = module_with(tmp_path, monkeypatch, [(USER, "p", relay_row)])
    posted = []
    monkeypatch.setattr(
        relay, "send",
        lambda origin, entries, **kw: posted.extend(entries) or [relay.Outcome(status=relay.SENT) for _ in entries],
    )
    module.deliver(request("secret"))
    message = posted[0]["message"]
    assert "category" not in message
    assert (message["title"], message["body"]) == ("scout", "Needs a secret")
    assert message["data"]["method"] == "secret" and "preview" not in message["data"]
    # The conversation stack and the collapse id are the relay's existing ones.
    assert message["collapseId"] == request("secret").event_id


def test_a_secure_input_never_carries_text_even_to_a_device_that_asked_for_previews(tmp_path, monkeypatch):
    module = module_with(tmp_path, monkeypatch, [(USER, "p", row(types=ALL_ON, preview=True))])
    sent = capture_expo(monkeypatch)
    module.deliver(request("sudo"))
    assert "preview" not in sent[0]["data"]
    assert sent[0]["body"] == "Needs your password"
    assert "categoryId" not in sent[0]


def test_the_security_notice_reaches_the_device_whatever_the_gateways_type_list_says(tmp_path, monkeypatch):
    module = module_with(
        tmp_path, monkeypatch, [(USER, "p", row(types={}))], settings={"push.types": ["message"]}
    )
    sent = capture_expo(monkeypatch)
    assert module.deliver(passkey("added")) == 1
    assert sent[0]["data"]["type"] == "security" and sent[0]["body"] == "A passkey was added"
    assert sent[0]["channelId"] == "security"


def test_a_kind_is_read_from_the_stored_id_of_a_request(tmp_path, monkeypatch):
    module = module_with(tmp_path, monkeypatch, [(USER, "p", row(types=ALL_ON))])
    sent = capture_expo(monkeypatch)
    asked = []
    monkeypatch.setattr(push_pkg.sessions, "kind_for", lambda session_id, **kw: asked.append(session_id) or "branch")
    module.deliver(confirm("passkey"))
    assert asked == [STORED]
    assert sent[0]["data"]["sessionKind"] == "branch" and sent[0]["data"]["sessionId"] == RUNTIME


# -- the hooks only some gateways have ---------------------------------------------------


class Ctx:
    """What `register` needs of a gateway, recording the hooks it is given."""

    profile_name = "scout"

    def __init__(self, tmp_path):
        self.hooks = {}
        self.unloads = []
        self.state = None

    def get_config(self, key, default=None):
        return default

    def register_hook(self, name, callback):
        self.hooks.setdefault(name, []).append(callback)

    def on_unload(self, callback):
        self.unloads.append(callback)


CORE_HOOKS = {
    "post_llm_call", "on_session_end", "pre_approval_request", "post_approval_response", "pre_tool_call",
    "post_tool_call",
}


def registered(tmp_path, monkeypatch, known):
    home = tmp_path / "hermes"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)
    monkeypatch.setattr(push_pkg, "known_hooks", lambda: known)
    ctx = Ctx(tmp_path)
    runtime = hermie_plugin.Runtime(ctx, home=home, store=FileStore(tmp_path / "state.json"))
    module = push_pkg.register(ctx, runtime)
    built = []
    monkeypatch.setattr(module, "offer", lambda note, delay=False: note is not None and built.append(note))
    return ctx, module, built


def test_an_upstream_gateway_that_has_none_of_the_optional_hooks_gets_only_the_ones_it_knows(tmp_path, monkeypatch):
    # The list a gateway without the fork's hooks would hold: everything the
    # plugin has always used, and none of the five.
    ctx, module, _ = registered(tmp_path, monkeypatch, CORE_HOOKS | {"pre_llm_call"})
    assert set(ctx.hooks) == CORE_HOOKS
    assert module.optional_hooks == set()
    for hook in push_pkg.OPTIONAL_HOOKS:
        assert hook not in ctx.hooks
    # ... and it claims none of what those hooks would have made true.
    capabilities = module.capabilities()
    for name in (
        contract.CAP_PUSH_CONFIRM, contract.CAP_PUSH_SECURITY, contract.CAP_PUSH_SECURE_INPUT,
        contract.CAP_PUSH_BACKGROUND,
    ):
        assert name not in capabilities


def test_a_gateway_that_cannot_be_asked_registers_nothing_optional(tmp_path, monkeypatch):
    ctx, module, _ = registered(tmp_path, monkeypatch, None)
    assert set(ctx.hooks) == CORE_HOOKS and module.optional_hooks == set()


def test_the_fork_gets_the_two_hooks_it_fires_and_claims_what_they_make_true(tmp_path, monkeypatch):
    ctx, module, _ = registered(tmp_path, monkeypatch, CORE_HOOKS | {"pre_confirm_request", "on_passkey_change"})
    assert set(ctx.hooks) == CORE_HOOKS | {"pre_confirm_request", "on_passkey_change"}
    capabilities = module.capabilities()
    assert contract.CAP_PUSH_CONFIRM in capabilities and contract.CAP_PUSH_SECURITY in capabilities
    assert contract.CAP_PUSH_SECURE_INPUT not in capabilities and contract.CAP_PUSH_BACKGROUND not in capabilities


def test_clearing_is_claimed_for_expo_and_web_push_and_not_yet_for_the_relay(tmp_path, monkeypatch):
    _, module, _ = registered(tmp_path, monkeypatch, set())
    capabilities = module.capabilities()
    assert contract.CAP_PUSH_CLEAR in capabilities
    assert contract.CAP_PUSH_CLEAR_RELAY not in capabilities
    monkeypatch.setattr(relay, "CAN_CLEAR", True)
    assert contract.CAP_PUSH_CLEAR_RELAY in module.capabilities()


def test_the_manifest_declares_the_optional_hooks_apart_from_the_ones_every_gateway_has():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    declared = set(manifest["provides_hooks"])
    optional = manifest["optional_hooks"]
    assert CORE_HOOKS | {"pre_llm_call"} == declared
    # Exactly the hooks the module registers conditionally, each named once, and
    # none of them also in the list a gateway without the key would hold the
    # plugin to.
    assert sorted(optional) == sorted(push_pkg.OPTIONAL_HOOKS)
    assert not declared & set(optional)


def all_hooks(tmp_path, monkeypatch):
    return registered(tmp_path, monkeypatch, CORE_HOOKS | set(push_pkg.OPTIONAL_HOOKS))


def test_pre_confirm_request_raises_a_confirm_push(tmp_path, monkeypatch):
    ctx, _, built = all_hooks(tmp_path, monkeypatch)
    ctx.hooks["pre_confirm_request"][0](
        session_id=RUNTIME, session_key=STORED, request_id=RID, level="passkey", user_id=USER,
        expires_at=AT + 120, reached=2,
    )
    [note] = built
    assert (note.type, note.extra["method"], note.extra["level"], note.user_id) == ("request", "confirm", "passkey", USER)
    assert note.session_id == RUNTIME and note.session_key == STORED and note.user_strict


def test_on_passkey_change_raises_a_security_push(tmp_path, monkeypatch):
    ctx, _, built = all_hooks(tmp_path, monkeypatch)
    ctx.hooks["on_passkey_change"][0](
        change="revoked", user_id=USER, credential={"id": "i", "name": "Pocket", "rp_id": "x"}, at=AT, via="operator"
    )
    [note] = built
    assert (note.type, note.extra["change"], note.body, note.at) == ("security", "revoked", "A passkey was removed", AT)
    assert "operator" not in json.dumps(note.payload(preview=True))


def test_a_hook_that_names_nobody_or_nothing_raises_nothing(tmp_path, monkeypatch):
    ctx, _, built = all_hooks(tmp_path, monkeypatch)
    ctx.hooks["on_passkey_change"][0](change="added", user_id="", credential={}, at=AT, via="operator")
    ctx.hooks["pre_confirm_request"][0](session_id=RUNTIME, session_key=STORED, request_id="", level="plain")
    ctx.hooks["pre_server_request"][0](method="secret", request_id="", session_id=RUNTIME)
    ctx.hooks["on_background_complete"][0](session_id="", session_key="", task_id="t")
    assert built == []


def test_the_generic_request_hooks_raise_and_withdraw(tmp_path, monkeypatch):
    ctx, _, built = all_hooks(tmp_path, monkeypatch)
    ctx.hooks["pre_server_request"][0](
        method="vault.code", request_id=RID, session_id=RUNTIME, session_key=STORED, user_id=USER, expires_at=AT + 180,
        reached=1,
    )
    ctx.hooks["post_server_request"][0](
        method="vault.code", request_id=RID, session_id=RUNTIME, session_key=STORED, user_id=USER, reason="timeout"
    )
    raised, cleared = built
    assert raised.extra["method"] == "vault.code" and not raised.clear
    assert cleared.clear and cleared.extra["reason"] == "timeout" and cleared.extra["replaces"] == raised.event_id


def test_a_background_task_is_raised_with_the_graces_delay(tmp_path, monkeypatch):
    ctx, module, _ = all_hooks(tmp_path, monkeypatch)
    delays = []
    monkeypatch.setattr(module, "offer", lambda note, delay=False: note is not None and delays.append((note.type, delay)))
    ctx.hooks["on_background_complete"][0](session_id=RUNTIME, session_key=STORED, task_id="bg-1")
    assert delays == [("turn_done", True)]


def test_an_approval_and_its_answer_agree_on_the_notification(tmp_path, monkeypatch):
    ctx, _, built = registered(tmp_path, monkeypatch, set())
    common = dict(
        surface="gateway", session_key=STORED, description="remove the build", request_id="appr-1", turn_id="t1",
        tool_call_id="call-1",
    )
    ctx.hooks["pre_approval_request"][0](**common)
    ctx.hooks["post_approval_response"][0](**common, choice="once")
    raised, cleared = built
    assert cleared.extra["replaces"] == raised.event_id and cleared.extra["reason"] == "answered"


def test_a_smart_approval_and_a_coalesced_follower_clear_nothing(tmp_path, monkeypatch):
    ctx, _, built = registered(tmp_path, monkeypatch, set())
    common = dict(session_key=STORED, description="d", request_id="r", turn_id="t")
    ctx.hooks["post_approval_response"][0](**common, surface="smart", choice="smart_approve", decided_by="aux_llm")
    ctx.hooks["post_approval_response"][0](**common, surface="gateway", choice="once", coalesced=True)
    assert built == []


def test_a_clarify_and_its_end_agree_on_the_notification(tmp_path, monkeypatch):
    ctx, _, built = registered(tmp_path, monkeypatch, set())
    common = dict(tool_name="clarify", session_id=STORED, tool_call_id="call-1")
    ctx.hooks["pre_tool_call"][0](**common, args={"question": "which?"})
    ctx.hooks["post_tool_call"][0](**common, args={}, status="ok", result=json.dumps({"answer": "a"}))
    ctx.hooks["post_tool_call"][0](tool_name="terminal", session_id=STORED, tool_call_id="x", status="ok", result="")
    raised, cleared = built
    assert cleared.extra["replaces"] == raised.event_id and cleared.extra["reason"] == "answered"
    assert raised.session_key == STORED and raised.session_id == ""


def test_a_gateway_that_only_names_the_hook_still_has_its_clarify_announced(tmp_path, monkeypatch):
    ctx, _, built = all_hooks(tmp_path, monkeypatch)
    ctx.hooks["pre_tool_call"][0](tool_name="clarify", session_id=STORED, tool_call_id="call-1", args={"question": "?"})
    assert [note.extra["method"] for note in built] == ["clarify"] and "requestId" not in built[0].extra


def test_once_the_gateway_has_reported_a_server_request_the_clarify_is_raised_once(tmp_path, monkeypatch):
    ctx, _, built = all_hooks(tmp_path, monkeypatch)
    ctx.hooks["pre_server_request"][0](method="secret", request_id="srq-000000000001", session_id=RUNTIME)
    built.clear()
    common = dict(tool_name="clarify", session_id=STORED, tool_call_id="call-1")
    ctx.hooks["pre_tool_call"][0](**common, args={"question": "which?"})
    ctx.hooks["post_tool_call"][0](**common, status="ok", result="{}")
    assert built == []
    ctx.hooks["pre_server_request"][0](method="clarify", request_id=RID, session_id=RUNTIME, session_key=STORED)
    assert [note.extra.get("requestId") for note in built] == [RID]


def test_a_clarify_raised_from_the_tool_hook_is_cleared_from_it_even_after_the_gateway_is_heard(tmp_path, monkeypatch):
    """The first question of a process can arrive before the first server request."""
    ctx, _, built = all_hooks(tmp_path, monkeypatch)
    common = dict(tool_name="clarify", session_id=STORED, tool_call_id="call-1")
    ctx.hooks["pre_tool_call"][0](**common, args={"question": "which?"})
    ctx.hooks["pre_server_request"][0](method="clarify", request_id=RID, session_id=RUNTIME, session_key=STORED)
    ctx.hooks["post_tool_call"][0](**common, status="ok", result="{}")
    raised, _, cleared = built
    assert cleared.clear and cleared.extra["replaces"] == raised.event_id


def test_two_approvals_in_one_turn_are_two_notifications_and_two_clears(tmp_path, monkeypatch):
    """The gateway's approval hooks carry no request id, and a turn id is shared."""
    ctx, _, built = registered(tmp_path, monkeypatch, set())
    one = dict(surface="gateway", session_key=STORED, description="rm a", turn_id="t1", tool_call_id="call-1")
    two = dict(surface="gateway", session_key=STORED, description="rm b", turn_id="t1", tool_call_id="call-2")
    for kwargs in (one, two):
        ctx.hooks["pre_approval_request"][0](**kwargs)
    ctx.hooks["post_approval_response"][0](**one, choice="once")
    first, second, cleared = built
    assert first.event_id != second.event_id
    assert cleared.extra["replaces"] == first.event_id != second.event_id


def test_two_approvals_in_one_call_stay_apart_by_what_they_ask():
    common = dict(bot="b", session_key="s", request_id="", turn_id="t", tool_call_id="c", at=1)
    one = events.from_approval(description="rm a", **common)
    two = events.from_approval(description="rm b", **common)
    assert one.event_id != two.event_id


def test_a_request_id_is_the_identity_when_the_hook_has_one():
    common = dict(bot="b", session_key="s", turn_id="t", tool_call_id="c", description="d", at=1)
    one = events.from_approval(request_id="r1", **common)
    assert one.event_id == events.from_approval(request_id="r1", **{**common, "tool_call_id": "other"}).event_id
    assert one.event_id != events.from_approval(request_id="r2", **common).event_id


def test_a_confirmations_clear_is_as_strict_as_its_raise(tmp_path, monkeypatch):
    ctx, _, built = all_hooks(tmp_path, monkeypatch)
    for rid, level in (("srq-000000000001", "passkey"), ("srq-000000000002", "plain")):
        ctx.hooks["pre_confirm_request"][0](
            session_id=RUNTIME, session_key=STORED, request_id=rid, level=level, user_id=USER
        )
        ctx.hooks["post_server_request"][0](
            method="confirm", request_id=rid, session_id=RUNTIME, session_key=STORED, reason="answered"
        )
    passkey_clear, plain_clear = built[1], built[3]
    assert passkey_clear.user_strict and passkey_clear.user_id == USER
    assert not plain_clear.user_strict and plain_clear.user_id == USER


# -- Web Push and request methods ------------------------------------------------------


def webpush_row(**overrides):
    entry = {
        "v": 1, "transport": "webpush", "endpoint": "https://push.example/x", "keys": {"p256dh": "a", "auth": "b"},
        "platform": "web", "types": ALL_ON, "updatedAt": 1,
    }
    entry.update(overrides)
    return entry


def test_a_web_push_row_gets_a_buttonless_request_only_if_its_worker_reads_methods():
    sec = section(rows=[(USER, "old", webpush_row()), (USER, "new", webpush_row(requestMethods=True))])
    for note in (confirm("plain"), request("secret"), request("vault.code")):
        assert told(note, sec) == ["new"], note.extra["method"]
    # An approval, a clarify and every other transport are unchanged.
    assert told(BUILDERS["approval"][0](), sec) == ["new", "old"]
    assert told(request("clarify"), sec) == ["new", "old"]
    assert told(confirm("plain"), section(rows=[(USER, "phone", row(types=ALL_ON))])) == ["phone"]


def test_a_row_says_its_worker_reads_methods_only_with_a_true_boolean():
    for value in ("true", 1, None):
        assert registration_of("i", webpush_row(requestMethods=value)).request_methods is False
    assert registration_of("i", webpush_row(requestMethods=True)).request_methods is True


def test_a_web_push_clear_has_no_title_and_no_body(tmp_path, monkeypatch):
    module = module_with(tmp_path, monkeypatch, [(USER, "w", webpush_row(clears=True))])
    sent = []
    monkeypatch.setattr(module, "vapid_key", lambda: object())
    monkeypatch.setattr(
        push_pkg.webpush, "send",
        lambda key, endpoint, p256dh, auth, payload, contact="": sent.append(payload)
        or push_pkg.webpush.Result(status=201),
    )
    assert module.deliver(BUILDERS["clear_approval_answered"][0]()) == 1
    assert set(sent[0]) == {"data"} and sent[0]["data"]["clear"] is True


def test_every_hook_this_module_registers_accepts_whatever_the_gateway_adds(tmp_path, monkeypatch):
    ctx, _, _ = all_hooks(tmp_path, monkeypatch)
    for name, callbacks in ctx.hooks.items():
        if name in ("post_llm_call", "on_session_end"):
            continue
        for callback in callbacks:
            callback(unheard_of_field=1)  # must neither raise nor misread


def test_every_hook_the_module_registers_is_declared_in_one_list_or_the_other(tmp_path, monkeypatch):
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    ctx, _, _ = all_hooks(tmp_path, monkeypatch)
    assert set(ctx.hooks) == set(manifest["provides_hooks"]) - {"pre_llm_call"} | set(manifest["optional_hooks"])
