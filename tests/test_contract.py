"""The capability negotiation, from both sides.

These are the tests that matter for a plugin nobody updates: an app newer than
the plugin must degrade, and an app older than the plugin must ignore what it
does not know.
"""

import yaml
from pathlib import Path

from hermie_plugin import contract

ROOT = Path(__file__).resolve().parent.parent


def test_manifest_version_matches_the_code():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert manifest["version"] == contract.PLUGIN_VERSION
    assert manifest["name"] == "hermie"


def test_advert_carries_only_what_was_offered():
    advert = contract.advert(modules={"push": "on"}, capabilities=["push.expo", "push.expo"], now=1000)
    assert advert["capabilities"] == ["push.expo"]
    assert advert["v"] == contract.CONTRACT_VERSION
    assert advert["updatedAt"] == 1000


def test_a_newer_contract_reads_as_nothing():
    """An app must not guess at a shape it does not know."""
    future = {"v": contract.CONTRACT_VERSION + 1, "capabilities": ["push.telepathy"]}
    assert contract.read_capabilities(future) == []


def test_an_older_plugin_simply_offers_less():
    """The app tests for a string; it never compares version numbers."""
    old = contract.advert(modules={"push": "on"}, capabilities=[contract.CAP_PUSH_EXPO])
    caps = contract.read_capabilities(old)
    assert contract.CAP_PUSH_EXPO in caps
    assert contract.CAP_PUSH_WEBPUSH not in caps


def test_absent_advert_is_absent_plugin():
    assert contract.read_capabilities(None) == []
    assert contract.read_capabilities({}) == []


def test_planned_modules_are_advertised_but_not_claimed():
    advert = contract.advert(
        modules={"push": "on", "presence": "planned"}, capabilities=[contract.CAP_PUSH_EXPO]
    )
    assert advert["modules"]["presence"] == "planned"
    assert "presence" not in " ".join(advert["capabilities"])


def test_the_advert_says_which_shapes_this_gateway_understands():
    """An app that moves its key in front of an older plugin fails silently."""
    from hermie_plugin import IMPLEMENTED

    assert contract.CAP_UIMETA_PER_USER == "ui_meta.per_user"
    assert contract.CAP_PUSH_SEEN_PER_CHAT == "push.seen.per_chat"
    assert "push" in IMPLEMENTED


def test_the_relay_origins_are_published_only_when_given():
    assert "relayOrigins" not in contract.advert(modules={}, capabilities=[])
    assert contract.advert(modules={}, capabilities=[], relay_origins=())["relayOrigins"] == []
    published = contract.advert(modules={}, capabilities=[], relay_origins=("https://push.hermie.dev",))
    assert published["relayOrigins"] == ["https://push.hermie.dev"]
    # Additive: a reader of the shape this advert has always had is not disturbed.
    assert contract.read_capabilities(published) == []


def test_every_module_that_ships_has_its_switch_in_the_manifest():
    """`module_states` reads `modules.<name>` for each of these, defaulting to on."""
    from hermie_plugin import IMPLEMENTED

    schema = yaml.safe_load((ROOT / "plugin.yaml").read_text())["config_schema"]
    switches = {key[len("modules."):] for key in schema if key.startswith("modules.")}

    assert switches == set(IMPLEMENTED)
    for name in IMPLEMENTED:
        assert schema[f"modules.{name}"]["type"] == "bool"
        assert schema[f"modules.{name}"]["default"] is True
    assert not set(IMPLEMENTED) & set(contract.PLANNED_MODULES)


def test_the_dashboard_manifest_follows_the_plugin_version_and_keeps_its_tab_hidden():
    import json

    manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text())

    assert manifest["version"] == contract.PLUGIN_VERSION
    assert manifest["tab"] == {"hidden": True}


def test_the_web_block_is_published_only_when_given():
    block = {"path": "/dashboard-plugins/hermie/app/index.html", "version": "0.2.0", "commit": "0123456789ab", "files": 6, "bytes": 1}

    assert "web" not in contract.advert(modules={}, capabilities=[])
    published = contract.advert(modules={"web": "on"}, capabilities=[contract.CAP_WEB_CLIENT], web=block)
    assert published["web"] == block and published["web"] is not block
    assert contract.read_capabilities(published) == ["web.client"]
