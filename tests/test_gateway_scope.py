"""One plugin in the gateway's home, hooks for every bot (`scope: gateway`).

Every Hermie bot is a Hermes profile, and a gateway runs a bot's turn in that
profile's own home. The plugin is installed and enabled in the gateway's home
only and declares `scope: gateway`, so a gateway that supports the key fires its
hooks for every bot's turns, with `ctx.profile_name` naming the bot. What the
gateway owns (device rows, advert, state, VAPID key) stays in the gateway's
home; what one bot owns (its display name, its sessions) is read from that bot's
own home. These tests hold both halves.
"""

import sys
import types
from pathlib import Path

import pytest
import yaml

import hermie_plugin
import hermie_plugin.push as push_pkg
from hermie_plugin import profile_name, uimeta
from hermie_plugin.push import events
from hermie_plugin.state import FileStore

ROOT = Path(__file__).resolve().parent.parent


class Ctx:
    """The slice of `ctx` the push path reads; `profile_name` moves per turn."""

    def __init__(self, profile="default"):
        self.profile_name = profile

    def get_config(self, key, default=None):
        return default


def expo_row():
    return {
        "v": 1,
        "transport": "expo",
        "token": "ExponentPushToken[abcdefghijklmnopqrstuv]",
        "platform": "android",
        "types": {name: True for name in events.TYPES},
        "preview": False,
        "updatedAt": 1789957143,
    }


def write_profile(home: Path, document: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "profile.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    """The gateway's home with one device row and its own label, and a bot `scout` beside it."""
    home = tmp_path / "hermes"
    write_profile(home, {
        "display_name": "Gateway Bot",
        "ui_meta": {"hermie-app:u1": {"v": 1, "push": {"registrations": {"i1": expo_row()}}}},
    })
    scout = home / "profiles" / "scout"
    write_profile(scout, {"display_name": "Scout the Researcher"})
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    homes = {"scout": scout}

    def resolve(name):
        if name not in homes:
            raise profile_name.ProfileNotFound(name)
        return homes[name]

    monkeypatch.setattr(profile_name, "profile_home", resolve)
    ctx = Ctx()
    runtime = hermie_plugin.Runtime(ctx, home=home, store=FileStore(tmp_path / "state.json"))
    return home, scout, ctx, push_pkg.PushModule(runtime)


def delivered(module, monkeypatch, notification):
    captured = []
    monkeypatch.setattr(
        push_pkg.expo, "send",
        lambda batch: captured.extend(batch) or [
            push_pkg.expo.Ticket(token=m["to"], status="ok", receipt_id="r") for m in batch
        ],
    )
    module.deliver(notification)
    return captured


def approval(bot):
    return events.from_approval(
        bot=bot, session_key="s1", description="d", request_id=f"r-{bot}", turn_id="t1", at=10
    )


def test_the_manifest_asks_for_gateway_scope():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["scope"] == "gateway"


def test_a_routed_bots_turn_is_about_that_bot(gateway, monkeypatch):
    """The gateway fires the hook in the bot's turn, where `ctx.profile_name` names it."""
    _, _, ctx, module = gateway
    offered = []
    monkeypatch.setattr(module, "offer", lambda note, **kw: offered.append(note))

    ctx.profile_name = "scout"
    module.on_post_llm_call(session_id="s1", turn_id="t1", assistant_response="done")

    assert [note.bot for note in offered] == ["scout"]


def test_a_routed_bot_is_shown_under_its_own_label(gateway, monkeypatch):
    _, _, _, module = gateway
    sent = delivered(module, monkeypatch, approval("scout"))
    assert sent[0]["title"] == "Scout the Researcher"
    assert sent[0]["data"]["bot"] == "scout"


def test_the_gateways_own_bot_keeps_the_gateways_label(gateway, monkeypatch):
    _, _, _, module = gateway
    sent = delivered(module, monkeypatch, approval("default"))
    assert sent[0]["title"] == "Gateway Bot"


def test_a_bot_whose_home_cannot_be_found_is_shown_by_profile_name(gateway, monkeypatch):
    """Never the gateway's label: that is another bot's name."""
    _, _, _, module = gateway
    sent = delivered(module, monkeypatch, approval("ghost"))
    assert sent[0]["title"] == "ghost"


def test_a_routed_bots_session_kind_is_read_from_its_own_state_db(gateway, monkeypatch):
    _, scout, _, module = gateway
    opened = []

    class Database:
        def get_session_title(self, session_id):
            return "Branch" if session_id == "s-scout" else None

    registry = types.ModuleType("hermes_state_registry")
    registry.acquire = lambda path=None: opened.append(path) or Database()
    registry.release = lambda database: True
    monkeypatch.setitem(sys.modules, "hermes_state_registry", registry)

    note = events.from_assistant_message(
        bot="scout", session_id="s-scout", turn_id="t1", assistant_response="hello", at=10
    )
    sent = delivered(module, monkeypatch, note)

    assert opened == [scout / "state.db"]
    assert sent[0]["data"]["sessionKind"] == "branch"


def test_the_gateways_own_session_kind_reads_the_active_database(gateway, monkeypatch):
    _, _, _, module = gateway
    opened = []

    class Database:
        def get_session_title(self, session_id):
            return "Bot Chat"

    registry = types.ModuleType("hermes_state_registry")
    registry.acquire = lambda path=None: opened.append(path) or Database()
    registry.release = lambda database: True
    monkeypatch.setitem(sys.modules, "hermes_state_registry", registry)

    note = events.from_assistant_message(
        bot="default", session_id="s0", turn_id="t1", assistant_response="hello", at=10
    )
    sent = delivered(module, monkeypatch, note)

    assert opened == [None]
    assert sent[0]["data"]["sessionKind"] == "canonical"


def test_an_unknown_bots_session_kind_is_left_out(gateway, monkeypatch):
    _, _, _, module = gateway
    note = events.from_assistant_message(
        bot="ghost", session_id="s1", turn_id="t1", assistant_response="hello", at=10
    )
    assert "sessionKind" not in delivered(module, monkeypatch, note)[0]["data"]


def test_device_rows_and_state_stay_in_the_gateways_home(gateway, monkeypatch, tmp_path):
    """A routed bot's notification is still sent to the gateway's own device rows."""
    home, scout, _, module = gateway
    sent = delivered(module, monkeypatch, approval("scout"))
    assert len(sent) == 1
    assert not (scout / "plugin-data").exists()
    assert (tmp_path / "state.json").exists()
