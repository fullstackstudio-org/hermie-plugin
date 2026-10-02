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
@pytest.mark.parametrize(
    "value",
    [None, "", "   ", 7, True, {"a": 1}, "x" * 201, " padded ", "a b", "a/b", "a+b", "abc=", "h_\u00e9t\u00e9", "a\nb"],
)
def test_the_handle_and_secret_are_what_the_relay_issues(field, value):
    """base64url, 1 to 200 characters: one field the relay cannot read fails a whole request."""
    assert registration_of("i1", relay_row(**{field: value})) is None


@pytest.mark.parametrize("field", ["handle", "secret"])
def test_the_longest_field_the_relay_takes_is_read(field):
    assert registration_of("i1", relay_row(**{field: "x" * 200})) is not None


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


class Clock:
    """A clock that only moves when the code under test sleeps."""

    def __init__(self, now=1_790_000_000.0):
        self.now = now
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def wired(monkeypatch, fake, clock=None):
    """Route the module's relay sends through *fake*, on a clock of the test's own."""
    clock = clock or Clock()
    monkeypatch.setattr(
        push_pkg.relay, "send",
        lambda origin, entries, clock_from_module=None, **options: REAL_SEND(
            origin, entries, post=fake, sleep=clock.sleep, clock=clock,
            **{key: value for key, value in options.items() if key != "clock"},
        ),
    )
    return clock


def run(entries, post, clock=None, pacing=None):
    clock = clock or Clock()
    return relay.send(
        "https://push.hermie.dev", entries, post=post, sleep=clock.sleep, clock=clock, pacing=pacing
    )


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


def test_a_gateway_that_does_not_post_to_the_apps_relay_does_not_claim_one(tmp_path, monkeypatch):
    """An app seeing `push.relay` moves its device to the relay; here it would go silent."""
    _, module = gateway(
        tmp_path, monkeypatch, {}, settings={"push.relay_origins": ["https://relay.example.org"]}
    )
    assert module.relay_origins == ("https://relay.example.org",)
    assert contract.CAP_PUSH_RELAY not in module.capabilities()


def test_the_default_relay_beside_another_is_still_claimed(tmp_path, monkeypatch):
    _, module = gateway(
        tmp_path, monkeypatch, {},
        settings={"push.relay_origins": ["https://relay.example.org", "https://push.hermie.dev"]},
    )
    assert contract.CAP_PUSH_RELAY in module.capabilities()


def advert_for(tmp_path, monkeypatch, settings=None):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "profile.yaml").write_text(yaml.safe_dump({"ui_meta": {}}))
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    class Ctx:
        profile_name = "scout"
        state = None

        def get_config(self, key, default=None):
            return (settings or {}).get(key, default)

        def register_hook(self, name, callback):
            pass

        def register_command(self, *args, **kwargs):
            return object()

        def on_unload(self, callback):
            pass

    hermie_plugin.register(Ctx())
    return uimeta.read_key(uimeta.PLUGIN_KEY, home)


def test_the_advert_says_which_relays_are_served(tmp_path, monkeypatch):
    advert = advert_for(tmp_path, monkeypatch)
    assert advert["relayOrigins"] == ["https://push.hermie.dev"]


def test_the_advert_lists_an_operators_own_relay(tmp_path, monkeypatch):
    advert = advert_for(
        tmp_path, monkeypatch, {"push.relay_origins": ["https://relay.example.org/", "not an origin"]}
    )
    assert advert["relayOrigins"] == ["https://relay.example.org"]
    assert contract.CAP_PUSH_RELAY not in contract.read_capabilities(advert)


def test_an_empty_allow_list_is_published_as_empty(tmp_path, monkeypatch):
    advert = advert_for(tmp_path, monkeypatch, {"push.relay_origins": []})
    assert advert["relayOrigins"] == []


