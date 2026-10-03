"""The devices that asked to be told, as the app wrote them.

This is the Python side of the schema ADR-0017 defines and Hermie Web's
`packages/hermie-web/src/push/registrations.ts` already reads. The two readers
must agree, because the app writes one shape and either process may be the one
that sends. They agree on the strict parts in particular: `v` is checked rather
than assumed, an entry carrying the fields of both transports is a confusion
rather than a choice, and an unreadable entry costs that entry and nothing else.

The section lives under the app's own key, and there is now one key per person:
`hermie-app:<user id>`, with the older shared `hermie-app` still read for one
version. `read_sections` merges them in the order `uimeta.read_app_sections`
hands them over — legacy first — so the per-user key wins for the same device.
Which person a registration belongs to is no longer a guess: it is the key it
was found under, and it is carried on the registration as `user_id`.

Two things beside a registration belong to the PERSON rather than to a device,
and both are read here: `mutes`, and the `perBot` bag that says where one chat's
notification switches differ from the global ones. A registration also carries
the `gatewayKey` its device computed for the address it registered against,
which is what a notification puts on the wire so a device with several gateways
can tell which one buzzed.

The plugin reads these keys and never writes them.

There are three transports, and a row is exactly one of them:

- ``expo`` — ``token``, an Expo push token;
- ``webpush`` — ``endpoint`` plus ``keys.p256dh`` and ``keys.auth``, a browser's
  subscription;
- ``relay`` — ``relay``, ``handle`` and ``secret``: a device that registered
  with a push relay and was handed a send capability for itself alone. Only
  ``ios`` and ``macos`` register there. ``relay`` names the relay the device
  chose, and it is checked twice: here that it is an https origin at all, and
  by the sender against its own allow-list, because a row is somebody else's
  input and the sender must never post wherever a row says. A row may also
  carry ``enc``, the key a later step uses to encrypt the notification for the
  device; it is carried but not used yet, so a relay row is never sent text.

A row carrying the fields of two transports is a confusion, never a choice,
and is dropped whichever transport it claims.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

from .gateway_key import is_gateway_key, origin_of

SECTION_VERSION = 1

# The platforms a relay registration can come from: the relay delivers through
# Apple's push service, and only Apple's platforms have a device there.
RELAY_PLATFORMS = ("ios", "macos")

# A handle and a send secret are what the relay issued: base64url text, and the
# relay takes at most 200 characters of either. A row is input, and one entry
# the relay cannot read fails the whole request it travels in — every other
# device's notification with it — so a field outside these bounds is dropped
# here, on the way in. `tests/test_relay.py` pins this to the relay's limit.
MAX_RELAY_FIELD = 200
RELAY_FIELD = re.compile(r"[A-Za-z0-9_-]{1,%d}" % MAX_RELAY_FIELD)

# Every event a device can ask about, and every one it can be sent — ONE list,
# which `push/events.py` re-exports as `TYPES`. They were two tuples once, and
# the shorter one was this: a row's `cron_done` and `cron_failed` were dropped
# on the way in, so two types the gateway advertised could be sent but never
# asked for.
#
# Absent still means OFF, and it is never inferred from a neighbour. A device
# registered before the app had the two cron switches keeps exactly the
# behaviour it has today, and somebody who turned `cron` on never quietly
# agreed to be told about every job that finished as well.
#
# ADR-0017 also listed a bot-to-bot `dm`. Hermes fires no hook when one arrives,
# so nothing can produce it — the type is gone rather than kept as a switch that
# turns nothing on.
PUSH_TYPES = (
    "message",
    "request",
    "cron",
    "cron_done",
    "cron_failed",
    "turn_done",
    "turn_failed",
)



@dataclass(frozen=True)
class Registration:
    installation_id: str
    transport: str  # "expo" | "webpush" | "relay"
    platform: str
    types: Dict[str, bool]
    preview: bool
    updated_at: int
    token: Optional[str] = None
    endpoint: Optional[str] = None
    keys: Dict[str, str] = field(default_factory=dict)
    # Whose device this is: the ui_meta key it was read from. Empty means the
    # legacy shared key, which names nobody.
    user_id: str = ""
    # `gatewayKeyOf` the address this device registered against, as the device
    # itself computed it. Empty for a row written before the app carried one,
    # and for a row whose claim is not the shape a key has. See `gateway_key`.
    gateway_key: str = ""
    # A relay row: the relay's origin as the row named it (checked to be an
    # https origin, NOT yet against any allow-list), the device's handle there,
    # and the capability to send to that one handle. The secret is kept out of
    # `repr`, so a registration that ends up in a log line does not take it along.
    relay: str = ""
    handle: Optional[str] = None
    secret: Optional[str] = field(default=None, repr=False)
    # The device's notification key, for the encrypted step. Carried as the row
    # wrote it; nothing reads it yet.
    enc: Dict[str, Any] = field(default_factory=dict, repr=False)
    # The device says it understands a clearing push (`clear: true`): one that
    # withdraws a request notification instead of raising one. Opt-in, because a
    # sender cannot tell which build of a client is on the other end, and a
    # build that does not know the field would show a clearing push as a new
    # notification — with the Allow and Deny buttons of the request it closes.
    clears: bool = False
    # A Web Push row only: the worker behind it reads a request's `method`, so it
    # shows no Allow or Deny for a confirmation or a secure input. The worker
    # shipped with the Expo web build gives every request both buttons, and never
    # writes this.
    request_methods: bool = False

    @property
    def may_preview(self) -> bool:
        """Whether this device's copy may ever carry message text.

        Never through the relay. Text is only allowed to cross a relay
        encrypted end to end, to a key only the device holds, and that is a
        later step: until it ships, a relay row is sent the bot and the kind of
        event, whatever its own `preview` says — including a row that already
        carries an `enc` key. The other transports keep the device's answer.
        """
        return self.transport != "relay"

    def wants(self, push_type: str) -> bool:
        """Whether this device asked about *push_type*, before any chat's say.

        The answer a notification is actually decided on is
        :func:`effective_types`, which folds that chat's overrides over this.
        This stays the device's own global answer, which is what the overrides
        are overrides OF.
        """
        return self.types.get(push_type, False)


@dataclass(frozen=True)
class Seen:
    """One device saying what it is looking at, and when it last said so."""

    at: int
    # Which chat that device has open. Empty means the device said only that
    # somebody was looking, without naming a bot — the shape older apps wrote.
    bot: str = ""


@dataclass(frozen=True)
class Section:
    registrations: List[Registration] = field(default_factory=list)
    # installation id -> that device's heartbeat.
    seen: Dict[str, Seen] = field(default_factory=dict)
    # user id -> bot -> the second at which the mute lapses, 0 meaning never.
    mutes: Dict[str, Dict[str, int]] = field(default_factory=dict)
    # user id -> bot -> the types that chat overrides. Partial on purpose: a
    # type nobody overrode is not in here and follows the device's own switch.
    per_bot: Dict[str, Dict[str, Dict[str, bool]]] = field(default_factory=dict)

    def overrides_for(self, user_id: str, bot: str) -> Dict[str, bool]:
        """What this person said about this chat, or an empty bag."""
        return self.per_bot.get(user_id, {}).get(bot, {})


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _number(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _types(value: Any) -> Dict[str, bool]:
    source = value if isinstance(value, dict) else {}
    return {name: source.get(name) is True for name in PUSH_TYPES}


def seen_of(value: Any) -> Optional[Seen]:
    """One heartbeat, in either shape, or nothing.

    The shape the app writes now says which chat the device has open:

        seen: {"<installation id>": {"bot": "<bot>", "at": <unix second>}}

    A bare number is the older shape and means "looking at some chat", with no
    way to tell which. It is read for one version and it suppresses the same way
    it always did — for every chat on that one device.
    """
    if isinstance(value, dict):
        at = _number(value.get("at"))
        return Seen(at=at, bot=_text(value.get("bot"))) if at > 0 else None
    at = _number(value)
    return Seen(at=at) if at > 0 else None


def _remember(into: Dict[str, Seen], installation_id: str, heartbeat: Optional[Seen]) -> None:
    """Keep the newer of two heartbeats for one device.

    A heartbeat can arrive from the `seen` map and from the registration entry
    itself, and across two keys while the app migrates. Newest wins, because the
    question it answers is "is somebody looking *now*" and an older answer to
    that is simply a worse one.
    """
    if heartbeat is None or not installation_id:
        return
    current = into.get(installation_id)
    if current is None or heartbeat.at >= current.at:
        into[installation_id] = heartbeat


def looking_at(
    section: Section, installation_id: str, bot: str, now: float, window_seconds: int
) -> bool:
    """Is *this device* looking at *this bot's* chat right now?

    The gateway cannot be asked who is watching — `session.active_list` reports
    the calling connection's own session and nobody else's — so this stays what
    ADR-0017 called it: a heartbeat the app writes and this reads. It is a
    heuristic, and it fails towards a redundant notification for a chat somebody
    is already reading, which is the right direction.
    """
    heartbeat = section.seen.get(installation_id)
    if heartbeat is None or now - heartbeat.at > window_seconds:
        return False
    # A heartbeat that names no bot is the older shape: it says a chat is open
    # without saying which, so it still covers every chat on that device.
    return not heartbeat.bot or heartbeat.bot == bot


def _mutes(value: Any) -> Dict[str, int]:
    """One person's mute list: bot -> `until`, dropping what is not a number.

    `until` is a unix second, and `0` means forever. A negative or absent value
    is not a mute: the app is the only writer here, and a mute that cannot be
    read is a bot that keeps notifying, which is the recoverable direction.
    """
    source = value if isinstance(value, dict) else {}
    return {
        str(bot): _number(until)
        for bot, until in source.items()
        if str(bot) and (isinstance(until, int) and not isinstance(until, bool) and until >= 0)
    }


def mutes_of(app_key_value: Any) -> Dict[str, int]:
    """The `mutes` map out of one app-owned bag, as the app writes it:

        mutes: {"<bot>": <until>}

    It lives at the top of the bag, beside `push` and `context`, because a mute
    is a fact about a person and a bot rather than about a transport. A copy
    under `push` is honoured too, for an app that files it with the rest of the
    push settings; the top-level one wins where they disagree.
    """
    if not isinstance(app_key_value, dict):
        return {}
    push = app_key_value.get("push") if isinstance(app_key_value.get("push"), dict) else {}
    return {**_mutes(push.get("mutes")), **_mutes(app_key_value.get("mutes"))}


def is_muted(section: Section, user_id: str, bot: str, now: float) -> bool:
    """Whether this person has silenced this bot right now.

    `0` is forever. An `until` that has passed is not a mute — the app is not
    obliged to come back and tidy up an expired entry, and a gateway that
    treated a lapsed mute as a live one would go quiet for good.
    """
    until = section.mutes.get(user_id, {}).get(bot)
    if until is None:
        return False
    return until == 0 or until > now


# -- what one chat says, where it differs from the device's own switches ------
#
# The app writes these beside the registrations rather than inside a row, and
# that is a decision about the READER rather than about a device: somebody who
# silences one bot's cron deliveries means it on their phone and on their Mac.
# It is the same argument `mutes` makes for living in the person's own bag.


def _overrides(value: Any) -> Dict[str, bool]:
    """One chat's overrides: only the types it actually mentions.

    PARTIAL on purpose, and that is the whole design — the app's own
    `PushTypeOverrides` says so. A type this bag does not name follows the
    global switch as the global switch moves; a full map would freeze every
    type at whatever it happened to be the day somebody touched one of them.
    """
    source = value if isinstance(value, dict) else {}
    return {name: source[name] for name in PUSH_TYPES if isinstance(source.get(name), bool)}


def per_bot_of(app_key_value: Any) -> Dict[str, Dict[str, bool]]:
    """The ``push.perBot`` map out of one app-owned bag, as the app writes it::

        push:
          perBot:
            <bot>: {cron: false, turn_failed: true}

    A bot with nothing recognisable under it is dropped rather than kept as an
    empty bag, because an empty bag and an absent one mean the same thing.
    """
    if not isinstance(app_key_value, dict):
        return {}
    push = app_key_value.get("push") if isinstance(app_key_value.get("push"), dict) else {}
    raw = push.get("perBot") if isinstance(push.get("perBot"), dict) else {}
    found: Dict[str, Dict[str, bool]] = {}
    for bot, value in raw.items():
        overrides = _overrides(value)
        if str(bot) and overrides:
            found[str(bot)] = overrides
    return found


def effective_types(types: Dict[str, bool], overrides: Optional[Dict[str, bool]]) -> Dict[str, bool]:
    """The device's switches with one chat's overrides folded in.

    This is the Python half of `effectivePushTypes` in the app's
    `packages/gateway-client/src/push.ts`, and it is written the same way on
    purpose: the app's switch screen and this gateway's decision must not be two
    rules that merely happen to agree. An override is honoured only when it is a
    boolean — anything else is not an answer and leaves the global one standing.
    """
    merged = dict(types)
    for name in PUSH_TYPES:
        value = (overrides or {}).get(name)
        if isinstance(value, bool):
            merged[name] = value
    return merged


def relay_origin(value: Any) -> str:
    """``https://host[:port]``, lowercased, when *value* is exactly an origin.

    Stricter than :func:`gateway_key.origin_of` on purpose. That one answers
    "which gateway is this address on" and so forgives a path; this one answers
    "where may a notification be posted", and an address with a path, a query,
    a fragment or a user name in it is not the origin of anything — it is a
    request to post somewhere else. https only: a relay request carries a send
    capability, and that never travels in the clear.
    """
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if (parts.scheme or "").lower() != "https":
        return ""
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return ""
    if parts.path not in ("", "/") or parts.query or parts.fragment or text.endswith(("?", "#")):
        return ""
    return origin_of(text)


def _relay_field(value: Any) -> str:
    text = _text(value)
    return text if RELAY_FIELD.fullmatch(text) else ""


def registration_of(installation_id: str, value: Any, user_id: str = "") -> Optional[Registration]:
    """One registration, or nothing."""
    if not installation_id or not isinstance(value, dict):
        return None
    if _number(value.get("v")) != SECTION_VERSION:
        return None

    transport = _text(value.get("transport"))
    token = _text(value.get("token"))
    endpoint = _text(value.get("endpoint"))
    raw_keys = value.get("keys") if isinstance(value.get("keys"), dict) else {}
    p256dh = _text(raw_keys.get("p256dh"))
    auth = _text(raw_keys.get("auth"))
    # Present-ness, not validity: a row that carries a handle at all is a relay
    # row, and an Expo row that also carries one is a confusion either way.
    has_handle = value.get("handle") is not None

    common = {
        "installation_id": installation_id,
        "user_id": user_id,
        "platform": _text(value.get("platform")) or "unknown",
        "types": _types(value.get("types")),
        "preview": value.get("preview") is True,
        "updated_at": _number(value.get("updatedAt")),
        # Checked rather than copied. A row saying its key is "yes" would put
        # "yes" on a notification, and a device comparing keys would match
        # nothing — which is the failure that looks like nothing at all.
        "gateway_key": str(value.get("gatewayKey")) if is_gateway_key(value.get("gatewayKey")) else "",
        "clears": value.get("clears") is True,
        "request_methods": value.get("requestMethods") is True,
    }

    if transport == "expo":
        if token and not endpoint and not has_handle:
            return Registration(transport="expo", token=token, **common)
        return None
    if transport == "webpush":
        if endpoint and p256dh and auth and not token and not has_handle:
            return Registration(
                transport="webpush", endpoint=endpoint, keys={"p256dh": p256dh, "auth": auth}, **common
            )
        return None
    if transport == "relay":
        return _relay_registration(value, token=token, endpoint=endpoint, common=common)
    return None


def _relay_registration(
    value: Dict[str, Any], *, token: str, endpoint: str, common: Dict[str, Any]
) -> Optional[Registration]:
    """A relay row, or nothing.

    Every check here is about the row's own shape. Whether THIS gateway will
    post to the relay it names is the sender's question, asked against the
    sender's allow-list (see `push/relay.py`): a reader that answered it would
    need the gateway's configuration, and a row that names an unknown relay is
    still a well-formed row that a differently configured sender may serve.
    """
    if token or endpoint or value.get("token") is not None:
        return None
    if common["platform"] not in RELAY_PLATFORMS:
        return None
    relay = relay_origin(value.get("relay"))
    handle = _relay_field(value.get("handle"))
    secret = _relay_field(value.get("secret"))
    if not (relay and handle and secret):
        return None
    raw_enc = value.get("enc")
    return Registration(
        transport="relay",
        relay=relay,
        handle=handle,
        secret=secret,
        enc=dict(raw_enc) if isinstance(raw_enc, dict) else {},
        **common,
    )


def read_section(app_key_value: Any, user_id: str = "") -> Section:
    """The whole ``push`` section out of one app-owned bag."""
    if not isinstance(app_key_value, dict):
        return Section()
    mutes = {user_id: found} if (found := mutes_of(app_key_value)) else {}
    per_bot = {user_id: chats} if (chats := per_bot_of(app_key_value)) else {}
    push = app_key_value.get("push")
    if not isinstance(push, dict):
        return Section(mutes=mutes, per_bot=per_bot)

    rows = push.get("registrations") if isinstance(push.get("registrations"), dict) else {}
    registrations: List[Registration] = []
    seen: Dict[str, Seen] = {}
    for key, value in rows.items():
        installation_id = str(key)
        parsed = registration_of(installation_id, value, user_id)
        if parsed is not None:
            registrations.append(parsed)
        # A heartbeat filed on the registration itself is honoured too, for an
        # app that keeps a device's "what am I looking at" beside the device.
        if isinstance(value, dict):
            _remember(seen, installation_id, seen_of(value.get("seen")))
    # A stable order, so a run's log and a test read the same twice.
    registrations.sort(key=lambda entry: entry.installation_id)

    raw_seen = push.get("seen") if isinstance(push.get("seen"), dict) else {}
    for key, value in raw_seen.items():
        _remember(seen, str(key), seen_of(value))

    return Section(registrations=registrations, seen=seen, mutes=mutes, per_bot=per_bot)


def read_sections(items: Iterable[Tuple[str, Any]]) -> Section:
    """One view over every app-owned bag, as ``(user id, value)`` pairs.

    The pairs arrive in precedence order (legacy first), and a device is a
    device: the same installation id in two keys is one registration, the later
    one. That is what makes the move to per-user keys safe to do gradually —
    while the app writes both, nobody is notified twice.
    """
    by_installation: Dict[str, Registration] = {}
    seen: Dict[str, Seen] = {}
    mutes: Dict[str, Dict[str, int]] = {}
    per_bot: Dict[str, Dict[str, Dict[str, bool]]] = {}
    for user_id, value in items:
        section = read_section(value, user_id)
        for registration in section.registrations:
            by_installation[registration.installation_id] = registration
        for installation_id, heartbeat in section.seen.items():
            _remember(seen, installation_id, heartbeat)
        mutes.update(section.mutes)
        per_bot.update(section.per_bot)
    return Section(
        registrations=sorted(by_installation.values(), key=lambda entry: entry.installation_id),
        seen=seen,
        mutes=mutes,
        per_bot=per_bot,
    )
