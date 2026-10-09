"""GitHub removal notices stay in the admin's setup interaction."""

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.discord.agent_setup import github_removal


class _Sessions:
    @asynccontextmanager
    async def begin(self):  # type: ignore[no-untyped-def]
        yield object()


@pytest.mark.asyncio
async def test_removal_notice_uses_ephemeral_reconnect_button_without_dm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid.uuid4()

    def is_admin(_interaction: object) -> bool:
        return True

    monkeypatch.setattr(github_removal, "is_guild_admin", is_admin)
    monkeypatch.setattr(
        github_removal,
        "claim_notice",
        AsyncMock(return_value=SimpleNamespace(account_login="example")),
    )
    finish = AsyncMock()
    monkeypatch.setattr(github_removal, "finish_notice", finish)
    monkeypatch.setattr(github_removal, "sync_connect_admin", AsyncMock())
    monkeypatch.setattr(
        github_removal, "connect_link", AsyncMock(return_value="https://mcp.test/connect")
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=123, display_name="Admin", create_dm=AsyncMock()),
        channel_id=456,
        application_id=789,
        token="interaction",
        followup=SimpleNamespace(send=AsyncMock()),
    )
    runtime = SimpleNamespace(sessionmaker=_Sessions(), settings=object())
    await github_removal.send_pending_notice(
        runtime,  # pyright: ignore[reportArgumentType]
        interaction,  # pyright: ignore[reportArgumentType]
        tenant_id=tenant_id,
    )
    sent = interaction.followup.send.await_args.kwargs
    assert sent["ephemeral"] is True
    assert sent["view"].children[0].url == "https://mcp.test/connect"
    interaction.user.create_dm.assert_not_awaited()
    assert finish.await_args is not None
    assert finish.await_args.kwargs["delivered"] is True