def test_without_push_the_advert_names_no_relays(tmp_path, monkeypatch):
    advert = advert_for(tmp_path, monkeypatch, {"modules.push": False})
    assert "relayOrigins" not in advert


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


def realistic_entries(count):
    """What the module really builds for an approval: about 600 bytes apiece."""
    rows = []
    for index in range(count):
        row = registration_of(
            f"i{index}",
            relay_row(handle=f"h_{index:022d}", secret="S" * 43, gatewayKey="bf796761db84e312"),
        )
        note = events.from_approval(
            bot="research-assistant", session_key="20261002_101500_" + "a" * 16,
            description="d", request_id="req-" + "b" * 32, turn_id="t1", at=1790000000,
            cron=None,
        )
        payload = note.payload(preview=False, gateway_key="bf796761db84e312", session_kind="canonical")
        title, body = note.rendered(preview=False)
        rows.append(relay.message_for(row, payload, title=title, body=body, gateway_key="bf796761db84e312"))
    return rows


def test_a_realistic_entry_is_the_size_the_limits_were_set_for():
    size = len(json.dumps(realistic_entries(1)[0], separators=(",", ":")).encode())
    assert 450 < size < 800


def test_twenty_realistic_entries_go_out_under_the_relays_byte_cap():
    fake = FakeRelay()
    outcomes = run(realistic_entries(20), fake)
    assert all(outcome.status == "sent" for outcome in outcomes)
    assert sum(len(body["messages"]) for _, body in fake.requests) == 20
    assert len(fake.requests) >= 2, "twenty approvals do not fit one 8 KB request"
    for _, body in fake.requests:
        assert len(relay.encode_request(body["messages"])) <= relay.MAX_REQUEST_BYTES
        assert len(body["messages"]) <= relay.MAX_BATCH


def test_the_request_is_measured_as_it_is_sent():
    """`_post` encodes with exactly the bytes `plan_requests` counted."""
    sample = realistic_entries(3)
    body = {"v": 1, "messages": sample}
    assert json.dumps(body, separators=(",", ":")).encode("utf-8") == relay.encode_request(sample)


def test_messages_go_out_in_requests_of_twenty():
    fake = FakeRelay()
    outcomes = run(entries(45), fake)
    assert [len(body["messages"]) for _, body in fake.requests] == [20, 20, 5]
    assert all(outcome.status == "sent" for outcome in outcomes)
    assert len(outcomes) == 45


def test_one_handle_is_never_in_a_request_twice():
    same = [
        {"handle": "h_same", "secret": "one", "message": {}},
        {"handle": "h_other", "secret": "s", "message": {}},
        {"handle": "h_same", "secret": "two", "message": {}},
    ]
    fake = FakeRelay()
    outcomes = run(same, fake)
    assert [[m["secret"] for m in body["messages"]] for _, body in fake.requests] == [["one", "s"], ["two"]]
    assert [outcome.status for outcome in outcomes] == ["sent", "sent", "sent"]


def test_answers_are_matched_by_handle_not_by_position():
    def post(url, body):
        rows = [{"handle": m["handle"], "status": "sent"} for m in body["messages"]]
        rows[0]["status"] = "gone"
        return relay.Reply(status=200, body={"results": list(reversed(rows))})

    outcomes = run(entries(3), post)
    assert [outcome.status for outcome in outcomes] == ["gone", "sent", "sent"]


def test_a_message_the_relay_said_nothing_about_is_dropped_not_retried():
    calls = []

    def post(url, body):
        calls.append(body)
        return relay.Reply(status=200, body={"results": []})

    outcome, = run(entries(1), post)
    assert outcome.status == "rejected"
    assert len(calls) == 1


@pytest.mark.parametrize("status", ["retry", "limited"])
def test_retry_and_limited_are_tried_once_more_after_the_wait_asked_for(status):
    fake = FakeRelay({"h_0000": [status, "sent"]})
    clock = Clock()
    outcomes = run(entries(2), fake, clock)

    assert [outcome.status for outcome in outcomes] == ["sent", "sent"]
    assert clock.sleeps == [3]
    # Only the message that was not delivered is sent again.
    assert [[m["handle"] for m in body["messages"]] for _, body in fake.requests] == [
        ["h_0000", "h_0001"], ["h_0000"],
    ]


