"""Slack-specific gating pure decision functions -- pure pre-DB checks."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, cast

from daimon.core.name_mentions import mentions_by_name


def mentions_bot(event: dict[str, Any], *, bot_user_id: str) -> bool:
    """Return True when the event text explicitly mentions the bot user.

    Slack has been observed delivering ``app_mention`` events for thread
    replies that never mention the bot (official docs say the event fires only
    on direct mentions). This is the Discord-parity belt-and-braces gate — the
    Discord adapter checks ``message.mentions`` explicitly on every message.

    Accepts both mention encodings: canonical ``<@U123>`` and the legacy
    pipe-labelled ``<@U123|display-name>``. Fails closed on an
    empty/unresolved ``bot_user_id``. Pure: no I/O.
    """
    if not bot_user_id:
        return False
    text = str(event.get("text") or "")
    pattern = rf"<@{re.escape(bot_user_id)}(?:\|[^>]*)?>"
    return re.search(pattern, text) is not None


# A plain post, one with files attached, or a thread reply also sent to the
# channel; edits, joins and every other subtype are not a request.
_PLAIN_TEXT_MENTION_SUBTYPES = frozenset({"", "file_share", "thread_broadcast"})
# Slack wraps links and real mentions in <...>; a name inside one is not typed.
_SLACK_TOKEN = re.compile(r"<[^<>]*>")
# A quoted line ("> ...", sent as "&gt; ..."): someone else's words, as Teams reply quotes.
_QUOTE_LINE = re.compile(r"^(?:&gt;|>).*$", re.MULTILINE)
# A ">>>" block quote runs from its line to the end of the message.
_BLOCK_QUOTE = re.compile(r"^(?:&gt;|>){3}[\s\S]*", re.MULTILINE)


def is_plain_text_mention_event(event: dict[str, Any], *, bot_names: Iterable[str | None]) -> bool:
    """Return True for a person's channel or thread message that types "@<bot name>".

    Only ``message`` events from a user (no ``bot_id``) in a public or private
    channel qualify; DMs already start a turn without a mention. The caller
    still checks the tenant switch, and that the text has no real
    ``<@bot_user_id>`` (that one arrives as ``app_mention`` too). Pure: no I/O.
    """
    if event.get("type") != "message" or event.get("bot_id") or not event.get("user"):
        return False
    if str(event.get("subtype") or "") not in _PLAIN_TEXT_MENTION_SUBTYPES:
        return False
    if event.get("channel_type") not in ("channel", "group"):
        return False
    text = _QUOTE_LINE.sub("", _BLOCK_QUOTE.sub("", str(event.get("text") or "")))
    return mentions_by_name(_SLACK_TOKEN.sub(" ", text), bot_names)


_EVENT_TEAM_KEYS = ("user_team", "user_team_id", "source_team")


def foreign_event_team(event: Mapping[str, Any], *, team_id: str) -> str | None:
    """The other workspace an event says its sender belongs to, or None.

    Slack Connect allows channels shared between workspaces.  The field that
    carries the sender's home workspace ID is ambiguous across SDK versions and
    documentation (``user_team`` vs ``user_team_id`` vs ``source_team``), so we
    check all three.  Only a non-empty team id that differs from ``team_id`` is
    evidence of another workspace; an empty or odd value is not (see
    `ambiguous_event_team`). Pure: no I/O.
    """
    for key in _EVENT_TEAM_KEYS:
        value = event.get(key)
        if isinstance(value, str) and value and value != team_id:
            return value
    return None


def ambiguous_event_team(event: Mapping[str, Any]) -> bool:
    """Whether an event team field is present but not a usable team id (not a string)."""
    return any(
        event.get(key) is not None and not isinstance(event.get(key), str)
        for key in _EVENT_TEAM_KEYS
    )


def is_slack_connect_external(event: dict[str, Any], *, team_id: str) -> bool:
    """Return True if the event originates from a different Slack workspace.

    One input to the requester's organisation (`decide_requester_org`); an
    event that omits the fields is not evidence of a host member.
    """
    return foreign_event_team(event, team_id=team_id) is not None


def is_external_interactive(payload: dict[str, Any]) -> bool:
    """Return True if an interactive (block_actions) payload comes from a
    different workspace than the one hosting the app (Slack Connect).

    An external member of a shared channel reaches only the controls
    `external_access.external_action_allowed` names; block actions carry the
    actor's home workspace at ``user.team_id`` and the host at ``team.id``, and
    when they differ the actor is external. The payload fields are Slack's own
    and optional: a click that omits them reads as the host's, which is why every
    admin re-check also verifies host membership (`resolve_is_admin`).

    Slash commands are not covered here: their payload carries only the host
    ``team_id`` (no sender-team field). Pure: no I/O.
    """
    raw_user = payload.get("user")
    raw_team = payload.get("team")
    if not isinstance(raw_user, dict) or not isinstance(raw_team, dict):
        return False
    user = cast("dict[str, Any]", raw_user)
    team = cast("dict[str, Any]", raw_team)
    sender_team: Any = user.get("team_id")
    host_team: Any = team.get("id")
    return sender_team is not None and host_team is not None and bool(sender_team != host_team)


def payload_proves_host(payload: dict[str, Any]) -> bool:
    """Whether an interactive payload positively says its user is in the host workspace.

    Needs both ``user.team_id`` and ``team.id`` present and equal. A payload that
    omits either proves nothing, so the caller verifies with ``users.info``.
    """
    raw_user = payload.get("user")
    raw_team = payload.get("team")
    if not isinstance(raw_user, dict) or not isinstance(raw_team, dict):
        return False
    sender: Any = cast("dict[str, Any]", raw_user).get("team_id")
    host: Any = cast("dict[str, Any]", raw_team).get("id")
    return isinstance(sender, str) and bool(sender) and sender == host
