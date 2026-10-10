"""/dm: a sealed source is refused before any history is read; the notices read plainly."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.slack import direct_messages
from daimon.core.stores.domain import Role
from slack_sdk.errors import SlackApiError
from structlog.testing import capture_logs


async def test_dm_move_from_a_sealed_channel_refuses_before_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    client.conversations_history = AsyncMock(return_value={"messages": [{"text": "sealed"}]})
    client.conversations_open = AsyncMock()
    client.chat_postEphemeral = AsyncMock()
    monkeypatch.setattr(direct_messages, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(direct_messages, "_live_role", AsyncMock(return_value=Role.USER))
    monkeypatch.setattr(direct_messages, "require_dm_enabled", AsyncMock())
    admission = MagicMock(source_sealed=True, memory_read_only=True)
    monkeypatch.setattr(direct_messages, "admit", AsyncMock(return_value=admission))
    monkeypatch.setattr(direct_messages, "user_group_ids", AsyncMock(return_value=frozenset()))
    start_dm = AsyncMock()
    monkeypatch.setattr(direct_messages, "start_dm", start_dm)

    await direct_messages.handle_dm_command(
        MagicMock(), {"team_id": "T1", "user_id": "U1", "channel_id": "C_SEALED", "text": ""}
    )

    client.conversations_history.assert_not_called()
    client.conversations_open.assert_not_called()
    start_dm.assert_not_called()
    client.chat_postEphemeral.assert_awaited_once()
    assert "Only turns inside" in client.chat_postEphemeral.await_args.kwargs["text"]


def _client() -> MagicMock:
    client = MagicMock()
    client.conversations_history = AsyncMock(return_value={"messages": []})
    client.conversations_open = AsyncMock(return_value={"channel": {"id": "D_NEW"}})
    client.chat_postMessage = AsyncMock()
    client.chat_postEphemeral = AsyncMock()
    return client


async def test_the_dm_opens_with_the_two_line_continue_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    monkeypatch.setattr(direct_messages, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(direct_messages, "_live_role", AsyncMock(return_value=Role.USER))
    monkeypatch.setattr(direct_messages, "require_dm_enabled", AsyncMock())
    monkeypatch.setattr(direct_messages, "sealed_channel_ids", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(
        direct_messages, "admit", AsyncMock(return_value=MagicMock(source_sealed=False))
    )
    monkeypatch.setattr(direct_messages, "user_group_ids", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(direct_messages, "start_dm", AsyncMock())

    await direct_messages.handle_dm_command(
        MagicMock(), {"team_id": "T1", "user_id": "U1", "channel_id": "C020", "text": ""}
    )

    assert client.chat_postMessage.await_args.kwargs["text"] == (
        "Continuing from <#C020>.\n\nSend your next message here."
    )


async def test_an_unknown_action_shows_the_usage_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    monkeypatch.setattr(direct_messages, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(direct_messages, "_live_role", AsyncMock(return_value=Role.USER))

    await direct_messages.handle_dm_command(
        MagicMock(), {"team_id": "T1", "user_id": "U1", "channel_id": "C020", "text": "help"}
    )

    assert client.chat_postEphemeral.await_args.kwargs["text"] == (
        "Run `/dm` in a channel to carry on in private.\n\nAdmins: `/dm enable` or `/dm disable`."
    )


async def test_missing_im_scopes_say_so_plainly_and_log_the_setup_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    response = MagicMock()
    response.headers = {"X-OAuth-Scopes": "chat:write,channels:history"}
    client.users_info = AsyncMock(return_value=response)
    monkeypatch.setattr(direct_messages, "resolve_web_client", AsyncMock(return_value=client))

    with capture_logs() as logs:
        await direct_messages.handle_dm_command(
            MagicMock(), {"team_id": "T1", "user_id": "U1", "channel_id": "C020", "text": ""}
        )

    text = client.chat_postEphemeral.await_args.kwargs["text"]
    assert text.startswith(
        "Daimon can't use DMs in this workspace yet.\n\n"
        "Ask a workspace admin to finish Daimon's DM setup.\n\n_Ref "
    )
    assert "im:history" not in text, "the setup steps go to the log, not the chat"
    [entry] = [e for e in logs if e["event"] == "slack.dm.setup_incomplete"]
    assert entry["log_level"] == "warning"
    assert entry["team_id"] == "T1"
    for step in ("im:history", "im:write", "message.im", "Messages tab"):
        assert step in entry["steps"], step
    assert entry["docs"].endswith("/slack/#dm-setup")


async def test_a_scope_error_from_slack_logs_the_setup_steps() -> None:
    response = MagicMock()
    response.get = MagicMock(return_value="missing_scope")
    error = SlackApiError("missing_scope", response)
    with capture_logs() as logs:
        message = direct_messages._error_message(  # pyright: ignore[reportPrivateUsage]
            error, settings=MagicMock(), team_id="T1"
        )
    assert message.startswith("Daimon can't use DMs in this workspace yet.\n\n")
    [entry] = [e for e in logs if e["event"] == "slack.dm.setup_incomplete"]
    assert entry["cause"] == "missing_scope"