@pytest.mark.parametrize("status", ["retry", "limited"])
def test_a_second_retry_is_dropped(status):
    fake = FakeRelay({"h_0000": status})
    outcome, = run(entries(1), fake)
    assert outcome.status == status
    assert len(fake.requests) == 2


def answering_every_entry(status, retry_after):
    calls = []

    def post(url, body):
        calls.append(body)
        return relay.Reply(
            status=200,
            body={"results": [
                {"handle": m["handle"], "status": status, "retryAfter": retry_after} for m in body["messages"]
            ]},
        )

    return post, calls


@pytest.mark.parametrize("status", ["retry", "limited"])
def test_a_long_wait_is_not_waited_for(status):
    """The sender's thread is shared by every notification after this one."""
    post, calls = answering_every_entry(status, 3600)
    clock = Clock()
    outcome, = run(entries(1), post, clock)
    assert outcome.status == status
    assert clock.sleeps == []
    assert len(calls) == 1


def test_a_limited_handle_is_left_alone_until_its_time():
    pacing = relay.Pacing()
    clock = Clock()
    post, calls = answering_every_entry("limited", 600)
    run(entries(2), post, clock, pacing)

    fake = FakeRelay()
    outcomes = run(entries(3), fake, clock, pacing)
    # The two limited handles are not asked about; the third one is.
    assert [outcome.status for outcome in outcomes] == ["deferred", "deferred", "sent"]
    assert [[m["handle"] for m in body["messages"]] for _, body in fake.requests] == [["h_0002"]]

    clock.now += 601
    fake = FakeRelay()
    assert [o.status for o in run(entries(2), fake, clock, pacing)] == ["sent", "sent"]


def test_a_network_failure_is_retried_once_then_dropped():
    fake = FakeRelay(fail_first=1)
    outcome, = run(entries(1), fake)
    assert outcome.status == "sent"

    fake = FakeRelay(fail_first=5)
    outcome, = run(entries(1), fake)
    assert outcome.status == "failed"
    assert len(fake.requests) == 2


def test_a_relay_in_trouble_is_retried_once_and_then_left_alone():
    fake = FakeRelay(status=503)
    clock = Clock()
    pacing = relay.Pacing()
    outcome, = run(entries(1), fake, clock, pacing)
    assert outcome.status == "retry"
    assert len(fake.requests) == 2
    assert clock.sleeps == [7]

    # The next notification does not knock at all while the relay rests...
    outcome, = run(entries(1), fake, clock, pacing)
    assert outcome.status == "deferred"
    assert len(fake.requests) == 2
    assert clock.sleeps == [7]

    # ...and is sent once the rest is over.
    clock.now += relay.BACKOFF_SECONDS
    outcome, = run(entries(1), FakeRelay(), clock, pacing)
    assert outcome.status == "sent"


def test_a_relay_that_asks_for_a_long_rest_is_not_waited_for():
    def post(url, body):
        return relay.Reply(status=429, retry_after=900)

    clock = Clock()
    pacing = relay.Pacing()
    outcome, = run(entries(1), post, clock, pacing)
    assert outcome.status == "retry"
    assert clock.sleeps == []
    assert pacing.origin_wait("https://push.hermie.dev", clock()) == 900


def test_one_wait_at_most_per_delivery():
    """Twenty-five entries over two requests, both asked to wait: one sleep."""
    post, calls = answering_every_entry("retry", 5)
    clock = Clock()
    run(entries(25), post, clock)
    assert clock.sleeps == [5]
    assert len(calls) == 4


