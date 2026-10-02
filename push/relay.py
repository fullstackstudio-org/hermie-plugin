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

# -- the relay's limits ---------------------------------------------------------
#
# These are the relay protocol's own numbers (v1), and `tests/test_relay.py`
# pins them. Each one is a place where getting it wrong does not cost one
# notification but every notification in the request, so every one of them is
# kept on this side with a margin rather than discovered at the far end.

# The relay takes 1 to 20 messages a request, each authorised on its own.
MAX_BATCH = 20

# The relay refuses a request body over 8 KB (8,192 bytes) as a whole, with a
# 413 — and a refused request has answered for nobody. Twenty real approvals
# are about 12 KB, so requests are cut by their encoded size as well as by
# count, and kept a margin under the relay's cap. The relay does not advertise
# a larger cap; if it ever does, this is the number to read it into.
MAX_REQUEST_BYTES = 7_680

# The relay's cap on one `message`, as serialised JSON, is 3,584 bytes; this
# side keeps a margin. A message this gateway builds is a few hundred bytes.
MAX_MESSAGE_BYTES = 3_500

# The relay's collapse id is at most 64 bytes, and its thread 256 characters.
MAX_COLLAPSE_ID_BYTES = 64
MAX_THREAD_CHARS = 256

# How long the relay should keep trying a device that is offline.
TTL_SECONDS = 3600

TIMEOUT_SECONDS = 10

# How much of an answer is read. The relay's answer to twenty messages is a
# couple of kilobytes; anything far larger is not an answer to trust.
MAX_RESPONSE_BYTES = 64 * 1024

# The one retry waits what the relay asked for, within these bounds. It runs on
# the sender's thread, so the upper bound is also how long the next
# notification in the queue can be held up — and a relay that asks for longer
# is not waited for at all: the message is dropped and the relay is left alone
# until the time it named (see `Pacing`).
DEFAULT_RETRY_SECONDS = 2
MAX_RETRY_SECONDS = 30

# How long a relay that failed a request twice in a row is left alone, when it
# named no time of its own. Every notification in that window is dropped
# without a request, instead of each one spending a timeout and a retry.
BACKOFF_SECONDS = 60

# The most handles `Pacing` remembers a limit for. A limit is a few seconds to a
# day; past this many, the soonest to lapse are forgotten first.
MAX_PACED_HANDLES = 1024

SENT = "sent"
GONE = "gone"
REJECTED = "rejected"
RETRY = "retry"
LIMITED = "limited"
# Not relay answers. `failed`: the request got no answer at all. `deferred`:
# no request was made, because that relay or that handle asked to be left
# alone until a time that has not come yet.
FAILED = "failed"
DEFERRED = "deferred"

RELAY_STATUSES = (SENT, GONE, REJECTED, RETRY, LIMITED)
RETRYABLE = (RETRY, LIMITED, FAILED)

# A whole request answered with one of these said nothing about any one
# message: 400 is one entry the relay could not read, 413 a body it would not
# take. Each entry is then sent on its own, once, so a bad entry costs itself.
SPLIT_ON = (400, 413)

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
    # Notifications from one chat on one gateway stack together, and the same
    # bot name on two gateways does not.
    thread = (f"{gateway_key}:{bot}" if gateway_key else bot)[:MAX_THREAD_CHARS]
    message: Dict[str, Any] = {
        "title": title,
        "body": body,
        # Everything this gateway sends is something a person asked to be told
        # about, which is the reason Expo and Web Push send it urgently too.
        "priority": "high",
        "ttl": TTL_SECONDS,
        "data": data,
    }
    # Optional members are left out rather than sent empty: the relay refuses
    # an empty thread or collapse id, and refuses it for this message only.
    if thread:
        message["thread"] = thread
    if collapse:
        message["collapseId"] = collapse
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
        # The same encoding `encode_request` measures, so a request sized to fit
        # is the request that is sent.
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


def encode_request(entries: Sequence[Dict[str, Any]]) -> bytes:
    """The body of one `/v1/send` request, exactly as it goes on the wire."""
    return json.dumps({"v": REQUEST_VERSION, "messages": list(entries)}, separators=(",", ":")).encode("utf-8")


_EMPTY_REQUEST_BYTES = len(encode_request([]))


def plan_requests(entries: Sequence[Dict[str, Any]], indices: Optional[Sequence[int]] = None) -> List[List[int]]:
    """Cut *entries* into requests the relay will take, as lists of indices.

    A request holds at most :data:`MAX_BATCH` entries, encodes to at most
    :data:`MAX_REQUEST_BYTES`, and never names one handle twice: answers are
    matched back by handle, so a handle in a request twice would leave one of
    its answers with nowhere to go. Order is kept. An entry too large to share
    a request travels alone, and the relay answers for it alone.
    """
    order = list(range(len(entries)) if indices is None else indices)
    requests: List[List[int]] = []
    current: List[int] = []
    handles: set = set()
    size = _EMPTY_REQUEST_BYTES
    for index in order:
        entry = entries[index]
        handle = str(entry.get("handle") or "")
        # The entry's own bytes, plus the comma between it and the one before.
        cost = len(json.dumps(entry, separators=(",", ":")).encode("utf-8")) + (1 if current else 0)
        if current and (
            len(current) >= MAX_BATCH or handle in handles or size + cost > MAX_REQUEST_BYTES
        ):
            requests.append(current)
            current, handles, size = [], set(), _EMPTY_REQUEST_BYTES
            cost -= 1
        current.append(index)
        handles.add(handle)
        size += cost
    if current:
        requests.append(current)
    return requests


