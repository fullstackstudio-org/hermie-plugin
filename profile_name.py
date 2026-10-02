"""Setting a profile's display name, from inside the gateway process.

Mounted the same way `memory` is — on the dashboard's own plugin router,
`dashboard/plugin_api.py` — because it answers the same kind of question: an
edit to a profile's own file, on the dashboard's port, behind the dashboard's
auth, with no route on this gateway ever seeing a user, a role or an owner.

Hermes already has a route for this, `PATCH /api/profiles/{name}` (see
`docs/DESIGN.md` §8), and this is deliberately not a second way to reach it.
That route takes `new_name` and *renames* a profile — directory, wrapper
script, service, active-profile pointer — except for `default`, whose home
IS the installation root and so cannot be renamed at all; there the call is
turned into exactly this field instead. This route is the general case that
falls out of that special case: it writes only the presentation label, the
same `display_name` key in `profile.yaml`, on ANY profile, and never touches
the id, the directory or anything a rename would move. `hermes_cli.profiles
.write_profile_meta` is the one function that does the write either way, so
both routes end up changing the same key through the same call.

**Auth is the dashboard's**, the same all-or-nothing model `memory` documents:
whoever is signed in to the dashboard is an operator of the machine, and nei-
ther this module nor its route knows anything about who that is.

**Profile resolution is deliberately not shared with `memory`.** Both modules
clean a caller-supplied name the same way and check it against the gateway's
real profile list, but they answer a wrong one differently: memory's routes
have always folded "not a valid name" and "not a profile that exists" into one
400, while this route owes the app a 404 for the second case, so an unknown
profile and a mistyped one do not read the same way here. Sharing the helper
would mean one of the two callers gets the other's answer, so the handful of
lines are kept here instead.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any, List

from . import contract

NAME = "profiles"

# An app-facing label gets a shorter, rounder cap than Hermes' own setter
# (64 chars, `hermes_cli.profiles.set_profile_display_name`) and a ban on
# control characters the file format tolerates but no UI should have to
# render.
MAX_DISPLAY_NAME_LENGTH = 60

# Characters a label is refused for, by Unicode category. Cc is the control
# characters, C0 and C1. Cf is the invisible formatting ones — the bidi
# overrides that make a name read backwards (U+202E), zero-width spaces and
# joiners — and Zl/Zp are the line and paragraph separators. A display name is
# shown on lock screens and in the app as the bot's name, and none of these
# belongs in a name somebody is asked to recognise. That also refuses an emoji
# built with a zero-width joiner, which is the price of refusing the rest.
UNFIT_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


class ProfileNameUnavailable(RuntimeError):
    """This process has no Hermes to ask — a test, a CLI, anything but a gateway."""


class ProfileRefused(ValueError):
    """The caller's name was never a profile identifier to begin with."""


class ProfileNotFound(LookupError):
    """A well-formed name, but this gateway has no profile by it."""


class DisplayNameRefused(ValueError):
    """The proposed display name is not one this route will write."""


# -- the profile --------------------------------------------------------


def _clean_profile(name: Any) -> str:
    """The name, or a refusal. Nothing here is turned into a path.

    Same shape as `memory._clean_profile`: rejected rather than sanitised, so a
    caller trying a traversal sees a refusal rather than a cleaned-up path.
    """
    text = str(name or "")
    if not text or text != text.strip():
        raise ProfileRefused("a profile name is required")
    if len(text) > 64:
        raise ProfileRefused("that is not a profile name")
    if any(bad in text for bad in ("/", "\\", "..", "\0", ":")) or text in (".", "~"):
        raise ProfileRefused("that is not a profile name")
    return text


def profile_names() -> List[str]:
    """Every profile this gateway has, including the default one."""
    try:
        from hermes_cli.profiles import list_profile_names  # type: ignore
    except Exception as exc:  # pragma: no cover - exercised only off a gateway
        raise ProfileNameUnavailable(f"hermes profiles are not importable here: {exc}")
    names = [str(item) for item in (list_profile_names() or [])]
    return names if "default" in names else ["default"] + names


