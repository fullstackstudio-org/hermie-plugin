"""What the app is allowed to assume about the plugin it found.

People do not update plugins. A gateway that was set up once and works is a
gateway nobody logs into again, so every version of the app will meet plugins
older than itself for as long as the product exists. The way out is not to
promise that the newest plugin is installed; it is to make the app ask.

The plugin therefore publishes a small, versioned advert into the gateway's own
``ui_meta`` under the ``hermie-plugin`` key, and the app reads it the same way
it reads everything else — through ``profiles.list``, over the connection it
already has. There is no endpoint to dial: Hermes has no extension point for
adding an HTTP route to the gateway (see DESIGN.md), and an advert in ui_meta
needs none.

Two rules keep this honest in both directions:

- **A capability is a string, not a version number.** The app tests for
  ``"push.webpush"``; it does not test for ``version >= "0.4.0"`` and hope. A
  plugin that gains a capability adds a string, and every older app simply does
  not ask for it. `version` is carried for the human-readable "update available"
  line only.
- **An absent advert means an absent plugin.** An old plugin that never wrote
  the key, a plugin that is installed but disabled, and no plugin at all are
  indistinguishable to the app, and all three mean the same thing: do not offer
  the feature.

The other half of the contract is what the **app** writes, under its own key —
one per person, ``hermie-app:<user id>``, with the older shared ``hermie-app``
read for one more version. The readers are ``push/registrations.py`` and
``context/render.py``; this is the shape they agree on::

    hermie-app:<user id>:
      mutes:                        # bot -> until (unix seconds, 0 = forever)
        jurist: 0
        marketing: 1790000000
      push:
        registrations:
          <installation id>:
            v: 1
            transport: expo         # or "webpush", or "relay"
            token: "..."            # expo only
            endpoint: "..."         # webpush only, with keys.p256dh + keys.auth
            relay: "https://..."    # relay only, with handle + secret (and enc,
                                    # carried for the encrypted step); ios/macos
            platform: ios
            types: {message: true, request: true, cron: true,
                    cron_done: true, cron_failed: true,
                    turn_done: false, turn_failed: false}   # absent = off
            preview: false
            clears: true            # understands a clearing push; absent = does not (opt-in)
            gatewayKey: bf796761db84e312   # FNV-1a over the origin it registered against
            updatedAt: 1789957143
        perBot:                     # where one chat differs from the switches above
          jurist: {cron: false}
        seen:                       # the heartbeat, per device
          <installation id>: {bot: jurist, at: 1789957143}
      context:
        v: 1
        default: <user id>
        users:
          <user id>: {displayName: "...", userIdAlt: "...", about: "...",
                      device: {model: "...", os: "...", appVersion: "..."},
                      timezone: "Europe/Amsterdam", locale: "nl-NL",
                      perBot: {<bot>: "..."}, updatedAt: 1789957143}

Two of those are new and both are read tolerantly for one version: a ``seen``
value that is a bare number is the older "looking at some chat" shape, and the
whole bag may still be the shared ``hermie-app``. ``mutes`` is also read from
``push.mutes`` for an app that files it with the push settings.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List

# The plugin's own release version. Also in plugin.yaml; a test keeps them equal.
PLUGIN_VERSION = "0.11.0"

# Where an update comes from, named here so the advert and the update check
# cannot disagree about which repository this plugin is.
REPO = "fullstackstudio-org/hermie-plugin"

# The shape of the `hermie-plugin` ui_meta key. Bumped only when an existing
# field changes meaning — adding a field does not bump it, because a reader that
# does not know a field ignores it.
CONTRACT_VERSION = 1

# The highest shape this build could write if an app asked for one. Equal to
# CONTRACT_VERSION today and published anyway, so the field has a settled
# meaning on the day they diverge: `v` is the shape of THIS advert, `maxContract`
# is the newest shape this plugin knows how to speak.
MAX_CONTRACT = CONTRACT_VERSION

# The oldest Hermie app this plugin can serve. This is the one version
# comparison in the contract, and it runs the other way from the rule above: the
# APP must never test the plugin's version, because a plugin older than the
# string it is looking for simply does not publish it. A plugin naming a floor
# is different — it is the only way to tell somebody running a three-year-old
# build why nothing works, and an app too old to read this field was never going
# to be told anything anyway.
MIN_APP_VERSION = "1.0.0"

# Every capability this build can offer. A module contributes its own subset
# once it is enabled AND its prerequisites are actually present, so the advert
# describes what this gateway can do rather than what the code could do
# somewhere else. That distinction is the whole point: a Web Push capability on
# a gateway with no signing library is a promise the app would act on.
#
# Two of these say which *shape* this gateway understands, and they are here
# because getting them wrong is silent. An app that moves its bag to
# `hermie-app:<user id>` in front of a plugin that only reads `hermie-app`
# notifies nobody, and an app that writes a `{bot, at}` heartbeat to a plugin
# that only reads a number suppresses nothing. Both are questions the app must
# be able to ask before it writes, which is what a capability string is for.
CAP_PUSH_EXPO = "push.expo"
# Rows with `transport: relay` are served: this gateway posts to a push relay on
# its own allow-list, which is how a native Apple app is reached. An app that
# does not see it keeps its Expo row, and keeps being reached through Expo.
#
# Claimed only while the allow-list holds RELAY_DEFAULT_ORIGIN, the relay the
# Hermie apps register with. An app that switched to its relay row on seeing
# the string, in front of a gateway that does not post to that relay, would go
# silent — so until apps read `relayOrigins` (see `advert`) and check their own
# relay against it, the string itself has to mean "your relay is served here".
CAP_PUSH_RELAY = "push.relay"
RELAY_DEFAULT_ORIGIN = "https://push.hermie.dev"
CAP_PUSH_WEBPUSH = "push.webpush"
CAP_PUSH_PREVIEW = "push.preview"
CAP_PUSH_MUTE = "push.mute"
CAP_PUSH_TURN_DONE = "push.type.turn_done"
CAP_PUSH_TURN_FAILED = "push.type.turn_failed"
CAP_PUSH_CRON_DONE = "push.type.cron_done"
CAP_PUSH_CRON_FAILED = "push.type.cron_failed"
# Says the gateway recognises a cron run by the marker the scheduler binds,
# rather than by looking for "cron" in a free-text platform string. An app that
# sees it can label a notification "scheduled job" and mean it; an app that does
# not is talking to a gateway whose cron answers were always a guess.
CAP_PUSH_CRON_SIGNAL = "push.cron.signal"
CAP_PUSH_SEEN_PER_CHAT = "push.seen.per_chat"
# The `push.perBot` overrides are read and folded over the device's own
# switches. Like `push.mute`, this is advertised whether or not an override
# exists yet: the string says this gateway will obey one, which is what the app
# needs to know before it offers a per-chat switch that would otherwise be a
# setting nothing honours.
CAP_PUSH_PER_BOT = "push.per_bot"
# Every payload names the gateway it came from, as FNV-1a over the gateway's
# origin — the same string the app computes for the address it registered
# against. A device set up against two gateways can tell which one buzzed.
CAP_PUSH_GATEWAY_KEY = "push.gateway_key"
# A payload says whether its session is the bot's canonical chat, a branch, or
# neither, so a tap can open the right conversation. Advertised only where this
# gateway can actually read a session's title.
CAP_PUSH_SESSION_KIND = "push.session_kind"
# A request notification that was answered, cancelled or timed out is followed
# by a clearing push (`clear: true`) to a device whose row says `clears: true`.
# Two strings because two roads: Expo and Web Push rows get it now; a relay row
# only once the relay can carry a message that is not an alert (see
# `push/relay.py::CAN_CLEAR`), and an app that reads the first and not the
# second must not assume the relay will clear anything.
CAP_PUSH_CLEAR = "push.clear"
CAP_PUSH_CLEAR_RELAY = "push.clear.relay"
# A `confirm` request raises a notification (`method: confirm`, with its
# `level`, never offering Allow or Deny). Claimed only where the gateway fires
# the hook that says one was opened, which is the fork's.
CAP_PUSH_CONFIRM = "push.request.confirm"
# The secure inputs (`secret`, `sudo`, `vault.*`) and a clarify question that
# has its request id raise a notification. Claimed only where the gateway
# reports its server requests, which no gateway does yet.
CAP_PUSH_SECURE_INPUT = "push.request.secure_input"
# A passkey added to or revoked from a person's account notifies that person's
# devices whatever they muted (`type: security`). Claimed only where the gateway
# fires the hook that says so, which is the fork's.
CAP_PUSH_SECURITY = "push.security"
# A background task finishing notifies (`type: turn_done`,
# `event: background.complete`). Claimed only where the gateway reports it,
# which no gateway does yet.
CAP_PUSH_BACKGROUND = "push.background"
CAP_UIMETA_PER_USER = "ui_meta.per_user"
CAP_CONTEXT_PROMPT = "context.system_prompt"
CAP_CONTEXT_PER_BOT = "context.per_bot"
# An edit made while a chat is open reaches that chat on its very next turn.
# Core freezes a plugin's prompt section for the life of a session, so without
# this the app has to tell somebody their change takes effect in a new chat --
# which is a sentence no app should have to write, and the wrong answer besides.
CAP_CONTEXT_LIVE = "context.live"
# The rendered section says what it is: that these facts are the person's own
# Hermie profile arriving through this plugin, what a bot may do with them, and
# where to look for the rest. Without it somebody has to explain the plugin to
# their bot before any of this works, which is the opposite of the feature. An
# app that sees this can say the bot already knows — and one that does not can
# keep telling people to spell it out, which is the truth on that gateway.
CAP_CONTEXT_ORIENTATION = "context.orientation"
# A person sending in a chat somebody else opened can say so, and the next turn
# is resolved for them. Hermes names the login that OPENED a session to every
# turn of it, so without this a shared Bot Chat hands every turn the opener's
# section. The app claims the turn over the dashboard's plugin route
# `POST /api/plugins/hermie/context/turn` just before `prompt.submit`; the
# identity is the dashboard login's, never anything in the body. Like the
# memory strings, this names an HTTP surface on the dashboard's port.
CAP_CONTEXT_TURN_CLAIM = "context.turn_claim"
CAP_COMMAND_ME = "command.me"
# The memory browser and the profile-display-name route. These are the only
# capabilities naming an HTTP surface rather than something reachable over the
# connection the app already has, and they say so: the routes are mounted by
# the dashboard, on the dashboard's port, under the dashboard's own
# all-or-nothing auth. An app that sees them still has to know the dashboard
# address and hold a dashboard credential.
CAP_MEMORY_BROWSE = "memory.browse"
CAP_MEMORY_EDIT = "memory.edit"
# A backend can be read as it is STORED, which the browsing routes cannot do:
# they answer a memory already parsed into entries, and a heading, a blank line
# or a delimiter that ended up inside an entry are invisible in that shape —
# as is anything an external provider holds, which has no entries to parse.
# Behind `memory.browse` like the rest of reading, and a string of its own
# because a plugin without the route answers 404 and an app should be able to
# know that before it draws a tab for it.
CAP_MEMORY_RAW = "memory.raw"
# Setting a profile's display name over `PATCH /api/plugins/hermie/profiles/
# {name}`. A different route from memory's, but the same category of
# capability, for the same reason: it names an HTTP surface, not a WebSocket
# method, so an app has to know it is even there before it can rely on it.
CAP_PROFILE_DISPLAY_NAME = "profiles.display_name"
# The gateway was asked to find out whether a newer plugin exists, and the
# advert carries the answer. Advertised only when the operator switched the
# check on: the string says an answer is there, not that one could be.
CAP_UPDATE_CHECK = "plugin.update_check"
# The web client in `dashboard/app/` matched its own `build.json` when this
# gateway loaded the plugin, and `modules.web` is on. The advert's `web` block
# says where it is. Like the memory strings this names an HTTP surface: files
# the dashboard serves from its own static route, behind its own sign-in. The
# string withdraws the offer, never the files (see web.py).
CAP_WEB_CLIENT = "web.client"

# Modules that exist as a name and a config key but have no implementation yet.
# They are advertised as "planned" rather than silently missing so the app can
# tell "this plugin is too old" from "this gateway has it switched off", and so
# the config surface does not change shape when they land.
# `search` stays planned and is NOT what the memory module's `search` route is:
# that one greps a profile's two memory files, this one is the transcript index
# with row ids. Removing it here would have quietly retired a different feature.
PLANNED_MODULES = ("sessions", "presence", "transcripts", "search", "attachments", "usage")


def advert(
    *,
    modules: Dict[str, str],
    capabilities: Iterable[str],
    limits: Dict[str, Any] | None = None,
    now: float | None = None,
    installed_ref: str = "",
    latest: str = "",
    relay_origins: Iterable[str] | None = None,
    web: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """The value written to the ``hermie-plugin`` ui_meta key.

    `source` is how the app says "a plugin update is available" without the
    gateway reaching anywhere: it carries this build's version and the commit
    the installed tree sits at, and comparing that against the newest release is
    something the app can do on its own network. `latest` is filled in only when
    the operator switched the gateway-side check on.

    `relayOrigins` lists the push relays this gateway posts to, as https
    origins, whenever the push module is on — an empty list included, which
    says "no relay at all". It is additive and needs no `v` bump: an app reads
    it to decide whether the relay IT registered with is served here before it
    moves a device onto a relay row. An app that does not know the field goes
    by `push.relay`, which is only claimed for the default relay.

    `web` says where the bundled web client is and which build it is
    (`path`, `version`, `commit`, `files`, `bytes`), and is present only beside
    the `web.client` capability: when `modules.web` is on and the files matched
    their `build.json` at load. Additive like `relayOrigins`; no `v` bump.
    """
    source: Dict[str, Any] = {"repo": REPO, "ref": installed_ref}
    if latest:
        source["latest"] = latest
    value: Dict[str, Any] = {
        "v": CONTRACT_VERSION,
        "version": PLUGIN_VERSION,
        "maxContract": MAX_CONTRACT,
        "minAppVersion": MIN_APP_VERSION,
        "source": source,
        "capabilities": sorted(set(capabilities)),
        "modules": dict(sorted(modules.items())),
        "limits": dict(limits or {}),
        "updatedAt": int(now if now is not None else time.time()),
    }
    if relay_origins is not None:
        value["relayOrigins"] = list(relay_origins)
    if web is not None:
        value["web"] = dict(web)
    return value


def read_capabilities(value: Any) -> List[str]:
    """The capability list out of an advert, for an app-side reader or a test.

    An advert whose ``v`` is newer than this reader understands yields nothing.
    That is deliberate and it is the same rule the registration reader follows:
    a shape you do not know is not a shape you guess at.
    """
    if not isinstance(value, dict):
        return []
    version = value.get("v")
    if not isinstance(version, int) or isinstance(version, bool) or version > CONTRACT_VERSION:
        return []
    raw = value.get("capabilities")
    return sorted({item for item in raw if isinstance(item, str)}) if isinstance(raw, list) else []