def test_a_whole_request_failure_stops_the_rest_of_that_round():
    calls = []

    def post(url, body):
        calls.append(body)
        raise OSError("connection refused")

    clock = Clock()
    outcomes = run(entries(45), post, clock)
    # One request per round, not three: the relay is not asked twice into the same wall.
    assert len(calls) == 2
    assert {outcome.status for outcome in outcomes} == {"failed"}


@pytest.mark.parametrize("status", [301, 302, 401, 404])
def test_a_refused_request_is_not_retried_and_retires_nobody(status):
    fake = FakeRelay(status=status)
    outcome, = run(entries(1), fake)
    assert outcome.status == "rejected"
    assert outcome.device_gone is False
    assert len(fake.requests) == 1


@pytest.mark.parametrize("status", [400, 413])
def test_a_request_refused_as_a_whole_costs_only_the_entry_that_did_it(status):
    """One bad entry must not cost every other device its notification."""

    def post(url, body):
        handles = [m["handle"] for m in body["messages"]]
        calls.append(handles)
        if "h_0001" in handles:
            return relay.Reply(status=status)
        return relay.Reply(status=200, body={"results": [{"handle": h, "status": "sent"} for h in handles]})

    calls = []
    outcomes = run(entries(3), post)
    assert [outcome.status for outcome in outcomes] == ["sent", "rejected", "sent"]
    assert outcomes[1].reason == f"http-{status}"
    assert calls == [["h_0000", "h_0001", "h_0002"], ["h_0000"], ["h_0001"], ["h_0002"]]


@pytest.mark.parametrize(
    "origin", ["http://push.hermie.dev", "https://push.hermie.dev/v1", "https://user@push.hermie.dev"]
)
def test_the_sender_posts_only_to_an_https_origin(origin):
    with pytest.raises(ValueError):
        relay.send_batch(origin, entries(1), post=FakeRelay())


def test_a_batch_over_the_limit_is_refused_rather_than_silently_cut():
    with pytest.raises(ValueError):
        relay.send_batch("https://push.hermie.dev", entries(21), post=FakeRelay())


def test_an_empty_collapse_id_or_thread_is_left_out():
    parsed = registration_of("i1", relay_row())
    entry = relay.message_for(parsed, {"type": "message", "bot": ""}, title="t", body="b")
    assert "collapseId" not in entry["message"]
    assert "thread" not in entry["message"]


# -- the relay's own numbers -------------------------------------------------
#
# The relay's protocol v1 (its `src/server/push/protocol.ts` and the protocol
# document beside it): 20 messages a request, a 3,584-byte message, a 64-byte
# collapse id, a 256-character thread, and at most 200 characters of handle or
# secret. Its request-body cap is the relay's to raise; what is pinned here is
# that this side's own, conservative cap fits under the smallest one the relay
# has had (8 KB) and under whatever it has now. When the relay's source is
# checked out and `HERMIE_RELAY_REPO` points at it, the numbers are read there.

RELAY_LIMITS = {
    "MAX_MESSAGES_PER_REQUEST": 20,
    "MAX_MESSAGE_BYTES": 3_584,
}
RELAY_COLLAPSE_ID_BYTES = 64
SMALLEST_RELAY_REQUEST_CAP = 8 * 1024


def test_the_limits_are_the_relays_with_a_margin():
    from hermie_plugin.push import registrations

    assert relay.MAX_BATCH == RELAY_LIMITS["MAX_MESSAGES_PER_REQUEST"]
    assert relay.MAX_REQUEST_BYTES < SMALLEST_RELAY_REQUEST_CAP
    assert relay.MAX_MESSAGE_BYTES <= RELAY_LIMITS["MAX_MESSAGE_BYTES"]
    assert relay.MAX_COLLAPSE_ID_BYTES == RELAY_COLLAPSE_ID_BYTES
    assert relay.MAX_THREAD_CHARS == 256
    assert registrations.MAX_RELAY_FIELD == 200


