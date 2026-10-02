"""Sending through the Expo Push API, and believing what it says back.

Expo's send endpoint takes no secret: the token IS the address, which is why
ADR-0017 could put this inside a self-hosted gateway without an account
anywhere. The consequence is that a token is worth protecting exactly as much
as a mailbox address and no more, and that a send says nothing about delivery.

Delivery is what the receipts say, and they say it later. So this module does
both halves: `send` hands Expo a batch and reads the immediate per-message
ticket, and `receipts` collects the verdicts afterwards. Only the second half
can report `DeviceNotRegistered`, which is the one answer that changes state —
it means the app was deleted or the token was reissued, and that registration
should stop being used.

Only the standard library is used. `urllib` cannot bound a pathological DNS
lookup, which is accepted: the worst case is one blocked sender thread, and the
whole path runs off the agent loop.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from .events import category_for, channel_for

logger = logging.getLogger(__name__)

SEND_URL = "https://exp.host/--/api/v2/push/send"
RECEIPTS_URL = "https://exp.host/--/api/v2/push/getReceipts"

# Expo accepts at most 100 messages per request.
MAX_BATCH = 100

TIMEOUT_SECONDS = 15


def is_expo_token(token: str) -> bool:
    """Whether *token* has the shape Expo issues.

    Checked before sending so a mangled registration is dropped here rather than
    spending a round trip to be told the same thing.
    """
    return isinstance(token, str) and (
        (token.startswith("ExponentPushToken[") or token.startswith("ExpoPushToken[")) and token.endswith("]")
    )


@dataclass(frozen=True)
class Ticket:
    """Expo's immediate answer for one message."""

    token: str
    status: str  # "ok" | "error"
    receipt_id: Optional[str] = None
    error: Optional[str] = None  # "DeviceNotRegistered", "MessageTooBig", ...
    message: str = ""

    @property
    def device_gone(self) -> bool:
        return self.error == "DeviceNotRegistered"


def _post(url: str, body: Dict[str, Any]) -> Dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def send(messages: List[Dict[str, Any]], *, post=_post) -> List[Ticket]:
    """Hand Expo one batch and return a ticket per message, in order.

    `post` is injected so the tests can drive every branch without a network,
    and so a caller can swap in a client with a saner timeout story.
    """
    if not messages:
        return []
    if len(messages) > MAX_BATCH:
        raise ValueError(f"expo accepts at most {MAX_BATCH} messages per request")

    tokens = [str(message.get("to", "")) for message in messages]
    try:
        body = post(SEND_URL, messages)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        logger.warning("hermie: expo send rejected the batch (%s): %s", exc.code, detail)
        return [Ticket(token=token, status="error", error="RequestFailed", message=detail) for token in tokens]
    except Exception as exc:
        logger.warning("hermie: expo send failed: %s", exc)
        return [Ticket(token=token, status="error", error="RequestFailed", message=str(exc)) for token in tokens]

    rows = body.get("data")
    if not isinstance(rows, list):
        # A top-level `errors` key with no `data` means Expo refused the whole
        # request — a malformed body, usually. Nothing here is retryable and
        # nothing here is a dead device, so no registration is touched.
        detail = json.dumps(body.get("errors") or body)[:400]
        logger.warning("hermie: expo returned no tickets: %s", detail)
        return [Ticket(token=token, status="error", error="RequestFailed", message=detail) for token in tokens]

    tickets: List[Ticket] = []
    for token, row in zip(tokens, rows):
        row = row if isinstance(row, dict) else {}
        details = row.get("details") if isinstance(row.get("details"), dict) else {}
        tickets.append(
            Ticket(
                token=token,
                status=str(row.get("status") or "error"),
                receipt_id=row.get("id") if isinstance(row.get("id"), str) else None,
                error=details.get("error") if isinstance(details.get("error"), str) else None,
                message=str(row.get("message") or ""),
            )
        )
    return tickets


def receipts(receipt_ids: Iterable[str], *, post=_post) -> Dict[str, Ticket]:
    """Ask Expo what became of tickets it handed out earlier.

    Returns receipt id -> verdict. A receipt id Expo has no answer for yet is
    simply absent: it is not an error and it is not a dead device, and the
    caller asks again later or lets it go.
    """
    ids = [str(item) for item in receipt_ids if item]
    if not ids:
        return {}
    try:
        body = post(RECEIPTS_URL, {"ids": ids[:MAX_BATCH]})
    except Exception as exc:
        logger.warning("hermie: expo receipt lookup failed: %s", exc)
        return {}

    rows = body.get("data")
    if not isinstance(rows, dict):
        return {}

    out: Dict[str, Ticket] = {}
    for receipt_id, row in rows.items():
        row = row if isinstance(row, dict) else {}
        details = row.get("details") if isinstance(row.get("details"), dict) else {}
        out[str(receipt_id)] = Ticket(
            token="",
            status=str(row.get("status") or "error"),
            receipt_id=str(receipt_id),
            error=details.get("error") if isinstance(details.get("error"), str) else None,
            message=str(row.get("message") or ""),
        )
    return out


def message_for(token: str, payload: Dict[str, Any], *, title: str, body: str) -> Dict[str, Any]:
    """One Expo message.

    `data` is what the app reads when it opens; `title`/`body` are what the lock
    screen renders. ADR-0017's default is that the visible half says who and what
    kind, never what was said, so the caller decides those two strings and this
    function does not enrich them.
    """
    message = {
        "to": token,
        "title": title,
        "body": body,
        "data": payload,
        "sound": "default",
        # A stable channel so Android users can silence one kind of notification
        # without silencing Hermie: the type name, as the push contract says.
        "channelId": channel_for(payload),
        "priority": "high",
    }
    # The category is what attaches the Allow/Deny actions the app registered,
    # and only an approval has an answer to give. It was the bare type once,
    # which no app registered, so approvals arrived without their buttons.
    category = category_for(payload)
    if category:
        message["categoryId"] = category
    return message
