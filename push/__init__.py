"""The push module: hooks in, notifications out.

The sending itself happens on a worker thread. Every hook this module registers
sits on the agent's own path — `post_llm_call` runs between the model answering
and the user seeing it — so nothing here may wait on a network. The hook builds
a decision, hands it to a bounded queue and returns; a daemon thread does the
talking. A full queue drops with a warning, because a notification that is
already late is worth less than a turn that is still fast.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .. import contract
from . import cron as cron_signal
from . import events, expo, gateway_key, relay, sessions, webpush
from .registrations import Section, read_sections

logger = logging.getLogger(__name__)

NAME = "push"

QUEUE_SIZE = 256

# How many distinct "not served" reports one process makes. A row is input, and
# a co-user writing a new origin into one on every turn must not be able to grow
# this set, or the log, without bound.
MAX_REPORTS = 64


class PushModule:
    """One instance per loaded plugin, holding the sender thread and the settings."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.queue: "queue.Queue[Optional[tuple]]" = queue.Queue(maxsize=QUEUE_SIZE)
        self.worker: Optional[threading.Thread] = None
        self.lock = threading.Lock()
        # Set once by `stop()` and never cleared: an unloaded module takes no
        # more work. Without it, an `offer()` after `stop()` saw no worker and
        # started a second sender while the first was still delivering.
        self._stopped = False
        self._vapid_key = None
        self._gateway_key: Optional[str] = None
        # What has already been reported as not served — a relay a row names, a
        # setting that is not an origin — so it costs one log line per process
        # rather than one per notification.
        self._reported: Set[str] = set()
        # When each relay, and each handle on it, may be asked again; and what
        # the relay has said about rows (see `relay.Trust`). Both run on a
        # clock that does not jump when the wall clock does.
        self._relay_pacing = relay.Pacing()
        self._relay_trust = relay.Trust()
        self._clock = time.monotonic

    # -- settings ------------------------------------------------------------
    #
    # `config_schema` in the manifest documents these and warns on a wrong type,
    # but Hermes never merges its `default` into the config — so every default
    # that actually applies is the one written here.

    @property
    def enabled_types(self) -> tuple:
        raw = self.runtime.config("push.types", list(events.TYPES))
        wanted = {str(item) for item in raw} if isinstance(raw, list) else set(events.TYPES)
        return tuple(name for name in events.TYPES if name in wanted)

    @property
    def gateway_preview(self) -> str:
        value = str(self.runtime.config("push.preview", "device"))
        return value if value in ("never", "device") else "device"

    @property
    def attached_window(self) -> int:
        return int(self.runtime.config("push.attached_window_seconds", 90) or 90)

    @property
    def delay_seconds(self) -> int:
        return int(self.runtime.config("push.delay_seconds", 5) or 5)

    @property
    def public_url(self) -> str:
        """What the operator says this gateway is called from outside.

        Only ever a FALLBACK: a registration that carries its own `gatewayKey`
        is believed first, because that is the string the device will compare
        against. See `gateway_key.py` for why that order is the safe one.
        """
        return str(self.runtime.config("push.public_url", "") or "")

    @property
    def relay_origins(self) -> Tuple[str, ...]:
        """The relays this gateway will post to, and nothing else.

        `push.relay_origins` replaces the default rather than adding to it: an
        operator who runs their own relay may want only theirs. An entry that is
        not exactly an https origin is refused, once, in the log.
        """
        allowed, refused = relay.allowed_origins(
            self.runtime.config("push.relay_origins", [relay.DEFAULT_ORIGIN])
        )
        for item in refused:
            self._report_once(
                f"setting:{item}",
                "hermie: push.relay_origins entry %r is not an https origin; ignoring it",
                item,
            )
        return allowed

    def _report_once(self, marker: str, message: str, *args: Any) -> None:
        if marker in self._reported or len(self._reported) >= MAX_REPORTS:
            return
        self._reported.add(marker)
        logger.warning(message, *args)

    def fallback_gateway_key(self) -> str:
        """The configured key, worked out once and remembered.

        Neither the setting nor `dashboard.public_url` changes while a gateway
        runs, and the second of them reads a config file — this is on the
        sender's thread, but so is every notification.
        """
        if self._gateway_key is None:
            try:
                self._gateway_key = gateway_key.configured_key(self.public_url)
            except Exception:
                self._gateway_key = ""
        return self._gateway_key

    # -- capabilities --------------------------------------------------------

    def capabilities(self) -> List[str]:
        """What this gateway can actually do, not what the code could do.

        Web Push is claimed only when the signing library imports here, because
        an app that sees the capability will offer the browser a subscribe
        button, and a button that cannot work is worse than one that is absent.
        """
        # The mute list is honoured whether or not one exists yet: the string
        # says this gateway will obey a mute, which is what the app needs to
        # know before it offers the switch.
        # `push.gateway_key` is unconditional, and that is not a shortcut: the
        # key a payload carries comes from the registration the app itself
        # wrote, so this gateway can honour it without knowing its own address.
        # A configured `push.public_url` only widens it to rows written before
        # the app carried one.
        found = [
            contract.CAP_PUSH_EXPO,
            contract.CAP_PUSH_MUTE,
            contract.CAP_PUSH_SEEN_PER_CHAT,
            contract.CAP_PUSH_PER_BOT,
            contract.CAP_PUSH_GATEWAY_KEY,
        ]
        if sessions.available():
            found.append(contract.CAP_PUSH_SESSION_KIND)
        if webpush.available():
            found.append(contract.CAP_PUSH_WEBPUSH)
        # The relay is claimed only while the relay the Hermie apps register
        # with is on the allow-list. An app that sees the string moves its
        # device onto a relay row, and in front of a gateway that does not post
        # there it would go silent. Which relays ARE served is in the advert's
        # `relayOrigins`, for an app that registered somewhere else.
        if relay.DEFAULT_ORIGIN in self.relay_origins:
            found.append(contract.CAP_PUSH_RELAY)
        if self.gateway_preview == "device":
            found.append(contract.CAP_PUSH_PREVIEW)
        if "turn_done" in self.enabled_types:
            found.append(contract.CAP_PUSH_TURN_DONE)
        if "turn_failed" in self.enabled_types:
            found.append(contract.CAP_PUSH_TURN_FAILED)
        if "cron_done" in self.enabled_types:
            found.append(contract.CAP_PUSH_CRON_DONE)
        if "cron_failed" in self.enabled_types:
            found.append(contract.CAP_PUSH_CRON_FAILED)
        if self.cron_signal_available():
            found.append(contract.CAP_PUSH_CRON_SIGNAL)
        return found

    def cron_signal_available(self) -> bool:
        """Whether a cron run can be recognised without guessing at `platform`.

        `task_id` is a kwarg on every turn hook this plugin registers, so the
        first signal is there whenever Hermes is new enough to send it; the
        session variable answers on any gateway that has the name at all. This
        asks the second, because it is the one that can be absent, and a
        capability names what is there rather than what shipped.
        """
        try:
            from ..context.session_vars import hermes_session_context

            module = hermes_session_context()
            return module is not None and cron_signal.CRON_SESSION in getattr(module, "_VAR_MAP", {})
        except Exception:
            return False

    # -- the queue -----------------------------------------------------------

    def offer(self, notification: Optional[events.Notification], *, delay: bool = False) -> None:
        """Hand one decision to the worker. Never raises, never blocks."""
        if notification is None or self._stopped:
            return
        try:
            due = time.time() + (self.delay_seconds if delay else 0)
            self.queue.put_nowait((notification, due))
        except queue.Full:
            logger.warning("hermie: push queue is full; dropped a %s notification", notification.type)
            return
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        with self.lock:
            if self._stopped:
                return
            if self.worker is not None and self.worker.is_alive():
                return
            self.worker = threading.Thread(target=self._run, name="hermie-push", daemon=True)
            self.worker.start()

    def stop(self) -> None:
        with self.lock:
            self._stopped = True
            worker = self.worker
            self.worker = None
        if worker is not None and worker.is_alive():
            try:
                self.queue.put_nowait(None)
            except queue.Full:
                pass

    def _run(self) -> None:
        while True:
            item = self.queue.get()
            if item is None:
                return
            notification, due = item
            # The delay is ADR-0017's grace period: an app that is opening gets
            # a moment to write its `seen` heartbeat and claim the chat.
            remaining = due - time.time()
            if remaining > 0:
                time.sleep(min(remaining, 30))
            try:
                self.deliver(notification)
            except Exception as exc:
                logger.warning("hermie: push delivery failed: %s", exc)

    # -- delivery ------------------------------------------------------------

    def section(self) -> Section:
        return read_sections(self.runtime.app_sections())

    def display_name(self) -> str:
        try:
            return self.runtime.bot_display_name()
        except Exception:
            return ""

    def session_kind(self, notification: events.Notification) -> str:
        """`canonical`, `branch`, `other`, or ``""`` when this gateway cannot say.

        A bot no longer has exactly one conversation, so a tap needs to know
        which kind of one it is opening. Never raises and never guesses: a
        session that cannot be read leaves the field out, and the app reads the
        notification the way it read every notification before this existed.
        """
        if not notification.session_id:
            return ""
        try:
            return sessions.kind_for(notification.session_id)
        except Exception:
            return ""

    def deliver(self, notification: events.Notification) -> int:
        """Send one notification to everybody who asked for it. Returns the count."""
        state = self.runtime.state
        if not state.claim(notification.event_id):
            return 0

        targets = events.recipients(
            notification,
            self.section(),
            now=time.time(),
            attached_window_seconds=self.attached_window,
            enabled_types=self.enabled_types,
            gateway_preview=self.gateway_preview,
            retired=state.is_retired,
        )
        if not targets:
            return 0

        # Read once for the notification rather than once per device: every
        # copy of one notification is about the same session, and this is a
        # database read. The key is the opposite — it is per device, because it
        # is the address THAT device registered against.
        session_kind = self.session_kind(notification)
        fallback_key = self.fallback_gateway_key()
        relay_origins = self.relay_origins
        # The lock screen shows the bot under the name a person gave it, where
        # there is one; `data.bot` stays the profile name, which is what the
        # app resolves a tap against. Read once per notification.
        display_name = self.display_name()

        expo_batch: List[Dict[str, Any]] = []
        expo_owners: List[str] = []
        # origin -> [(installation id, message)], so each relay gets its own
        # requests and each answer can be traced back to the row it retires.
        relay_batches: Dict[str, List[Tuple[str, str, relay.Pair, Dict[str, Any]]]] = {}
        trusted_at = self._clock()
        relay_seen: Set[Tuple[str, str, str]] = set()
        sent = 0

        for registration, preview in targets:
            # `recipients` already refused text to a relay row; asked again here
            # because this is the last decision before the wire.
            preview = preview and registration.may_preview
            title, body = notification.rendered(preview=preview)
            title = display_name or title
            key = gateway_key.key_for(registration.gateway_key, fallback_key)
            payload = notification.payload(preview=preview, gateway_key=key, session_kind=session_kind)
            if registration.transport == "relay":
                if registration.relay not in relay_origins:
                    # A row is input. Posting wherever it points would let
                    # anybody who can write a row aim this gateway at an
                    # address of their choosing, with a body it composed.
                    self._report_once(
                        f"row:{registration.relay}",
                        "hermie: a registration names relay %r, which is not on this gateway's "
                        "allow-list (push.relay_origins); not sending to it",
                        registration.relay,
                    )
                    continue
                # Two rows with the same handle AND secret are one device written
                # twice: one buzz. The same handle with another secret is a
                # different claim (one of them stale or wrong), and the relay is
                # the one that can tell which — `plan_requests` keeps the two in
                # separate requests, because answers are matched by handle.
                marker = (registration.relay, registration.handle or "", registration.secret or "")
                if marker in relay_seen:
                    continue
                relay_seen.add(marker)
                if self._relay_trust.is_gone(marker, trusted_at):
                    # The relay already said this pair is gone; a row written
                    # again with it will not be answered differently.
                    continue
                if not self._relay_trust.is_proven(marker, trusted_at) and self._relay_trust.over_budget(
                    registration.user_id, trusted_at
                ):
                    self._report_once(
                        f"budget:{registration.user_id}",
                        "hermie: relay rows of user %r keep coming back gone; their unproven rows are "
                        "not sent for up to an hour",
                        registration.user_id,
                    )
                    continue
                entry = relay.message_for(registration, payload, title=title, body=body, gateway_key=key)
                if relay.too_large(entry):
                    logger.warning(
                        "hermie: a %s notification for %s is over the relay's size limit; skipping",
                        notification.type, registration.installation_id,
                    )
                    continue
                relay_batches.setdefault(registration.relay, []).append(
                    (registration.installation_id, registration.user_id, marker, entry)
                )
                continue
            if registration.transport == "expo":
                if not expo.is_expo_token(registration.token or ""):
                    logger.warning(
                        "hermie: registration %s is not a usable Expo token; skipping",
                        registration.installation_id,
                    )
                    continue
                expo_batch.append(expo.message_for(registration.token or "", payload, title=title, body=body))
                expo_owners.append(registration.installation_id)
            elif registration.transport == "webpush":
                sent += self._send_webpush(registration, payload, title, body)

        if expo_batch:
            sent += self._send_expo(expo_batch, expo_owners)
        for origin, entries in relay_batches.items():
            sent += self._send_relay(origin, entries)

        state.save()
        return sent

    def _send_expo(self, batch: List[Dict[str, Any]], owners: List[str]) -> int:
        sent = 0
        for index in range(0, len(batch), expo.MAX_BATCH):
            window = batch[index : index + expo.MAX_BATCH]
            tickets = expo.send(window)
            for installation_id, ticket in zip(owners[index : index + expo.MAX_BATCH], tickets):
                if ticket.status == "ok":
                    sent += 1
                elif ticket.device_gone:
                    logger.info("hermie: expo says %s is gone; retiring it", installation_id)
                    self.runtime.state.retire(installation_id, "DeviceNotRegistered")
                else:
                    logger.warning(
                        "hermie: expo refused a message for %s: %s %s",
                        installation_id, ticket.error or "?", ticket.message,
                    )
        return sent

    def _send_relay(self, origin: str, entries: List[Tuple[str, str, "relay.Pair", Dict[str, Any]]]) -> int:
        try:
            outcomes = relay.send(
                origin, [entry for _, _, _, entry in entries], pacing=self._relay_pacing, clock=self._clock
            )
        except Exception as exc:
            logger.warning("hermie: relay delivery to %s failed: %s", origin, type(exc).__name__)
            return 0
        sent = 0
        now = self._clock()
        for (installation_id, user_id, pair, entry), outcome in zip(entries, outcomes):
            self._relay_trust.record(pair, user_id, outcome, now)
            hint = relay.handle_hint(entry.get("handle"))
            if outcome.status == relay.SENT:
                sent += 1
            elif outcome.device_gone:
                logger.info("hermie: relay says %s (%s) is gone; retiring it", installation_id, hint)
                self.runtime.state.retire(installation_id, "relay-gone")
            elif outcome.status == relay.DEFERRED:
                # Asked to wait and dropped without a request; said quietly,
                # because during an outage it is said about every notification.
                logger.info("hermie: not sent to %s (%s): %s", installation_id, hint, outcome.reason)
            else:
                logger.warning(
                    "hermie: relay did not deliver to %s (%s): %s %s",
                    installation_id, hint, outcome.status, outcome.reason,
                )
        return sent

    def vapid_key(self):
        if self._vapid_key is None:
            configured = str(self.runtime.config("push.vapid_key_path", "") or "")
            path = Path(configured) if configured else (self.runtime.data_dir / "vapid.pem")
            self._vapid_key = webpush.load_or_create_key(path)
        return self._vapid_key

    def _send_webpush(self, registration, payload: Dict[str, Any], title: str, body: str) -> int:
        if not webpush.available():
            return 0
        try:
            key = self.vapid_key()
        except Exception:
            return 0
        result = webpush.send(
            key,
            registration.endpoint or "",
            registration.keys.get("p256dh", ""),
            registration.keys.get("auth", ""),
            # The shape the app's service worker reads: the two visible
            # strings, and the payload under `data`. It was flat once, and the
            # worker found no bot in it — so a tap opened nothing in particular.
            {"title": title, "body": body, "data": payload},
            contact=str(self.runtime.config("push.vapid_contact", "") or ""),
        )
        if result.device_gone:
            logger.info("hermie: %s says %s is gone; retiring it", result.status, registration.installation_id)
            self.runtime.state.retire(registration.installation_id, f"http-{result.status}")
            return 0
        if not result.ok:
            logger.warning("hermie: web push returned %s for %s", result.status, registration.installation_id)
            return 0
        return 1

    # -- hooks ---------------------------------------------------------------
    #
    # Every callback takes **kwargs. Hermes inspects a callback's signature and
    # passes only the fields it declares, so a narrow signature silently stops
    # receiving fields that are added later; **kwargs is the forward-compatible
    # shape, and `hermes plugins doctor` checks for it.

    def cron_of(self, kwargs: Dict[str, Any]) -> Optional[cron_signal.Cron]:
        """Whether this turn belongs to a scheduled job. See `cron.py`."""
        return cron_signal.detect(kwargs, session_var=cron_signal.read_session_var())

    def on_post_llm_call(self, **kwargs: Any) -> None:
        bot = self.runtime.bot_name()
        session_id = str(kwargs.get("session_id") or "")
        turn_id = str(kwargs.get("turn_id") or "")
        at = int(time.time())
        cron = self.cron_of(kwargs)
        if cron is not None:
            self.offer(
                events.from_cron_delivery(
                    bot=bot,
                    session_id=session_id,
                    turn_id=turn_id,
                    assistant_response=kwargs.get("assistant_response"),
                    at=at,
                    cron=cron,
                ),
                delay=True,
            )
            return
        self.offer(
            events.from_assistant_message(
                bot=bot,
                session_id=session_id,
                turn_id=turn_id,
                assistant_response=kwargs.get("assistant_response"),
                at=at,
            ),
            delay=True,
        )

    def on_session_end(self, **kwargs: Any) -> None:
        self.offer(
            events.from_session_end(
                bot=self.runtime.bot_name(),
                session_id=str(kwargs.get("session_id") or ""),
                turn_id=kwargs.get("turn_id"),
                completed=kwargs.get("completed"),
                failed=kwargs.get("failed"),
                interrupted=kwargs.get("interrupted"),
                at=int(time.time()),
                cron=self.cron_of(kwargs),
            ),
            delay=True,
        )

    def on_pre_approval_request(self, **kwargs: Any) -> None:
        if str(kwargs.get("surface") or "") == "smart":
            # The smart path answers itself; nobody is being asked.
            return
        self.offer(
            events.from_approval(
                bot=self.runtime.bot_name(),
                session_key=str(kwargs.get("session_key") or ""),
                description=kwargs.get("description"),
                request_id=kwargs.get("request_id"),
                turn_id=kwargs.get("turn_id"),
                at=int(time.time()),
                # This hook carries no `task_id`, so the answer comes from the
                # session variable or from a `cron_…` session id — core fills in
                # `session_id` on an approval hook when the context has one.
                cron=self.cron_of(kwargs),
            )
        )

    def on_pre_tool_call(self, **kwargs: Any) -> None:
        if str(kwargs.get("tool_name") or "") != "clarify":
            return
        args = kwargs.get("args") if isinstance(kwargs.get("args"), dict) else {}
        self.offer(
            events.from_clarify(
                bot=self.runtime.bot_name(),
                session_id=str(kwargs.get("session_id") or ""),
                tool_call_id=kwargs.get("tool_call_id"),
                question=args.get("question") or args.get("prompt"),
                at=int(time.time()),
                cron=self.cron_of(kwargs),
            )
        )


def register(ctx, runtime) -> PushModule:
    module = PushModule(runtime)
    ctx.register_hook("post_llm_call", module.on_post_llm_call)
    ctx.register_hook("on_session_end", module.on_session_end)
    ctx.register_hook("pre_approval_request", module.on_pre_approval_request)
    ctx.register_hook("pre_tool_call", module.on_pre_tool_call)
    ctx.on_unload(module.stop)
    return module