def test_the_limits_match_the_relays_source_when_it_is_here():
    import os
    import re
    from pathlib import Path

    root = os.environ.get("HERMIE_RELAY_REPO", "")
    source = Path(root) / "src" / "server" / "push" / "protocol.ts" if root else None
    if source is None or not source.is_file():
        pytest.skip("the relay's source is not checked out here")
    text = source.read_text(encoding="utf-8")

    def constants(pattern):
        found = {}
        for name, expression in re.findall(rf"export const ({pattern}) = ([0-9_ *]+);", text):
            found[name] = eval(expression.replace("_", ""), {})  # digits, `*` and spaces only
        return found

    for name, value in RELAY_LIMITS.items():
        assert constants(name) == {name: value}, name
    # The request cap may be one constant or one per route; the send route's is
    # the largest of them, and this side's cap must fit under every one.
    caps = constants(r"MAX_[A-Z_]*REQUEST_BYTES")
    assert caps, "no request-body cap found in the relay's source"
    assert min(caps.values()) >= relay.MAX_REQUEST_BYTES, caps
    # The collapse id's bound: a constant once, a pattern of printable ASCII now.
    collapse = constants("MAX_COLLAPSE_ID_BYTES").get("MAX_COLLAPSE_ID_BYTES")
    if collapse is None:
        found = re.search(r"collapseId: z\.string\(\)\.regex\(/\^\[\\x21-\\x7E\]\{1,(\d+)\}\$/", text)
        assert found, "no collapse id bound found in the relay's source"
        collapse = int(found.group(1))
    assert collapse == relay.MAX_COLLAPSE_ID_BYTES
    assert re.search(r"handle: z\.string\(\)\.max\(200\)", text)
    assert re.search(r"secret: z\.string\(\)\.max\(200\)", text)
    assert re.search(r"thread: z\.string\(\)\.min\(1\)\.max\(256\)", text)


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


def rewrite_rows(home, rows):
    path = home / "profile.yaml"
    document = yaml.safe_load(path.read_text())
    document["ui_meta"]["hermie-app:u1"]["push"]["registrations"] = rows
    path.write_text(yaml.safe_dump(document))


def test_a_fresh_registration_after_retirement_is_served_again(tmp_path, monkeypatch):
    home, module = gateway(tmp_path, monkeypatch, {"i1": relay_row()})
    wired(monkeypatch, FakeRelay({HANDLE: "gone"}))
    module.deliver(approval("r1"))

    # The device registered again: a new handle and secret, a newer row.
    rewrite_rows(home, {"i1": relay_row(handle="h_new_handle_after_gone", secret="new-secret", updatedAt=1899999999)})
    wired(monkeypatch, FakeRelay())
    assert module.deliver(approval("r2")) == 1


def test_a_pair_that_came_back_gone_is_not_sent_again_when_rewritten(tmp_path, monkeypatch):
    """A relay never hands a handle out twice; re-arming the same pair changes nothing."""
    home, module = gateway(tmp_path, monkeypatch, {"i1": relay_row()})
    wired(monkeypatch, FakeRelay({HANDLE: "gone"}))
    module.deliver(approval("r1"))

    rewrite_rows(home, {"i9": relay_row(updatedAt=1899999999)})  # same pair, new id, newer row
    fake = FakeRelay()
    wired(monkeypatch, fake)
    assert module.deliver(approval("r2")) == 0
    assert fake.requests == []


