"""Tests for help.py — build_help_blocks (pure) + handle_help_command.

TDD test file (written before implementation, per Task 1 plan).

Behavior asserted:
- build_help_blocks() output text contains the five slash commands and the @bot entrypoint.
- handle_help_command posts chat.postEphemeral with the help blocks, never views.open.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from daimon.adapters.slack.runtime import SlackRuntime
from yarl import URL


def test_build_help_blocks_contains_all_commands_and_at_bot_entrypoint() -> None:
    """build_help_blocks() must include all five slash commands and the @bot entrypoint.

    Acceptance: output text contains /routines, /billing, /privacy, /help, /agent-setup,
    and the @bot conversational entrypoint (at-sign present per _CONVERSATIONAL constant).
    """
    from daimon.adapters.slack.help import build_help_blocks

    blocks = build_help_blocks()
    # Flatten to a single string to allow text to span across dicts / nested keys.
    all_text = str(blocks)

    for cmd in ("/routines", "/billing", "/privacy", "/help", "/agent-setup"):
        assert cmd in all_text, (
            f"build_help_blocks() output must contain {cmd!r} "
            f"(found keys: {[b.get('text', {}).get('text', '') if isinstance(b.get('text'), dict) else '' for b in blocks]!r})"
        )

    assert "@" in all_text, (
        "build_help_blocks() must include the @bot configure-by-chat entrypoint "
        "(_CONVERSATIONAL constant from Discord help.py)"
    )


async def test_handle_help_command_posts_ephemeral_and_no_views_open(
    fake_slack_web_client: Any,
) -> None:
    """handle_help_command must post chat.postEphemeral, never views.open.

    /help is ephemeral (no modal). The handler must:
    - call chat.postEphemeral with the blocks from build_help_blocks()
    - NOT call views.open (no trigger_id / no modal)
    """
    from daimon.adapters.slack.help import handle_help_command

    payload: dict[str, Any] = {
        "command": "/help",
        "team_id": "T_HELP_TEST",
        "user_id": "U_HELP_TEST",
        "channel_id": "C_HELP_TEST",
        "trigger_id": "trigger_help_001",
    }

    runtime = MagicMock(spec=SlackRuntime)

    with patch(
        "daimon.adapters.slack.help.resolve_web_client",
        new_callable=AsyncMock,
    ) as mock_resolve:
        mock_resolve.return_value = fake_slack_web_client.client
        await handle_help_command(runtime, payload)

    mock = fake_slack_web_client.mock

    ephemeral_calls = [
        req
        for (_, url), reqs in mock.requests.items()
        if url == URL("https://slack.com/api/chat.postEphemeral")
        for req in reqs
    ]
    assert len(ephemeral_calls) >= 1, (
        "handle_help_command must call chat.postEphemeral (ephemeral, no modal)"
    )

    views_open_calls = [
        req
        for (_, url), reqs in mock.requests.items()
        if url == URL("https://slack.com/api/views.open")
        for req in reqs
    ]
    assert len(views_open_calls) == 0, (
        "handle_help_command must NOT call views.open (/help is ephemeral, not a modal)"
    )


async def test_handle_help_command_drops_silently_when_no_token(
    fake_slack_web_client: Any,
) -> None:
    """handle_help_command must drop silently when resolve_web_client returns None.

    No chat.postEphemeral or views.open must be called for unknown team_id.
    """
    from daimon.adapters.slack.help import handle_help_command

    payload: dict[str, Any] = {
        "command": "/help",
        "team_id": "T_UNKNOWN_TEAM",
        "user_id": "U_HELP_TEST",
        "channel_id": "C_HELP_TEST",
    }

    runtime = MagicMock(spec=SlackRuntime)

    with patch(
        "daimon.adapters.slack.help.resolve_web_client",
        new_callable=AsyncMock,
    ) as mock_resolve:
        mock_resolve.return_value = None  # unknown team — no token
        await handle_help_command(runtime, payload)

    mock = fake_slack_web_client.mock
    all_calls = list(mock.requests.items())
    # No Slack API calls should be made when there is no token.
    assert not any(
        url == URL("https://slack.com/api/chat.postEphemeral") for (_, url), _ in all_calls
    ), "handle_help_command must not call chat.postEphemeral when no token (silent drop)"
