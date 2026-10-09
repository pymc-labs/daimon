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
        repo_names=["private/connected", "private/unconnected"],
        required_ability="write",
    )
    monkeypatch.setattr(module, "lookup_request", AsyncMock(return_value=request))
    monkeypatch.setattr(
        module, "get_tenant", AsyncMock(return_value=SimpleNamespace(external_id="123"))
    )
    principal = SimpleNamespace(account_id=account_id)
    monkeypatch.setattr(module, "find_platform_principal", AsyncMock(return_value=principal))
    monkeypatch.setattr(module, "get_delivery", AsyncMock(return_value=None))
    monkeypatch.setattr(
        module,
        "list_authorized_repos",
        AsyncMock(
            return_value=[
                SimpleNamespace(
                    repo_full_name="private/connected", status="active", installation_id=7
                ),
            ]
        ),
    )
    monkeypatch.setattr(
        module,
        "get_app_installation",
        AsyncMock(return_value=SimpleNamespace(repo_full_names=["private/connected"])),
    )
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
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    runtime = SimpleNamespace(sessionmaker=_Sessions())
    assert await module.handle_request_card(interaction, runtime)  # type: ignore[arg-type]
    assert "Only a server admin" in interaction.edit_original_response.await_args.kwargs["content"]
    sync.assert_not_awaited()
    admin.return_value = True
    interaction.edit_original_response.reset_mock()
    assert await module.handle_request_card(interaction, runtime)  # type: ignore[arg-type]
    interaction.response.defer.assert_awaited()
    sent = interaction.edit_original_response.await_args.kwargs
    assert "private/connected" in sent["embed"].description
    assert "private/unconnected" not in sent["embed"].description
    assert "1 other repo(s) not connected yet" in sent["embed"].description
    assert {button.label for button in sent["view"].children} >= {"Approve", "Decline"}
    sync.assert_awaited_once()


@pytest.mark.asyncio
async def test_review_defers_before_fetch_member(monkeypatch: pytest.MonkeyPatch) -> None:
    request_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    request = SimpleNamespace(
        id=request_id,
        tenant_id=tenant_id,
        thread_id="200",
        admin_card_message_id="42",
        status="open",
        agent_name="Helper",
        repo_names=[],
        required_ability="read",
    )
    monkeypatch.setattr(module, "lookup_request", AsyncMock(return_value=request))
    monkeypatch.setattr(
        module, "get_tenant", AsyncMock(return_value=SimpleNamespace(external_id="123"))
    )
    monkeypatch.setattr(module, "find_platform_principal", AsyncMock(return_value=None))
    monkeypatch.setattr(module, "get_delivery", AsyncMock(return_value=None))
    interaction = MagicMock()
    interaction.data = {"custom_id": f"github_request:{request_id}:review"}
    interaction.message.id = 42
    interaction.channel_id = 200
    interaction.user.id = 99
    deferred = False

    async def defer(**_kw: object) -> None:
        nonlocal deferred
        deferred = True

    async def fetch_member(_id: int) -> object:
        assert deferred
        raise module.discord.NotFound(MagicMock(), "missing")

    interaction.response.defer = AsyncMock(side_effect=defer)
    interaction.edit_original_response = AsyncMock()
    interaction.client.get_guild.return_value = SimpleNamespace(
        owner_id=1,
        get_member=lambda _id: None,
        fetch_member=fetch_member,
    )
    assert await module.handle_request_card(interaction, SimpleNamespace(sessionmaker=_Sessions()))  # type: ignore[arg-type]
    interaction.edit_original_response.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["hide", "approve"])
async def test_uncached_member_decision_edits_requester_thread_card(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    request_id, tenant_id, account_id, agent_id = (uuid.uuid4() for _ in range(4))
    request = SimpleNamespace(
        id=request_id,
        tenant_id=tenant_id,
        thread_id="200",
        status="open",
        requester_account_id=account_id,
        agent_id=agent_id,
        ma_agent_id="ma_agent",
        agent_name="Helper",
    )
    monkeypatch.setattr(module, "lookup_request", AsyncMock(return_value=request))
    monkeypatch.setattr(
        module, "get_tenant", AsyncMock(return_value=SimpleNamespace(external_id="123"))
    )
    monkeypatch.setattr(
        module,
        "find_platform_principal",
        AsyncMock(return_value=SimpleNamespace(account_id=account_id)),
    )
    monkeypatch.setattr(
        module,
        "get_delivery",
        AsyncMock(
            return_value=SimpleNamespace(
                message_id="42",
                dismissed_at=None,
            )
        ),
    )
    monkeypatch.setattr(module, "is_member_guild_admin", MagicMock(return_value=True))
    monkeypatch.setattr(module, "dismiss_delivery", AsyncMock(return_value=True))
    monkeypatch.setattr(module, "approve_connected_request", AsyncMock(return_value=True))
    monkeypatch.setattr(module, "update_requester_card", AsyncMock())
    monkeypatch.setattr(
        module,
        "find_agent_by_derived_uuid",
        AsyncMock(
            return_value=SimpleNamespace(
                id="ma_agent",
                metadata={},
                name="Helper",
            )
        ),
    )
    monkeypatch.setattr(module, "decide_operation", MagicMock(return_value="allow"))
    member = SimpleNamespace(id=99)
    fetched = AsyncMock(return_value=member)
    guild = SimpleNamespace(owner_id=1, get_member=lambda _id: None, fetch_member=fetched)
    interaction = MagicMock()
    interaction.data = {"custom_id": f"github_request:{request_id}:{action}"}
    interaction.message.id = 42
    interaction.channel_id = 200
    interaction.user.id = 99
    interaction.client.get_guild.return_value = guild
    interaction.response.defer = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    runtime = SimpleNamespace(sessionmaker=_Sessions(), anthropic=object())
    assert await module.handle_request_card(interaction, runtime)  # type: ignore[arg-type]
    interaction.response.defer.assert_awaited_once_with(thinking=False)
    interaction.response.edit_message.assert_not_awaited()
    assert interaction.edit_original_response.await_args.kwargs["embed"] is not None
