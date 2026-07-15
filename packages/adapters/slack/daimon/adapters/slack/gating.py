"""Slack-specific gating pure decision functions -- pure pre-DB checks."""

from __future__ import annotations

from typing import Any, cast


def is_app_mention(event: dict[str, Any]) -> bool:
    """Return True when the Slack event type is ``app_mention``."""
    return event.get("type") == "app_mention"


def is_slack_connect_external(event: dict[str, Any], *, team_id: str) -> bool:
    """Return True if the event originates from a different Slack workspace.

    Slack Connect allows channels shared between workspaces.  The field that
    carries the sender's home workspace ID is ambiguous across SDK versions and
    documentation (RESEARCH A2/Pitfall 3: ``user_team`` vs ``user_team_id`` vs
    ``source_team``), so we defensively check all three.  Any present value
    that differs from ``team_id`` indicates an external sender.

    The listener, not this function, performs the ``chat_postEphemeral``
    rejection.  This function is pure: no I/O, no error handling.
    """
    for key in ("user_team", "user_team_id", "source_team"):
        value = event.get(key)
        if value is not None and value != team_id:
            return True
    return False


def is_external_interactive(payload: dict[str, Any]) -> bool:
    """Return True if an interactive (block_actions) payload comes from a
    different workspace than the one hosting the app (Slack Connect).

    STURN-04 gates app_mention via ``is_slack_connect_external``, but slash
    commands and block actions bypassed it — an external member of a shared
    channel could click a panel button and drive reads (roster, routine
    output) resolved against the HOST tenant. block_actions carry the actor's
    home workspace at ``user.team_id`` and the host at ``team.id``; when they
    differ the actor is external.

    Slash commands are not covered here: their payload carries only the host
    ``team_id`` (no sender-team field), and an external member generally cannot
    invoke the host app's slash commands at all. Pure: no I/O.
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
