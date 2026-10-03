"""/dm move refuses a sealed source before reading any of its history."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.slack import direct_messages
from daimon.core.stores.domain import Role


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
    assert "sealed" in client.chat_postEphemeral.await_args.kwargs["text"]