@dataclass(frozen=True)
class Answer:
    """One request's answer: what it said about each entry, and about itself.

    `whole` is ``""`` when the relay answered per entry, `unavailable` when the
    request as a whole failed in a way worth trying again (429, a 5xx, no
    answer), `split` when it was refused as a whole in a way that one entry can
    cause (400, 413), and `refused` for every other whole-request refusal.
    """

    outcomes: List[Outcome]
    whole: str = ""
    retry_after: int = 0


def _ask(origin: str, entries: Sequence[Dict[str, Any]], post: Callable[[str, Dict[str, Any]], Reply]) -> Answer:
    if relay_origin(origin) != origin:
        # Never reached through the module, which only passes allow-listed
        # origins; here so that no caller can make this post somewhere else.
        raise ValueError("a relay is posted to by its https origin and nothing else")
    try:
        reply = post(origin + SEND_PATH, {"v": REQUEST_VERSION, "messages": list(entries)})
    except Exception as exc:
        # The exception names the host at most; nothing about a message is in it.
        logger.warning("hermie: relay %s did not answer: %s", origin, type(exc).__name__)
        return Answer([Outcome(status=FAILED, reason="no answer") for _ in entries], whole="unavailable")

    if reply.status == 429 or 500 <= reply.status < 600:
        logger.warning("hermie: relay %s answered %s to the whole request", origin, reply.status)
        outcomes = [Outcome(status=RETRY, reason=f"http-{reply.status}", retry_after=reply.retry_after) for _ in entries]
        return Answer(outcomes, whole="unavailable", retry_after=reply.retry_after)
    if reply.status != 200:
        # A redirect lands here too, because it is never followed. None of these
        # says a device is gone, so no registration is touched.
        logger.warning("hermie: relay %s refused a request of %d: %s", origin, len(entries), reply.status)
        outcomes = [Outcome(status=REJECTED, reason=f"http-{reply.status}") for _ in entries]
        return Answer(outcomes, whole="split" if reply.status in SPLIT_ON else "refused")

    results = reply.body.get("results") if isinstance(reply.body, dict) else None
    if not isinstance(results, list):
        logger.warning("hermie: relay %s answered without results", origin)
        return Answer([Outcome(status=REJECTED, reason="no results") for _ in entries], whole="refused")

    # Matched by handle rather than by position: a relay that answered in
    # another order, or skipped one, must not have one device's `gone` retire
    # another. `plan_requests` never puts one handle in a request twice.
    by_handle: Dict[str, Any] = {}
    for row in results:
        if isinstance(row, dict) and isinstance(row.get("handle"), str):
            by_handle.setdefault(row["handle"], row)
    return Answer([_outcome_of(by_handle.get(str(entry.get("handle") or ""))) for entry in entries])


def send_batch(
    origin: str, entries: Sequence[Dict[str, Any]], *, post: Callable[[str, Dict[str, Any]], Reply] = _post
) -> List[Outcome]:
    """One request to one relay, and an outcome per entry, in order."""
    if not entries:
        return []
    if len(entries) > MAX_BATCH:
        raise ValueError(f"the relay accepts at most {MAX_BATCH} messages per request")
    return _ask(origin, entries, post).outcomes


class Pacing:
    """When each relay, and each handle on it, may be asked again.

    Kept by the push module for as long as the gateway runs, so a relay that
    said "not now" is not asked again by the very next notification. Both
    answers cost nothing to honour: a notification that would have waited is
    dropped without a request, which is what it would have been anyway.
    """

    def __init__(self) -> None:
        self.origins: Dict[str, float] = {}
        self.handles: Dict[Tuple[str, str], float] = {}

    def origin_wait(self, origin: str, now: float) -> float:
        return max(0.0, self.origins.get(origin, 0.0) - now)

    def hold_origin(self, origin: str, until: float) -> None:
        self.origins[origin] = max(self.origins.get(origin, 0.0), until)

    def handle_wait(self, origin: str, handle: str, now: float) -> float:
        return max(0.0, self.handles.get((origin, handle), 0.0) - now)

    def hold_handle(self, origin: str, handle: str, until: float, now: float) -> None:
        key = (origin, handle)
        self.handles[key] = max(self.handles.get(key, 0.0), until)
        if len(self.handles) > MAX_PACED_HANDLES:
            for stale in [k for k, at in self.handles.items() if at <= now]:
                del self.handles[stale]
            while len(self.handles) > MAX_PACED_HANDLES:
                del self.handles[min(self.handles, key=self.handles.get)]


