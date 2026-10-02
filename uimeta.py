"""Reading and writing a profile's ``ui_meta`` from inside the gateway process.

Hermie's app reaches ``ui_meta`` over the WebSocket, through ``profiles.list``
and ``profiles.configure``. A plugin is already inside the process that owns the
file, so it reads the file instead: no second connection, no credential of its
own, and no dependency on the gateway being willing to talk to itself.

The file is ``profile.yaml`` — ``$HERMES_HOME/profile.yaml`` for the default
profile and ``$HERMES_HOME/profiles/<name>/profile.yaml`` for the others. It
holds two maps that matter here:

    ui_meta:              <key> -> arbitrary JSON
    _ui_meta_revisions:   <key> -> integer, bumped on every write

The revision map is the gateway's per-key compare-and-swap. A client sends the
revision it last saw and its write is rejected when the key moved underneath it.
This module honours that from the writing side: it only ever writes keys this
plugin owns, it bumps their revisions, and it never touches a neighbouring key.

**The plugin does not write `hermie-app`.** That key belongs to the app, which
holds a revision for it and will have its write rejected if the plugin bumps it
behind the app's back. Everything the plugin publishes goes under its own
`hermie-plugin` key, which no app version writes.

The app is moving its own bag from one shared `hermie-app` key to one key per
person, `hermie-app:<user id>` — the gateway identity the app resolved for the
signed-in person, which is `owner` on a token gateway. Both are read here for
one version. They are read in a fixed order, legacy first, and the order IS the
precedence: the per-user key wins for the person it names.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# The keys the app owns and the plugin only ever READS. `hermie-app` is the one
# shared bag every app version up to now wrote; `hermie-app:<user id>` is the
# per-user bag it writes from now on.
APP_KEY = "hermie-app"
APP_KEY_PREFIX = APP_KEY + ":"

# The key the plugin owns and is free to write.
PLUGIN_KEY = "hermie-plugin"


def hermes_home() -> Path:
    """The active profile's home, from Hermes when it is importable.

    A test (and `python -m hermie` style poking) has no Hermes on the path, so
    ``HERMES_HOME`` is honoured as a fallback rather than crashing the import.
    """
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(str(get_hermes_home()))
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def profile_path(home: Optional[Path] = None) -> Path:
    """The ``profile.yaml`` of the profile whose home this is."""
    return (home or hermes_home()) / "profile.yaml"


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml  # PyYAML ships with Hermes.

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        # An unreadable profile.yaml is the gateway's problem, not ours, and
        # guessing at {} here would be destructive on the next write. Callers
        # that write re-raise; callers that read degrade to "no registrations".
        logger.warning("hermie: could not read %s: %s", path, exc)
        raise
    return data if isinstance(data, dict) else {}


def app_key_for(user_id: str) -> str:
    """The ui_meta key holding one person's bag."""
    return f"{APP_KEY_PREFIX}{user_id}" if user_id else APP_KEY


def user_of_app_key(key: str) -> Optional[str]:
    """Whose bag a key is: ``""`` for the legacy key, ``None`` for anything else."""
    if key == APP_KEY:
        return ""
    if key.startswith(APP_KEY_PREFIX) and key[len(APP_KEY_PREFIX) :]:
        return key[len(APP_KEY_PREFIX) :]
    return None


def is_app_key(key: str) -> bool:
    """Whether this key belongs to the app rather than to the plugin."""
    return user_of_app_key(key) is not None


def profile_stamp(home: Optional[Path] = None) -> Tuple[int, int]:
    """A cheap "has ``profile.yaml`` moved?" reading: ``(mtime_ns, size)``.

    One ``stat`` call. It exists so a caller on the agent's own path can ask
    whether anything the app wrote could have changed without parsing YAML for
    an answer that is almost always "no". A missing or unreadable file is
    ``(0, 0)``, which differs from every real file and so reads as a change
    exactly once — the recoverable direction.

    It is a *change* detector, never an equality proof: two writes inside one
    filesystem timestamp tick collide. Every caller here treats a moved stamp
    as "go and look", so a collision costs a missed look rather than a wrong
    answer, and the next write moves it again.
    """
    try:
        info = profile_path(home).stat()
    except Exception:
        return (0, 0)
    return (int(getattr(info, "st_mtime_ns", 0)), int(info.st_size))


