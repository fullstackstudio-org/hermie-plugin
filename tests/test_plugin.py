"""The plugin loaded the way a gateway loads it, against a fake event source.

`FakeCtx` is the whole Hermes surface this plugin uses. Keeping it small is the
point: if this class has to grow, the plugin reached for something new, and that
is worth noticing in review rather than discovering on somebody's gateway.
"""

import json
from pathlib import Path

import yaml

import hermie_plugin
from hermie_plugin import contract, uimeta
from hermie_plugin.push import events
from hermie_plugin.state import FileStore


class FakeState:
    """Stands in for `ctx.state`, which is a get/set JSON store."""

    def __init__(self, directory: Path):
        self.data_dir = directory
        self._store = FileStore(directory / "state.json")

    def get(self, key, default=None):
        return self._store.get(key, default)

    def set(self, key, value):
        self._store.set(key, value)


class FakeCtx:
    def __init__(self, home: Path, settings=None, profile="jurist"):
        self.hooks = {}
        self.sections = {}
        self.commands = {}
        self.unloads = []
        self.profile_name = profile
        self.state = FakeState(home / "plugin-data")
        self._settings = settings or {}

    def get_config(self, key, default=None):
        return self._settings.get(key, default)

    def register_hook(self, name, callback):
        self.hooks.setdefault(name, []).append(callback)

    def register_system_prompt_section(self, section_id, content, *, position="after_memory", max_chars=4000):
        assert position == "after_memory", "core accepts no other position today"
        assert max_chars <= 4000, "core caps a section at 4000 characters"
        self.sections[section_id] = content

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        # Core answers None rather than raising when the name is taken, and the
        # plugin advertises the command only when it gets a handle back.
        if name in self.commands:
            return None
        self.commands[name] = handler
        return object()

    def on_unload(self, callback):
        self.unloads.append(callback)

    def fire(self, name, **kwargs):
        return [callback(**kwargs) for callback in self.hooks.get(name, [])]


_register_command = FakeCtx.register_command


def gateway(tmp_path, *, app_meta=None, per_user=None, settings=None, profile="jurist"):
    """A HERMES_HOME with a profile.yaml, the way an install looks.

    `app_meta` is the legacy shared `hermie-app` bag; `per_user` is the
    `{user id: bag}` the app writes under `hermie-app:<user id>` from now on.
    """
    home = tmp_path / "hermes"
    home.mkdir(parents=True, exist_ok=True)
    meta = {"hermie-app": app_meta or {}}
    for user_id, bag in (per_user or {}).items():
        meta[f"hermie-app:{user_id}"] = bag
    (home / "profile.yaml").write_text(
        yaml.safe_dump({"ui_meta": meta, "_ui_meta_revisions": {"hermie-app": 7}})
    )
    return home, FakeCtx(home, settings=settings, profile=profile)


def app_meta_with(*, registrations=None, seen=None):
    return {"v": 1, "push": {"registrations": registrations or {}, "seen": seen or {}}}


# The switches a REAL row carries: the five the app's settings screen has
# shipped. Filling in every type the gateway knows about would have been a
# fixture for a device that does not exist — and while the reader parsed only
# five, it was also a fixture that quietly disagreed with itself. A row gets the
# two cron switches once the app writes them, which is what
# `test_events.py` pins from a parsed row rather than from here.
APP_ROW_TYPES = ("message", "request", "cron", "turn_done", "turn_failed")


def expo_registration(**overrides):
    entry = {
        "v": 1,
        "transport": "expo",
        "token": "ExponentPushToken[abcdefghijklmnopqrstuv]",
        "platform": "ios",
        "types": {name: True for name in APP_ROW_TYPES},
        "preview": False,
        "updatedAt": 1789957143,
    }
    entry.update(overrides)
    return entry


# -- loading and advertising -------------------------------------------------


