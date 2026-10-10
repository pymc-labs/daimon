"""Tests for the click-time write-path gates: the spec gate and the attachment gate."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
from daimon.adapters.discord.agent_setup.authz import (
    refuse_if_reachable_and_not_admin,
    refuse_if_shared_and_not_admin,
)
from daimon.adapters.discord.agent_setup.state import RosterEntry
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.specs import AgentSpec
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _entry(name: str = "bot", *, is_system: bool = False) -> RosterEntry:
    return RosterEntry(
        name=name,
        model="claude-sonnet-4-6",
        spec=AgentSpec(name=name, model="claude-sonnet-4-6"),
        is_system=is_system,
    )


def _runtime(
    *,
    sessionmaker: object,
    deployment_default: DeploymentDefault | None = None,
) -> DiscordRuntime:
    return DiscordRuntime(
        settings=MagicMock(),
        anthropic=MagicMock(),
        sessionmaker=sessionmaker,  # type: ignore[arg-type]  # a real async_sessionmaker or a spy MagicMock, never invoked as a client
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=deployment_default or DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # never runs a turn
    )


def _admin_interaction(*, guild_id: int = 111, acked: bool = False) -> MagicMock:
    interaction = MagicMock()
    interaction.guild_id = guild_id
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 1
    interaction.user.guild_permissions.administrator = True
    interaction.user.guild_permissions.manage_guild = False
    interaction.guild.owner_id = 999
    interaction.response.is_done.return_value = acked
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _member_interaction(*, guild_id: int = 111, acked: bool = False) -> MagicMock:
    interaction = MagicMock()
    interaction.guild_id = guild_id
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 2
    interaction.user.guild_permissions.administrator = False
    interaction.user.guild_permissions.manage_guild = False
    interaction.guild.owner_id = 999
    interaction.response.is_done.return_value = acked
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


async def test_no_target_refuses_silently_without_db_read() -> None:
    session_factory = MagicMock()
    runtime = _runtime(sessionmaker=session_factory)
    interaction = _member_interaction()

    refused = await refuse_if_reachable_and_not_admin(interaction, runtime=runtime, entry=None)

    assert refused is True, "no selected agent must refuse"
    session_factory.assert_not_called()
    interaction.response.send_message.assert_not_called()
    interaction.followup.send.assert_not_called()


async def test_system_agent_refuses_even_for_a_live_admin() -> None:
    session_factory = MagicMock()
    runtime = _runtime(sessionmaker=session_factory)
    interaction = _admin_interaction()
    entry = _entry("daimon", is_system=True)

    refused = await refuse_if_reachable_and_not_admin(interaction, runtime=runtime, entry=entry)

    assert refused is True, "a system agent must refuse even a live admin"
    session_factory.assert_not_called()
    interaction.response.send_message.assert_called_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True
    interaction.followup.send.assert_not_called()


async def test_admin_passes_without_touching_the_database() -> None:
    session_factory = MagicMock()
    runtime = _runtime(sessionmaker=session_factory)
    interaction = _admin_interaction()
    entry = _entry("bot")

    refused = await refuse_if_reachable_and_not_admin(interaction, runtime=runtime, entry=entry)

    assert refused is False, "a live admin editing a non-system agent must pass"
    session_factory.assert_not_called()
    interaction.response.send_message.assert_not_called()
    interaction.followup.send.assert_not_called()


async def test_non_admin_unreachable_agent_refuses(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild_id = 222001
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, platform="discord", workspace_id=str(guild_id))
    runtime = _runtime(sessionmaker=db_session_factory)
    interaction = _member_interaction(guild_id=guild_id)
    entry = _entry("bot")

    refused = await refuse_if_reachable_and_not_admin(interaction, runtime=runtime, entry=entry)

    assert refused is True, "unreachable does not establish ownership"
    interaction.response.send_message.assert_called_once()
    interaction.followup.send.assert_not_called()


async def test_non_admin_reachable_agent_refuses(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild_id = 222002
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, platform="discord", workspace_id=str(guild_id))
    entry = _entry("bot")
    runtime = _runtime(
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(agent_name=entry.name),
    )
    interaction = _member_interaction(guild_id=guild_id)

    refused = await refuse_if_reachable_and_not_admin(interaction, runtime=runtime, entry=entry)

    assert refused is True, "the workspace's current default agent must refuse a non-admin"
    interaction.response.send_message.assert_called_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True
    interaction.followup.send.assert_not_called()


async def test_reachable_refusal_uses_followup_when_already_acked(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild_id = 222003
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, platform="discord", workspace_id=str(guild_id))
    entry = _entry("bot")
    runtime = _runtime(
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(agent_name=entry.name),
    )
    interaction = _member_interaction(guild_id=guild_id, acked=True)

    refused = await refuse_if_reachable_and_not_admin(interaction, runtime=runtime, entry=entry)

    assert refused is True
    interaction.response.send_message.assert_not_called()
    interaction.followup.send.assert_called_once()
    assert interaction.followup.send.call_args.kwargs.get("ephemeral") is True


async def test_system_agent_refusal_uses_followup_when_already_acked() -> None:
    session_factory = MagicMock()
    runtime = _runtime(sessionmaker=session_factory)
    interaction = _member_interaction(acked=True)
    entry = _entry("daimon", is_system=True)

    refused = await refuse_if_reachable_and_not_admin(interaction, runtime=runtime, entry=entry)

    assert refused is True
    session_factory.assert_not_called()
    interaction.response.send_message.assert_not_called()
    interaction.followup.send.assert_called_once()
    assert interaction.followup.send.call_args.kwargs.get("ephemeral") is True


# ---------------------------------------------------------------------------
# refuse_if_shared_and_not_admin — the attachment gate (repo binding, env vars)
# ---------------------------------------------------------------------------


async def test_shared_gate_no_target_refuses_silently_without_db_read() -> None:
    session_factory = MagicMock()
    runtime = _runtime(sessionmaker=session_factory)
    interaction = _member_interaction()

    refused = await refuse_if_shared_and_not_admin(interaction, runtime=runtime, entry=None)

    assert refused is True, "no selected agent must refuse"
    session_factory.assert_not_called()
    interaction.response.send_message.assert_not_called()
    interaction.followup.send.assert_not_called()


async def test_shared_gate_admin_passes_on_a_system_agent() -> None:
    session_factory = MagicMock()
    runtime = _runtime(sessionmaker=session_factory)
    interaction = _admin_interaction()
    entry = _entry("daimon", is_system=True)

    refused = await refuse_if_shared_and_not_admin(interaction, runtime=runtime, entry=entry)

    assert refused is False, (
        "an admin must be able to attach a repo to the seeded agent — that is the "
        "first-run onboarding step, and the spec gate's system-agent absolutism "
        "must not leak into the attachment gate"
    )
    interaction.response.send_message.assert_not_called()
    interaction.followup.send.assert_not_called()


async def test_shared_gate_admin_passes_on_a_reachable_agent_without_touching_the_database() -> (
    None
):
    session_factory = MagicMock()
    entry = _entry("bot")
    runtime = _runtime(
        sessionmaker=session_factory,
        deployment_default=DeploymentDefault(agent_name=entry.name),
    )
    interaction = _admin_interaction()

    refused = await refuse_if_shared_and_not_admin(interaction, runtime=runtime, entry=entry)

    assert refused is False, "a live admin must pass even when the target is the current default"
    session_factory.assert_not_called()
    interaction.response.send_message.assert_not_called()
    interaction.followup.send.assert_not_called()


async def test_shared_gate_non_admin_refuses_on_a_system_agent() -> None:
    session_factory = MagicMock()
    runtime = _runtime(sessionmaker=session_factory)
    interaction = _member_interaction()
    entry = _entry("daimon", is_system=True)

    refused = await refuse_if_shared_and_not_admin(interaction, runtime=runtime, entry=entry)

    assert refused is True, "a member must not write attachments on the deployment's own agent"
    session_factory.assert_not_called()
    interaction.response.send_message.assert_called_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True
    interaction.followup.send.assert_not_called()


async def test_shared_gate_non_admin_refuses_on_a_reachable_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild_id = 222004
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, platform="discord", workspace_id=str(guild_id))
    entry = _entry("bot")
    runtime = _runtime(
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(agent_name=entry.name),
    )
    interaction = _member_interaction(guild_id=guild_id)

    refused = await refuse_if_shared_and_not_admin(interaction, runtime=runtime, entry=entry)

    assert refused is True, "the workspace's current default agent must refuse a non-admin"
    interaction.response.send_message.assert_called_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True
    interaction.followup.send.assert_not_called()


async def test_shared_gate_non_admin_passes_on_an_unreachable_non_system_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild_id = 222005
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, platform="discord", workspace_id=str(guild_id))
    runtime = _runtime(sessionmaker=db_session_factory)
    interaction = _member_interaction(guild_id=guild_id)
    entry = _entry("bot")

    refused = await refuse_if_shared_and_not_admin(interaction, runtime=runtime, entry=entry)

    assert refused is True, "unreachable does not establish ownership"
    interaction.response.send_message.assert_called_once()
    interaction.followup.send.assert_not_called()


async def test_shared_gate_refusal_uses_followup_when_already_acked() -> None:
    session_factory = MagicMock()
    runtime = _runtime(sessionmaker=session_factory)
    interaction = _member_interaction(acked=True)
    entry = _entry("daimon", is_system=True)

    refused = await refuse_if_shared_and_not_admin(interaction, runtime=runtime, entry=entry)

    assert refused is True, "an acked interaction must still refuse"
    session_factory.assert_not_called()
    interaction.response.send_message.assert_not_called()
    interaction.followup.send.assert_called_once()
    assert interaction.followup.send.call_args.kwargs.get("ephemeral") is True


async def test_both_gates_let_a_channel_admin_edit_an_agent_local_to_their_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Locality reaches the panel gates as it reaches Slack's, on an agent a server admin
    gave their channel: own channel yes, server default no, a member's binding no."""
    guild_id = 222006
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(guild_id))
        await set_channel_admins(
            session,
            tenant_id=tenant.id,
            platform="discord",
            channel_id="500",
            role_ids=[],
            user_ids=["2"],
            actor_account_id=None,
        )
        for channel, name, by_admin in (("500", "local-bot", True), ("501", "bound-bot", False)):
            await set_fields(
                session,
                scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=channel),
                tenant_id=tenant.id,
                agent_name=name,
                mode="agent",
                set_by_admin=by_admin,
            )
        await set_channel_admins(
            session,
            tenant_id=tenant.id,
            platform="discord",
            channel_id="501",
            role_ids=[],
            user_ids=["2"],
            actor_account_id=None,
        )
    async with db_session_factory.begin() as session:
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(agent_channel_pins={"local-bot": ("500",)}),
        )
    runtime = _runtime(
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(agent_name="wide-bot"),
    )
    for gate in (refuse_if_reachable_and_not_admin, refuse_if_shared_and_not_admin):
        bound = await gate(
            _member_interaction(guild_id=guild_id), runtime=runtime, entry=_entry("bound-bot")
        )
        assert bound is True, f"{gate.__name__}: a member's binding does not make it the admin's"
        local = await gate(
            _member_interaction(guild_id=guild_id), runtime=runtime, entry=_entry("local-bot")
        )
        wide = await gate(
            _member_interaction(guild_id=guild_id), runtime=runtime, entry=_entry("wide-bot")
        )
        assert local is False, f"{gate.__name__}: the agent answers only in the admin's channel"
        assert wide is True, f"{gate.__name__}: the server default stays with server admins"
