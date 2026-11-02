"""The Hand over button on a thread whose channel now answers with another agent.

Driven through the real `HandOverButton.callback` against real Postgres and an
MA fake: a member's click binds the thread to the channel's agent, so the next
message routes to it; a refused click writes nothing and answers privately.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from daimon.adapters.discord.feedback_button import FeedbackButton
from daimon.adapters.discord.support_escalation import SupportEscalateButton
from daimon.adapters.discord.theme import COLOR_RED
from daimon.adapters.discord.thread_handoff import HandOverButton, build_custom_id, hand_over_view
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, ScopeContext
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.agent_notices import take_agent_notices
from daimon.core.stores.scoped_config_read import resolve
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.thread_agent_bindings import get_binding
from daimon.core.thread_handoff import HANDOVER_FALLBACK_SEED
from daimon.core.turn.agent_notices import HANDED_OVER_NOTICE
from daimon.testing.db import db_nullpool_engine as db_nullpool_engine
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_agent
from daimon.testing.turn_fakes import deps_for
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

_GUILD = "700000009"
_PARENT = 111111111111111111
_THREAD = 222222222222222222
_DEFAULT = DeploymentDefault(agent_name="daimon")


async def _seed(db_session: AsyncSession, *, policy: TenantAccessPolicy | None = None) -> Any:
    tenant = await make_tenant(db_session, platform="discord", workspace_id=_GUILD)
    # The channel now answers with research-bot.
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=str(_PARENT)),
        tenant_id=tenant.id,
        agent_name="research-bot",
    )
    if policy is not None:
        await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
    await db_session.commit()
    return tenant


def _interaction(sessionmaker: async_sessionmaker[AsyncSession], tenant_id: Any) -> MagicMock:
    router = MARouter()
    router.add_agent(ma_agent(id="ag_research", name="research-bot", tenant_id=tenant_id))
    runtime = SimpleNamespace(
        anthropic=build_fake_anthropic(router.dispatch),
        sessionmaker=sessionmaker,
        turn_deps=replace(deps_for(None, None), deployment_default=_DEFAULT, turn_path=None),
    )
    thread = MagicMock(spec=discord.Thread)
    thread.id = _THREAD
    thread.parent_id = _PARENT
    thread.send = AsyncMock()
    interaction = MagicMock()
    tasks: list[Any] = []
    interaction.client = SimpleNamespace(
        runtime=runtime, application_id=77, _spawn=tasks.append, on_message=AsyncMock(), tasks=tasks
    )
    interaction.id = 999999999999999999
    interaction.message.id = 456
    interaction.message.channel = thread
    interaction.message.add_reaction = AsyncMock()
    interaction.message.remove_reaction = AsyncMock()

    async def history(**kwargs: Any) -> AsyncIterator[Any]:
        yield SimpleNamespace(
            author=SimpleNamespace(id=42, bot=False), content="Please finish the analysis."
        )

    thread.history = history
    interaction.guild_id = int(_GUILD)
    interaction.channel = thread
    interaction.user = SimpleNamespace(id=42, mention="<@42>")
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=True)
    interaction.followup.send = AsyncMock()
    interaction.message.edit = AsyncMock()
    interaction.message.author.name = "Daimon"
    interaction.message.webhook_id = None
    interaction.message.application_id = None
    interaction.delete_original_response = AsyncMock()
    return interaction


@pytest.mark.parametrize("clicks", [1, 2])
async def test_a_click_hands_the_thread_to_the_channels_agent(
    clicks: int, db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed(db_session)
    interaction = _interaction(db_session_factory, tenant.id)

    await HandOverButton(agent_id="ag_research").callback(interaction)

    interaction.channel.send.assert_not_awaited()
    interaction.message.edit.assert_awaited_once_with(view=None)
    async with db_session_factory.begin() as session:
        notices = await take_agent_notices(
            session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id=str(_THREAD),
            agent_id="ag_research",
            since=datetime.now(UTC) - timedelta(hours=1),
        )
    assert notices == [HANDED_OVER_NOTICE], "research-bot hears of it, not the thread"
    async with db_session_factory() as session:
        binding = await get_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id=str(_PARENT),
            thread_id=str(_THREAD),
        )
        routed = await resolve(
            session,
            context=ScopeContext(
                tenant_id=tenant.id,
                channel_id=str(_PARENT),
                thread_id=str(_THREAD),
                platform="discord",
            ),
            default=_DEFAULT,
        )
    assert binding is not None and binding.kind == "handoff"
    assert routed.responder_ma_agent_id == "ag_research", "later messages go to research-bot"
    assert routed.thread_binding_kind == "handoff"

    if clicks == 2:
        await HandOverButton(agent_id="ag_research").callback(interaction)
    await asyncio.gather(*interaction.client.tasks)
    interaction.client.on_message.assert_awaited_once()
    resent = interaction.client.on_message.await_args.args[0]
    assert resent.author is interaction.user
    assert resent.content == "Please finish the analysis."
    assert resent.id == interaction.id


async def test_a_refused_click_answers_privately_and_writes_nothing(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed(
        db_session, policy=TenantAccessPolicy(agent_channel_pins={"research-bot": ("C9",)})
    )
    interaction = _interaction(db_session_factory, tenant.id)

    await HandOverButton(agent_id="ag_research").callback(interaction)

    interaction.channel.send.assert_not_awaited()
    interaction.followup.send.assert_awaited_once()
    refusal = interaction.followup.send.call_args.kwargs["embed"]
    assert "only runs in other channels" in refusal.description
    assert refusal.color is not None and refusal.color.value == COLOR_RED
    assert interaction.followup.send.call_args.kwargs["ephemeral"] is True
    async with db_session_factory() as session:
        binding = await get_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id=str(_PARENT),
            thread_id=str(_THREAD),
        )
    assert binding is None
    async with db_session_factory.begin() as session:
        notices = await take_agent_notices(
            session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id=str(_THREAD),
            agent_id="ag_research",
            since=datetime.now(UTC) - timedelta(hours=1),
        )
    assert notices == [], "a refused click tells the agent nothing"

    assert interaction.client.tasks == []
    interaction.client.on_message.assert_not_awaited()


async def test_a_click_removes_a_webhook_cards_button(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed(db_session)
    interaction = _interaction(db_session_factory, tenant.id)
    interaction.channel.parent = MagicMock(spec=discord.TextChannel)
    interaction.message.webhook_id = 123
    interaction.message.application_id = 77
    hook = MagicMock(spec=discord.Webhook)
    hook.edit_message = AsyncMock()

    with patch(
        "daimon.adapters.discord.post_transport.DiscordPostTransport._webhook",
        new_callable=AsyncMock,
        return_value=hook,
    ):
        await HandOverButton(agent_id="ag_research").callback(interaction)

    await asyncio.gather(*interaction.client.tasks)
    hook.edit_message.assert_awaited_once_with(
        interaction.message.id, view=None, thread=interaction.channel
    )
    interaction.message.edit.assert_not_awaited()


async def test_the_button_carries_the_agent_and_its_template_overlaps_no_other() -> None:
    view = hand_over_view(agent_id="agent_011CSabc", agent_name="research-bot")
    (button,) = view.children
    custom_id = build_custom_id("agent_011CSabc")
    assert getattr(button, "custom_id", None) == custom_id
    assert HandOverButton.__discord_ui_compiled_template__.fullmatch(custom_id)
    for other in (FeedbackButton, SupportEscalateButton):
        assert other.__discord_ui_compiled_template__.fullmatch(custom_id) is None


@pytest.mark.parametrize("busy,later", [(False, False), (True, False), (False, True), (True, True)])
async def test_click_and_following_mention_use_normal_ingress_in_order(
    busy: bool,
    later: bool,
    db_session: AsyncSession,
    db_nullpool_engine: AsyncEngine,
) -> None:
    from decimal import Decimal

    from daimon.adapters.discord import bot as bot_module
    from daimon.adapters.discord.button_message import ButtonMessage
    from daimon.adapters.discord.runtime import DiscordRuntime
    from daimon.core.config import McpSettings
    from daimon.core.membership import Membership
    from daimon.core.notebooks._rate_limit import RateLimiter
    from daimon.core.stores import tenant_ledger
    from daimon.core.stores.task_continuations import list_pending_continuations
    from daimon.core.turn.run import RunOutcome
    from daimon.core.turn.state import TurnState
    from daimon.testing.ma import resolved_agent_env_router
    from daimon.testing.ma_models import ma_environment
    from mux.testing.drivers.anthropic.ma_models import ma_session

    from .harness import make_bot

    db_session_factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    tenant = await _seed(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100"),
        reason="trial",
        idempotency_key="busy-handover-credit",
    )
    await db_session.commit()
    agent = ma_agent(id="ag_research", name="research-bot", tenant_id=tenant.id)
    environment = ma_environment(id="env_handover", name="default", tenant_id=tenant.id)
    client = build_fake_anthropic(resolved_agent_env_router(agent, environment).dispatch)
    default = DeploymentDefault(agent_name="daimon", environment_name="default")
    deps = replace(deps_for(client, db_session_factory), turn_path=None, deployment_default=default)
    settings = MagicMock()
    settings.mcp = McpSettings()
    settings.app.env = "production"
    settings.discord.command_guild_ids = (int(_GUILD),)
    settings.discord.bot_display_name = "Daimon"
    settings.agent_identity.enabled = False
    settings.billing.markup = Decimal("1")
    settings.discord.max_concurrent_turns_per_tenant = 3
    bot = make_bot(
        DiscordRuntime(
            settings=settings,
            anthropic=client,
            sessionmaker=db_session_factory,
            notebook_rate_limiter=RateLimiter(max_requests=999),
            billing_config=None,
            deployment_default=default,
            resolver_cache=deps.resolver_cache,
            turn_deps=deps,
        )
    )
    interaction = _interaction(db_session_factory, tenant.id)
    interaction.client = bot
    thread = interaction.channel
    thread.parent = MagicMock(spec=discord.TextChannel)
    thread.parent.category_id = None
    member = MagicMock(spec=discord.Member)
    member.id = 42
    member.guild_permissions.administrator = True
    member.guild_permissions.manage_guild = False
    member.roles = []
    thread.guild.id = int(_GUILD)
    thread.guild.owner_id = 99
    thread.guild.get_member.return_value = member
    interaction.message.guild = thread.guild
    interaction.message.channel = thread
    interaction.message._state = bot._connection
    member.bot = False
    member.guild = thread.guild
    member.display_name = "Clicker"
    interaction.user = member
    thread.guild.fetch_member = AsyncMock(return_value=member)
    message = MagicMock(spec=discord.Message)
    message.id = 789
    message.edit = AsyncMock(return_value=message)
    message.delete = AsyncMock()
    thread.send = AsyncMock(return_value=message)

    # A newer gateway membership event already revoked the snapshot's admin role.
    bot.membership_resolver.observe(
        tenant.id, _GUILD, "42", Membership(is_member=True, is_admin=False)
    )
    started, release = asyncio.Event(), asyncio.Event()
    order: list[str] = []
    real_orchestrate = bot._orchestrate

    async def turn(inbound: Any, *args: Any, **kwargs: Any) -> None:
        if isinstance(inbound, ButtonMessage):
            await real_orchestrate(inbound, *args, **kwargs)
        else:
            order.append(inbound.content)
            if "running turn" in inbound.content:
                started.set()
                await release.wait()

    def human(text: str, message_id: int) -> Any:
        inbound = MagicMock(spec=discord.Message)
        inbound.id = message_id
        inbound.author = member
        inbound.guild = thread.guild
        inbound.channel = thread
        inbound.content = text
        inbound.mentions = [bot.user]
        inbound.webhook_id = None
        inbound.reference = None
        inbound.attachments = []
        inbound.add_reaction = AsyncMock()
        inbound.remove_reaction = AsyncMock()
        return inbound

    async def create(*args: Any, **kwargs: Any) -> Any:
        await kwargs["before_create"]()
        return ma_session(id="session_handover", agent=agent, environment_id=environment.id)

    async def context(*args: Any, **kwargs: Any) -> Any:
        return kwargs.get("query_text") or kwargs["trigger"].content, None

    async def answer(deps: Any, prepared: Any, **kwargs: Any) -> Any:
        assert prepared.admission.agent.id == "ag_research"
        assert prepared.admission.grant.subject.is_admin is busy
        assert prepared.admission.account_id is not None
        assert "Please finish the analysis." in kwargs["user_message"]
        order.append("button")
        if not busy and later:
            started.set()
            await release.wait()
        await kwargs["lifecycle"].on_terminal_success(TurnState())
        return RunOutcome(
            state=TurnState(),
            ma_session_id=prepared.ma_session_id,
            mapping_id=prepared.mapping_id,
            recovered=False,
        )

    with (
        patch.object(bot, "_orchestrate", side_effect=turn),
        patch("daimon.core.turn.prepare.create_session", side_effect=create),
        patch.object(bot_module, "build_context_xml", side_effect=context),
        patch.object(bot_module, "admit", wraps=bot_module.admit) as admitted,
        patch.object(bot_module, "bind_session", wraps=bot_module.bind_session) as bound,
        patch.object(bot_module, "run_prepared_turn", side_effect=answer) as ran,
    ):
        owner = None
        if busy:
            owner = asyncio.create_task(bot.on_message(human("<@999> running turn", 123)))
            await asyncio.wait_for(started.wait(), 20)
        await HandOverButton(agent_id="ag_research").callback(interaction)
        # A repeated click finds the changed binding and creates no second input.
        await HandOverButton(agent_id="ag_research").callback(interaction)
        if busy:
            async with asyncio.timeout(20):
                while not bot._pending.get(thread.id):
                    await asyncio.sleep(0.01)
            interaction.message.add_reaction.assert_any_await("⌛")
            ran.assert_not_awaited()
        elif later:
            await asyncio.wait_for(started.wait(), 20)
        if later:
            await bot.on_message(human("<@999> Later request.", interaction.id + 1))
        release.set()
        if owner is not None:
            await owner
        async with asyncio.timeout(30):
            while bot._bg_tasks:
                await asyncio.gather(*list(bot._bg_tasks))
                await asyncio.sleep(0)
    assert order == (["<@999> running turn"] if busy else []) + ["button"] + (
        ["<@999> Later request."] if later else []
    )
    admitted.assert_awaited_once()
    assert admitted.await_args.kwargs["external_user_id"] == "42"
    bound.assert_awaited_once()
    ran.assert_awaited_once()
    assert thread.id not in bot._processing
    assert not bot._pending.get(thread.id)
    async with db_session_factory() as session:
        assert (
            await list_pending_continuations(
                session, tenant_id=tenant.id, platform="discord", thread_id=str(_THREAD)
            )
            == []
        )


async def test_unreadable_seed_history_resends_the_fixed_prompt(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed(db_session)
    interaction = _interaction(db_session_factory, tenant.id)
    response = MagicMock()
    response.status = 403
    interaction.channel.history = MagicMock(side_effect=discord.Forbidden(response, "unreadable"))
    await HandOverButton(agent_id="ag_research").callback(interaction)
    await asyncio.gather(*interaction.client.tasks)
    resent = interaction.client.on_message.await_args.args[0]
    assert resent.content == HANDOVER_FALLBACK_SEED
