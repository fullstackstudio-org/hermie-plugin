"""The push relay: which rows are read, where they are sent, and what an answer does."""

import http.server
import json
import logging
import threading

import pytest
import yaml

import hermie_plugin
import hermie_plugin.push as push_pkg
from hermie_plugin import contract, uimeta
from hermie_plugin.push import events, relay
from hermie_plugin.push.registrations import read_section, registration_of, relay_origin
from hermie_plugin.state import FileStore

SECRET = "c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0LXNlY3I"
HANDLE = "h_3fJ0abcdefghijklmnop"


def relay_row(**overrides):
    entry = {
        "v": 1,
        "transport": "relay",
        "relay": "https://push.hermie.dev",
        "handle": HANDLE,
        "secret": SECRET,
        "platform": "ios",
        "types": {name: True for name in events.TYPES},
        "preview": False,
        "gatewayKey": "bf796761db84e312",
        "updatedAt": 1789957143,
    }
    entry.update(overrides)
    return {key: value for key, value in entry.items() if value is not None}


def expo_row(**overrides):
    entry = {
        "v": 1,
        "transport": "expo",
        "token": "ExponentPushToken[abcdefghijklmnopqrstuv]",
        "platform": "android",
        "types": {name: True for name in events.TYPES},
        "preview": False,
        "updatedAt": 1789957143,
    }
    entry.update(overrides)
    return entry


# -- reading a relay row -----------------------------------------------------


def test_a_good_relay_row_reads():
    parsed = registration_of("i1", relay_row())
    assert parsed is not None
    assert parsed.transport == "relay"
    assert parsed.relay == "https://push.hermie.dev"
    assert parsed.handle == HANDLE
    assert parsed.secret == SECRET
    assert parsed.platform == "ios"
    assert parsed.gateway_key == "bf796761db84e312"


def test_a_mac_registers_with_the_relay_too():
    assert registration_of("i1", relay_row(platform="macos")) is not None


@pytest.mark.parametrize("platform", ["android", "web", "unknown", "", None])
def test_only_apple_platforms_register_with_the_relay(platform):
    assert registration_of("i1", relay_row(platform=platform)) is None


def test_an_unknown_version_is_dropped():
    assert registration_of("i1", relay_row(v=2)) is None


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "push.hermie.dev",
        "http://push.hermie.dev",
        "wss://push.hermie.dev",
        "https://push.hermie.dev/v1/send",
        "https://push.hermie.dev/?to=elsewhere",
        "https://push.hermie.dev/#x",
        "https://user@push.hermie.dev",
        "https://user:pass@push.hermie.dev",
        "https://push.hermie.dev:notaport",
        "https://",
        42,
        ["https://push.hermie.dev"],
    ],
)
def test_the_relay_must_be_exactly_an_https_origin(value):
    assert registration_of("i1", relay_row(relay=value)) is None


def test_an_origin_is_read_the_way_a_browser_writes_it():
    assert relay_origin("https://PUSH.hermie.dev") == "https://push.hermie.dev"
    assert relay_origin("https://push.hermie.dev:443/") == "https://push.hermie.dev"
    assert relay_origin("https://relay.example.org:8443") == "https://relay.example.org:8443"


@pytest.mark.parametrize("field", ["handle", "secret"])
@pytest.mark.parametrize("value", [None, "", "   ", 7, True, {"a": 1}, "x" * 513, " padded "])
def test_the_handle_and_secret_are_non_empty_strings(field, value):
    assert registration_of("i1", relay_row(**{field: value})) is None


def test_a_row_never_carries_both_a_token_and_a_handle():
    assert registration_of("i1", relay_row(token="ExponentPushToken[abc]")) is None
    assert registration_of("i1", relay_row(endpoint="https://push.example/x")) is None
    assert registration_of("i1", expo_row(handle=HANDLE)) is None


def test_an_unknown_transport_is_still_dropped():
    assert registration_of("i1", relay_row(transport="apns")) is None