def test_a_users_gone_budget_stops_their_unproven_rows_for_an_hour(tmp_path, monkeypatch, caplog):
    """A co-user's made-up rows must not spend the gateway's auth-failure allowance at the relay."""
    proven = relay_row(handle="h_proven_device_0001", secret="proven")
    home, module = gateway(tmp_path, monkeypatch, {"good": proven})
    now = [5_000.0]
    module._clock = lambda: now[0]
    wired(monkeypatch, FakeRelay())
    assert module.deliver(approval("r0")) == 1  # the real device is proven

    fake = FakeRelay({f"h_fake_{i:04d}": "gone" for i in range(10)})
    wired(monkeypatch, fake)
    for round_ in range(3):
        rewrite_rows(home, {
            "good": proven,
            f"bad{round_}": relay_row(handle=f"h_fake_{round_:04d}", secret="made-up", updatedAt=1899999999),
        })
        module.deliver(approval(f"r{round_ + 1}"))

    # Three gone answers in the hour: the next made-up row is not sent; the proven one still is.
    rewrite_rows(home, {
        "good": proven,
        "bad9": relay_row(handle="h_fake_0009", secret="made-up", updatedAt=1899999999),
    })
    fake.requests.clear()
    with caplog.at_level(logging.WARNING):
        assert module.deliver(approval("r9")) == 1
    assert [m["handle"] for _, body in fake.requests for m in body["messages"]] == ["h_proven_device_0001"]
    assert any("keep coming back gone" in r.getMessage() for r in caplog.records)

    # After the hour, that user's new rows are tried again.
    now[0] += relay.GONE_WINDOW_SECONDS
    fake.requests.clear()
    module.deliver(approval("r10"))
    assert "h_fake_0009" in [m["handle"] for _, body in fake.requests for m in body["messages"]]


def test_one_users_budget_does_not_touch_another_users_rows(tmp_path, monkeypatch):
    trust = relay.Trust()
    gone = relay.Outcome(status="gone")
    for index in range(relay.GONE_BUDGET):
        trust.record(("o", f"h{index}", "s"), "mallory", gone, 100.0)
    assert trust.over_budget("mallory", 101.0) is True
    assert trust.over_budget("alice", 101.0) is False
    assert trust.over_budget("mallory", 100.0 + relay.GONE_WINDOW_SECONDS) is False


def test_the_remembered_pairs_are_bounded():
    trust = relay.Trust()
    gone = relay.Outcome(status="gone")
    for index in range(relay.MAX_REMEMBERED_PAIRS + 50):
        trust.record(("o", f"h{index}", "s"), f"u{index}", gone, float(index))
    assert len(trust.gone) == relay.MAX_REMEMBERED_PAIRS
    assert len(trust.failures) <= relay.MAX_REMEMBERED_PAIRS
    # The soonest to lapse went first: the oldest pairs.
    assert ("o", "h0", "s") not in trust.gone
    assert ("o", f"h{relay.MAX_REMEMBERED_PAIRS + 49}", "s") in trust.gone


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


def test_the_same_handle_with_another_secret_is_asked_about_separately(tmp_path, monkeypatch):
    _, module = gateway(tmp_path, monkeypatch, {"i1": relay_row(), "i2": relay_row(secret="stale-secret")})
    fake = FakeRelay({HANDLE: "sent"})
    wired(monkeypatch, fake)
    assert module.deliver(approval()) == 2
    assert [[m["secret"] for m in body["messages"]] for _, body in fake.requests] == [[SECRET], ["stale-secret"]]


def test_reports_about_rows_are_capped(tmp_path, monkeypatch, caplog):
    """A co-user writing a new origin into a row on every turn cannot grow the log without bound."""
    rows = {f"i{index}": relay_row(handle=f"h_{index:04d}", relay=f"https://r{index}.example") for index in range(100)}
    _, module = gateway(tmp_path, monkeypatch, rows)
    wired(monkeypatch, FakeRelay())
    with caplog.at_level(logging.WARNING):
        module.deliver(approval())
    reports = [record for record in caplog.records if "allow-list" in record.getMessage()]
    assert len(reports) == push_pkg.MAX_REPORTS
    # Row-supplied text is quoted in the log, never written out raw.
    assert "'https://r0.example'" in reports[0].getMessage()


# -- the name on the lock screen ---------------------------------------------


def with_display_name(home, value):
    path = home / "profile.yaml"
    document = yaml.safe_load(path.read_text())
    document["display_name"] = value
    path.write_text(yaml.safe_dump(document))


