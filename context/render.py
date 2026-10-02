"""Working out who sent a turn, and cleaning what gets said about them.

Until HERM-119 this module also read a `context` section the app wrote under
its own `ui_meta` key — a person's name, device, timezone, locale and their own
free text — and turned it into a paragraph for a bot's system prompt. That is
gone: the app no longer sends it, on any build, and this module no longer reads
it. What is left is the half that was never the app's to begin with — working
out WHO the gateway thinks is sending a turn, from its own session state, and
the string-cleaning helpers that make a login safe to put in a sentence a model
is told to rely on.

The ids on the two sides of a sender lookup are not spelled the same. A gateway
login carries the provider that issued it — `self-hosted:<uuid>`, `oidc:<sub>`,
`basic:<name>` — while some callers compare against a bare id. `same_user`
below is the one place that knows both forms name one person.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Tuple

# The provider prefix on a gateway login, and the whole subtlety of reading it:
# an OIDC subject may itself be a URL, and the colon in
# `https://accounts.example.com/12345` is a scheme, not a provider. Requiring
# that what follows is not `//` is what keeps such a subject whole — including
# when it arrives prefixed, as `oidc:https://…`, where the first colon really
# is the provider and the second really is not.
PROVIDER_PREFIX = re.compile(r"^([A-Za-z][A-Za-z0-9._-]*):(?!//)(.+)$")

# Every rung of the resolution order, named once. The first four are where a
# sender can come from, in the order they are asked, and belong to the module
# that asks; the rest are what a caller falls back to. They are here together
# because `/me` answers with them and a person reading that answer should be
# reading one vocabulary.
BY_CLAIM = "turn claim"
BY_HOOK = "hook sender"
BY_PLATFORM = "platform sender"
BY_SESSION_VARS = "session variables"
BY_LIVE_SESSION = "live session record"
BY_CONFIGURED = "configured default"
BY_APP_DEFAULT = "app default"
BY_ONLY_USER = "only registered person"
BY_NOBODY = "nobody"

SENDER_RUNGS = (BY_CLAIM, BY_HOOK, BY_PLATFORM, BY_LIVE_SESSION, BY_SESSION_VARS)

# And the split that decides what may be asserted about the sender a rung named.
#
# **No rung is verified today.** Two rungs were once treated as answering "who
# sent THIS turn" — a turn claim, and a hook sender the dashboard did not admit
# — and both assertions were withdrawn on review. A claim was bound to a
# *session*, so one left unspent by a slow agent build could still be sitting in
# the store, unrelated to the submit that actually triggered a render; and
# `BY_PLATFORM` rested on the registry-name convention (`Session.provider ==
# registry.name`) that nothing in Hermes actually pins to the login on the
# ticket. Neither proved what it was asked to prove. See DESIGN.md, "Decision
# (2026-09-22): a claim is bound to the submit it is for", for the replacement
# (a claim bound to `sha256` of the exact prompt text), which has not landed
# yet — `VERIFIED_RUNGS` is empty until it does, and `asserted_sender` in
# `__init__.py` answers `""` for every rung there is.
#
# `BY_CLAIM` sits in `UNCONFIRMED_RUNGS`, alongside every rung that names the
# opener, rather than in neither list: a claim is bound to a *session* and not
# to the submit it was made for, so the gateway has not confirmed who is
# sending, exactly as for any other unconfirmed rung. `BY_PLATFORM` is the one
# rung that sits in neither: a messaging platform names its own sender per
# message, which is Hermes' business and something this plugin's claim
# mechanism neither confirms nor doubts, so it produces no assertion regardless
# of what lands later.
#
# Every other rung names the person who OPENED the session, on every turn of
# it. That is not a hedge, it is this repo's own finding (DESIGN.md, "A shared
# chat names its opener on every turn"): the hook's `sender_id` is the agent's
# `_user_id`, set once from the record's `auth_user_id` when the agent is built;
# the live record is the record that session was admitted on; the session
# variables are bound at creation and never rebound. A Bot Chat is shared, so on
# any turn after the first, every one of those may be somebody who left hours
# ago. Asserting one of them as the sender of this turn is how one person's
# turn is attributed to another AS FACT, which is worse than staying silent.
#
# The two lists are disjoint, `VERIFIED_RUNGS` is the closed one, and a rung
# this module does not know about — the empty one a caller that has not been
# told passes included, and `BY_PLATFORM` beside it — is in neither. Every
# direction fails towards silence.
VERIFIED_RUNGS = ()
UNCONFIRMED_RUNGS = (
    BY_CLAIM,
    BY_HOOK,
    BY_LIVE_SESSION,
    BY_SESSION_VARS,
    BY_CONFIGURED,
    BY_APP_DEFAULT,
    BY_ONLY_USER,
)

# The one field left that ever reaches a prompt line: a login, cleaned like
# everything else that arrives from outside and ends up in a sentence a model
# is told to rely on.
LIMITS = {"login": 128}


def split_provider(user_id: str) -> Tuple[str, str]:
    """``("oidc", "<sub>")`` for a prefixed id, ``("", the id)`` for a bare one."""
    match = PROVIDER_PREFIX.match(user_id or "")
    return (match.group(1), match.group(2)) if match else ("", user_id or "")


def same_user(one: str, other: str) -> bool:
    """Whether two ids name one person, across the provider prefix.

    Equal ids are one person. Otherwise exactly one of the two may carry a
    prefix and the bare halves must match: `self-hosted:ef11…` is `ef11…`, and
    `max` is `basic:max`.

    Two *different* prefixes are two different logins and never match, however
    alike the bare halves look. `oidc:max` and `basic:max` are as likely to be
    two people as one, and this module would rather name nobody than the wrong
    person.
    """
    if not one or not other:
        return False
    if one == other:
        return True
    one_prefix, one_bare = split_provider(one)
    other_prefix, other_bare = split_provider(other)
    if bool(one_prefix) == bool(other_prefix):
        return False
    return one_bare == other_bare


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit] if isinstance(value, (str, int, float)) else ""


# What is left of a string after `_text` has flattened its whitespace, and has
# no business in a name: control and format characters, the zero-width run, the
# bidi overrides that can make text render in an order it is not written in, the
# soft hyphen, the Arabic letter mark, the tag characters and the variation
# selectors. Decided by Unicode category rather than by a list of ranges, so a
# character added to the standard later is covered without an edit, and written
# without a single literal invisible character, because one in this file is both
# invisible to a reader and a finding for the Hermes plugin scanner.
_STRIPPED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
_VARIATION_SELECTORS = ((0x180B, 0x180D), (0xFE00, 0xFE0F), (0xE0100, 0xE01EF))


def _is_invisible(char: str) -> bool:
    if unicodedata.category(char) in _STRIPPED_CATEGORIES:
        return True
    code = ord(char)
    return any(low <= code <= high for low, high in _VARIATION_SELECTORS)


def strip_invisible(text: str) -> str:
    """`text` without any character `_is_invisible` names."""
    return "".join(char for char in text if not _is_invisible(char))


# And the punctuation that turns a line of a prompt into structure: a heading, a
# rule, a fence, emphasis, a quote, a link, a tag. A name that is only these is
# left empty, which is the safe ending.
MARKUP = re.compile(r"[\"`#*_~\[\]{}<>|\\=]")


def _safe(value: Any, limit: int) -> str:
    """`_text`, and then everything that must not reach a prompt line.

    **Applied at the rendering boundary, never where a value was parsed.**
    `_text` takes the whole-line breaks out, here included the ones Python
    calls whitespace and a terminal does not (`\\u2028`, `\\u0085`), so a value
    cannot become a second line. This strips what is left that could read as
    structure or reorder what is drawn. And the caller quotes what comes out,
    so a sentence somebody buried in a login reads as part of it rather than as
    a sentence of the section's own.
    """
    text = MARKUP.sub("", strip_invisible(_text(value, limit)))
    return " ".join(text.split())


# The last thing this module still puts in front of a model.
#
# **It names the login and not a profile**, because there is no profile any
# more, and because a login is minted by the gateway rather than typed by
# anyone — which is what makes the claim checkable against `/me` or the
# gateway's log. `VERIFIED_RUNGS` is empty today, so `asserted_sender` in
# `__init__.py` never actually calls this — it is here for the rung that will
# eventually earn it.
SENDER_VERIFIED = "The gateway verified that this turn was sent by the person signed in as {login}."


def sender_sentence(login: str) -> str:
    """`SENDER_VERIFIED` for a login, or `""` when there is none to name.

    The login is cleaned like any other string that arrives from outside and
    ends up in a sentence a model is told to rely on.
    """
    cleaned = _safe(login, LIMITS["login"])
    return SENDER_VERIFIED.format(login=cleaned) if cleaned else ""