def test_an_encryption_key_is_carried_but_does_not_unlock_text():
    parsed = registration_of(
        "i1", relay_row(preview=True, enc={"alg": "chacha20-poly1305", "key": "k", "kid": 1})
    )
    assert parsed is not None
    assert parsed.enc == {"alg": "chacha20-poly1305", "key": "k", "kid": 1}
    assert parsed.preview is True  # what the row says
    assert parsed.may_preview is False  # what the gateway will do


def test_the_send_capability_stays_out_of_a_log_line():
    parsed = registration_of("i1", relay_row())
    assert SECRET not in repr(parsed)


def test_one_bad_relay_row_costs_only_itself():
    section = read_section(
        {"push": {"registrations": {"good": relay_row(), "bad": relay_row(relay="http://evil.example")}}}
    )
    assert [entry.installation_id for entry in section.registrations] == ["good"]


# -- the allow-list ----------------------------------------------------------


def test_the_default_allow_list_is_exactly_the_hermie_relay():
    allowed, refused = relay.allowed_origins(None)
    assert allowed == ("https://push.hermie.dev",)
    assert refused == ()


def test_a_configured_list_replaces_the_default():
    allowed, refused = relay.allowed_origins(["https://relay.example.org", "http://plain.example.org", "nope"])
    assert allowed == ("https://relay.example.org",)
    assert refused == ("http://plain.example.org", "nope")


def test_one_origin_may_be_configured_as_a_string():
    assert relay.allowed_origins("https://relay.example.org/")[0] == ("https://relay.example.org",)


def test_an_empty_list_serves_no_relay():
    assert relay.allowed_origins([])[0] == ()


# -- a gateway with relay rows -----------------------------------------------


def gateway(tmp_path, monkeypatch, rows, settings=None, extra_meta=None):
    home = tmp_path / "hermes"
    home.mkdir(parents=True, exist_ok=True)
    bag = {"v": 1, "push": {"registrations": rows}}
    bag.update(extra_meta or {})
    (home / "profile.yaml").write_text(yaml.safe_dump({"ui_meta": {"hermie-app:u1": bag}}))
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    class Ctx:
        profile_name = "scout"

        def get_config(self, key, default=None):
            return (settings or {}).get(key, default)

    runtime = hermie_plugin.Runtime(Ctx(), home=home, store=FileStore(tmp_path / "state.json"))
    return home, push_pkg.PushModule(runtime)


class FakeRelay:
    """Stands in for `relay._post`: records every request and answers per handle."""

    def __init__(self, answers=None, *, fail_first=0, status=200):
        self.requests = []
        self.answers = answers or {}
        self.fail_first = fail_first
        self.status = status

    def __call__(self, url, body):
        self.requests.append((url, json.loads(json.dumps(body))))
        if self.fail_first:
            self.fail_first -= 1
            raise OSError("connection reset")
        if self.status != 200:
            return relay.Reply(status=self.status, retry_after=7)
        results = []
        for message in body["messages"]:
            answer = self.answers.get(message["handle"], "sent")
            answers = answer if isinstance(answer, list) else [answer]
            current = answers.pop(0) if len(answers) > 1 else answers[0]
            if isinstance(answer, list) and len(answer) > 1:
                self.answers[message["handle"]] = answers
            row = {"handle": message["handle"], "status": current}
            if current in ("retry", "limited"):
                row["retryAfter"] = 3
            if current == "rejected":
                row["reason"] = "payload_too_large"
            results.append(row)
        return relay.Reply(status=200, body={"results": results})


REAL_SEND = relay.send


def wired(monkeypatch, fake, sleeps=None):
    """Route the module's relay sends through *fake*, recording every wait."""
    real_send = REAL_SEND
    record = sleeps if sleeps is not None else []
    monkeypatch.setattr(
        push_pkg.relay, "send",
        lambda origin, entries: real_send(origin, entries, post=fake, sleep=record.append),
    )
    return record


def approval(request_id="r1"):
    return events.from_approval(
        bot="scout", session_key="s1", description="delete the build directory",
        request_id=request_id, turn_id="t1", at=10,
    )