def test_every_transport_shows_the_bots_display_name(tmp_path, monkeypatch):
    home, module = gateway(tmp_path, monkeypatch, {"i1": relay_row(), "i2": expo_row()})
    with_display_name(home, "  Scout the Researcher ")
    fake = FakeRelay()
    wired(monkeypatch, fake)
    expo_sent = []
    monkeypatch.setattr(
        push_pkg.expo, "send",
        lambda batch: expo_sent.extend(batch) or [push_pkg.expo.Ticket(token=m["to"], status="ok") for m in batch],
    )

    assert module.deliver(approval()) == 2
    relay_message = fake.requests[0][1]["messages"][0]["message"]
    assert relay_message["title"] == "Scout the Researcher"
    assert expo_sent[0]["title"] == "Scout the Researcher"
    # A tap is resolved against the profile name, which does not change.
    assert relay_message["data"]["bot"] == "scout"
    assert expo_sent[0]["data"]["bot"] == "scout"
    assert relay_message["thread"].endswith(":scout")


@pytest.mark.parametrize("value", [None, "", "   ", 42, ["x"], "x" * 61, "two\nlines", "bell\x07"])
def test_a_label_unfit_to_show_falls_back_to_the_profile_name(tmp_path, monkeypatch, value):
    home, module = gateway(tmp_path, monkeypatch, {"i1": relay_row()})
    with_display_name(home, value)
    fake = FakeRelay()
    wired(monkeypatch, fake)
    assert module.deliver(approval()) == 1
    assert fake.requests[0][1]["messages"][0]["message"]["title"] == "scout"


# -- holds are bounded, and said once ----------------------------------------


def test_a_huge_retry_after_buys_an_hour_not_forever(caplog):
    def post(url, body):
        return relay.Reply(status=429, retry_after=1_000_000_000)

    clock = Clock()
    pacing = relay.Pacing()
    with caplog.at_level(logging.WARNING):
        run(entries(1), post, clock, pacing)
        run(entries(1), post, clock, pacing)  # deferred: no request, no second warning
    assert pacing.origin_wait("https://push.hermie.dev", clock()) == relay.MAX_ORIGIN_HOLD_SECONDS
    holds = [r for r in caplog.records if "left alone for" in r.getMessage()]
    assert len(holds) == 1

    clock.now += relay.MAX_ORIGIN_HOLD_SECONDS
    assert run(entries(1), FakeRelay(), clock, pacing)[0].status == "sent"


def test_a_huge_retry_after_on_one_handle_buys_a_day_at_most():
    post, _ = answering_every_entry("limited", 10**12)
    clock = Clock()
    pacing = relay.Pacing()
    outcome, = run(entries(1), post, clock, pacing)
    assert outcome.retry_after == relay.MAX_HANDLE_HOLD_SECONDS
    assert pacing.handle_wait("https://push.hermie.dev", "h_0000", clock()) == relay.MAX_HANDLE_HOLD_SECONDS


def test_the_retry_after_header_is_read_within_bounds():
    assert relay._retry_after({"Retry-After": "1000000000"}) == relay.MAX_HANDLE_HOLD_SECONDS
    assert relay._retry_after({"Retry-After": "-5"}) == 0
    assert relay._retry_after({"Retry-After": "soon"}) == 0


def test_the_default_clock_does_not_jump_with_the_wall_clock():
    import inspect
    import time as time_module

    assert inspect.signature(relay.send).parameters["clock"].default is time_module.monotonic


def test_a_relay_that_refuses_every_message_alone_too_is_left_alone():
    """No 1 + N requests on every notification when no entry is to blame."""
    calls = []

    def post(url, body):
        calls.append(len(body["messages"]))
        return relay.Reply(status=400)

    clock = Clock()
    pacing = relay.Pacing()
    outcomes = run(entries(3), post, clock, pacing)
    assert [outcome.status for outcome in outcomes] == ["rejected"] * 3
    assert calls == [3, 1, 1, 1]
    assert pacing.origin_wait("https://push.hermie.dev", clock()) == relay.BACKOFF_SECONDS

    assert run(entries(3), post, clock, pacing)[0].status == "deferred"
    assert calls == [3, 1, 1, 1]