def test_loading_registers_the_hooks_a_gateway_will_fire(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    hermie_plugin.register(ctx)

    for name in ("post_llm_call", "on_session_end", "pre_approval_request", "pre_tool_call", "pre_llm_call"):
        assert name in ctx.hooks, f"{name} was never registered"
    # HERM-119: the context module no longer renders anything into the system
    # prompt — there is no more profile to say. `pre_llm_call` alone is left.
    assert "hermie.device" not in ctx.sections


def test_loading_publishes_an_advert_the_app_can_read(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    hermie_plugin.register(ctx)

    advert = uimeta.read_key(uimeta.PLUGIN_KEY, home)
    caps = contract.read_capabilities(advert)
    assert contract.CAP_PUSH_EXPO in caps
    assert contract.CAP_PUSH_MUTE in caps
    assert contract.CAP_PUSH_SEEN_PER_CHAT in caps
    assert contract.CAP_UIMETA_PER_USER in caps
    assert contract.CAP_PROFILE_DISPLAY_NAME in caps
    assert contract.CAP_CONTEXT_TURN_CLAIM in caps
    # HERM-119: nothing left to render a profile, so nothing claims to.
    assert contract.CAP_CONTEXT_PROMPT not in caps
    assert contract.CAP_CONTEXT_ORIENTATION not in caps
    assert contract.CAP_CONTEXT_PER_BOT not in caps
    assert contract.CAP_CONTEXT_LIVE not in caps
    assert advert["version"] == contract.PLUGIN_VERSION
    assert advert["modules"]["push"] == "on"
    assert advert["modules"]["presence"] == "planned"


def test_loading_registers_the_me_command_and_advertises_it(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    hermie_plugin.register(ctx)

    assert "me" in ctx.commands
    assert contract.CAP_COMMAND_ME in contract.read_capabilities(uimeta.read_key(uimeta.PLUGIN_KEY, home))
    # It answers here and now, without a model and without a session.
    assert "Hermie context" in ctx.commands["me"]("")


def test_a_command_hermes_would_not_take_is_never_advertised(tmp_path, monkeypatch):
    """A capability names something that is actually there, not something that
    shipped. Core answers None when the name is taken; so does the fake."""
    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)
    ctx.commands["me"] = lambda raw_args: "somebody else's"

    hermie_plugin.register(ctx)

    assert contract.CAP_COMMAND_ME not in contract.read_capabilities(
        uimeta.read_key(uimeta.PLUGIN_KEY, home)
    )


def test_a_gateway_too_old_for_commands_still_loads(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)
    del ctx.__class__.register_command

    try:
        hermie_plugin.register(ctx)
    finally:
        ctx.__class__.register_command = _register_command

    caps = contract.read_capabilities(uimeta.read_key(uimeta.PLUGIN_KEY, home))
    assert contract.CAP_COMMAND_ME not in caps
    assert contract.CAP_CONTEXT_TURN_CLAIM in caps


def test_a_switched_off_module_claims_nothing(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with(), settings={"modules.push": False})
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    hermie_plugin.register(ctx)

    advert = uimeta.read_key(uimeta.PLUGIN_KEY, home)
    assert advert["modules"]["push"] == "off"
    caps = contract.read_capabilities(advert)
    assert contract.CAP_PUSH_EXPO not in caps
    assert contract.CAP_PUSH_MUTE not in caps
    # Reading the per-user key is the plugin's, not the push module's.
    assert contract.CAP_UIMETA_PER_USER in caps
    assert "post_llm_call" not in ctx.hooks


def test_switching_off_profile_editing_takes_only_that_capability(tmp_path, monkeypatch):
    """Not a `modules.*` switch: there is no `modules.profiles` and none is
    needed, since there is no hook or prompt section to skip loading."""
    home, ctx = gateway(tmp_path, app_meta=app_meta_with(), settings={"profiles.edit": False})
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    hermie_plugin.register(ctx)

    caps = contract.read_capabilities(uimeta.read_key(uimeta.PLUGIN_KEY, home))
    assert contract.CAP_PROFILE_DISPLAY_NAME not in caps
    assert contract.CAP_PUSH_EXPO in caps


def test_the_advert_never_touches_the_app_key(tmp_path, monkeypatch):
    """`hermie-app` is the app's, and it holds a compare-and-swap revision for it."""
    home, ctx = gateway(tmp_path, app_meta=app_meta_with(registrations={"i1": expo_registration()}))
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    hermie_plugin.register(ctx)

    document = yaml.safe_load((home / "profile.yaml").read_text())
    assert document["ui_meta"]["hermie-app"]["push"]["registrations"]["i1"]["v"] == 1
    assert document["_ui_meta_revisions"]["hermie-app"] == 7  # untouched
    assert document["_ui_meta_revisions"][uimeta.PLUGIN_KEY] == 1


def test_unloading_takes_the_advert_down(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    hermie_plugin.register(ctx)
    for callback in ctx.unloads:
        callback()

    assert uimeta.read_key(uimeta.PLUGIN_KEY, home) is None


def test_writing_an_app_key_is_refused_outright():
    for key in (uimeta.APP_KEY, uimeta.app_key_for("u1")):
        try:
            uimeta.write_key(key, {"anything": True})
        except ValueError as exc:
            assert "belongs to the app" in str(exc)
        else:
            raise AssertionError(f"writing {key} should be refused")


def test_the_per_user_key_is_read_for_push(tmp_path, monkeypatch):
    home, ctx = gateway(
        tmp_path,
        per_user={
            "u1": {
                "v": 1,
                "push": {"registrations": {"i1": expo_registration()}},
            }
        },
    )
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    hermie_plugin.register(ctx)

    module = push_pkg.PushModule(hermie_plugin.Runtime(ctx, home=home))
    assert [r.user_id for r in module.section().registrations] == ["u1"]


def test_a_device_in_both_keys_is_notified_once(tmp_path, monkeypatch):
    """The app writes both while it migrates; that must not double a buzz."""
    home, ctx = gateway(
        tmp_path,
        app_meta=app_meta_with(registrations={"i1": expo_registration()}),
        per_user={"u1": {"v": 1, "push": {"registrations": {"i1": expo_registration()}}}},
    )
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    sent = []
    monkeypatch.setattr(
        push_pkg.expo, "send",
        lambda batch: sent.extend(batch) or [
            push_pkg.expo.Ticket(token=m["to"], status="ok", receipt_id="r") for m in batch
        ],
    )

    module = push_pkg.PushModule(hermie_plugin.Runtime(ctx, home=home))
    note = events.from_approval(bot="b", session_key="s", description="d", request_id="r", turn_id="t", at=10)
    assert module.deliver(note) == 1
    assert len(sent) == 1


# -- push, end to end --------------------------------------------------------


def test_an_approval_becomes_a_notification(tmp_path, monkeypatch):
    home, ctx = gateway(
        tmp_path, app_meta=app_meta_with(registrations={"i1": expo_registration()})
    )
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    captured = []
    monkeypatch.setattr(push_pkg.expo, "send", lambda batch: captured.append(batch) or [
        push_pkg.expo.Ticket(token=message["to"], status="ok", receipt_id="r") for message in batch
    ])

    runtime = hermie_plugin.Runtime(ctx, home=home)
    module = push_pkg.PushModule(runtime)

    # Take the decision the hook would take, then deliver it on this thread:
    # the worker is what the queue test covers, and racing it here would only
    # make this assertion flaky.
    built = []
    monkeypatch.setattr(module, "offer", lambda note, delay=False: built.append(note))
    module.on_pre_approval_request(
        surface="gateway", session_key="s1", description="delete the build directory",
        request_id="req-1", turn_id="t1", command="remove-build",
    )
    assert len(built) == 1
    assert module.deliver(built[0]) == 1

    assert captured, "nothing reached the transport"
    message = captured[0][0]
    assert message["data"]["type"] == "request"
    assert message["data"]["bot"] == "jurist"
    assert message["title"] == "jurist"
    # The default payload says who and what kind, never what was said.
    assert "preview" not in message["data"]
    assert "remove-build" not in json.dumps(message)


def test_the_same_approval_twice_buzzes_once(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with(registrations={"i1": expo_registration()}))
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    monkeypatch.setattr(
        push_pkg.expo, "send",
        lambda batch: [push_pkg.expo.Ticket(token=m["to"], status="ok", receipt_id="r") for m in batch],
    )

    module = push_pkg.PushModule(hermie_plugin.Runtime(ctx, home=home))
    note = events.from_approval(
        bot="jurist", session_key="s1", description="d", request_id="req-1", turn_id="t1", at=10
    )
    assert module.deliver(note) == 1
    assert module.deliver(note) == 0


def test_a_dead_device_is_retired_and_then_skipped(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with(registrations={"i1": expo_registration()}))
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    monkeypatch.setattr(
        push_pkg.expo, "send",
        lambda batch: [
            push_pkg.expo.Ticket(token=m["to"], status="error", error="DeviceNotRegistered") for m in batch
        ],
    )

    module = push_pkg.PushModule(hermie_plugin.Runtime(ctx, home=home))
    first = events.from_approval(bot="b", session_key="s", description="d", request_id="r1", turn_id="t", at=10)
    assert module.deliver(first) == 0

    second = events.from_approval(bot="b", session_key="s", description="d", request_id="r2", turn_id="t", at=11)
    assert module.deliver(second) == 0

    # The plugin records the retirement in its own state; it never edits the
    # app's registration, which the app owns.
    assert module.runtime.state.is_retired("i1", updated_at=1789957143) is True
    document = yaml.safe_load((home / "profile.yaml").read_text())
    assert "i1" in document["ui_meta"]["hermie-app"]["push"]["registrations"]


def test_an_unregistered_gateway_sends_nothing(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    def explode(batch):
        raise AssertionError("nothing should be sent")

    monkeypatch.setattr(push_pkg.expo, "send", explode)
    module = push_pkg.PushModule(hermie_plugin.Runtime(ctx, home=home))
    note = events.from_approval(bot="b", session_key="s", description="d", request_id="r", turn_id="t", at=10)
    assert module.deliver(note) == 0


# -- context, end to end -----------------------------------------------------


def test_a_turn_from_a_named_sender_adds_nothing_since_no_rung_is_verified(tmp_path, monkeypatch):
    """HERM-119: there is no more profile to render, and `VERIFIED_RUNGS` is
    still empty, so a plain sender never earns a line on the turn either."""
    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)
    hermie_plugin.register(ctx)

    assert ctx.fire("pre_llm_call", session_id="s1", sender_id="u1") == [None]


def test_a_turn_from_nobody_is_just_as_quiet(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)
    hermie_plugin.register(ctx)

    assert ctx.fire("pre_llm_call", session_id="s1", sender_id="") == [None]


def test_a_smart_approval_asks_nobody_and_so_tells_nobody(tmp_path, monkeypatch):
    """The smart path answers itself; there is no question on a lock screen."""
    home, ctx = gateway(tmp_path, app_meta=app_meta_with(registrations={"i1": expo_registration()}))
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    module = push_pkg.PushModule(hermie_plugin.Runtime(ctx, home=home))
    built = []
    monkeypatch.setattr(module, "offer", lambda note, delay=False: built.append(note))
    module.on_pre_approval_request(surface="smart", session_key="s1", description="d", request_id="r")
    assert built == []


def test_the_worker_delivers_what_was_offered(tmp_path, monkeypatch):
    """The one test that does exercise the thread, since everything rides it."""
    import time

    home, ctx = gateway(tmp_path, app_meta=app_meta_with(registrations={"i1": expo_registration()}))
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    seen = []
    monkeypatch.setattr(
        push_pkg.expo, "send",
        lambda batch: seen.append(batch) or [
            push_pkg.expo.Ticket(token=m["to"], status="ok", receipt_id="r") for m in batch
        ],
    )

    module = push_pkg.PushModule(hermie_plugin.Runtime(ctx, home=home))
    module.offer(
        events.from_approval(bot="b", session_key="s", description="d", request_id="r", turn_id="t", at=10)
    )

    deadline = time.time() + 5
    while not seen and time.time() < deadline:
        time.sleep(0.01)
    module.stop()
    assert seen, "the worker never delivered"


def test_a_full_queue_drops_rather_than_slowing_the_turn(tmp_path, monkeypatch):
    """Every hook this plugin registers sits on the agent's own path."""
    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    module = push_pkg.PushModule(hermie_plugin.Runtime(ctx, home=home))
    monkeypatch.setattr(module, "_ensure_worker", lambda: None)
    note = events.from_approval(bot="b", session_key="s", description="d", request_id="r", turn_id="t", at=10)
    for _ in range(push_pkg.QUEUE_SIZE + 10):
        module.offer(note)  # must never raise
    assert module.queue.full()



def test_an_offer_after_stop_starts_no_second_sender(tmp_path, monkeypatch):
    """The old sender may still be delivering; a second one would race it."""
    import threading

    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    module = push_pkg.PushModule(hermie_plugin.Runtime(ctx, home=home))
    started = threading.Event()
    release = threading.Event()
    delivered = []

    def slow_deliver(note):
        delivered.append(note)
        started.set()
        release.wait(5)
        return 0

    monkeypatch.setattr(module, "deliver", slow_deliver)
    note = events.from_approval(bot="b", session_key="s", description="d", request_id="r", turn_id="t", at=10)
    module.offer(note)
    assert started.wait(5), "the sender never picked the first notification up"
    first = module.worker

    def senders():
        return {thread for thread in threading.enumerate() if thread.name == "hermie-push"}

    before = senders()
    module.stop()
    module.offer(note)

    assert senders() - before == set(), "a second sender was started"
    assert first.is_alive()
    release.set()
    first.join(5)
    assert not first.is_alive()
    assert len(delivered) == 1
    assert module.worker is None


def test_a_probe_unload_leaves_the_serving_advert_alone(tmp_path, monkeypatch):
    """`hermes plugins doctor` registers against a probe context and unloads it
    again. That unload must not erase the advert of the gateway that is
    actually serving, or the app stops offering push until the next restart."""
    home, serving = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    stamps = iter([1_000, 2_000])
    monkeypatch.setattr(contract.time, "time", lambda: next(stamps))

    hermie_plugin.register(serving)
    _, probe = gateway(tmp_path, app_meta=app_meta_with())
    hermie_plugin.register(probe)

    # The probe's advert is the newer one; withdrawing it is its own business.
    for callback in probe.unloads:
        callback()
    assert uimeta.read_key(uimeta.PLUGIN_KEY, home) is None

    # The serving process republishes; a stale unload from the earlier
    # registration must not touch what is on disk now.
    monkeypatch.setattr(contract.time, "time", lambda: 3_000)
    hermie_plugin.publish(hermie_plugin.Runtime(serving), {"push": "on"}, ["push.expo"])
    for callback in serving.unloads:
        callback()
    assert uimeta.read_key(uimeta.PLUGIN_KEY, home)["updatedAt"] == 3_000


# -- what one delivered payload actually carries -----------------------------


def sent_payloads(module, monkeypatch, push_pkg, notification):
    """Deliver one notification and hand back the `data` bag of every message."""
    captured = []
    monkeypatch.setattr(
        push_pkg.expo, "send",
        lambda batch: captured.extend(batch) or [
            push_pkg.expo.Ticket(token=m["to"], status="ok", receipt_id="r") for m in batch
        ],
    )
    module.deliver(notification)
    return [message["data"] for message in captured]


def an_approval():
    return events.from_approval(
        bot="jurist", session_key="s1", description="d", request_id="r1", turn_id="t1", at=10
    )


def test_a_delivered_payload_names_the_gateway_the_device_registered_against(tmp_path, monkeypatch):
    home, ctx = gateway(
        tmp_path,
        app_meta=app_meta_with(
            registrations={"i1": expo_registration(gatewayKey="bf796761db84e312")}
        ),
    )
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    module = push_pkg.PushModule(hermie_plugin.Runtime(ctx, home=home))
    payload = sent_payloads(module, monkeypatch, push_pkg, an_approval())[0]

    assert payload["gatewayKey"] == "bf796761db84e312"


def test_a_row_with_no_key_falls_back_to_the_configured_origin(tmp_path, monkeypatch):
    """Every registration written by an app build older than this one."""
    home, ctx = gateway(
        tmp_path,
        app_meta=app_meta_with(registrations={"i1": expo_registration()}),
        settings={"push.public_url": "https://gateway.example.com:8443/hermes"},
    )
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    module = push_pkg.PushModule(hermie_plugin.Runtime(ctx, home=home))
    payload = sent_payloads(module, monkeypatch, push_pkg, an_approval())[0]

    assert payload["gatewayKey"] == "bf796761db84e312"


def test_a_gateway_that_cannot_name_itself_sends_what_it_always_sent(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with(registrations={"i1": expo_registration()}))
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    module = push_pkg.PushModule(hermie_plugin.Runtime(ctx, home=home))

    assert "gatewayKey" not in sent_payloads(module, monkeypatch, push_pkg, an_approval())[0]


def test_the_gateway_key_capability_is_advertised(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    hermie_plugin.register(ctx)

    assert contract.CAP_PUSH_GATEWAY_KEY in contract.read_capabilities(
        uimeta.read_key(uimeta.PLUGIN_KEY, home)
    )


def test_a_chat_the_person_silenced_is_not_delivered(tmp_path, monkeypatch):
    """The per-chat overrides, through the section the app really writes."""
    home, ctx = gateway(
        tmp_path,
        per_user={
            "u1": {
                "v": 1,
                "push": {
                    "registrations": {"i1": expo_registration()},
                    "perBot": {"jurist": {"message": False}},
                },
            }
        },
    )
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    module = push_pkg.PushModule(hermie_plugin.Runtime(ctx, home=home))
    note = events.from_assistant_message(
        bot="jurist", session_id="s1", turn_id="t1", assistant_response="hello", at=10
    )

    assert sent_payloads(module, monkeypatch, push_pkg, note) == []


def test_the_per_bot_capability_is_advertised(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    hermie_plugin.register(ctx)

    assert contract.CAP_PUSH_PER_BOT in contract.read_capabilities(
        uimeta.read_key(uimeta.PLUGIN_KEY, home)
    )


def test_a_delivered_payload_says_which_conversation_it_is_about(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with(registrations={"i1": expo_registration()}))
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    monkeypatch.setattr(push_pkg.sessions, "read_title", lambda session_id: "Branch \u00b7 late filing")

    module = push_pkg.PushModule(hermie_plugin.Runtime(ctx, home=home))
    note = events.from_assistant_message(
        bot="jurist", session_id="stored-abc123", turn_id="t1", assistant_response="hello", at=10
    )
    payload = sent_payloads(module, monkeypatch, push_pkg, note)[0]

    assert payload["sessionId"] == "stored-abc123"
    assert payload["sessionKind"] == "branch"


def test_a_session_this_gateway_cannot_read_carries_no_kind(tmp_path, monkeypatch):
    """A bare checkout has no session registry, which is also the state a
    gateway too old for one is in."""
    home, ctx = gateway(tmp_path, app_meta=app_meta_with(registrations={"i1": expo_registration()}))
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    module = push_pkg.PushModule(hermie_plugin.Runtime(ctx, home=home))
    note = events.from_assistant_message(
        bot="jurist", session_id="stored-abc123", turn_id="t1", assistant_response="hello", at=10
    )

    assert "sessionKind" not in sent_payloads(module, monkeypatch, push_pkg, note)[0]


def test_the_session_kind_is_read_once_for_every_device(tmp_path, monkeypatch):
    """It is a database read and every copy of one notification is about the
    same session."""
    home, ctx = gateway(
        tmp_path,
        app_meta=app_meta_with(
            registrations={
                "i1": expo_registration(),
                "i2": expo_registration(token="ExponentPushToken[bbbbbbbbbbbbbbbbbbbbbb]"),
            }
        ),
    )
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    reads = []
    monkeypatch.setattr(
        push_pkg.sessions, "read_title", lambda session_id: reads.append(session_id) or "Bot Chat"
    )

    module = push_pkg.PushModule(hermie_plugin.Runtime(ctx, home=home))
    note = events.from_assistant_message(
        bot="jurist", session_id="s1", turn_id="t1", assistant_response="hello", at=10
    )
    payloads = sent_payloads(module, monkeypatch, push_pkg, note)

    assert len(payloads) == 2
    assert [payload["sessionKind"] for payload in payloads] == ["canonical", "canonical"]
    assert reads == ["s1"]


def test_the_session_kind_capability_follows_the_registry_being_there(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    # No Hermes on the path here, so nothing to read a session title from.
    hermie_plugin.register(ctx)
    caps = contract.read_capabilities(uimeta.read_key(uimeta.PLUGIN_KEY, home))
    assert contract.CAP_PUSH_SESSION_KIND not in caps

    monkeypatch.setattr(push_pkg.sessions, "available", lambda: True)
    module = push_pkg.PushModule(hermie_plugin.Runtime(ctx, home=home))
    assert contract.CAP_PUSH_SESSION_KIND in module.capabilities()
