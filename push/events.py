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
- **A request that takes a secret, or a device's own proof, says nothing about
  itself.** A secure input (`secret`, `sudo`, `vault.*`) carries no text at all,
  whatever the preview setting, and a confirmation carries none because the
  gateway never tells a plugin what it asks. Neither gets an Allow button: a
  tap on a lock screen is not a password, and not a passkey either.
- **A security notice is not a message.** A passkey added to or removed from a
  person's account reaches every device of that person, whatever they muted or
  switched off, and is never held back because a chat is open.
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

# A notification that is about the person's own account rather than about a bot
# talking. It is not a switch: no device asks for it and none can refuse it, a
# mute does not apply and an open chat does not hold it back. It is therefore
# NOT in `TYPES` (which is what a device can ask for) but it is a value of the
# payload's `type`.
SECURITY = "security"
UNFILTERED_TYPES = (SECURITY,)
PAYLOAD_TYPES = TYPES + UNFILTERED_TYPES

# The server requests a notification can be raised for, by the name the gateway
# gives the request. `approval` and `clarify` were the first two; the secure
# inputs and `confirm` came with the native app, the interactive requests (a form
# to fill in, files to hand over, a draft to review) after them.
METHOD_APPROVAL = "approval"
METHOD_CLARIFY = "clarify"
METHOD_CONFIRM = "confirm"
SECURE_INPUT_METHODS = ("secret", "sudo", "vault.unlock_prompt", "vault.code", "vault.save_login")
# Answered in the app, never from a notification, and never described by one: the
# form's fields, the file kinds asked for and the draft's text are the agent's
# words and stay behind the tap, exactly like a secure input's name.
INTERACTIVE_METHODS = ("input.form", "input.file", "review.draft")
REQUEST_METHODS = (METHOD_APPROVAL, METHOD_CLARIFY, *SECURE_INPUT_METHODS, METHOD_CONFIRM, *INTERACTIVE_METHODS)

# What a lock screen says about a request that takes something from the person.
# The KIND of thing wanted and never which one: a variable name, a site or a
# command is exactly what these requests exist to keep off a screen.
SECURE_INPUT_BODY = {
    "secret": "Needs a secret",
    "sudo": "Needs your password",
    "vault.unlock_prompt": "Needs your master password",
    "vault.save_login": "Wants to save a login",
    "vault.code": "Needs a verification code",
}

# What a lock screen says about an interactive request: the KIND of thing wanted,
# never what is in it. The same rule as for a secure input.
INTERACTIVE_BODY = {
    "input.form": "Has a form for you",
    "input.file": "Needs a file",
    "review.draft": "Has a draft to review",
}

# `confirm` asks at one of two levels. A level this build has never heard of is
# worded as the stricter one: the request itself says what it needs once opened.
CONFIRM_LEVELS = ("plain", "passkey")

# Why a request stopped being open, as a clearing push says it.
CLEAR_ANSWERED = "answered"
CLEAR_CANCELLED = "cancelled"
CLEAR_TIMEOUT = "timeout"
CLEAR_REASONS = (CLEAR_ANSWERED, CLEAR_CANCELLED, CLEAR_TIMEOUT)

# What a passkey change is called on the wire: the gateway's own words.
PASSKEY_CHANGES = ("added", "revoked")