def test_a_relay_row_on_the_allow_list_is_sent(tmp_path, monkeypatch):
    _, module = gateway(tmp_path, monkeypatch, {"i1": relay_row()})
    fake = FakeRelay()
    wired(monkeypatch, fake)

    assert module.deliver(approval()) == 1
    (url, body), = fake.requests
    assert url == "https://push.hermie.dev/v1/send"
    assert body["v"] == 1
    entry, = body["messages"]
    assert entry["handle"] == HANDLE
    assert entry["secret"] == SECRET
    message = entry["message"]
    assert message["title"] == "scout"
    assert message["body"] == "Needs your approval"
    assert message["category"] == "hermie.request"
    assert message["thread"] == "bf796761db84e312:scout"
    assert message["collapseId"] == message["data"]["eventId"]
    assert len(message["collapseId"].encode()) <= 64
    assert message["priority"] == "high"
    assert message["ttl"] == 3600
    assert message["data"]["requestId"] == "r1"
    assert message["data"]["gatewayKey"] == "bf796761db84e312"


def test_a_row_naming_another_relay_is_ignored_and_reported_once(tmp_path, monkeypatch, caplog):
    _, module = gateway(
        tmp_path, monkeypatch,
        {"i1": relay_row(relay="https://evil.example"), "i2": relay_row(handle="h_other_device_0001")},
    )
    fake = FakeRelay()
    wired(monkeypatch, fake)

    with caplog.at_level(logging.WARNING):
        assert module.deliver(approval("r1")) == 1
        assert module.deliver(approval("r2")) == 1

    assert {url for url, _ in fake.requests} == {"https://push.hermie.dev/v1/send"}
    assert all(m["handle"] == "h_other_device_0001" for _, body in fake.requests for m in body["messages"])
    reports = [record for record in caplog.records if "https://evil.example" in record.getMessage()]
    assert len(reports) == 1


def test_an_operator_may_name_their_own_relay(tmp_path, monkeypatch):
    _, module = gateway(
        tmp_path, monkeypatch,
        {"i1": relay_row(relay="https://relay.example.org"), "i2": relay_row(handle="h_default_relay_0001")},
        settings={"push.relay_origins": ["https://relay.example.org"]},
    )
    fake = FakeRelay()
    wired(monkeypatch, fake)

    assert module.deliver(approval()) == 1
    (url, body), = fake.requests
    assert url == "https://relay.example.org/v1/send"
    assert [m["handle"] for m in body["messages"]] == [HANDLE]


def test_the_relay_capability_is_advertised_by_default(tmp_path, monkeypatch):
    _, module = gateway(tmp_path, monkeypatch, {})
    assert contract.CAP_PUSH_RELAY == "push.relay"
    assert contract.CAP_PUSH_RELAY in module.capabilities()
    assert contract.CAP_PUSH_EXPO in module.capabilities()


def test_a_gateway_that_serves_no_relay_does_not_claim_one(tmp_path, monkeypatch):
    _, module = gateway(tmp_path, monkeypatch, {}, settings={"push.relay_origins": []})
    assert contract.CAP_PUSH_RELAY not in module.capabilities()


def test_the_plugin_advert_carries_the_relay_capability(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "profile.yaml").write_text(yaml.safe_dump({"ui_meta": {}}))
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    class Ctx:
        profile_name = "scout"
        state = None

        def get_config(self, key, default=None):
            return default

        def register_hook(self, name, callback):
            pass

        def register_command(self, *args, **kwargs):
            return object()

        def on_unload(self, callback):
            pass

    hermie_plugin.register(Ctx())
    advert = uimeta.read_key(uimeta.PLUGIN_KEY, home)
    assert contract.CAP_PUSH_RELAY in contract.read_capabilities(advert)


# -- batching and answers ----------------------------------------------------


def entries(count):
    return [{"handle": f"h_{index:04d}", "secret": "s", "message": {}} for index in range(count)]


def test_messages_go_out_in_requests_of_twenty():
    fake = FakeRelay()
    outcomes = relay.send("https://push.hermie.dev", entries(45), post=fake, sleep=lambda s: None)
    assert [len(body["messages"]) for _, body in fake.requests] == [20, 20, 5]
    assert all(outcome.status == "sent" for outcome in outcomes)
    assert len(outcomes) == 45


