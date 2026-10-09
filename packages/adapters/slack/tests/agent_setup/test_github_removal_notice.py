"""GitHub removal notices stay in the admin's setup channel."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.slack.agent_setup import github_removal


class _Sessions:
    @asynccontextmanager
    async def begin(self):  # type: ignore[no-untyped-def]
        yield object()


@pytest.mark.asyncio
async def test_removal_notice_uses_ephemeral_button_without_dm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        github_removal,
        "claim_notice",
        AsyncMock(return_value=SimpleNamespace(account_login="example")),
    )
    finish = AsyncMock()
    monkeypatch.setattr(github_removal, "finish_notice", finish)
    monkeypatch.setattr(github_removal, "sync_connect_admin", AsyncMock())
    connect_link = AsyncMock(return_value="https://mcp.test/connect")
    monkeypatch.setattr(github_removal, "connect_link", connect_link)
    send_link = AsyncMock()
    monkeypatch.setattr(github_removal, "send_link", send_link)
    client = SimpleNamespace(conversations_open=AsyncMock(), chat_postMessage=AsyncMock())
    runtime = SimpleNamespace(sessionmaker=_Sessions(), settings=object())
    await github_removal.send_pending_notice(
        runtime,  # pyright: ignore[reportArgumentType]
        client,  # pyright: ignore[reportArgumentType]
        team_id="T1",
        channel_id="C1",
        user_id="U1",
        response_url="https://hooks.slack.test/response",
    )
    assert connect_link.await_args is not None
    assert connect_link.await_args.kwargs["origin_followup_token"] == (
        "https://hooks.slack.test/response"
    )
    assert send_link.await_args is not None
    assert send_link.await_args.kwargs["channel_id"] == "C1"
    assert send_link.await_args.kwargs["user_id"] == "U1"
    assert send_link.await_args.kwargs["url"] == "https://mcp.test/connect"
    client.conversations_open.assert_not_awaited()
    client.chat_postMessage.assert_not_awaited()
    assert finish.await_args is not None
    assert finish.await_args.kwargs["delivered"] is True