# -- the shared push contract --------------------------------------------------
#
# Every sender and every app generation conform to one contract file, kept in
# the app's repository as `contract/push/contract.json`; `tests/fixtures/
# push_contract.json` is this repo's copy of it. Three of its ids are decided
# by the SENDER and they live here so that no transport spells them on its own:
#
# - the notification category, which is what grows the Allow and Deny buttons.
#   It is `hermie.request`, and only on an approval: a clarify question has no
#   answer a button could send, a secure input must be typed, and a
#   confirmation is the person's own act (at `passkey` only the app can do it,
#   and at `plain` a tap on a notification would prove nothing). The action ids
#   inside it are registered by the app, never sent, so they are not named here;
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
    if payload.get("clear"):
        # A clearing push withdraws a notification; it offers nothing.
        return ""
    if payload.get("type") == "request" and payload.get("method") == METHOD_APPROVAL and payload.get("requestId"):
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
    # A request's conversation under its stored id (`sessionKey`), where
    # `session_id` is the RUNTIME id the request is open in. The two are
    # different strings for the same conversation, and a request needs both: the
    # live id is what an answer is addressed with, the stored one is what opens
    # the conversation. Empty when the sender cannot tell.
    session_key: str = ""
    # Whose notification this is, as the gateway names a person
    # (`<provider>:<user id>`). Never sent: it only decides which devices are
    # asked. Empty names nobody, which means every device that wants it.
    user_id: str = ""
    # With `user_id`: whether a device that belongs to nobody (the legacy shared
    # bag) is left out. A request bound to one person's passkey, and a security
    # notice, must not reach a device nobody can vouch for.
    user_strict: bool = False
    # A clearing push withdraws an earlier notification; it raises nothing, and
    # goes only to a device that said it understands one.
    clear: bool = False

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
        if self.session_key:
            body["sessionKey"] = self.session_key
        if session_kind:
            body["sessionKind"] = session_kind
        if gateway_key:
            body["gatewayKey"] = gateway_key
        for key, value in self.extra.items():
            body[key] = value
        if preview and self.text:
            body["preview"] = self.text[:200]
        return body

    @property
    def needs_method_worker(self) -> bool:
        """Whether a Web Push worker must know request METHODS to show this right.

        The worker that shipped with the Expo web build adds Allow and Deny to every
        `type: request` push. For an approval that is right. For a confirmation or a
        secure input it is exactly what must never be offered, so such a push goes to
        a Web Push row only if the row says its worker reads `method`.
        """
        return (
            self.type == "request"
            and not self.clear
            and self.extra.get("method") in (METHOD_CONFIRM, *SECURE_INPUT_METHODS, *INTERACTIVE_METHODS)
        )

    @property
    def kind_session(self) -> str:
        """The STORED id of this notification's conversation, for reading its kind.

        A session's title is looked up by the id it is saved under. For a
        message that is `session_id`; for a request `session_id` is the live
        id, which no title is filed under, and the stored one is `session_key`.
        """
        return self.session_key or self.session_id

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
    runtime_session_id: Any = "",
    tool_call_id: Any = "",
) -> Notification:
    """An agent stopped to ask (`pre_approval_request`).

    The request id travels so the app can find the request again — and it finds
    it by asking the gateway, not by trusting this. A notification that names a
    request which is already answered opens an app that says so.

    **`sessionId` is the runtime id or nothing.** The approval hook names the
    conversation by its STORED key, which is a different string from the id the
    app's open approval is filed under; sent as `sessionId` it never matched and
    an Allow from the lock screen quietly became "open the chat". So the key
    travels as `sessionKey`, and `sessionId` only when a caller really holds the
    live id (`runtime_session_id`). The app then matches the request by its id
    and its bot alone, which is what it does for any notification without one.

    A request raised INSIDE a scheduled run carries the cron fields too. It
    stays a `request` — somebody is still being asked — but "this is a job you
    are not watching" is the most useful thing a lock screen can add to a
    question, and it is the same answer core's own unattended-approval check
    arrives at from the same signal.
    """
    # `method` is what tells a sender this request can be answered with a
    # button, and it is how the contract decides the category. See
    # `category_for`.
    extra: Dict[str, Any] = {"method": METHOD_APPROVAL}
    if request_id:
        extra["requestId"] = str(request_id)
    extra.update(_cron_extra(cron))
    return Notification(
        type="request",
        bot=bot,
        title=bot,
        body="Needs your approval",
        text=_clean(description, 200),
        session_id=str(runtime_session_id or ""),
        session_key=str(session_key or ""),
        at=at,
        event_id=approval_event_id(session_key, request_id, tool_call_id, turn_id, description),
        extra=extra,
    )