def test_answers_are_matched_by_handle_not_by_position():
    def post(url, body):
        rows = [{"handle": m["handle"], "status": "sent"} for m in body["messages"]]
        rows[0]["status"] = "gone"
        return relay.Reply(status=200, body={"results": list(reversed(rows))})

    outcomes = relay.send("https://push.hermie.dev", entries(3), post=post, sleep=lambda s: None)
    assert [outcome.status for outcome in outcomes] == ["gone", "sent", "sent"]


def test_a_message_the_relay_said_nothing_about_is_dropped_not_retried():
    calls = []

    def post(url, body):
        calls.append(body)
        return relay.Reply(status=200, body={"results": []})

    outcome, = relay.send("https://push.hermie.dev", entries(1), post=post, sleep=lambda s: None)
    assert outcome.status == "rejected"
    assert len(calls) == 1


@pytest.mark.parametrize("status", ["retry", "limited"])
def test_retry_and_limited_are_tried_once_more_after_the_wait_asked_for(status):
    fake = FakeRelay({"h_0000": [status, "sent"]})
    sleeps = []
    outcomes = relay.send("https://push.hermie.dev", entries(2), post=fake, sleep=sleeps.append)

    assert [outcome.status for outcome in outcomes] == ["sent", "sent"]
    assert sleeps == [3]
    # Only the message that was not delivered is sent again.
    assert [[m["handle"] for m in body["messages"]] for _, body in fake.requests] == [
        ["h_0000", "h_0001"], ["h_0000"],
    ]


@pytest.mark.parametrize("status", ["retry", "limited"])
def test_a_second_retry_is_dropped(status):
    fake = FakeRelay({"h_0000": status})
    outcome, = relay.send("https://push.hermie.dev", entries(1), post=fake, sleep=lambda s: None)
    assert outcome.status == status
    assert len(fake.requests) == 2


def test_the_wait_is_bounded():
    def post(url, body):
        return relay.Reply(
            status=200,
            body={"results": [{"handle": m["handle"], "status": "limited", "retryAfter": 3600} for m in body["messages"]]},
        )

    sleeps = []
    relay.send("https://push.hermie.dev", entries(1), post=post, sleep=sleeps.append)
    assert sleeps == [relay.MAX_RETRY_SECONDS]


def test_a_network_failure_is_retried_once_then_dropped():
    fake = FakeRelay(fail_first=1)
    outcome, = relay.send("https://push.hermie.dev", entries(1), post=fake, sleep=lambda s: None)
    assert outcome.status == "sent"

    fake = FakeRelay(fail_first=5)
    outcome, = relay.send("https://push.hermie.dev", entries(1), post=fake, sleep=lambda s: None)
    assert outcome.status == "failed"
    assert len(fake.requests) == 2


def test_a_relay_in_trouble_is_retried_once():
    fake = FakeRelay(status=503)
    sleeps = []
    outcome, = relay.send("https://push.hermie.dev", entries(1), post=fake, sleep=sleeps.append)
    assert outcome.status == "retry"
    assert len(fake.requests) == 2
    assert sleeps == [7]


@pytest.mark.parametrize("status", [301, 302, 400, 401, 404, 413])
def test_a_refused_request_is_not_retried_and_retires_nobody(status):
    fake = FakeRelay(status=status)
    outcome, = relay.send("https://push.hermie.dev", entries(1), post=fake, sleep=lambda s: None)
    assert outcome.status == "rejected"
    assert outcome.device_gone is False
    assert len(fake.requests) == 1


@pytest.mark.parametrize(
    "origin", ["http://push.hermie.dev", "https://push.hermie.dev/v1", "https://user@push.hermie.dev"]
)
def test_the_sender_posts_only_to_an_https_origin(origin):
    with pytest.raises(ValueError):
        relay.send_batch(origin, entries(1), post=FakeRelay())


def test_a_batch_over_the_limit_is_refused_rather_than_silently_cut():
    with pytest.raises(ValueError):
        relay.send_batch("https://push.hermie.dev", entries(21), post=FakeRelay())


# -- what each answer does to a registration ---------------------------------