def _round(
    origin: str,
    entries: Sequence[Dict[str, Any]],
    indices: Sequence[int],
    outcomes: List[Optional[Outcome]],
    *,
    post: Callable[[str, Dict[str, Any]], Reply],
    pacing: Pacing,
    clock: Callable[[], float],
    backoff: float,
) -> None:
    """Send *indices* once, in as many requests as the limits need."""
    for request in plan_requests(entries, indices):
        wait = pacing.origin_wait(origin, clock())
        if wait > 0:
            # An earlier request in this round found the relay unavailable.
            # The rest are not sent into the same wall.
            for index in request:
                outcomes[index] = Outcome(status=FAILED, reason="relay unavailable", retry_after=int(wait + 0.999))
            continue
        answer = _ask(origin, [entries[index] for index in request], post)
        if answer.whole == "split" and len(request) > 1:
            # One entry can sink a request (a field the relay will not read, a
            # body over its cap). Each is asked about on its own, once, so the
            # one that did it is the only one that pays.
            for index in request:
                single = _ask(origin, [entries[index]], post)
                outcomes[index] = single.outcomes[0]
                if single.whole == "unavailable":
                    pacing.hold_origin(origin, clock() + max(single.retry_after, backoff))
                    break
            for index in request:
                if outcomes[index] is None:
                    outcomes[index] = Outcome(status=FAILED, reason="relay unavailable")
            continue
        for index, outcome in zip(request, answer.outcomes):
            outcomes[index] = outcome
        if answer.whole == "unavailable":
            pacing.hold_origin(origin, clock() + max(answer.retry_after, backoff))


def send(
    origin: str,
    entries: Sequence[Dict[str, Any]],
    *,
    post: Callable[[str, Dict[str, Any]], Reply] = _post,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    pacing: Optional[Pacing] = None,
) -> List[Outcome]:
    """Every entry for one relay, within its limits, retried at most once.

    Returns an outcome per entry, in order. At most one wait happens per call,
    and never one longer than :data:`MAX_RETRY_SECONDS`: an entry the relay
    asked to hold for longer is dropped on the spot, and the relay or the
    handle is left alone until then. An entry still waiting after its one retry
    comes back with the status it last got, and the caller drops it.
    """
    pacing = pacing if pacing is not None else Pacing()
    outcomes: List[Optional[Outcome]] = [None] * len(entries)
    if not entries:
        return []

    now = clock()
    wait = pacing.origin_wait(origin, now)
    if wait > 0:
        return [Outcome(status=DEFERRED, reason=f"relay unavailable for {int(wait + 0.999)}s") for _ in entries]

    todo: List[int] = []
    for index, entry in enumerate(entries):
        held = pacing.handle_wait(origin, str(entry.get("handle") or ""), now)
        if held > 0:
            outcomes[index] = Outcome(status=DEFERRED, reason=f"handle limited for {int(held + 0.999)}s")
        else:
            todo.append(index)

    _round(origin, entries, todo, outcomes, post=post, pacing=pacing, clock=clock, backoff=DEFAULT_RETRY_SECONDS)

    now = clock()
    again: List[int] = []
    for index in todo:
        outcome = outcomes[index]
        if outcome is not None and outcome.status in RETRYABLE and outcome.retry_after <= MAX_RETRY_SECONDS:
            again.append(index)
    if not again:
        return _finish(origin, entries, outcomes, pacing, now)

    asked = max([outcomes[index].retry_after for index in again] + [DEFAULT_RETRY_SECONDS])  # type: ignore[union-attr]
    asked = max(asked, pacing.origin_wait(origin, now))
    if asked > MAX_RETRY_SECONDS:
        # The relay as a whole asked for longer than this thread may wait.
        return _finish(origin, entries, outcomes, pacing, now)
    sleep(asked)
    # The second round is the last: a relay still unavailable after it is left
    # alone for a while rather than asked by every notification that follows.
    _round(origin, entries, again, outcomes, post=post, pacing=pacing, clock=clock, backoff=BACKOFF_SECONDS)
    return _finish(origin, entries, outcomes, pacing, clock())


def _finish(
    origin: str, entries: Sequence[Dict[str, Any]], outcomes: List[Optional[Outcome]], pacing: Pacing, now: float
) -> List[Outcome]:
    """Remember every handle the relay said is over its limit, and close the books."""
    for entry, outcome in zip(entries, outcomes):
        if outcome is not None and outcome.status == LIMITED and outcome.retry_after:
            pacing.hold_handle(origin, str(entry.get("handle") or ""), now + outcome.retry_after, now)
    return [outcome or Outcome(status=REJECTED, reason="not sent") for outcome in outcomes]
