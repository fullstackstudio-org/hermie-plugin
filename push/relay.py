"""Sending through a push relay, and believing what it says back.

Only an app's publisher can talk to Apple's push service for that app, so a
device running the native Hermie app on an iPhone, iPad or Mac registers with a
relay its publisher operates and writes what it got back into its row: the
relay's origin, a `handle` that names the device there, and a `secret` that
lets whoever holds it send to that one handle and nothing else. Whoever can read
the row can make that device buzz — the same trust an Expo token carried — and
only the device itself can retarget or revoke the handle.

Three rules shape this module:

- **Post only where this gateway decided to.** A row is input, and its `relay`
  field is a URL somebody else wrote. The sender posts to an origin on its OWN
  allow-list (`push.relay_origins`, by default exactly :data:`DEFAULT_ORIGIN`)
  and to nothing else, over https, without following a redirect and without
  picking up a proxy from anywhere but the process environment. A row naming
  any other origin is not served.
- **The relay is handed an abstract message, never text.** A title, a body, a
  category, a thread and the contract's data bag; the relay builds the platform
  payload itself. For now the title is the bot and the body is the kind of
  event, whatever the device's own `preview` says: message text only ever
  crosses a relay encrypted end to end, and that is a later step (see
  `Registration.may_preview`).
- **The send capability is a credential.** It is never logged, and neither is a
  whole handle: a log line names a handle by its first few characters, which is
  enough to find it in the relay's own log and not enough to send with.

Answers, per message: `sent`; `gone` (an unknown handle, a wrong secret, or a
device the platform dropped — indistinguishable on purpose) retires the row the
way Expo's `DeviceNotRegistered` does; `rejected` is logged and dropped;
`retry` and `limited` are tried once more after the wait the relay asked for,
bounded, and then dropped. A request that did not get an answer at all is
treated the same as `retry`. A notification is a hint, and one that is late
twice is not worth a third try.

Only the standard library is used, for the same reasons `expo.py` gives.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .. import contract
from .events import category_for
from .registrations import Registration, relay_origin

logger = logging.getLogger(__name__)

# The relay the Hermie apps register with. It is the whole default allow-list:
# a gateway posts nowhere else unless its operator says so.
DEFAULT_ORIGIN = "https://push.hermie.dev"

SEND_PATH = "/v1/send"
REQUEST_VERSION = 1

# The relay takes 1 to 20 messages a request, each authorised on its own.
MAX_BATCH = 20

# The relay's cap on one message, in bytes of JSON. A message this gateway
# builds is a few hundred; the check exists so an oversized one is dropped here
# with a reason rather than refused at the far end.
MAX_MESSAGE_BYTES = 3500

# The relay's collapse id is at most 64 bytes; an event id is well under that.
MAX_COLLAPSE_ID_BYTES = 64

# How long the relay should keep trying a device that is offline.
TTL_SECONDS = 3600

TIMEOUT_SECONDS = 10

# How much of an answer is read. The relay's answer to twenty messages is a
# couple of kilobytes; anything far larger is not an answer to trust.
MAX_RESPONSE_BYTES = 64 * 1024

# The one retry waits what the relay asked for, within these bounds. It runs on
# the sender's thread, so the upper bound is also how long the next
# notification in the queue can be held up.
DEFAULT_RETRY_SECONDS = 2
MAX_RETRY_SECONDS = 30

SENT = "sent"
GONE = "gone"
REJECTED = "rejected"
RETRY = "retry"
LIMITED = "limited"
# Not a relay answer: the request itself failed, so nothing was said at all.
FAILED = "failed"

RELAY_STATUSES = (SENT, GONE, REJECTED, RETRY, LIMITED)
RETRYABLE = (RETRY, LIMITED, FAILED)

HANDLE_HINT_CHARS = 6


def handle_hint(handle: Any) -> str:
    """How a log line names a handle: its first few characters, never all of it."""
    text = str(handle or "")
    return f"{text[:HANDLE_HINT_CHARS]}…" if text else "?"


def allowed_origins(configured: Any) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """The allow-list as this gateway will use it, and what it had to refuse.

    *configured* is `push.relay_origins` as the config holds it: a list of
    origins, or one origin as a string. Every entry must be an https origin and
    nothing more; one that is not is refused and reported, and never widened
    into something that would match. An empty list is a gateway that serves no
    relay at all.
    """
    items: Iterable[Any]
    if isinstance(configured, str):
        items = [configured]
    elif isinstance(configured, (list, tuple)):
        items = configured
    else:
        items = [DEFAULT_ORIGIN]
    allowed: List[str] = []
    refused: List[str] = []
    for item in items:
        origin = relay_origin(item)
        if origin:
            if origin not in allowed:
                allowed.append(origin)
        else:
            refused.append(str(item))
    return tuple(allowed), tuple(refused)


def message_for(
    registration: Registration,
    payload: Dict[str, Any],
    *,
    title: str,
    body: str,
    gateway_key: str = "",
) -> Dict[str, Any]:
    """One entry of a `/v1/send` request.

    `payload` is the contract's data bag as the other transports send it. Any
    `preview` in it is taken out here as well as upstream: this function is the
    last place before the relay, and text must not reach it whatever a caller
    did.
    """
    data = {key: value for key, value in payload.items() if key != "preview"}
    bot = str(data.get("bot") or "")
    collapse = str(data.get("eventId") or "").encode("utf-8")[:MAX_COLLAPSE_ID_BYTES].decode("utf-8", "ignore")
    message: Dict[str, Any] = {
        "title": title,
        "body": body,
        # Notifications from one chat on one gateway stack together, and the
        # same bot name on two gateways does not.
        "thread": f"{gateway_key}:{bot}" if gateway_key else bot,
        "collapseId": collapse,
        # Everything this gateway sends is something a person asked to be told
        # about, which is the reason Expo and Web Push send it urgently too.
        "priority": "high",
        "ttl": TTL_SECONDS,
        "data": data,
    }
    category = category_for(data)
    if category:
        message["category"] = category
    return {"handle": registration.handle or "", "secret": registration.secret or "", "message": message}


def too_large(entry: Dict[str, Any]) -> bool:
    encoded = json.dumps(entry.get("message"), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return len(encoded) > MAX_MESSAGE_BYTES


@dataclass(frozen=True)
class Outcome:
    """What became of one message."""

    status: str
    reason: str = ""
    retry_after: int = 0

    @property
    def device_gone(self) -> bool:
        return self.status == GONE


@dataclass(frozen=True)
class Reply:
    """One HTTP answer, already read."""

    status: int
    body: Any = None
    retry_after: int = 0


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is an answer, not a new address to post a send capability to."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib's signature
        return None


def _proxies() -> Dict[str, str]:
    """An https proxy the operator set in the environment, and nothing else.

    `urllib` would otherwise also consult the operating system's own proxy
    settings, which a gateway's operator may never have looked at. A proxy set
    in the environment is a decision somebody made for this process; it tunnels
    the TLS connection, so it learns the relay's host and nothing it carries.
    """
    proxy = urllib.request.getproxies_environment().get("https", "")
    return {"https": proxy} if proxy else {}


def _retry_after(headers: Any) -> int:
    try:
        return max(0, int(str(headers.get("Retry-After") or "0").strip()))
    except Exception:
        return 0


def _read_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _post(url: str, body: Dict[str, Any]) -> Reply:
    """POST *body* to *url*; raises when no answer arrived at all."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": f"hermie-plugin/{contract.PLUGIN_VERSION}",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(_proxies()), _NoRedirects())
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES)
            return Reply(status=response.status, body=_read_json(raw), retry_after=_retry_after(response.headers))
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(MAX_RESPONSE_BYTES)
        except Exception:
            raw = b""
        return Reply(status=exc.code, body=_read_json(raw), retry_after=_retry_after(exc.headers))