def test_gone_retires_the_row_like_expo_does(tmp_path, monkeypatch):
    home, module = gateway(tmp_path, monkeypatch, {"i1": relay_row()})
    fake = FakeRelay({HANDLE: "gone"})
    wired(monkeypatch, fake)

    assert module.deliver(approval("r1")) == 0
    assert module.runtime.state.is_retired("i1", updated_at=1789957143) is True
    # The app's row is the app's: retirement lives in the plugin's own state.
    document = yaml.safe_load((home / "profile.yaml").read_text())
    assert "i1" in document["ui_meta"]["hermie-app:u1"]["push"]["registrations"]

    # And the next notification does not knock on that door again.
    assert module.deliver(approval("r2")) == 0
    assert len(fake.requests) == 1


def test_a_fresh_registration_after_retirement_is_served_again(tmp_path, monkeypatch):
    _, module = gateway(tmp_path, monkeypatch, {"i1": relay_row()})
    wired(monkeypatch, FakeRelay({HANDLE: "gone"}))
    module.deliver(approval("r1"))

    module.runtime.state.data["retired"]["i1"]["at"] = 1789957143 - 1
    wired(monkeypatch, FakeRelay())
    assert module.deliver(approval("r2")) == 1


def test_rejected_is_logged_and_retires_nobody(tmp_path, monkeypatch, caplog):
    _, module = gateway(tmp_path, monkeypatch, {"i1": relay_row()})
    fake = FakeRelay({HANDLE: "rejected"})
    wired(monkeypatch, fake)

    with caplog.at_level(logging.WARNING):
        assert module.deliver(approval()) == 0
    assert module.runtime.state.is_retired("i1", updated_at=1789957143) is False
    assert len(fake.requests) == 1
    assert any("payload_too_large" in record.getMessage() for record in caplog.records)


def test_no_log_line_carries_the_secret_or_a_whole_handle(tmp_path, monkeypatch, caplog):
    _, module = gateway(
        tmp_path, monkeypatch,
        {
            "i1": relay_row(),
            "i2": relay_row(handle="h_second_device_handle", secret="another-secret-value"),
            "i3": relay_row(relay="https://evil.example", handle="h_third_device_handle"),
        },
    )
    wired(monkeypatch, FakeRelay({HANDLE: "gone", "h_second_device_handle": "rejected"}))

    with caplog.at_level(logging.DEBUG):
        module.deliver(approval())
        module.relay_origins  # noqa: B018 - reads the setting, which may log

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert text, "the test should have produced log lines"
    for secret in (SECRET, "another-secret-value"):
        assert secret not in text
    for handle in (HANDLE, "h_second_device_handle", "h_third_device_handle"):
        assert handle not in text
    assert relay.handle_hint(HANDLE) in text


# -- the rules every transport follows ---------------------------------------


def test_text_never_crosses_the_relay(tmp_path, monkeypatch):
    """D29: a relay row's own `preview: true` is not an answer it can give."""
    _, module = gateway(
        tmp_path, monkeypatch,
        {
            "i1": relay_row(preview=True),
            "i2": relay_row(handle="h_encrypted_device", preview=True, enc={"alg": "x", "key": "k", "kid": 1}),
        },
        settings={"push.preview": "device"},
    )
    fake = FakeRelay()
    wired(monkeypatch, fake)

    note = events.from_assistant_message(
        bot="scout", session_id="s1", turn_id="t1", assistant_response="the settlement is 40k", at=10
    )
    assert module.deliver(note) == 2
    wire = json.dumps(fake.requests)
    assert "settlement" not in wire
    for _, body in fake.requests:
        for entry in body["messages"]:
            assert entry["message"]["title"] == "scout"
            assert entry["message"]["body"] == "New message"
            assert "preview" not in entry["message"]["data"]


def test_an_expo_device_beside_it_still_gets_the_preview_it_asked_for(tmp_path, monkeypatch):
    _, module = gateway(
        tmp_path, monkeypatch,
        {"i1": relay_row(preview=True), "i2": expo_row(preview=True)},
        settings={"push.preview": "device"},
    )
    fake = FakeRelay()
    wired(monkeypatch, fake)
    expo_sent = []
    monkeypatch.setattr(
        push_pkg.expo, "send",
        lambda batch: expo_sent.extend(batch) or [
            push_pkg.expo.Ticket(token=m["to"], status="ok") for m in batch
        ],
    )

    note = events.from_assistant_message(
        bot="scout", session_id="s1", turn_id="t1", assistant_response="the settlement is 40k", at=10
    )
    assert module.deliver(note) == 2
    assert expo_sent[0]["data"]["preview"] == "the settlement is 40k"
    assert "settlement" not in json.dumps(fake.requests)


