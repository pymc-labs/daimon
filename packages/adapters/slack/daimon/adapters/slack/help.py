"""Slack /help handler.

Pure `build_help_blocks()` returns raw Block Kit section dicts listing the
four admin commands and the @bot configure-by-chat entrypoint.

`handle_help_command` posts an ephemeral message (no modal, no trigger_id).
Text constants ported verbatim from the Discord `commands/help.py` and adapted
to Slack mrkdwn syntax.

No slack_sdk.models.blocks types — raw dicts only (S4 project convention).
"""

from __future__ import annotations

from typing import Any

import structlog
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.errors import DaimonError
from slack_sdk.errors import SlackApiError

log = structlog.get_logger()

# Static text constants ported from discord/commands/help.py (_BODY / _CONVERSATIONAL).
# Slack mrkdwn: *bold* instead of **bold**; -# (Discord small text) replaced by plain text.
_BODY = """\
*Agent management*
/agent-setup — Manage this workspace's agents

*Routines*
/routines — Show scheduled routines for this workspace

*Billing*
/billing — Show your billing usage (admins see per-member breakdown)

*Privacy*
/privacy — See, export, or delete what daimon stores about you

*Meta*
/help — List commands and the @bot conversational entrypoint\
"""

_CONVERSATIONAL = """\
💬 *Or just talk to your agent*
@daimon help me set up
@daimon make a routine that runs daily\
"""


def build_help_blocks() -> list[dict[str, Any]]:
    """Build the static /help Block Kit blocks.

    Pure — no I/O, zero args. Returns raw dicts (S4 convention; no
    slack_sdk.models.blocks types). Mirrors Discord's build_help_view().
    """
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": _BODY}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": _CONVERSATIONAL}},
    ]


async def handle_help_command(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Ephemeral /help handler.

    Ack-first contract: called from app.on_request AFTER the Socket Mode ack.
    Resolves the per-event web client, then posts a chat.postEphemeral containing
    build_help_blocks(). The ephemeral is visible only to the invoker and does not
    steal focus (no views.open).

    Catches DaimonError | SlackApiError at the listener boundary (S3).

    Args:
        runtime: Injected SlackRuntime (sessionmaker, settings).
        payload: Slash-command payload from the Socket Mode envelope.
    """
    team_id: str = str(payload.get("team_id") or "")
    user_id: str = str(payload.get("user_id") or "")
    channel_id: str = str(payload.get("channel_id") or "")

    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        log.warning("slack.help_command.no_token", team_id=team_id)
        return

    try:
        await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            channel=channel_id,
            user=user_id,
            text="daimon command reference",  # fallback for accessibility / notifications
            blocks=build_help_blocks(),
        )
    except (DaimonError, SlackApiError) as exc:
        log.error("slack.help_command.failed", team_id=team_id, exc_info=exc)
