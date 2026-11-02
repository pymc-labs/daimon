"""Tests for help.py — build_help_blocks (pure) + handle_help_command.

TDD test file (written before implementation, per Task 1 plan).

Behavior asserted:
- build_help_blocks() output text contains every slash command in the app manifest and the
  @bot entrypoint.
- handle_help_command posts chat.postEphemeral with the help blocks, never views.open.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.platform_words import DESCRIPTIONS, WORDS
from yarl import URL

from .conftest import shown_text

_MANIFEST = Path(__file__).resolve().parents[4] / "docs" / "slack-app-manifest.yaml"


def _manifest_commands() -> list[str]:
    manifest = cast(dict[str, Any], yaml.safe_load(_MANIFEST.read_text()))
    return [entry["command"] for entry in manifest["features"]["slash_commands"]]


def test_build_help_blocks_contains_all_commands_and_at_bot_entrypoint() -> None:
    """build_help_blocks() must include every manifest slash command and the @bot entrypoint."""
    from daimon.adapters.slack.help import build_help_blocks

    blocks = build_help_blocks()
    # Flatten to a single string to allow text to span across dicts / nested keys.
    all_text = str(blocks)

    commands = _manifest_commands()
    assert "/memory" in commands and "/dm-me" in commands
    for cmd in commands:
        assert f"{cmd} " in all_text, (
            f"/help must list the manifest command {cmd!r} "
            f"(found keys: {[b.get('text', {}).get('text', '') if isinstance(b.get('text'), dict) else '' for b in blocks]!r})"
        )

    assert "@" in all_text, (
        "build_help_blocks() must include the @bot configure-by-chat entrypoint "
        "(_CONVERSATIONAL constant from Discord help.py)"
    )


def test_manifest_and_help_descriptions_equal_the_shared_table() -> None:
    """Slack registers `/github`; the table's word for it is `/github connect`."""
    manifest = cast(dict[str, Any], yaml.safe_load(_MANIFEST.read_text()))
    registered = {
        entry["command"]: entry["description"] for entry in manifest["features"]["slash_commands"]
    }
    expected = {
        command.word.split()[0]: DESCRIPTIONS[action]
        for action, command in WORDS["slack"].commands.items()
    }
    assert registered == expected, "the manifest says what the table says, on every command"

    from daimon.adapters.slack.help import build_help_blocks

    lines = build_help_blocks()[2]["text"]["text"].splitlines()
    for action, command in WORDS["slack"].commands.items():
        line = next(line for line in lines if line.startswith(command.word + " "))
        assert line.removeprefix(command.word).strip() == DESCRIPTIONS[action]


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
    runtime.settings = MagicMock()
    runtime.settings.slack = None

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
    runtime.settings = MagicMock()
    runtime.settings.slack = None

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


async def test_handle_help_command_reports_failure_ephemerally() -> None:
    """When the help post fails, the invoker gets an error notice with a short
    ref instead of silence."""
    from daimon.adapters.slack.help import handle_help_command
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.async_client import AsyncWebClient

    client = MagicMock(spec=AsyncWebClient)
    client.chat_postEphemeral = AsyncMock(
        side_effect=[SlackApiError("down", MagicMock()), MagicMock()]
    )
    payload: dict[str, Any] = {
        "command": "/help",
        "team_id": "T_HELP_TEST",
        "user_id": "U_HELP_TEST",
        "channel_id": "C_HELP_TEST",
    }
    runtime = MagicMock(spec=SlackRuntime)
    runtime.settings = MagicMock()
    runtime.settings.slack = None

    with patch(
        "daimon.adapters.slack.help.resolve_web_client",
        new_callable=AsyncMock,
        return_value=client,
    ):
        await handle_help_command(runtime, payload)

    assert client.chat_postEphemeral.await_count == 2, (
        "a failed help post must be followed by an ephemeral error notice"
    )
    notice = client.chat_postEphemeral.await_args_list[1].kwargs
    assert notice["channel"] == "C_HELP_TEST"
    assert notice["user"] == "U_HELP_TEST"
    assert shown_text(notice).startswith("Slack didn't accept that.\n\nTry again.\n\nRef "), (
        "the notice carries the plain words and a short ref for log lookup"
    )
    assert "down" not in shown_text(notice), "exception text stays in the logs"
    card = notice["attachments"][0]["blocks"]
    assert card[-1]["type"] == "context", "the ref is drawn small"
    assert card[-1]["elements"][0]["text"].startswith("Ref ")


def test_help_uses_the_configured_bot_name() -> None:
    from daimon.adapters.slack.help import build_help_blocks

    text = str(build_help_blocks(display_name="research-bot"))
    assert "@research-bot help me set up" in text, "help should mention the configured bot"
    assert "daimon" not in text, "help should contain no hardcoded bot name"