def test_relay_message_strips_a_preview_whatever_the_caller_passed():
    parsed = registration_of("i1", relay_row())
    entry = relay.message_for(
        parsed, {"type": "message", "bot": "scout", "eventId": "e", "preview": "secret text"},
        title="scout", body="New message",
    )
    assert "preview" not in entry["message"]["data"]
    assert "secret text" not in json.dumps(entry)


def test_a_mute_silences_a_relay_device_too(tmp_path, monkeypatch):
    _, module = gateway(tmp_path, monkeypatch, {"i1": relay_row()}, extra_meta={"mutes": {"scout": 0}})
    fake = FakeRelay()
    wired(monkeypatch, fake)
    assert module.deliver(approval()) == 0
    assert fake.requests == []


def test_a_relay_device_reading_the_chat_is_not_told_about_a_message(tmp_path, monkeypatch):
    import time

    rows = {"i1": relay_row()}
    _, module = gateway(tmp_path, monkeypatch, rows)
    bag_path = module.runtime.home / "profile.yaml"
    document = yaml.safe_load(bag_path.read_text())
    document["ui_meta"]["hermie-app:u1"]["push"]["seen"] = {"i1": {"bot": "scout", "at": int(time.time())}}
    bag_path.write_text(yaml.safe_dump(document))
    fake = FakeRelay()
    wired(monkeypatch, fake)

    note = events.from_assistant_message(
        bot="scout", session_id="s1", turn_id="t1", assistant_response="hello", at=10
    )
    assert module.deliver(note) == 0
    # A request is never suppressed.
    assert module.deliver(approval()) == 1


def test_the_same_event_buzzes_a_relay_device_once(tmp_path, monkeypatch):
    _, module = gateway(tmp_path, monkeypatch, {"i1": relay_row()})
    fake = FakeRelay()
    wired(monkeypatch, fake)
    assert module.deliver(approval()) == 1
    assert module.deliver(approval()) == 0
    assert len(fake.requests) == 1


def test_one_handle_in_two_rows_is_one_buzz(tmp_path, monkeypatch):
    _, module = gateway(tmp_path, monkeypatch, {"i1": relay_row(), "i2": relay_row()})
    fake = FakeRelay()
    wired(monkeypatch, fake)
    assert module.deliver(approval()) == 1
    assert [len(body["messages"]) for _, body in fake.requests] == [1]


def test_an_oversized_message_is_dropped_here(tmp_path, monkeypatch):
    _, module = gateway(tmp_path, monkeypatch, {"i1": relay_row()})
    fake = FakeRelay()
    wired(monkeypatch, fake)
    note = events.from_session_end(
        bot="scout" * 1000, session_id="s1", turn_id="t1", completed=True, failed=False,
        interrupted=False, at=10,
    )
    assert module.deliver(note) == 0
    assert fake.requests == []


# -- the wire itself ---------------------------------------------------------


class Redirecting(http.server.BaseHTTPRequestHandler):
    hits = []

    def do_POST(self):  # noqa: N802 - http.server's naming
        Redirecting.hits.append(self.path)
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(307)
        self.send_header("Location", "/elsewhere")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def test_a_redirect_is_an_answer_not_a_new_address():
    server = http.server.HTTPServer(("127.0.0.1", 0), Redirecting)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        Redirecting.hits = []
        reply = relay._post(f"http://127.0.0.1:{server.server_port}/v1/send", {"v": 1, "messages": []})
    finally:
        server.shutdown()
    assert reply.status == 307
    assert Redirecting.hits == ["/v1/send"]


def test_only_an_environment_proxy_is_used(monkeypatch):
    for name in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    assert relay._proxies() == {}
    monkeypatch.setenv("HTTP_PROXY", "http://plain-proxy.example:3128")
    assert relay._proxies() == {}
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    assert relay._proxies() == {"https": "http://proxy.example:3128"}
