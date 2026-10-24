"""Slack /help handler.

Pure `build_help_blocks()` returns raw Block Kit section dicts listing the
app's slash commands and the @bot configure-by-chat entrypoint.

`handle_help_command` posts an ephemeral message (no modal, no trigger_id).
The command list comes from `platform_words`, the same table Discord and Teams read.

No slack_sdk.models.blocks types — raw dicts only (S4 project convention).
"""

from __future__ import annotations

from typing import Any

import structlog
from daimon.adapters.slack.errors import generate_request_id, surface_command_error
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.adapters.slack.runtime import SlackRuntime, resolve_bot_display_name
from daimon.core.errors import DaimonError
from daimon.core.platform_words import DESCRIPTIONS, WORDS
from slack_sdk.errors import SlackApiError

log = structlog.get_logger()


def _body() -> str:
    """One line per Slack command, in the shared table's order."""
    rows = [
        (command.word, DESCRIPTIONS[action]) for action, command in WORDS["slack"].commands.items()
    ]
    width = max(len(word) for word, _ in rows) + 2
    return "\n".join(f"{word.ljust(width)}{description}" for word, description in rows)


_CONVERSATIONAL = """\
💬 Or just ask

@{display_name} help me set up\
"""


def build_help_blocks(*, display_name: str = "daimon") -> list[dict[str, Any]]:
    """Build the static /help Block Kit blocks.

    Pure — no I/O. Returns raw dicts (S4 convention; no
    slack_sdk.models.blocks types). Mirrors Discord's build_help_view().
    """
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "📖 Commands"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "Only you can see this."}},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": _body(),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": _CONVERSATIONAL.format(display_name=escape_mrkdwn(display_name)),
            },
        },
    ]


async def handle_help_command(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Ephemeral /help handler.

    Ack-first contract: called from app.on_request AFTER the Socket Mode ack.
    Resolves the per-event web client, then posts a chat.postEphemeral containing
    build_help_blocks(). The ephemeral is visible only to the invoker and does not
    steal focus (no views.open).

    Catches DaimonError | SlackApiError at the listener boundary.

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
            text=f"{resolve_bot_display_name(runtime.settings)} command reference",
            blocks=build_help_blocks(display_name=resolve_bot_display_name(runtime.settings)),
        )
    except (DaimonError, SlackApiError) as exc:
        request_id = generate_request_id()
        log.error("slack.help_command.failed", team_id=team_id, request_id=request_id, exc_info=exc)
        await surface_command_error(
            client,
            exc,
            request_id=request_id,
            title="Help",
            view_id="",
            channel_id=channel_id,
            user_id=user_id,
        )