def _outcome_of(row: Any) -> Outcome:
    if not isinstance(row, dict):
        return Outcome(status=REJECTED, reason="no answer for this message")
    status = row.get("status")
    if status not in RELAY_STATUSES:
        return Outcome(status=REJECTED, reason=f"unknown status {str(status)[:40]!r}")
    reason = row.get("reason")
    retry_after = row.get("retryAfter")
    return Outcome(
        status=str(status),
        reason=str(reason)[:120] if isinstance(reason, str) else "",
        retry_after=retry_after if isinstance(retry_after, int) and not isinstance(retry_after, bool) and retry_after > 0 else 0,
    )


def send_batch(
    origin: str, entries: Sequence[Dict[str, Any]], *, post: Callable[[str, Dict[str, Any]], Reply] = _post
) -> List[Outcome]:
    """One request to one relay, and an outcome per entry, in order."""
    if not entries:
        return []
    if len(entries) > MAX_BATCH:
        raise ValueError(f"the relay accepts at most {MAX_BATCH} messages per request")
    if relay_origin(origin) != origin:
        # Never reached through the module, which only passes allow-listed
        # origins; here so that no caller can make this post somewhere else.
        raise ValueError("a relay is posted to by its https origin and nothing else")

    try:
        reply = post(origin + SEND_PATH, {"v": REQUEST_VERSION, "messages": list(entries)})
    except Exception as exc:
        # The exception names the host at most; nothing about a message is in it.
        logger.warning("hermie: relay %s did not answer: %s", origin, type(exc).__name__)
        return [Outcome(status=FAILED, reason="no answer") for _ in entries]

    if reply.status == 429 or 500 <= reply.status < 600:
        logger.warning("hermie: relay %s answered %s to the whole request", origin, reply.status)
        return [Outcome(status=RETRY, reason=f"http-{reply.status}", retry_after=reply.retry_after) for _ in entries]
    if reply.status != 200:
        # A redirect lands here too, because it is never followed. None of these
        # says a device is gone, so no registration is touched.
        logger.warning("hermie: relay %s refused the request: %s", origin, reply.status)
        return [Outcome(status=REJECTED, reason=f"http-{reply.status}") for _ in entries]

    results = reply.body.get("results") if isinstance(reply.body, dict) else None
    if not isinstance(results, list):
        logger.warning("hermie: relay %s answered without results", origin)
        return [Outcome(status=REJECTED, reason="no results") for _ in entries]

    # Matched by handle rather than by position alone: a relay that answered in
    # another order, or skipped one, must not have one device's `gone` retire
    # another. The module never puts one handle in a request twice.
    by_handle: Dict[str, Any] = {}
    for row in results:
        if isinstance(row, dict) and isinstance(row.get("handle"), str):
            by_handle.setdefault(row["handle"], row)
    return [_outcome_of(by_handle.get(str(entry.get("handle") or ""))) for entry in entries]