def read_display_name(home: Optional[Path] = None) -> Any:
    """The profile's own ``display_name``, as the file holds it, or ``None``.

    Read raw: deciding whether it is fit to show is the caller's business, and
    the one caller (`Runtime.bot_display_name`) cleans it.
    """
    try:
        return _load_yaml(profile_path(home)).get("display_name")
    except Exception:
        return None


def read_meta(home: Optional[Path] = None) -> Dict[str, Any]:
    """The whole ``ui_meta`` map, or ``{}`` when the profile cannot be read."""
    try:
        data = _load_yaml(profile_path(home))
    except Exception:
        return {}
    meta = data.get("ui_meta")
    return meta if isinstance(meta, dict) else {}


def read_app_sections(home: Optional[Path] = None) -> List[Tuple[str, Any]]:
    """Every bag the app owns on this profile, as ``(user id, value)``.

    The legacy `hermie-app` comes first with an empty user id, then the per-user
    keys in a stable order. **That order is the precedence**: a caller merges in
    sequence and lets a later entry win, which makes the per-user key beat the
    legacy one for the same person, the same device and the same heartbeat.
    """
    meta = read_meta(home)
    out: List[Tuple[str, Any]] = []
    if APP_KEY in meta:
        out.append(("", meta[APP_KEY]))
    for key in sorted(meta):
        user_id = user_of_app_key(str(key))
        if user_id:
            out.append((user_id, meta[key]))
    return out


def read_key(key: str, home: Optional[Path] = None) -> Any:
    """One ``ui_meta`` key, or ``None`` when it is absent or unreadable."""
    try:
        data = _load_yaml(profile_path(home))
    except Exception:
        return None
    meta = data.get("ui_meta")
    return meta.get(key) if isinstance(meta, dict) else None


def read_revision(key: str, home: Optional[Path] = None) -> int:
    """The gateway's revision counter for one key; 0 when it has never been written."""
    try:
        data = _load_yaml(profile_path(home))
    except Exception:
        return 0
    revisions = data.get("_ui_meta_revisions")
    value = revisions.get(key) if isinstance(revisions, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def write_key(key: str, value: Any, home: Optional[Path] = None) -> bool:
    """Replace one ``ui_meta`` key and bump its revision, leaving the rest alone.

    Returns whether the file was written. This is a read-modify-write on a file
    the gateway also writes, so it is done through a temp file and an atomic
    rename: a reader sees the old bytes or the new ones, never a half file. The
    window between the read and the rename is real and is documented in
    DESIGN.md — it is narrow, this plugin writes rarely, and the only key it
    writes is one nothing else touches.
    """
    if is_app_key(key):
        raise ValueError(f"hermie: refusing to write {key!r}; that key belongs to the app")

    path = profile_path(home)
    try:
        data = _load_yaml(path)
    except Exception:
        return False

    meta = data.get("ui_meta")
    if not isinstance(meta, dict):
        meta = {}
    revisions = data.get("_ui_meta_revisions")
    if not isinstance(revisions, dict):
        revisions = {}

    if value is None:
        # `None` removes a key, the same way the app's own writes do, and the
        # revision counter survives the removal so a stale client still loses.
        meta.pop(key, None)
    else:
        meta[key] = json.loads(json.dumps(value))

    current = revisions.get(key)
    revisions[key] = (current if isinstance(current, int) and not isinstance(current, bool) else 0) + 1

    if meta:
        data["ui_meta"] = meta
    else:
        data.pop("ui_meta", None)
    data["_ui_meta_revisions"] = revisions

    try:
        import yaml

        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent), prefix=".hermie-", suffix=".yaml", delete=False
        ) as handle:
            yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True)
            handle.flush()
            os.fsync(handle.fileno())
            temp = Path(handle.name)
        os.replace(temp, path)
    except Exception as exc:
        logger.warning("hermie: could not write %s: %s", path, exc)
        return False
    return True
