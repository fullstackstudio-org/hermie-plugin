"""Hermie's gateway-side companion.

One plugin, several modules, one advert. `register(ctx)` builds a `Runtime` —
the small surface every module is allowed to use, so a module never reaches into
Hermes internals directly and a test can hand it a fake — then loads the modules
that are switched on, collects what they can actually do, and publishes that as
a capability list in the gateway's own `ui_meta`.

Modules are the reason this is one plugin rather than several. A person installs
a plugin once; asking them to install five is asking them to install none. So
the ones that do not exist yet are still named here, still have a config key,
and are advertised as `planned` — the app can tell "too old" from "switched off"
from "not built yet" without the config surface changing shape when they land.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import contract, uimeta, update
from .state import State

logger = logging.getLogger(__name__)

__version__ = contract.PLUGIN_VERSION

# Modules that ship, each on unless told otherwise; the rest are named in
# contract.PLANNED_MODULES and load nothing. `web` is the client build in
# `dashboard/app/`: on means it is checked at load and advertised when intact.
IMPLEMENTED = ("push", "context", "memory", "web")


def _has_state(ctx: Any) -> bool:
    try:
        ctx.state.get  # a facade that cannot be read is not a store
        return True
    except Exception:
        return False


class Runtime:
    """Everything a module may touch, and nothing else.

    Modules take this rather than `ctx` so that the places where the plugin
    depends on Hermes are countable, and so the tests can drive a module with a
    plain object instead of a gateway.
    """

    def __init__(self, ctx: Any, *, home: Optional[Path] = None, store: Any = None):
        self.ctx = ctx
        self.home = home or uimeta.hermes_home()
        # A context with no state facade is one built for probing (`hermes
        # plugins validate`, `doctor`): such a load may read what the serving
        # gateway keeps, and must not create any of it — the VAPID key above
        # all, which every browser subscribes with.
        self.probe = not _has_state(ctx)
        self.state = State(store if store is not None else self._store()).load()

    def _store(self) -> Any:
        """`ctx.state` when there is one, a file of our own when there is not.

        `hermes plugins validate` calls `register()` against a context built for
        probing, which has no state facade, and an older Hermes may not have one
        either. Neither is a reason to fail to load.
        """
        try:
            state = self.ctx.state
            state.get  # a facade that cannot be read is not a store
            return state
        except Exception:
            from .state import FileStore

            directory = self.home / "plugin-data" / "hermie"
            directory.mkdir(parents=True, exist_ok=True)
            return FileStore(directory / "state.json")

    def config(self, key: str, default: Any = None) -> Any:
        """One setting, from `plugins.entries.hermie.settings.<key>`.

        Hermes validates a setting against the manifest's `config_schema` and
        warns on a mismatch, but it never merges the schema's `default` into the
        config. The default that applies is the one passed here.
        """
        try:
            return self.ctx.get_config(key, default)
        except Exception:
            return default

    def bot_name(self) -> str:
        """Which bot this is.

        A Hermie bot is a Hermes profile, so the profile name is the name the
        person sees in their chat list.
        """
        try:
            return str(self.ctx.profile_name or "default")
        except Exception:
            return "default"

    def bot_display_name(self) -> str:
        """The name this bot is shown under, or ``""`` when it has none.

        `display_name` in the profile's own `profile.yaml` — the label a person
        gave the bot, which `profiles.display_name` also writes. Only a label
        that route would accept is returned, checked by the very function it
        uses (`profile_name.clean_display_name`): a string, trimmed, at most 60
        characters, with no control, invisible formatting (bidi overrides,
        zero-width characters) or line-separator characters. The file can be
        written by other hands than that route, so it is checked again here.
        Anything else reads as "no label", and the caller falls back to the
        profile name.
        """
        from .profile_name import DisplayNameRefused, clean_display_name

        value = uimeta.read_display_name(self.home)
        if not isinstance(value, str):
            return ""
        try:
            return clean_display_name(value)
        except DisplayNameRefused:
            return ""

    def app_sections(self) -> List[Tuple[str, Any]]:
        """Every bag the app owns, as `(user id, value)`, in precedence order.

        One key per person (`hermie-app:<user id>`) plus the older shared
        `hermie-app`, which is read for one more version. The legacy bag comes
        first and names nobody, so a caller that merges in order lets the
        per-user key win for the person it names.
        """
        return uimeta.read_app_sections(self.home)

    def app_stamp(self) -> Tuple[int, int]:
        """Whether anything the app wrote could have moved, in one `stat`.

        `app_sections` parses YAML, and the context module sits on the agent's
        own path where it would rather ask a cheap question first. See
        `uimeta.profile_stamp` for what the answer is worth.
        """
        return uimeta.profile_stamp(self.home)

    @property
    def data_dir(self) -> Path:
        """Where the plugin may keep files of its own (the VAPID key, mostly)."""
        try:
            return Path(str(self.ctx.state.data_dir))
        except Exception:
            directory = self.home / "plugin-data" / "hermie"
            directory.mkdir(parents=True, exist_ok=True)
            return directory


def module_states(runtime: Runtime) -> Dict[str, str]:
    """Every module's name mapped to `on`, `off` or `planned`."""
    states: Dict[str, str] = {}
    for name in IMPLEMENTED:
        states[name] = "on" if runtime.config(f"modules.{name}", True) else "off"
    for name in contract.PLANNED_MODULES:
        states[name] = "planned"
    return states


