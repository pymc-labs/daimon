"""Managed agent roles follow the roster and remain identifiable by role ID."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

from daimon.adapters.discord.agent_roles import sync_agent_roles
from daimon.adapters.discord.bot import DaimonBot
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.stores.discord_agent_roles import list_roles, roles_mentioned
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_sync_creates_renames_and_deletes_only_managed_roles(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    guild = MagicMock()
    guild.id = 123
    guild.me.guild_permissions.manage_roles = True
    role = MagicMock()
    role.id = 456
    role.name = "Planner"
    role.mentionable = True
    role.edit = AsyncMock()
    role.delete = AsyncMock()
    guild.create_role = AsyncMock(return_value=role)
    guild.get_role = MagicMock(return_value=role)
    first = ma_agent(id="agent_1", name="Planner", tenant_id=tenant.id)
    renamed = ma_agent(id="agent_1", name="Researcher", tenant_id=tenant.id)
    roster = [first]
    with patch(
        "daimon.adapters.discord.agent_roles.list_agents_by_tenant",
        new_callable=AsyncMock,
        side_effect=lambda *_args, **_kwargs: roster,
    ):
        await sync_agent_roles(
            guild=guild,
            tenant_id=tenant.id,
            anthropic=MagicMock(),
            sessionmaker=db_session_factory,
        )
        async with db_session_factory() as session:
            stored = await roles_mentioned(session, tenant_id=tenant.id, role_ids=["456"])
        assert [(row.ma_agent_id, row.agent_name) for row in stored] == [("agent_1", "Planner")]
        roster[:] = [renamed]
        await sync_agent_roles(
            guild=guild,
            tenant_id=tenant.id,
            anthropic=MagicMock(),
            sessionmaker=db_session_factory,
        )
        role.edit.assert_awaited_once()
        roster.clear()
        await sync_agent_roles(
            guild=guild,
            tenant_id=tenant.id,
            anthropic=MagicMock(),
            sessionmaker=db_session_factory,
        )
    role.delete.assert_awaited_once()
    async with db_session_factory() as session:
        assert not await list_roles(session, tenant_id=tenant.id)


async def test_missing_manage_roles_is_a_nonfatal_fallback(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild = MagicMock()
    guild.me.guild_permissions.manage_roles = False
    guild.create_role = AsyncMock()
    await sync_agent_roles(
        guild=guild,
        tenant_id=uuid.uuid4(),
        anthropic=MagicMock(),
        sessionmaker=db_session_factory,
    )
    guild.create_role.assert_not_awaited()


async def test_missing_manage_roles_logs_once_per_guild(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild = MagicMock()
    guild.id = 991
    guild.me.guild_permissions.manage_roles = False
    with patch("daimon.adapters.discord.agent_roles.log.warning") as warning:
        for _ in range(2):
            await sync_agent_roles(
                guild=guild,
                tenant_id=uuid.uuid4(),
                anthropic=MagicMock(),
                sessionmaker=db_session_factory,
            )
    warning.assert_called_once()


async def test_role_sweep_uses_one_roster_listing_for_all_guilds() -> None:
    guilds = [SimpleNamespace(id=91), SimpleNamespace(id=92)]
    fake = SimpleNamespace(
        guilds=guilds,
        runtime=SimpleNamespace(anthropic=MagicMock()),
        draining=False,
        is_closed=lambda: False,
        wait_until_ready=AsyncMock(),
    )
    synced: list[int] = []

    async def sync(guild: SimpleNamespace, _tenant_id: uuid.UUID, *, agents: object) -> None:
        assert agents == []
        synced.append(guild.id)
        if len(synced) == 2:
            fake.draining = True

    fake._sync_agent_roles = sync
    with patch(
        "daimon.adapters.discord.bot.list_agents_by_tenants", new_callable=AsyncMock
    ) as read:
        read.return_value = {}
        await DaimonBot._agent_role_sync_loop(cast(DaimonBot, fake))
    assert read.await_count == 1
    assert synced == [91, 92]


async def test_agents_with_a_home_or_no_run_channel_have_no_mentionable_role(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    guild = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    guild.create_role = AsyncMock()
    with (
        patch(
            "daimon.adapters.discord.agent_roles.list_agents_by_tenant",
            new_callable=AsyncMock,
            return_value=[
                ma_agent(id="local", name="local", tenant_id=tenant.id),
                ma_agent(id="stopped", name="stopped", tenant_id=tenant.id),
            ],
        ),
        patch(
            "daimon.adapters.discord.agent_roles.load_access_policy",
            new_callable=AsyncMock,
            return_value=TenantAccessPolicy(
                channel_rules={"private": ChannelRule(readers="own", writers="own")},
                agent_rules={
                    "local": AgentRule(runs_in=("private",)),
                    "stopped": AgentRule(runs_in=()),
                },
            ),
        ),
    ):
        await sync_agent_roles(
            guild=guild,
            tenant_id=tenant.id,
            anthropic=MagicMock(),
            sessionmaker=db_session_factory,
        )
    guild.create_role.assert_not_awaited()


async def test_sweep_adopts_an_unmapped_role_after_a_failed_save(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    guild = MagicMock()
    guild.id = 919
    guild.me.guild_permissions.manage_roles = True
    role = MagicMock()
    role.id = 818
    role.name = "Planner"
    role.mentionable = True
    guild.roles = []
    guild.get_role.return_value = None
    guild.create_role = AsyncMock(return_value=role)
    agent = ma_agent(id="planner", name="Planner", tenant_id=tenant.id)
    with patch(
        "daimon.adapters.discord.agent_roles.save_role",
        new_callable=AsyncMock,
        side_effect=RuntimeError("save failed"),
    ):
        await sync_agent_roles(
            guild=guild,
            tenant_id=tenant.id,
            anthropic=MagicMock(),
            sessionmaker=db_session_factory,
            agents=[agent],
        )
    guild.roles = [role]
    await sync_agent_roles(
        guild=guild,
        tenant_id=tenant.id,
        anthropic=MagicMock(),
        sessionmaker=db_session_factory,
        agents=[agent],
    )
    guild.create_role.assert_awaited_once()
    async with db_session_factory() as session:
        assert (await list_roles(session, tenant_id=tenant.id))[0].role_id == "818"


async def test_one_agent_role_failure_does_not_stop_the_next_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    guild = MagicMock()
    guild.id = 920
    guild.me.guild_permissions.manage_roles = True
    guild.roles = []
    guild.get_role.return_value = None
    role = MagicMock()
    role.id = 819
    guild.create_role = AsyncMock(side_effect=[RuntimeError("first failed"), role])
    await sync_agent_roles(
        guild=guild,
        tenant_id=tenant.id,
        anthropic=MagicMock(),
        sessionmaker=db_session_factory,
        agents=[
            ma_agent(id="first", name="First", tenant_id=tenant.id),
            ma_agent(id="second", name="Second", tenant_id=tenant.id),
        ],
    )
    assert guild.create_role.await_count == 2
    async with db_session_factory() as session:
        assert [row.ma_agent_id for row in await list_roles(session, tenant_id=tenant.id)] == [
            "second"
        ]
