"""Who a Connect GitHub card shows the requester's link to, read from the owner tables."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord import github_connect_button as button_module
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.native_admins import AdminCaller
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import agent_owners
from daimon.core.stores.github_connect import create_discord_connect_intent
from daimon.testing.factories import (
    make_account,
    make_platform_principal,
    make_tenant,
    mark_principal_foreign,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_MA_AGENT = "ma-agent"


@pytest.fixture
async def world(db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]):
    tenant = await make_tenant(db_session, platform="discord", workspace_id="9200")
    accounts: dict[int, uuid.UUID] = {}
    for user in (1, 2, 3, 4, 5):  # requester, owner, co-owner, member, foreign co-owner
        account = await make_account(db_session, tenant=tenant)
        await make_platform_principal(
            db_session, tenant=tenant, account=account, platform="discord", external_id=str(user)
        )
        accounts[user] = account.id
    agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_MA_AGENT)
    await agent_owners.set_owner(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        owner_account_id=accounts[2],
        source="admin_assigned",
    )
    for user in (3, 5):
        await agent_owners.add_co_owner(
            db_session,
            tenant_id=tenant.id,
            agent_id=agent_id,
            account_id=accounts[user],
            added_by_account_id=None,
        )
    await mark_principal_foreign(db_session, account_id=accounts[5])
    intent_id = await create_discord_connect_intent(
        db_session,
        tenant_id=tenant.id,
        requester_account_id=accounts[1],
        requester_platform_user_id="1",
        agent_id=agent_id,
        agent_name="ResearchBot",
        parent_channel_id="55",
        thread_id="56",
        origin_ma_agent_id="ma-origin",
        origin_responder_name="Daimon",
        requested_work=None,
        agent_ma_id=_MA_AGENT,
    )
    await db_session.commit()
    bot = SimpleNamespace(
        runtime=SimpleNamespace(
            sessionmaker=db_session_factory,
            anthropic=object(),
            deployment_default=DeploymentDefault(),
        )
    )
    return tenant.id, bot, intent_id


async def _check(monkeypatch: pytest.MonkeyPatch, world, user: int, *, admin: bool = False):
    tenant_id, bot, intent_id = world
    monkeypatch.setattr(
        button_module,
        "find_agent_by_derived_uuid",
        AsyncMock(return_value=SimpleNamespace(name="ResearchBot", metadata={})),
    )
    monkeypatch.setattr(button_module, "is_member_guild_admin", lambda *_a, **_k: admin)
    monkeypatch.setattr(
        button_module,
        "admin_caller",
        lambda member: AdminCaller(platform_user_id=str(member.id)),
    )
    clicker = MagicMock(spec=discord.Member)
    clicker.id = user
    return await button_module._clicker_check(  # pyright: ignore[reportPrivateUsage]
        bot,  # type: ignore[arg-type]
        clicker,
        guild_owner_id=1,
        requester_id="1",
        tenant_id=tenant_id,
        intent_id=intent_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user", "shown"),
    [(1, True), (2, True), (3, True), (4, False), (5, False)],
    ids=["requester", "owner", "co-owner", "other member", "foreign co-owner"],
)
async def test_only_the_requester_and_the_agents_owners_are_shown_the_link(
    monkeypatch: pytest.MonkeyPatch, world, user: int, shown: bool
) -> None:
    check = await _check(monkeypatch, world, user)
    if shown:
        assert check.refusal is None and check.agent_id is not None
    else:
        assert check.refusal == "Ask <@1> or an owner of ResearchBot."


@pytest.mark.asyncio
async def test_a_live_server_admin_is_shown_the_link(
    monkeypatch: pytest.MonkeyPatch, world
) -> None:
    assert (await _check(monkeypatch, world, 4, admin=True)).refusal is None


@pytest.mark.asyncio
async def test_a_card_that_expired_is_no_longer_available(
    monkeypatch: pytest.MonkeyPatch, world
) -> None:
    tenant_id, bot, _ = world
    monkeypatch.setattr(button_module, "is_member_guild_admin", lambda *_a, **_k: False)
    clicker = MagicMock(spec=discord.Member)
    clicker.id = 2
    check = await button_module._clicker_check(  # pyright: ignore[reportPrivateUsage]
        bot,  # type: ignore[arg-type]
        clicker,
        guild_owner_id=1,
        requester_id="1",
        tenant_id=tenant_id,
        intent_id=uuid.uuid4(),
    )
    assert check.refusal == "This connection is no longer available."
