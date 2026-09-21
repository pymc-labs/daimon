"""Setup entry creates public platform content and durable identity routing."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
from daimon.adapters.discord.agent_setup.conversations import open_setup_conversation
from daimon.adapters.discord.agent_setup.roster_view import RosterView
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.runtime import DiscordRuntime, build_turn_deps
from daimon.core.config import Settings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.roster import RosterAgent
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.thread_agent_bindings import get_binding
from daimon.testing import ma_agent
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import build_fake_anthropic, list_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.parametrize("is_admin", [False, True])
@pytest.mark.parametrize("storage_fails", [False, True])
async def test_setup_entry_binds_exact_target_before_ready_without_a_billed_turn(
    db_session_factory: async_sessionmaker[AsyncSession], is_admin: bool, storage_fails: bool
) -> None:
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id="111")
        account = await make_account(session, tenant=tenant)
    responder = ma_agent(
        id="agent_daimon",
        name="daimon",
        tenant_id=tenant.id,
        metadata={"daimon_managed": "true"},
    )
    target = ma_agent(
        id="agent_specialist",
        name="specialist",
        tenant_id=tenant.id,
    )
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET", "opening setup must not create a session or billed turn"
        if request.url.path.endswith("/agents"):
            return list_response(
                [responder.model_dump(mode="json"), target.model_dump(mode="json")]
            )
        assert request.url.path.endswith("/agents/agent_specialist"), (
            "validate the exact selected identity"
        )
        return httpx.Response(200, json=target.model_dump(mode="json"))

    anthropic = build_fake_anthropic(handle)
    settings = Settings.model_validate(
        {
            "database": {"url": "postgresql+asyncpg://test:test@localhost/daimon_test"},
            "anthropic": {"api_key": "test"},
        }
    )
    defaults = DeploymentDefault(agent_name="specialist")
    cache = new_resolver_cache()
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=anthropic,
        sessionmaker=db_session_factory,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=defaults,
        resolver_cache=cache,
        turn_deps=build_turn_deps(
            settings,
            anthropic,
            db_session_factory,
            deployment_default=defaults,
            resolver_cache=cache,
            billing_config=None,
        ),
    )
    specialist = RosterAgent(
        name="specialist",
        ma_agent_id="agent_specialist",
        model_id="claude-sonnet-4-6",
        is_built_in=False,
    )
    state = PanelState(
        roster=[],
        selected=None,
        account_id=uuid.uuid4() if storage_fails else account.id,
        is_admin=is_admin,
        channel_id=222,
        deployment_default=defaults,
        roster_agents=(specialist,),
        answering=specialist,
    )
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 42
    interaction.user.mention = "<@42>"
    interaction.user.guild_permissions = discord.Permissions(administrator=is_admin)
    interaction.guild_id = 111
    interaction.guild.owner_id = 999
    interaction.client.user.mention = "<@123>"
    interaction.channel = MagicMock(spec=discord.TextChannel)
    interaction.channel.id = 222
    thread = MagicMock(spec=discord.Thread)
    thread.id = 333
    thread.jump_url = "https://discord.com/channels/111/333"
    thread.delete = AsyncMock()
    interaction.channel.create_thread = AsyncMock(return_value=thread)
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    async def send_opener(*args: object, **kwargs: object) -> None:
        async with db_session_factory() as session:
            binding = await get_binding(
                session,
                tenant_id=tenant.id,
                platform="discord",
                parent_channel_id="222",
                thread_id="333",
            )
        assert binding is not None, "the binding must be committed before the opener is visible"
        assert "specialist" in str(args[0]) and "<@123>" in str(args[0]), (
            "opener names target and reply mention"
        )

    thread.send = AsyncMock(side_effect=send_opener)
    view = RosterView(state, runtime=runtime, allowed_user_id=42)
    await view._on_setup(interaction)  # pyright: ignore[reportPrivateUsage]  # the live setup entry point
    interaction.response.defer.assert_awaited_once()
    assert (
        interaction.channel.create_thread.call_args.kwargs["type"]
        is discord.ChannelType.public_thread
    ), "setup threads must explicitly be public"
    async with db_session_factory() as session:
        binding = await get_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="222",
            thread_id="333",
        )
    if storage_fails:
        assert binding is None, "failed persistence must not leave a ready binding"
        thread.delete.assert_awaited_once()
        thread.send.assert_not_awaited()
        assert "Continue setup" not in str(interaction.followup.send.call_args), (
            "failure must not advertise readiness"
        )
    else:
        assert binding is not None, "setup routing must be durable"
        assert binding.responder_ma_agent_id == "agent_daimon", (
            "Daimon responds independently of the selected specialist"
        )
        assert binding.configuration_target_ma_agent_id == "agent_specialist", (
            "target stores exact MA identity"
        )
        assert thread.jump_url in interaction.followup.send.call_args.args[0], (
            "entry returns a clickable link"
        )
        thread.delete.assert_not_awaited()
    assert requests, "entry validates live agent identities"


async def test_setup_entry_refuses_unsupported_channel_before_platform_creation() -> None:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = None
    interaction.followup.send = AsyncMock()
    await open_setup_conversation(
        interaction,
        runtime=MagicMock(spec=DiscordRuntime),
        state=PanelState(roster=[], selected=None, account_id=uuid.uuid4()),
    )
    assert "server text channel" in interaction.followup.send.call_args.args[0], (
        "unsupported channels need a concrete remedy"
    )