def approval_event_id(session_key: Any, request_id: Any, tool_call_id: Any, turn_id: Any, description: Any) -> str:
    """The id of an approval's notification, so the clearing push can name it.

    One function for both halves: the id a clear points at is only the id of the
    notification if the two are built from the same facts in the same order.

    A request id, where the hook has one, is the identity (and the same one Hermie
    Web's daemon derives). On the gateway path the approval hooks carry none, and
    a turn id is the same for every approval in one turn, so two approvals would
    share an id: the second would be dropped as already sent, and the first one's
    clear would withdraw both. The tool call id is set on every approval hook and
    is per call; the description is added so two approvals inside one call
    (`execute_code` flagging two commands) stay apart.
    """
    if request_id:
        return event_id("request", session_key, request_id)
    return event_id("request", session_key, tool_call_id or turn_id or "", description)


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
    not visible here, so this payload carries no id: the app opens the chat and
    finds the open question itself. A gateway that reports its server requests
    (`pre_server_request`, see `from_server_request`) gives the question its id,
    and then this one is not sent at all.

    `session_id` is the hook's own, the STORED id, and travels as `sessionKey`
    for the reason `from_approval` gives.
    """
    return Notification(
        type="request",
        bot=bot,
        title=bot,
        body="Asked you a question",
        text=_clean(question, 200),
        session_key=session_id,
        at=at,
        event_id=clarify_event_id(session_id, tool_call_id),
        extra={"method": METHOD_CLARIFY, **_cron_extra(cron)},
    )


def clarify_event_id(session_id: Any, tool_call_id: Any) -> str:
    return event_id("clarify", session_id, tool_call_id)


def request_event_id(method: Any, request_id: Any) -> str:
    """The id of a request a gateway reported by its own id.

    Ids only: a server request's id (`srq-…`) is unique on its own, and the
    clearing push for it is built from the same two values and nothing else.
    """
    return event_id("request", method, request_id)


def from_server_request(
    *,
    bot: str,
    method: Any,
    request_id: Any,
    session_id: Any,
    session_key: Any,
    at: int,
    user_id: Any = "",
    cron: Optional["Cron"] = None,
) -> Optional[Notification]:
    """A server request the gateway wrote to the person's apps (`pre_server_request`).

    Only the kinds this module has a sentence for: the secure inputs, the
    interactive requests (a form, files, a draft) and a clarify question that
    now has its id. Anything else — an approval and a
    confirmation have hooks of their own, a window read is nobody's business —
    answers ``None``, which is also the answer for a request that names no id,
    because a push for something the app cannot look up is a bell with nothing
    behind it.

    **No text, ever.** The request's own parameters (an environment variable's
    name, a site, a hint, a command, a form's fields, a draft) never reach this
    function and are not asked for: what would be said about a password prompt is
    the one thing a lock screen must not.

    The interactive requests are raised however many apps the gateway reached,
    **including none**: a request parked for want of a capable device is exactly
    the one a notification can bring the phone back for. This function never
    reads `reached`, so nothing here can hold such a push back.
    """
    method = str(method or "")
    rid = str(request_id or "")
    if not rid or method not in (*SECURE_INPUT_METHODS, *INTERACTIVE_METHODS, METHOD_CLARIFY):
        return None
    if method == METHOD_CLARIFY:
        body = "Asked you a question"
    elif method in INTERACTIVE_METHODS:
        body = INTERACTIVE_BODY[method]
    else:
        body = SECURE_INPUT_BODY[method]
    return Notification(
        type="request",
        bot=bot,
        title=bot,
        body=body,
        session_id=str(session_id or ""),
        session_key=str(session_key or ""),
        user_id=str(user_id or ""),
        at=at,
        event_id=request_event_id(method, rid),
        extra={"method": method, "requestId": rid, **_cron_extra(cron)},
    )


def from_confirm(
    *,
    bot: str,
    request_id: Any,
    level: Any,
    session_id: Any,
    session_key: Any,
    user_id: Any,
    at: int,
    cron: Optional["Cron"] = None,
) -> Optional[Notification]:
    """A `confirm` request was written to the person's apps (`pre_confirm_request`).

    The category is none, at either level, and that is the point: at `passkey`
    only the app can run the ceremony, and at `plain` a button on a notification
    would be the very tap the request exists to ask for — from a screen that may
    be locked, or lying on a table.

    There is no text to preview. The hook says which request, which level and
    for whom, and never what is being confirmed; the app shows that itself, once
    the notification has opened it. At `passkey` the request is bound to one
    person and so is the notification: only that person's own devices are asked,
    and a device that belongs to nobody (the legacy shared bag) is not one of
    them. At `plain` a person who is named still gets it first-hand, and a
    device that names nobody still may.
    """
    rid = str(request_id or "")
    wanted = str(level or "")
    wanted = wanted if wanted in CONFIRM_LEVELS else "passkey"
    # A request bound to a person's passkey with no person named has nobody to
    # ask: sent to everyone it would tell the wrong people that something is
    # waiting for one of them.
    if not rid or (wanted == "passkey" and not user_id):
        return None
    return Notification(
        type="request",
        bot=bot,
        title=bot,
        body="Confirm this in the app" if wanted == "passkey" else "Needs your confirmation",
        session_id=str(session_id or ""),
        session_key=str(session_key or ""),
        user_id=str(user_id or ""),
        user_strict=wanted == "passkey",
        at=at,
        event_id=request_event_id(METHOD_CONFIRM, rid),
        extra={"method": METHOD_CONFIRM, "requestId": rid, "level": wanted, **_cron_extra(cron)},
    )


def from_background_complete(
    *,
    bot: str,
    session_id: Any,
    session_key: Any,
    task_id: Any,
    at: int,
    user_id: Any = "",
) -> Optional[Notification]:
    """Something the person sent to the background finished (`on_background_complete`).

    A `turn_done` with `event: background.complete`: it answers to the same
    switch, because the thing the person wants to know is the same — the bot is
    done — and a device that muted finished turns for this chat does not want a
    second kind of finished. It is suppressed like one too: a chat that is open
    on the device already shows the result arriving.

    `sessionId` is the STORED id, as it is for every type but `request`: a tap
    opens the conversation, and nothing here is answered by id.
    """
    stored = str(session_key or session_id or "")
    if not stored:
        return None
    return Notification(
        type="turn_done",
        bot=bot,
        title=bot,
        body="A background task finished",
        session_id=stored,
        user_id=str(user_id or ""),
        at=at,
        event_id=event_id("background", stored, task_id or ""),
        extra={"event": "background.complete"},
    )


# The one thing about how a passkey was added that the lock screen may say: the
# person added it themselves after signing in again (`via == "self"`), not with
# a code someone minted. Any other `via`, or none, is the plain wording.
SELF_ENROLMENT = "self"
AFTER_SIGN_IN = " after a new sign-in"


def from_passkey_change(
    *, bot: str, change: Any, user_id: Any, credential: Any, at: int, via: Any = None
) -> Optional[Notification]:
    """A passkey was added to, or revoked from, a person's account (`on_passkey_change`).

    A passkey nobody expected is how a stolen session that enrolled one shows
    up, which is why this is not a message: it reaches that person's devices
    whatever they muted and whatever types they switched off (see `recipients`).

    The lock screen says THAT, never which: the credential's name is a label the
    person chose and appears only as the preview text, on a device that asked for
    previews and a gateway that allows them. What does not travel at all is the
    credential id, the relying party and the kind of client. How the change was
    authorised travels in one case only: a passkey the person enrolled with a
    fresh sign-in (`via == "self"`) says so in its words, because that is the
    path a stolen session would use, and a notice that says "after a new
    sign-in" is the one the real owner recognises or does not. `via` is never
    carried on its own, and a hook that passes none gets the plain wording.
    """
    what = str(change or "")
    uid = str(user_id or "")
    # A security notice for nobody is for everybody, and that is exactly the
    # leak this must not be: without a person there is no device to ask.
    if what not in PASSKEY_CHANGES or not uid:
        return None
    record = credential if isinstance(credential, dict) else {}
    word = "added" if what == "added" else "removed"
    if what == "added" and via == SELF_ENROLMENT:
        word += AFTER_SIGN_IN
    name = _clean(record.get("name"), 80)
    return Notification(
        type=SECURITY,
        bot=bot,
        title=bot,
        body=f"A passkey was {word}",
        text=f"The passkey \u201c{name}\u201d was {word}" if name else "",
        user_id=uid,
        user_strict=True,
        at=at,
        event_id=event_id("security", uid, what, record.get("id") or "", at),
        extra={"change": what},
    )


# -- clearing pushes -----------------------------------------------------------
#
# A request that stopped being open — answered on another device, cancelled, or
# timed out — leaves a notification on every device it reached, with buttons
# that no longer do anything. A clearing push says so. It is the same
# `requestId`, the same method and the same conversation as the notification it
# withdraws, plus `clear: true`, why, and `replaces`: the event id of the
# notification it points at, which is the only handle a clarify has, because a
# clarify seen through the tool hook never had a request id.


def _clear(
    original: str,
    *,
    bot: str,
    method: str,
    reason: str,
    at: int,
    request_id: Any = "",
    session_id: Any = "",
    session_key: Any = "",
    user_id: Any = "",
    user_strict: bool = False,
    cron: Optional["Cron"] = None,
) -> Notification:
    extra: Dict[str, Any] = {"method": method, "clear": True, "reason": reason, "replaces": original}
    if request_id:
        extra["requestId"] = str(request_id)
    extra.update(_cron_extra(cron))
    return Notification(
        type="request",
        bot=bot,
        title=bot,
        body="Request closed",
        session_id=str(session_id or ""),
        session_key=str(session_key or ""),
        user_id=str(user_id or ""),
        user_strict=user_strict,
        at=at,
        event_id=event_id("clear", original),
        extra=extra,
        clear=True,
    )


def approval_clear_reason(choice: Any) -> str:
    """Why an approval stopped being open, from the choice its hook reports.

    Anything that is a person's own answer — `once`, `session`, `always`,
    `deny` — is "answered"; `timeout` is its own; and everything else (the turn
    was interrupted, the notification could not be delivered, a plugin's own
    transport gave up) is "cancelled", the honest word for "nobody answered
    and nobody will".
    """
    text = str(choice or "")
    if text in ("once", "session", "always", "deny"):
        return CLEAR_ANSWERED
    if text == "timeout":
        return CLEAR_TIMEOUT
    return CLEAR_CANCELLED


def clear_approval(
    *,
    bot: str,
    session_key: str,
    description: Any,
    request_id: Any,
    turn_id: Any,
    choice: Any,
    at: int,
    cron: Optional["Cron"] = None,
    runtime_session_id: Any = "",
    tool_call_id: Any = "",
) -> Optional[Notification]:
    """An approval is over (`post_approval_response`)."""
    reason = approval_clear_reason(choice)
    if str(choice or "").startswith("smart_"):
        # The smart path answers itself; nobody was asked, nothing was raised.
        return None
    return _clear(
        approval_event_id(session_key, request_id, tool_call_id, turn_id, description),
        bot=bot,
        method=METHOD_APPROVAL,
        reason=reason,
        at=at,
        request_id=request_id,
        session_id=runtime_session_id,
        session_key=session_key,
        cron=cron,
    )


def clarify_clear_reason(status: Any, result: Any) -> str:
    """Why a clarify stopped, from what its tool call reported afterwards.

    A question that timed out comes back as an ordinary result carrying
    `timed_out`, so the result is looked at; a call that errored was
    cancelled. Anything else is an answer, which includes a skipped question.
    """
    if str(status or "") == "error":
        return CLEAR_CANCELLED
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and parsed.get("timed_out") is True:
        return CLEAR_TIMEOUT
    return CLEAR_ANSWERED


def clear_clarify(
    *,
    bot: str,
    session_id: str,
    tool_call_id: Any,
    reason: str,
    at: int,
    cron: Optional["Cron"] = None,
) -> Notification:
    """A clarify question is over (`post_tool_call` for the `clarify` tool)."""
    return _clear(
        clarify_event_id(session_id, tool_call_id),
        bot=bot,
        method=METHOD_CLARIFY,
        reason=reason,
        at=at,
        session_key=session_id,
        cron=cron,
    )


def clear_server_request(
    *,
    bot: str,
    method: Any,
    request_id: Any,
    reason: Any,
    session_id: Any,
    session_key: Any,
    at: int,
    user_id: Any = "",
    user_strict: bool = False,
    cron: Optional["Cron"] = None,
) -> Optional[Notification]:
    """A server request the gateway reported is over (`post_server_request`).

    For the kinds `from_server_request` and `from_confirm` raise (a form, files
    and a draft among them). The gateway's
    word for how it ended is mapped onto the three a device is told: it was
    answered (by anyone), it timed out, or it was cancelled — which is every
    other way a request ends, including the gateway having no client left to ask.
    """
    method = str(method or "")
    rid = str(request_id or "")
    if not rid or method not in (*SECURE_INPUT_METHODS, *INTERACTIVE_METHODS, METHOD_CLARIFY, METHOD_CONFIRM):
        return None
    why = str(reason or "")
    if why in ("answered", "resolved"):
        why = CLEAR_ANSWERED
    elif why != CLEAR_TIMEOUT:
        why = CLEAR_CANCELLED
    return _clear(
        request_event_id(method, rid),
        bot=bot,
        method=method,
        reason=why,
        at=at,
        request_id=rid,
        session_id=session_id,
        session_key=session_key,
        # Named, so it goes to that person's devices, and as strict as the raise
        # was (the caller remembers): a clear then reaches the devices the
        # request reached and no others.
        user_id=user_id,
        user_strict=user_strict,
        cron=cron,
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
    """The lock-screen line, which names the kind of event and never the job.

    The job id rides in the payload as `jobId`, where the app can resolve it,
    and the push contract says it is carried, never shown: it is a name the
    person chose, and a lock screen — or a relay passing the line on — has no
    business displaying it. It was appended here once.
    """
    return fallback


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
    unfiltered = notification.type in UNFILTERED_TYPES
    # A security notice is not one of the switches, so the gateway's own list of
    # types does not hold it back either: `push.types` says which KINDS OF CHAT
    # EVENT this gateway notifies about, and this is about the account.
    if not unfiltered and notification.type not in enabled_types:
        return []
    suppressible = notification.type not in NEVER_SUPPRESSED and not unfiltered

    out: List[tuple[Registration, bool]] = []
    for registration in section.registrations:
        # Whose device it is, where the notification is somebody's. A device in
        # the legacy shared bag names nobody: it is skipped when the person must
        # be vouched for (`user_strict`) and kept otherwise, the way an approval
        # always reached it.
        if notification.user_id and registration.user_id != notification.user_id:
            if registration.user_id or notification.user_strict:
                continue
        # A clearing push is for a build that said it understands one.
        if notification.clear and not registration.clears:
            continue
        # A request with no buttons is not for a worker that gives every request
        # buttons. See `needs_method_worker`.
        if (
            notification.needs_method_worker
            and registration.transport == "webpush"
            and not registration.request_methods
        ):
            continue
        if not unfiltered:
            # The device's own switches with this chat's overrides folded over
            # them, by the same rule the app's switch screen uses. An absent
            # override is not "off": it is "whatever the global switch says",
            # now and later.
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
            # Suppression is per device and per chat: this one says it is
            # reading this bot right now, so it is told nothing. Every other
            # device of the same person still is.
            if suppressible and looking_at(
                section, registration.installation_id, notification.bot, now, attached_window_seconds
            ):
                continue
        if retired(registration.installation_id, registration.updated_at):
            continue
        # The gateway's policy is a ceiling, never a floor: `never` overrides a
        # device that asked for previews, and `device` never turns one on. Above
        # both sits the transport: text never crosses the relay in the clear,
        # so a relay row's own `preview: true` is not an answer it can give.
        preview = gateway_preview == "device" and registration.preview and registration.may_preview
        out.append((registration, preview))
    return out