def update_fields(runtime: Runtime) -> Tuple[str, str]:
    """What the advert says about this build, and about a newer one.

    The installed ref is always read: it is two small file reads on the local
    machine and it is what lets an app work out whether an update exists without
    the gateway reaching anywhere. The newest release is asked for only when the
    operator said so, because an unprompted outbound request from somebody's
    gateway is a surprise on a product that promises no account and no call
    nobody asked for. See `update.py`.
    """
    try:
        ref = update.installed_ref(Path(__file__).resolve().parent)
    except Exception:
        ref = ""
    if runtime.config("update.check", False) is not True:
        return ref, ""
    try:
        return ref, str(update.check(runtime.state).get("latest") or "")
    except Exception as exc:
        logger.warning("hermie: could not check for a newer plugin: %s", exc)
        return ref, ""


def publish(
    runtime: Runtime,
    states: Dict[str, str],
    capabilities: List[str],
    installed_ref: str = "",
    latest: str = "",
    relay_origins: Optional[List[str]] = None,
    web: Optional[Dict[str, Any]] = None,
    web_push: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """Tell the app what this gateway can do.

    Written under the plugin's own `hermie-plugin` key, never under `hermie-app`:
    that one belongs to the app, which holds a compare-and-swap revision for it
    and would have its next write rejected if the plugin bumped it from behind.

    Returns the advert's `updatedAt` stamp, which is how the unload hook later
    tells its own advert from one written by another process.
    """
    try:
        value = contract.advert(
            modules=states,
            capabilities=capabilities,
            limits={"payloadBytes": 3500, "contextChars": int(runtime.config("context.max_chars", 1200) or 1200)},
            installed_ref=installed_ref,
            latest=latest,
            relay_origins=relay_origins,
            web=web,
            web_push=web_push,
        )
        uimeta.write_key(uimeta.PLUGIN_KEY, value, runtime.home)
        return int(value["updatedAt"])
    except Exception as exc:
        logger.warning("hermie: could not publish the plugin advert: %s", exc)


def register(ctx: Any) -> None:
    """Hermes calls this once, at load."""
    runtime = Runtime(ctx)
    states = module_states(runtime)
    # Not a module's: this build reads `hermie-app:<user id>` whichever modules
    # are switched on, and the app has to know that before it moves its bag.
    capabilities: List[str] = [contract.CAP_UIMETA_PER_USER]

    # Not gated behind `states`: like `update.check` below, setting a display
    # name has one switch of its own (`profiles.edit`) and no on/off "module"
    # around it, because there is no hook or system-prompt section to load or
    # skip — only a route the dashboard always mounts.
    from . import profile_name as profile_name_module

    capabilities.extend(profile_name_module.register(ctx, runtime).capabilities())

    # Which push relays this gateway posts to, published beside the
    # capabilities; absent when push is off. See `contract.advert`.
    relay_origins: Optional[List[str]] = None
    # This gateway's VAPID public key, beside `push.webpush.key`: the key every
    # browser subscribes with. Absent when push is off, when nothing here can
    # sign, or when the key file could not be read (it is never replaced).
    web_push_block: Optional[Dict[str, Any]] = None
    if states.get("push") == "on":
        from . import push as push_module

        push = push_module.register(ctx, runtime)
        capabilities.extend(push.capabilities())
        relay_origins = list(push.relay_origins)
        public_key = push.web_push_public_key()
        if public_key and contract.CAP_PUSH_WEBPUSH_KEY in capabilities:
            web_push_block = {"publicKey": public_key}

    if states.get("context") == "on":
        from . import context as context_module

        capabilities.extend(context_module.register(ctx, runtime).capabilities())

    if states.get("memory") == "on":
        from . import memory as memory_module

        capabilities.extend(memory_module.register(ctx, runtime).capabilities())

    # The bundled web client: hashed once, here, and never on a hook path. The
    # block and the capability travel together, and only when every file
    # matched its manifest. See web.py.
    web_block: Optional[Dict[str, Any]] = None
    if states.get("web") == "on":
        from . import web as web_module

        web = web_module.register(ctx, runtime)
        capabilities.extend(web.capabilities())
        web_block = web.advert_block()

    installed_ref, latest = update_fields(runtime)
    if latest:
        capabilities.append(contract.CAP_UPDATE_CHECK)

    stamp = publish(runtime, states, capabilities, installed_ref, latest, relay_origins, web_block, web_push_block)

    # An advert that outlives the plugin is a lie the app would act on, so the
    # key is removed on unload. A gateway that is killed rather than unloaded
    # leaves it behind; the `updatedAt` stamp is how the app notices.
    #
    # Only this process's own advert is removed: `hermes plugins doctor` and
    # `validate` register against a probe context and unload it again, and
    # without this check that unload would erase the advert of the gateway
    # that is actually serving, and the app would stop offering push until
    # the next restart.
    def withdraw() -> None:
        current = uimeta.read_key(uimeta.PLUGIN_KEY, runtime.home)
        if stamp is not None and isinstance(current, dict) and current.get("updatedAt") == stamp:
            uimeta.write_key(uimeta.PLUGIN_KEY, None, runtime.home)

    ctx.on_unload(withdraw)

    logger.info(
        "hermie %s loaded: %s",
        contract.PLUGIN_VERSION,
        ", ".join(f"{name}={state}" for name, state in sorted(states.items()) if state != "planned"),
    )
