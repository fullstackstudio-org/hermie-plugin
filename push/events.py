"""Turning what a hook says into what a device is told, or into nothing at all.

Everything here is a pure function of a hook's kwargs and the current
registrations, so the whole decision — notify or not, whom, saying what — is
testable without a gateway, a network, or a clock.

Three rules shape it, all of them from ADR-0017:

- **The payload says who, not what.** A bot's name and an event type. The text
  rides only when that device turned `preview` on AND the gateway allows it, and
  the gateway's setting is the stricter of the two.
- **A message is suppressed on the device that is looking at that chat.** Not on
  the others, and not for another bot: a phone in a pocket should still buzz
  while the same person reads that chat on a laptop. Requests are not
  suppressed at all — a question with a countdown on it is worth a buzz even if
  the chat is open in another room.
- **A chat may override the global switches, and a mute outranks both.** The
  overrides are per person and per bot, partial (a type nobody touched follows
  the global switch as it moves), and folded by the same function name the app
  uses. A mute is not weighed against any of it: somebody said no.
- **A muted bot is silent on every device that person owns.** That one is not a
  heuristic and not per type: somebody said no.
- **Every notification is a hint, never an instruction.** Nothing in a payload
  is an id the app acts on without re-reading the gateway first.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .cron import Cron, declared_failure
from .registrations import (
    PUSH_TYPES,
    Registration,
    Section,
    effective_types,
    is_muted,
    looking_at,
)

PAYLOAD_VERSION = 1

# Everything the app is told about, which is exactly what a device can ask for:
# the reader's own list under the name the sender uses, rather than a second
# copy of it. The two were separate tuples once and had drifted apart — the
# sender knew about `cron_done` and `cron_failed`, the reader did not, and a
# registration asking for them was parsed as asking for nothing.
#
# Hermes fires no hook when a bot-to-bot message arrives (see DESIGN.md), so
# there is no type for one: a switch that turns nothing on is worse than no
# switch.
TYPES = PUSH_TYPES

# Types a device is told about even when it says somebody is watching.
#
# `cron_done` is deliberately NOT here while `cron` and `cron_failed` are. A
# scheduled job that finished is the quietest good news this plugin carries, and
# a device that says it is reading that very chat is already looking at it. A
# job that FAILED is the opposite: nobody asked for the run, so nobody is
# waiting to notice that it did not work.
NEVER_SUPPRESSED = ("request", "cron", "cron_failed", "turn_failed")

# -- the shared push contract --------------------------------------------------
#
# Every sender and every app generation conform to one contract file, kept in
# the app's repository as `contract/push/contract.json`; `tests/fixtures/
# push_contract.json` is this repo's copy of it. Three of its ids are decided
# by the SENDER and they live here so that no transport spells them on its own:
#
# - the notification category, which is what grows the Allow and Deny buttons.
#   It is `hermie.request`, and only on an approval: a clarify question has no
#   answer a button could send. The action ids inside it are registered by the
#   app, never sent, so they are not named here;
# - the Android channel, which is the type name — one channel per type;
# - the request a notification was raised for, which travels as `requestId`.
#
# This plugin once put the TYPE in the category (`request`), which no app ever
# registered, so an approval arrived without its buttons.
REQUEST_CATEGORY = "hermie.request"


def category_for(payload: Dict[str, Any]) -> str:
    """The category one payload is posted under, or ``""`` for none.

    An approval that names no request gets no buttons either: the app would
    turn an Allow without a request id into a plain open anyway, and a button
    that cannot do what it says is worse than no button.
    """
    if payload.get("type") == "request" and payload.get("method") == "approval" and payload.get("requestId"):
        return REQUEST_CATEGORY
    return ""


def channel_for(payload: Dict[str, Any]) -> str:
    """The Android channel: the type name, which is what the contract says."""
    return str(payload.get("type") or "message")


@dataclass(frozen=True)
class Notification:
    type: str
    bot: str
    title: str
    body: str
    event_id: str
    session_id: str = ""
    at: int = 0
    text: str = ""  # the previewable content, never sent unless preview is on
    extra: Dict[str, Any] = field(default_factory=dict)

    def payload(self, *, preview: bool, gateway_key: str = "", session_kind: str = "") -> Dict[str, Any]:
        """What travels. Keep this small: APNs caps at ~4KB and so does the rest.

        `gateway_key` and `session_kind` are facts about this DELIVERY rather
        than about the event, which is why they are arguments here instead of
        fields on the notification: the key is the one the receiving device
        registered against, and the kind is read when the notification is sent
        rather than when the hook fired. Both are omitted when empty, so a
        reader checks for absence rather than for a falsy value it would then
        have to decide about.
        """
        body = {
            "v": PAYLOAD_VERSION,
            "type": self.type,
            "bot": self.bot,
            "at": self.at,
            "eventId": self.event_id,
        }
        if self.session_id:
            body["sessionId"] = self.session_id
        if session_kind:
            body["sessionKind"] = session_kind
        if gateway_key:
            body["gatewayKey"] = gateway_key
        for key, value in self.extra.items():
            body[key] = value
        if preview and self.text:
            body["preview"] = self.text[:200]
        return body

    def rendered(self, *, preview: bool) -> tuple[str, str]:
        """The two strings a lock screen shows."""
        if preview and self.text:
            return self.bot, self.text[:200]
        return self.title, self.body


def event_id(kind: str, *parts: Any) -> str:
    """A stable id for one fact, so two hooks describing it buzz once.

    Built from the identity of the event rather than from a counter, because the
    same turn can be described by `post_llm_call` and by `on_session_end` and
    those two must collide on purpose.
    """
    digest = hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()
    return f"{kind}:{digest[:32]}"


def _clean(text: Any, limit: int = 200) -> str:
    return " ".join(str(text or "").split())[:limit]


def from_assistant_message(
    *, bot: str, session_id: str, turn_id: str, assistant_response: Any, at: int
) -> Optional[Notification]:
    """A bot wrote something (`post_llm_call`)."""
    text = _clean(assistant_response, 400)
    if not text:
        return None
    return Notification(
        type="message",
        bot=bot,
        title=bot,
        body="New message",
        text=text,
        session_id=session_id,
        at=at,
        event_id=event_id("message", session_id, turn_id),
    )


def from_approval(
    *,
    bot: str,
    session_key: str,
    description: Any,
    request_id: Any,
    turn_id: Any,
    at: int,
    cron: Optional["Cron"] = None,
) -> Notification:
    """An agent stopped to ask (`pre_approval_request`).

    The request id travels so the app can find the request again — and it finds
    it by asking the gateway, not by trusting this. A notification that names a
    request which is already answered opens an app that says so.

    A request raised INSIDE a scheduled run carries the cron fields too. It
    stays a `request` — somebody is still being asked — but "this is a job you
    are not watching" is the most useful thing a lock screen can add to a
    question, and it is the same answer core's own unattended-approval check
    arrives at from the same signal.
    """
    # `method` is what tells a sender this request can be answered with a
    # button, and it is how the contract decides the category. See
    # `category_for`.
    extra: Dict[str, Any] = {"method": "approval"}
    if request_id:
        extra["requestId"] = str(request_id)
    extra.update(_cron_extra(cron))
    return Notification(
        type="request",
        bot=bot,
        title=bot,
        body="Needs your approval",
        text=_clean(description, 200),
        session_id=str(session_key or ""),
        at=at,
        event_id=event_id("request", session_key, request_id or turn_id or description),
        extra=extra,
    )


def from_clarify(
    *,
    bot: str,
    session_id: str,
    tool_call_id: Any,
    question: Any,
    at: int,
    cron: Optional["Cron"] = None,
) -> Notification:
    """An agent asked a question (`pre_tool_call` for the `clarify` tool).

    Hermes fires no clarify-specific hook, so this rides the generic tool hook.
    The clarify request id is minted inside the gateway's blocking prompt and is
    not visible here, so the payload carries no id at all: the app opens the
    chat and finds the open question itself.
    """
    return Notification(
        type="request",
        bot=bot,
        title=bot,
        body="Asked you a question",
        text=_clean(question, 200),
        session_id=session_id,
        at=at,
        event_id=event_id("clarify", session_id, tool_call_id),
        extra={"method": "clarify", **_cron_extra(cron)},
    )


def from_session_end(
    *,
    bot: str,
    session_id: str,
    turn_id: Any,
    completed: Any,
    failed: Any,
    interrupted: Any,
    at: int,
    cron: Optional["Cron"] = None,
) -> Optional[Notification]:
    """A turn finished, one way or another (`on_session_end`).

    A cron run is an ordinary agent session, so this is also where a scheduled
    job's turn ends. It gets its own two types rather than borrowing
    `turn_done` and `turn_failed`, because the two answer different questions:
    a turn is something the person started and is waiting on, and a cron run is
    something that happened while they were not looking.
    """
    if interrupted:
        # Somebody pressed stop. They know.
        return None
    scheduled = cron is not None
    if failed or not completed:
        return Notification(
            type="cron_failed" if scheduled else "turn_failed",
            bot=bot,
            title=bot,
            body=_cron_body(cron, "A scheduled job failed") if scheduled else "A turn failed",
            session_id=session_id,
            at=at,
            event_id=event_id("cron_failed" if scheduled else "turn_failed", session_id, turn_id),
            extra=_cron_extra(cron),
        )
    return Notification(
        type="cron_done" if scheduled else "turn_done",
        bot=bot,
        title=bot,
        body=_cron_body(cron, "A scheduled job finished") if scheduled else "Finished working",
        session_id=session_id,
        at=at,
        event_id=event_id("cron_done" if scheduled else "turn_done", session_id, turn_id),
        extra=_cron_extra(cron),
    )


def _cron_body(cron: Optional["Cron"], fallback: str) -> str:
    """The lock-screen line. A job id is a name the person chose, so it is shown."""
    return f"{fallback}: {cron.job_id}" if cron is not None and cron.job_id else fallback


def _cron_extra(cron: Optional["Cron"]) -> Dict[str, Any]:
    """What rides in the payload, and how sure the gateway is that it is a cron.

    `certain` is there so the app does not have to guess at how the gateway
    guessed. A turn recognised by the platform string alone is still a guess,
    and an app that wants to say "scheduled job" rather than "message" should
    be able to tell the two apart.
    """
    if cron is None:
        return {}
    extra: Dict[str, Any] = {"cron": True, "cronCertain": cron.certain}
    if cron.job_id:
        extra["jobId"] = cron.job_id
    return extra


def from_cron_delivery(
    *, bot: str, session_id: str, turn_id: str, assistant_response: Any, at: int, cron: "Cron"
) -> Optional[Notification]:
    """A scheduled job delivered something (`post_llm_call` inside a cron run).

    The agent may also have declared its own failure on the first line, which is
    the one kind of job failure a turn can see — the scheduler decides the rest
    after the agent is gone and fires no hook about it.
    """
    text = _clean(assistant_response, 400)
    if not text:
        return None
    if declared_failure(assistant_response):
        return Notification(
            type="cron_failed",
            bot=bot,
            title=bot,
            body=_cron_body(cron, "A scheduled job failed"),
            text=text,
            session_id=session_id,
            at=at,
            event_id=event_id("cron_failed", session_id, turn_id),
            extra=_cron_extra(cron),
        )
    return Notification(
        type="cron",
        bot=bot,
        title=bot,
        body=_cron_body(cron, "Cron delivered"),
        text=text,
        session_id=session_id,
        at=at,
        event_id=event_id("message", session_id, turn_id),
        extra=_cron_extra(cron),
    )


def recipients(
    notification: Notification,
    section: Section,
    *,
    now: float,
    attached_window_seconds: int,
    enabled_types: tuple,
    gateway_preview: str,
    retired,
) -> List[tuple[Registration, bool]]:
    """Who gets this, and whether their copy may carry the text.

    `retired` is a predicate over (installation id, updatedAt) so the state file
    can veto a registration a transport already told us is dead, without this
    function needing to know what a state file is.
    """
    if notification.type not in enabled_types:
        return []
    suppressible = notification.type not in NEVER_SUPPRESSED

    out: List[tuple[Registration, bool]] = []
    for registration in section.registrations:
        # The device's own switches with this chat's overrides folded over them,
        # by the same rule the app's switch screen uses. An absent override is
        # not "off": it is "whatever the global switch says", now and later.
        wanted = effective_types(
            registration.types, section.overrides_for(registration.user_id, notification.bot)
        )
        if not wanted.get(notification.type, False):
            continue
        # A mute is the person's decision about a bot, so it outranks every
        # per-type switch: it silences this bot on every device that person
        # registered, including the types that are never suppressed.
        if is_muted(section, registration.user_id, notification.bot, now):
            continue
        # Suppression is per device and per chat: this one says it is reading
        # this bot right now, so it is told nothing. Every other device of the
        # same person still is.
        if suppressible and looking_at(
            section, registration.installation_id, notification.bot, now, attached_window_seconds
        ):
            continue
        if retired(registration.installation_id, registration.updated_at):
            continue
        # The gateway's policy is a ceiling, never a floor: `never` overrides a
        # device that asked for previews, and `device` never turns one on.
        preview = gateway_preview == "device" and registration.preview
        out.append((registration, preview))
    return out
