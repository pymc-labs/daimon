"""Shared Discord GitHub requests reveal repo details only to live admins."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.discord.agent_setup import github_requests as module


class _Sessions:
    @asynccontextmanager
    async def __call__(self):  # type: ignore[no-untyped-def]
        yield object()

    @asynccontextmanager
    async def begin(self):  # type: ignore[no-untyped-def]
        yield object()


@pytest.mark.asyncio
async def test_shared_review_requires_live_admin_and_returns_ephemeral_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id, tenant_id, account_id = (uuid.uuid4() for _ in range(3))
    request = SimpleNamespace(
        id=request_id,
        tenant_id=tenant_id,
        thread_id="200",
        admin_card_message_id="42",
        status="open",
        agent_name="Helper",
        repo_names=["private/repo"],
        required_ability="write",
    )
    monkeypatch.setattr(module, "lookup_request", AsyncMock(return_value=request))
    monkeypatch.setattr(
        module, "get_tenant", AsyncMock(return_value=SimpleNamespace(external_id="123"))
    )
    principal = SimpleNamespace(account_id=account_id)
    monkeypatch.setattr(module, "find_platform_principal", AsyncMock(return_value=principal))
    monkeypatch.setattr(module, "get_delivery", AsyncMock(return_value=None))
    admin = MagicMock(return_value=False)
    monkeypatch.setattr(module, "is_member_guild_admin", admin)
    sync = AsyncMock()
    monkeypatch.setattr(module, "sync_connect_admin", sync)
    guild = SimpleNamespace(owner_id=1, get_member=lambda _id: object())
    interaction = MagicMock()
    interaction.data = {"custom_id": f"github_request:{request_id}:review"}
    interaction.message.id = 42
    interaction.channel_id = 200
    interaction.user.id = 99
    interaction.client.get_guild.return_value = guild
    interaction.response.send_message = AsyncMock()
    runtime = SimpleNamespace(sessionmaker=_Sessions())
    assert await module.handle_request_card(interaction, runtime)  # type: ignore[arg-type]
    assert "Only a server admin" in interaction.response.send_message.await_args.args[0]
    sync.assert_not_awaited()
    admin.return_value = True
    interaction.response.send_message.reset_mock()
    assert await module.handle_request_card(interaction, runtime)  # type: ignore[arg-type]
    sent = interaction.response.send_message.await_args.kwargs
    assert sent["ephemeral"] is True
    assert "private/repo" in sent["embed"].description
    assert {button.label for button in sent["view"].children} >= {"Approve", "Decline"}
    sync.assert_awaited_once()