def send(
    origin: str,
    entries: Sequence[Dict[str, Any]],
    *,
    post: Callable[[str, Dict[str, Any]], Reply] = _post,
    sleep: Callable[[float], None] = time.sleep,
) -> List[Outcome]:
    """Every entry for one relay, in requests of :data:`MAX_BATCH`, retried once.

    Returns an outcome per entry, in order. An entry still waiting after its
    one retry comes back with the retryable status it last got, and the caller
    drops it.
    """
    outcomes: List[Optional[Outcome]] = [None] * len(entries)
    for start in range(0, len(entries), MAX_BATCH):
        window = list(range(start, min(start + MAX_BATCH, len(entries))))
        first = send_batch(origin, [entries[index] for index in window], post=post)
        again: List[int] = []
        for index, outcome in zip(window, first):
            outcomes[index] = outcome
            if outcome.status in RETRYABLE:
                again.append(index)
        if not again:
            continue
        asked = max((outcomes[index].retry_after for index in again), default=0)  # type: ignore[union-attr]
        sleep(min(max(asked, DEFAULT_RETRY_SECONDS), MAX_RETRY_SECONDS))
        second = send_batch(origin, [entries[index] for index in again], post=post)
        for index, outcome in zip(again, second):
            outcomes[index] = outcome
    return [outcome or Outcome(status=REJECTED, reason="not sent") for outcome in outcomes]