def profile_home(name: Any) -> Path:
    """The profile's home directory, or the specific refusal the caller earned.

    Raises `ProfileRefused` for a name that was never a profile identifier and
    `ProfileNotFound` for one that is well-formed but not on this gateway's own
    list — two different answers, on purpose: see the module docstring.
    """
    text = _clean_profile(name)
    if text not in profile_names():
        raise ProfileNotFound(text)
    try:
        from hermes_cli.profiles import get_profile_dir  # type: ignore
    except Exception as exc:
        raise ProfileNameUnavailable(f"hermes profiles are not importable here: {exc}")
    return Path(str(get_profile_dir(text)))


# -- the setting ----------------------------------------------------------


def target_profile_edit_enabled(home: Path) -> bool:
    """Whether *that profile's* own config.yaml allows this route to write it.

    A route has no plugin context to call `ctx.get_config` through, so it reads
    the same `plugins.entries.hermie.settings` a config.yaml holds, scoped to
    the profile actually being edited — which, on a multiplexed gateway, may
    not be the profile the dashboard process started under. Unreadable config
    means the default, which is on, the same posture `memory.settings_for`
    takes for the same reason: a profile whose config cannot be read has not
    asked for anything to be switched off.
    """
    try:
        from hermes_constants import (  # type: ignore
            reset_hermes_home_override,
            set_hermes_home_override,
        )
        from hermes_cli.config import load_config  # type: ignore
    except Exception:
        return True
    token = set_hermes_home_override(str(home))
    try:
        config = load_config() or {}
    except Exception:
        return True
    finally:
        reset_hermes_home_override(token)
    entry = (((config.get("plugins") or {}).get("entries") or {}).get("hermie") or {}).get(
        "settings"
    ) or {}
    section = entry.get("profiles") if isinstance(entry.get("profiles"), dict) else {}
    value = section.get("edit")
    return value if isinstance(value, bool) else True


# -- the write --------------------------------------------------------------


def clean_display_name(value: Any) -> str:
    """*value*, trimmed, or the refusal a bad one has earned."""
    text = str(value or "")
    cleaned = text.strip()
    if not cleaned:
        raise DisplayNameRefused("display_name must not be empty")
    if len(cleaned) > MAX_DISPLAY_NAME_LENGTH:
        raise DisplayNameRefused(
            f"display_name must be {MAX_DISPLAY_NAME_LENGTH} characters or fewer"
        )
    if any(unicodedata.category(ch) in UNFIT_CATEGORIES for ch in cleaned):
        raise DisplayNameRefused("display_name must not contain control or formatting characters")
    return cleaned


def set_display_name(home: Path, display_name: Any) -> str:
    """Write *display_name* into that profile's own `profile.yaml`, and nothing else.

    Delegates entirely to `hermes_cli.profiles.write_profile_meta` — the same
    function core's own `PATCH /api/profiles/{name}` calls for `default` — so
    the write is core's own atomic temp-file-and-rename, onto the file's own
    permissions, and only the `display_name` key is ever passed: `description`
    and `description_auto` stay `None`, which that function reads as "leave
    this alone".
    """
    cleaned = clean_display_name(display_name)
    try:
        from hermes_cli.profiles import write_profile_meta  # type: ignore
    except Exception as exc:
        raise ProfileNameUnavailable(f"hermes profiles are not importable here: {exc}")
    write_profile_meta(home, display_name=cleaned)
    return cleaned


# -- the module ---------------------------------------------------------


class ProfileNameModule:
    """What the plugin advertises about this route.

    Holds no handler — `dashboard/plugin_api.py` mounts it, the way it mounts
    memory's routes — only the switch, because a capability has to say whether
    this gateway will actually answer.
    """

    def __init__(self, runtime):
        self.runtime = runtime

    @property
    def edit_enabled(self) -> bool:
        return self.runtime.config("profiles.edit", True) is not False

    def capabilities(self) -> List[str]:
        return [contract.CAP_PROFILE_DISPLAY_NAME] if self.edit_enabled else []


def register(ctx, runtime) -> ProfileNameModule:
    """No hook and no route: the dashboard mounts that. Only the advert."""
    return ProfileNameModule(runtime)
