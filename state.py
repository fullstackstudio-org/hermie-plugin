"""The plugin's own durable state, and what happens to it across an upgrade.

An upgrade that loses state is an upgrade that re-notifies every device about
every message it already delivered, and then retires nothing. So state carries a
version and every version has a migration to the next one. A state file from a
version this build has never heard of is left on disk untouched and treated as
empty for the run — losing dedupe history costs a duplicate notification, and
overwriting a newer file costs a downgrade its data.

What lives here is small and all of it is disposable in the "fails towards a
redundant notification" direction:

- ``sent``: event id -> unix time, so the same approval does not buzz twice when
  two hooks describe it.
- ``retired``: registrations a transport told us are dead (Expo's
  ``DeviceNotRegistered``, a Web Push 404/410, a Web Push 403 for a key the
  subscription was not made with, the relay's ``gone``), kept until the app
  rewrites that installation's entry.
- ``update``: the newest release tag last seen, and when it was asked for, so an
  hourly check is hourly across restarts rather than per load.

Nothing here is a registration and nothing here is a credential. Registrations
live in the app's own ``ui_meta`` and survive any plugin change, including
removal; the one secret this plugin holds is the VAPID key beside this file,
which it minted itself. So a migration that went wrong could cost a duplicate
notification and never an account.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

logger = logging.getLogger(__name__)

STATE_VERSION = 2

# How long a dedupe key is worth keeping. Long enough that a retry storm cannot
# walk past it, short enough that the file does not grow without bound.
DEDUPE_TTL_SECONDS = 24 * 60 * 60


def _empty() -> Dict[str, Any]:
    return {"v": STATE_VERSION, "sent": {}, "retired": {}, "update": {}}


def _v1_to_v2(data: Dict[str, Any]) -> Dict[str, Any]:
    """Version 2 added the update-check cache.

    Everything a version 1 file holds is carried across untouched. A migration
    that adds a key is the easy shape and this one is deliberately the whole
    thing: the test that matters is not that ``update`` appeared, it is that
    ``sent`` and ``retired`` came through unchanged, because a lost ``retired``
    entry means talking to a dead device again and a lost ``sent`` entry means
    somebody's phone buzzes twice about a message they already read.
    """
    data.setdefault("update", {})
    return data


# version -> (next version, migrate). A migration takes the whole state dict and
# returns the whole state dict; it never raises, because a failed migration on a
# gateway nobody is watching must not stop the plugin from loading.
MIGRATIONS: Dict[int, Tuple[int, Callable[[Dict[str, Any]], Dict[str, Any]]]] = {
    1: (2, _v1_to_v2),
}


def migrate(data: Dict[str, Any]) -> Dict[str, Any]:
    """Bring a loaded state dict up to :data:`STATE_VERSION`, or start over."""
    if not isinstance(data, dict):
        return _empty()
    version = data.get("v")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return _empty()
    if version > STATE_VERSION:
        logger.warning(
            "hermie: state file is version %d, this build knows %d; "
            "leaving it alone and starting from empty for this run",
            version, STATE_VERSION,
        )
        return _empty()
    while version < STATE_VERSION:
        step = MIGRATIONS.get(version)
        if step is None:
            logger.warning("hermie: no migration from state version %d; starting from empty", version)
            return _empty()
        version, run = step
        try:
            data = run(data)
        except Exception as exc:
            logger.warning("hermie: state migration to version %d failed (%s); starting from empty", version, exc)
            return _empty()
        data["v"] = version
    for key in ("sent", "retired", "update"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    return data


# The key this plugin's whole state lives under inside `ctx.state`.
STATE_KEY = "hermie"


class FileStore:
    """A `ctx.state`-shaped store backed by one JSON file.

    Hermes gives a plugin `ctx.state`, which is atomic, file-locked, per-profile
    and quota'd — that is the store the plugin uses at runtime. This one exists
    so the tests (and anything run outside a gateway) exercise the same code
    without importing Hermes.
    """

    def __init__(self, path: Path):
        self.path = path

    def get(self, key: str, default: Any = None) -> Any:
        try:
            if not self.path.exists():
                return default
            return json.loads(self.path.read_text(encoding="utf-8")).get(key, default)
        except Exception:
            return default

    def set(self, key: str, value: Any) -> None:
        try:
            document = {}
            if self.path.exists():
                document = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                document = {}
        except Exception:
            document = {}
        document[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(self.path.parent), prefix=".hermie-", suffix=".json", delete=False
        ) as handle:
            json.dump(document, handle)
            handle.flush()
            os.fsync(handle.fileno())
            temp = Path(handle.name)
        os.replace(temp, self.path)
        os.chmod(self.path, 0o600)


class State:
    """The plugin's state, loaded once and written after every change that matters."""

    def __init__(self, store: Any):
        self.store = store
        self.data = _empty()

    def load(self) -> "State":
        try:
            self.data = migrate(self.store.get(STATE_KEY, None))
        except Exception as exc:
            logger.warning("hermie: unreadable state (%s); starting from empty", exc)
            self.data = _empty()
        return self

    def save(self) -> bool:
        try:
            self.store.set(STATE_KEY, self.data)
        except Exception as exc:
            logger.warning("hermie: could not write state: %s", exc)
            return False
        return True

    def claim(self, event_id: str, now: float | None = None) -> bool:
        """Record *event_id* as sent; ``False`` when it already was.

        This is the dedupe gate. Two hooks can describe the same fact — a
        clarify tool call and the request opening, an approval and its response
        — and a device should feel one buzz, not two.
        """
        stamp = int(now if now is not None else time.time())
        sent = self.data.setdefault("sent", {})
        previous = sent.get(event_id)
        if isinstance(previous, int) and stamp - previous < DEDUPE_TTL_SECONDS:
            return False
        sent[event_id] = stamp
        self.prune(stamp)
        return True

    def prune(self, now: int) -> None:
        sent = self.data.get("sent")
        if isinstance(sent, dict):
            for key in [k for k, at in sent.items() if not isinstance(at, int) or now - at >= DEDUPE_TTL_SECONDS]:
                sent.pop(key, None)

    def retire(self, installation_id: str, reason: str, now: float | None = None) -> None:
        """Remember that a transport said this registration is gone.

        The plugin does not delete the app's registration: that entry lives under
        the app's own ui_meta key, and a write there would fight the app's
        compare-and-swap. Recording it here is enough — a retired registration is
        skipped until the device writes a fresh entry, at which point its
        ``updatedAt`` moves past the retirement and it becomes live again.
        """
        self.data.setdefault("retired", {})[installation_id] = {
            "reason": reason,
            "at": int(now if now is not None else time.time()),
        }

    def is_retired(self, installation_id: str, updated_at: int) -> bool:
        entry = self.data.get("retired", {}).get(installation_id)
        if not isinstance(entry, dict):
            return False
        at = entry.get("at")
        return isinstance(at, int) and updated_at <= at
