"""Which conversation a notification belongs to, when there is more than one.

A bot used to have exactly one chat, so a notification naming the bot named the
conversation too. That stopped being true: the app now branches a conversation
and retires the one `/new` puts away, so a turn can happen in a session nobody
is looking at and a tap has to land on the right one.

The payload already carries `sessionId`. What it could not say is what KIND of
session that is, and the app needs that to choose a destination before it has
resolved anything: the canonical chat opens as the Bot Chat, a branch opens as a
read-only conversation, and anything else opens as a past conversation. So the
payload carries `sessionKind` beside the id, and the app still resolves the id
itself — the field is a hint about presentation, never an instruction.

## The title is the only signal, on both sides

Upstream's session row has no parent field and no kind field. The app says so in
`features/sessions/session-model.ts` and classifies by title; this reads the
same three titles out of the same registry:

| Title | Kind |
|---|---|
| exactly `Bot Chat` | `canonical` — it is the registry key ADR-0007 gives a bot |
| `Branch` or `Branch · …` | `branch` |
| anything else, including `Bot Chat · <date time>` | `other` |

`other` rather than the app's own `past`, because the app's `past` group is
"everything else it decided to list" and this gateway cannot know that. What it
can say is "not the canonical chat and not a branch", and that is what the word
means here.

**A session this gateway cannot read says nothing at all.** No title, no
Hermes, an id the registry does not hold: the field is omitted and the app reads
the notification exactly as it read every notification before this existed. The
one thing not done is to guess `other` for a session that could not be looked
up, because that is the answer that would send a tap to the wrong screen.

## Why this is read per notification, on the sender's thread

`get_session_title` is one indexed read of the sessions table, and it happens on
the push worker beside the HTTP calls rather than on the agent's own path. It is
deliberately not cached: a branch that somebody promotes to Bot Chat changes its
title, and a stale kind would open the wrong conversation for as long as the
cache held. A row read is cheaper than that mistake.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ADR-0007's forever-chat, by the exact title that IS its registry key.
CANONICAL_TITLE = "Bot Chat"

# What `branchTitle` writes: the bare word, or the word and a summary.
BRANCH_PREFIX = "Branch"
BRANCH_SEPARATOR = " · "  # the same middle dot the retired conversations use

KIND_CANONICAL = "canonical"
KIND_BRANCH = "branch"
KIND_OTHER = "other"

KINDS = (KIND_CANONICAL, KIND_BRANCH, KIND_OTHER)


def kind_of(title: Optional[str]) -> str:
    """Classify one session title, or ``""`` when there is nothing to classify.

    A session with no title is not "other" here: it is unreadable and unread
    sessions get no claim. See the module note — `read_title` cannot tell a row
    with a NULL title from a row that is not there, so neither can this.
    """
    if not isinstance(title, str) or not title.strip():
        return ""
    if title == CANONICAL_TITLE:
        return KIND_CANONICAL
    if title == BRANCH_PREFIX or title.startswith(BRANCH_PREFIX + BRANCH_SEPARATOR):
        return KIND_BRANCH
    return KIND_OTHER


def _session_db(home: Optional[Path] = None):
    """Hermes' shared session database, or a raised import error outside one.

    `home` names the profile whose `state.db` is meant; ``None`` is the active
    home's, which on the push worker is the gateway's own.

    `acquire`/`release` and nothing else. The older `get_shared_session_db`
    pair still resolves today, but it is in Hermes' own compat manifest with a
    removal date on it — and a plugin whose source so much as NAMES a scheduled
    name is refused at load, whole, by the static scan `hermes plugins doctor`
    runs. A fallback that costs the plugin its entire load is not a fallback.
    """
    from hermes_state_registry import acquire, release  # type: ignore

    return (acquire(Path(home) / "state.db") if home is not None else acquire()), release


def read_title(session_id: str, home: Optional[Path] = None) -> Optional[str]:
    """The title Hermes holds for this session, or ``None``.

    ``None`` covers every way of not knowing — no Hermes, no database, an id the
    registry has never seen — because they all mean the same thing to the caller
    and none of them is worth a different payload.
    """
    if not session_id:
        return None
    try:
        database, close = _session_db(home)
    except Exception:
        return None
    try:
        title = database.get_session_title(session_id)
        return title if isinstance(title, str) else None
    except Exception as exc:
        logger.debug("hermie: could not read the title of session %s: %s", session_id, exc)
        return None
    finally:
        try:
            close(database)
        except Exception:
            pass


def available() -> bool:
    """Whether this gateway can answer the question at all.

    Asked once, for the capability string, which names what is there rather
    than what shipped: an app that sees `push.session_kind` may rely on a
    payload from a non-canonical session saying so, and on a gateway where the
    registry is not there it never would.

    Deliberately does NOT open the database. This runs at plugin load — and
    under `hermes plugins validate`, which registers against a probe context —
    and acquiring a session database to answer a question about whether one
    could be acquired is a side effect on somebody's gateway in exchange for
    nothing. The two imports are the whole of what the lookup needs.
    """
    try:
        import hermes_state_registry  # type: ignore
        from hermes_state_titles import SessionTitlesMixin  # type: ignore

        return callable(getattr(hermes_state_registry, "acquire", None)) and callable(
            getattr(SessionTitlesMixin, "get_session_title", None)
        )
    except Exception:
        return False


def kind_for(session_id: str, *, read: Any = None, home: Optional[Path] = None) -> str:
    """The kind of one session, or ``""`` when this gateway cannot say.

    `read` is the title lookup, passed in so the classification can be driven
    without a gateway — the same shape `cron.detect` uses for its session
    variable. `home` is the profile the session belongs to when that is not the
    active one (a bot routed to another profile); the lookup is then asked with
    it.
    """
    reader = read if read is not None else read_title
    try:
        return kind_of(reader(session_id) if home is None else reader(session_id, home=home))
    except Exception:
        return ""