def test_one_bad_entry_does_not_put_the_relay_on_hold():
    def post(url, body):
        handles = [m["handle"] for m in body["messages"]]
        if "h_0001" in handles:
            return relay.Reply(status=400)
        return relay.Reply(status=200, body={"results": [{"handle": h, "status": "sent"} for h in handles]})

    clock = Clock()
    pacing = relay.Pacing()
    run(entries(3), post, clock, pacing)
    assert pacing.origin_wait("https://push.hermie.dev", clock()) == 0


# -- what is measured is what is sent ----------------------------------------


def test_bytes_are_counted_as_sent_with_a_non_ascii_title_and_an_emoji(monkeypatch):
    parsed = registration_of("i1", relay_row())
    note = approval()
    payload = note.payload(preview=False, gateway_key="bf796761db84e312")
    entry = relay.message_for(parsed, payload, title="Zoë the Owl 🦉", body="Needs your approval")

    sent = []

    class Opener:
        def open(self, request, timeout):
            sent.append(request.data)
            raise OSError("not on the wire in a test")

    monkeypatch.setattr(relay.urllib.request, "build_opener", lambda *handlers: Opener())
    with pytest.raises(OSError):
        relay._post("https://push.hermie.dev/v1/send", {"v": 1, "messages": [entry]})
    assert sent[0] == relay.encode_request([entry])
    # One request of exactly that size is what `plan_requests` budgets for.
    assert relay.plan_requests([entry]) == [[0]]


def test_too_large_measures_a_message_the_way_the_relay_does():
    """The relay measures `JSON.stringify(message)` in UTF-8: compact, nothing escaped."""
    parsed = registration_of("i1", relay_row())

    def entry_with_body(text):
        return relay.message_for(parsed, {"type": "message", "bot": "scout", "eventId": "e"}, title="t", body=text)

    base = len(json.dumps(entry_with_body("")["message"], separators=(",", ":"), ensure_ascii=False).encode())
    room = relay.MAX_MESSAGE_BYTES - base
    owls = "🦉" * (room // 4)
    filler = "x" * (room - len(owls) * 4)
    exact = entry_with_body(owls + filler)
    assert len(json.dumps(exact["message"], separators=(",", ":"), ensure_ascii=False).encode()) == relay.MAX_MESSAGE_BYTES
    assert relay.too_large(exact) is False
    # Escaped, as Python would write it by default, the same message is far bigger;
    # the relay does not count it that way, and neither does this side.
    assert len(json.dumps(exact["message"]).encode()) > relay.MAX_MESSAGE_BYTES
    assert relay.too_large(entry_with_body(owls + filler + "x")) is True


@pytest.mark.parametrize("event_id", ["", "has space", "tab\there", "é-accent", "x" * 65])
def test_a_collapse_id_the_relay_would_refuse_is_left_out(event_id):
    """The relay takes 1 to 64 printable ASCII characters, and refuses the message otherwise."""
    parsed = registration_of("i1", relay_row())
    entry = relay.message_for(parsed, {"type": "message", "bot": "scout", "eventId": event_id}, title="t", body="b")
    collapse = entry["message"].get("collapseId")
    assert collapse is None or (collapse == event_id[:64] and collapse.isascii() and collapse.isprintable() and " " not in collapse)


def test_an_event_id_is_a_collapse_id_as_it_stands():
    note = approval()
    parsed = registration_of("i1", relay_row())
    entry = relay.message_for(parsed, note.payload(preview=False), title="t", body="b")
    assert entry["message"]["collapseId"] == note.event_id
