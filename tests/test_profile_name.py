"""Setting a profile's display name: validation, profile scoping, and the advert.

Mirrors `test_memory.py`'s Hermes-facing half: a fake `hermes_constants` and a
fake `hermes_cli.profiles`/`hermes_cli.config` stand in for Hermes, because it
is not importable here, and what is worth pinning is the plugin's own contract
with those modules rather than anything Hermes itself does.

The one thing genuinely different from `memory`'s version of this is the split
between `ProfileRefused` (400: never a profile name) and `ProfileNotFound`
(404: a real name, just not one this gateway has) — see `profile_name.py`'s
module docstring for why the two cannot share an exception the way memory's
routes do.
"""

import sys
import types

import pytest

from hermie_plugin import contract, profile_name


@pytest.fixture
def hermes(monkeypatch):
    """A stand-in for the Hermes modules this module reaches."""
    bound = {"home": None, "resets": 0, "config": {}, "written": []}

    constants = types.ModuleType("hermes_constants")

    def set_override(path):
        bound["home"] = str(path)
        return object()

    def reset_override(_token):
        bound["home"] = None
        bound["resets"] += 1

    constants.set_hermes_home_override = set_override
    constants.reset_hermes_home_override = reset_override

    def write_profile_meta(profile_dir, *, description=None, description_auto=None, display_name=None):
        bound["written"].append(
            {
                "profile_dir": str(profile_dir),
                "description": description,
                "description_auto": description_auto,
                "display_name": display_name,
            }
        )

    profiles = types.ModuleType("hermes_cli.profiles")
    profiles.list_profile_names = lambda: ["default", "jurist", "marketing"]
    profiles.get_profile_dir = lambda name: f"/homes/{name}"
    profiles.write_profile_meta = write_profile_meta

    config = types.ModuleType("hermes_cli.config")
    config.load_config = lambda: bound["config"]

    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.profiles = profiles
    hermes_cli.config = config

    for name, module in (
        ("hermes_constants", constants),
        ("hermes_cli", hermes_cli),
        ("hermes_cli.profiles", profiles),
        ("hermes_cli.config", config),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return bound


def settings_config(**profiles_settings):
    return {"plugins": {"entries": {"hermie": {"settings": {"profiles": profiles_settings}}}}}


# -- profile resolution -------------------------------------------------


def test_a_known_profile_resolves_to_its_own_home(hermes):
    assert str(profile_name.profile_home("jurist")) == "/homes/jurist"


@pytest.mark.parametrize(
    "name",
    ["../../etc", "jurist/../marketing", "/etc/passwd", "..", ".", "~", "a\\b", "with\0null", "c:name"],
)
def test_a_name_that_is_really_a_path_is_refused(hermes, name):
    with pytest.raises(profile_name.ProfileRefused):
        profile_name.profile_home(name)


@pytest.mark.parametrize("name", [None, "", "   ", " jurist", "jurist "])
def test_an_absent_or_padded_name_is_refused(hermes, name):
    with pytest.raises(profile_name.ProfileRefused):
        profile_name.profile_home(name)


def test_a_well_formed_but_unknown_profile_is_not_found_rather_than_refused(hermes):
    """The whole reason this module keeps its own profile lookup: memory's
    routes fold this into the same 400 a bad name gets, and this route owes
    the app a 404 instead."""
    with pytest.raises(profile_name.ProfileNotFound):
        profile_name.profile_home("somebody-elses")


# -- display name validation ---------------------------------------------


@pytest.mark.parametrize("value", ["", "   ", None])
def test_an_empty_display_name_is_refused(value):
    with pytest.raises(profile_name.DisplayNameRefused):
        profile_name.clean_display_name(value)


def test_a_display_name_over_the_limit_is_refused():
    with pytest.raises(profile_name.DisplayNameRefused):
        profile_name.clean_display_name("x" * (profile_name.MAX_DISPLAY_NAME_LENGTH + 1))


def test_a_display_name_at_the_limit_is_accepted():
    value = "x" * profile_name.MAX_DISPLAY_NAME_LENGTH
    assert profile_name.clean_display_name(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "Ju\x00rist", "Jurist\x07", "a\nb", "a\tb",
        "a\x85b",          # C1 control (NEL)
        "\u202etsiruJ",    # right-to-left override: reads as "Jurist" backwards
        "Ju\u200brist",    # zero-width space
        "Ju\u200drist",    # zero-width joiner
        "Ju\u2066rist",    # left-to-right isolate
        "a\u2028b",        # line separator
        "a\u2029b",        # paragraph separator
        "\ufeffJurist",    # byte order mark
    ],
)
def test_a_control_or_formatting_character_is_refused(value):
    with pytest.raises(profile_name.DisplayNameRefused):
        profile_name.clean_display_name(value)


@pytest.mark.parametrize("value", ["Zoë", "Ünïcödé Bot", "研究助手", "Owl 🦉", "Café ☕️"])
def test_ordinary_unicode_is_accepted(value):
    assert profile_name.clean_display_name(value) == value


def test_surrounding_whitespace_is_trimmed():
    assert profile_name.clean_display_name("  Jurist  ") == "Jurist"


# -- the write -------------------------------------------------------------


def test_setting_writes_only_the_display_name_key(hermes):
    cleaned = profile_name.set_display_name("/homes/jurist", "  Jurist  ")

    assert cleaned == "Jurist"
    assert hermes["written"] == [
        {"profile_dir": "/homes/jurist", "description": None, "description_auto": None, "display_name": "Jurist"}
    ]


def test_setting_a_bad_display_name_writes_nothing(hermes):
    with pytest.raises(profile_name.DisplayNameRefused):
        profile_name.set_display_name("/homes/jurist", "")

    assert hermes["written"] == []


# -- the per-profile setting -----------------------------------------------


def test_editing_is_on_by_default(hermes):
    hermes["config"] = {}

    assert profile_name.target_profile_edit_enabled("/homes/jurist") is True


def test_editing_can_be_switched_off_for_that_profile(hermes):
    hermes["config"] = settings_config(edit=False)

    assert profile_name.target_profile_edit_enabled("/homes/jurist") is False


def test_unreadable_config_defaults_to_on(hermes, monkeypatch):
    def broken():
        raise RuntimeError("no config here")

    monkeypatch.setattr(sys.modules["hermes_cli.config"], "load_config", broken)

    assert profile_name.target_profile_edit_enabled("/homes/jurist") is True


def test_the_home_override_is_set_and_put_back(hermes):
    hermes["config"] = settings_config(edit=True)

    profile_name.target_profile_edit_enabled("/homes/marketing")

    assert hermes["home"] is None and hermes["resets"] == 1


# -- the advert --------------------------------------------------------------


class FakeRuntime:
    def __init__(self, settings=None):
        self.settings = settings or {}

    def config(self, key, default=None):
        return self.settings.get(key, default)


def module_with(settings=None):
    return profile_name.ProfileNameModule(FakeRuntime(settings))


def test_the_capability_is_advertised_by_default():
    assert module_with().capabilities() == [contract.CAP_PROFILE_DISPLAY_NAME]


def test_switching_the_setting_off_takes_the_capability_with_it():
    assert module_with({"profiles.edit": False}).capabilities() == []


def test_registering_returns_a_module_the_advert_can_ask():
    class Runtime:
        def config(self, key, default=None):
            return default

    registered = profile_name.register(ctx=object(), runtime=Runtime())

    assert isinstance(registered, profile_name.ProfileNameModule)
    assert registered.capabilities() == [contract.CAP_PROFILE_DISPLAY_NAME]
