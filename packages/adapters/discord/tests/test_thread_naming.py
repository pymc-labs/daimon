"""Tests for the Discord auto-naming task and its wiring in ``DaimonBot``.

``auto_name_thread`` runs against a real ``AsyncAnthropic`` over
``httpx.MockTransport`` and a real Postgres, so the metering path is the
production one. The ``discord.Thread`` is a ``MagicMock(spec=...)`` — the
edit is discord.py glue at the system boundary, like ``create_thread`` in the
sibling bot tests.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import httpx
import pytest
from anthropic.types import Message, TextBlock, Usage
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent, BetaManagedAgentsSession
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from anthropic.types.beta.beta_managed_agents_session_agent import BetaManagedAgentsSessionAgent
from anthropic.types.beta.beta_managed_agents_session_stats import BetaManagedAgentsSessionStats
from anthropic.types.beta.beta_managed_agents_session_usage import BetaManagedAgentsSessionUsage
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime, build_turn_deps
from daimon.adapters.discord.thread_naming import auto_name_thread
from daimon.core.config import McpSettings, ThreadNamingSettings
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault, ResolvedConfig
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.thread_naming import THREAD_NAMING_MODEL
from daimon.testing.factories import make_tenant
from daimon.testing.ma import EMPTY_CLOUD_CONFIG, MARouter, build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio


def _message_payload(text: str) -> dict[str, Any]:
    return Message(
        id="msg_thread_name",
        type="message",
        role="assistant",
        model=THREAD_NAMING_MODEL,
        content=[TextBlock(type="text", text=text)],
        stop_reason="end_turn",
        stop_sequence=None,
        usage=Usage(input_tokens=120, output_tokens=9),
    ).model_dump(mode="json")


def _naming_router(text: str) -> MARouter:
    router = MARouter()
    router.add(
        "POST", r"/v1/messages", lambda _req, _m: httpx.Response(200, json=_message_payload(text))
    )
    return router


_PLACEHOLDER = "Chat with test-agent"


def _thread(thread_id: int = 4242, *, name: str = _PLACEHOLDER) -> Any:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.name = name
    thread.edit = AsyncMock()
    return thread


# ---------------------------------------------------------------------------
# auto_name_thread
# ---------------------------------------------------------------------------


async def test_auto_name_thread_renames_and_meters_haiku_call_to_author(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("5.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    await db_session.commit()
    thread = _thread()

    await auto_name_thread(
        thread=thread,
        expected_name=_PLACEHOLDER,
        message_text="<@999> why does my PyMC model diverge on the M2 Mac?",
        anthropic=build_fake_anthropic(_naming_router("PyMC Divergences on M2 Mac").dispatch),
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        platform_user_id="555",
        markup=Decimal("1.0"),
        max_input_chars=2000,
    )

    thread.edit.assert_awaited_once_with(name="PyMC Divergences on M2 Mac")
    rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant.id)
    assert [(r.model, r.platform_user_id, r.input_tokens, r.managed_session_id) for r in rows] == [
        (THREAD_NAMING_MODEL, "555", 120, "thread-naming:4242")
    ], "the naming call must be metered to the tenant under the author, keyed on the thread"
    balance = await tenant_ledger.get_balance(db_session, tenant_id=tenant.id)
    assert balance < Decimal("5.00"), "the tenant ledger must carry the naming debit"


async def test_auto_name_thread_keeps_placeholder_but_still_meters_when_model_declines(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    thread = _thread()

    await auto_name_thread(
        thread=thread,
        expected_name=_PLACEHOLDER,
        message_text="<@999> hey",
        anthropic=build_fake_anthropic(_naming_router("NONE").dispatch),
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        platform_user_id="555",
        markup=Decimal("1.0"),
        max_input_chars=2000,
    )

    thread.edit.assert_not_awaited()
    rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant.id)
    assert len(rows) == 1, "tokens were spent even though no title came back; meter them"


async def test_auto_name_thread_swallows_api_error_without_metering_or_rename(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    thread = _thread()
    router = MARouter()
    router.add(
        "POST",
        r"/v1/messages",
        lambda _req, _m: httpx.Response(400, json={"type": "error", "error": {"message": "x"}}),
    )

    await auto_name_thread(
        thread=thread,
        expected_name=_PLACEHOLDER,
        message_text="<@999> help with sampling",
        anthropic=build_fake_anthropic(router.dispatch),
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        platform_user_id="555",
        markup=Decimal("1.0"),
        max_input_chars=2000,
    )

    thread.edit.assert_not_awaited()
    rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant.id)
    assert rows == [], "a failed call produced no usage and must not be billed"


async def test_auto_name_thread_meters_even_when_discord_rejects_the_edit(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    thread = _thread()
    thread.edit.side_effect = discord.HTTPException(
        SimpleNamespace(status=429, reason="Too Many Requests"),  # pyright: ignore[reportArgumentType]
        "rate limited",
    )

    await auto_name_thread(
        thread=thread,
        expected_name=_PLACEHOLDER,
        message_text="<@999> help with sampling",
        anthropic=build_fake_anthropic(_naming_router("Sampling Help").dispatch),
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        platform_user_id="555",
        markup=Decimal("1.0"),
        max_input_chars=2000,
    )

    rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant.id)
    assert len(rows) == 1, "the Haiku tokens were spent before Discord refused the rename"


async def test_auto_name_thread_skips_call_when_thread_already_renamed(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A member or the agent renamed the thread before Haiku answered: the
    deliberate title wins, and no tokens are spent finding out."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    thread = _thread(name="My Own Title")

    def refuse(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no model call is allowed once the placeholder is gone")

    await auto_name_thread(
        thread=thread,
        expected_name=_PLACEHOLDER,
        message_text="help with sampling",
        anthropic=build_fake_anthropic(refuse),
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        platform_user_id="555",
        markup=Decimal("1.0"),
        max_input_chars=2000,
    )

    thread.edit.assert_not_awaited()
    rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant.id)
    assert rows == [], "no call, no usage row"


# ---------------------------------------------------------------------------
# Bot wiring: on_message spawns the rename after create_thread
# ---------------------------------------------------------------------------


def _fake_session() -> BetaManagedAgentsSession:
    return BetaManagedAgentsSession(
        id="sess_naming",
        agent=BetaManagedAgentsSessionAgent(
            id="ag_test",
            mcp_servers=[],
            model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-5"),
            name="test-agent",
            skills=[],
            tools=[],
            type="agent",
            version=1,
        ),
        created_at="2026-04-28T00:00:00Z",
        environment_id="env_test",
        metadata={},
        resources=[],
        stats=BetaManagedAgentsSessionStats(),
        status="idle",
        type="session",
        updated_at="2026-04-28T00:00:00Z",
        usage=BetaManagedAgentsSessionUsage(),
        vault_ids=[],
        outcome_evaluations=[],
    )


def _ma_router() -> MARouter:
    agent = BetaManagedAgentsAgent(
        id="ag_test",
        version=1,
        name="test-agent",
        type="agent",
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-5"),
        created_at=datetime(2026, 4, 28, tzinfo=UTC),
        updated_at=datetime(2026, 4, 28, tzinfo=UTC),
        mcp_servers=[],
        metadata={},
        skills=[],
        tools=[],
    ).model_dump(mode="json")
    environment = BetaEnvironment(
        id="env_test",
        name="test-env",
        type="environment",
        config=EMPTY_CLOUD_CONFIG,
        created_at="2026-04-28T00:00:00Z",
        updated_at="2026-04-28T00:00:00Z",
        description="",
        metadata={},
    ).model_dump(mode="json")
    router = MARouter()
    router.add("GET", r"/v1/agents/ag_test", lambda _req, _m: httpx.Response(200, json=agent))
    router.add(
        "GET", r"/v1/environments/env_test", lambda _req, _m: httpx.Response(200, json=environment)
    )
    return router


def _runtime(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    router: MARouter,
    thread_naming: ThreadNamingSettings,
) -> DiscordRuntime:
    settings = MagicMock()
    settings.mcp = McpSettings()
    settings.defaults_root = MagicMock()
    settings.billing.markup = Decimal("1.0")
    settings.thread_naming = thread_naming
    discord_settings = MagicMock()
    discord_settings.max_concurrent_turns_per_tenant = 100
    discord_settings.per_caller_thread_sessions = True
    settings.discord = discord_settings
    anthropic = build_fake_anthropic(router.dispatch)
    resolver_cache = new_resolver_cache()
    deployment_default = DeploymentDefault()
    return DiscordRuntime(
        settings=settings,
        anthropic=anthropic,
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=deployment_default,
        resolver_cache=resolver_cache,
        turn_deps=build_turn_deps(
            settings,
            anthropic,
            sessionmaker,
            deployment_default=deployment_default,
            resolver_cache=resolver_cache,
            billing_config=None,
        ),
    )


def _bot(runtime: DiscordRuntime) -> DaimonBot:
    intents = discord.Intents.default()
    intents.message_content = True
    bot = DaimonBot(runtime=runtime, intents=intents)
    bot._connection.user = MagicMock(spec=discord.ClientUser)  # pyright: ignore[reportPrivateUsage]
    bot._connection.user.id = 999  # pyright: ignore[reportPrivateUsage]
    bot._connection.user.mentioned_in = MagicMock(return_value=True)  # pyright: ignore[reportPrivateUsage]
    return bot


def _channel_message(*, guild_id: int, content: str) -> Any:
    message = MagicMock(spec=discord.Message)
    message.content = content
    message.author = MagicMock()
    message.author.bot = False
    message.author.id = 555
    message.guild = MagicMock(spec=discord.Guild)
    message.guild.id = guild_id
    message.guild.owner_id = 9999
    message.channel = MagicMock()
    message.channel.__class__ = discord.TextChannel
    message.channel.id = 789
    message.channel.send = AsyncMock()
    message.create_thread = AsyncMock()
    message.add_reaction = AsyncMock()
    message.mentions = [SimpleNamespace(id=999)]
    message.attachments = []
    return message


@pytest.mark.parametrize("enabled", [True, False])
@patch("daimon.adapters.discord.bot.auto_name_thread", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
@patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
@patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
async def test_on_message_spawns_rename_of_new_thread_only_when_naming_enabled(
    mock_resolve_config: AsyncMock,
    mock_create_session: AsyncMock,
    mock_run_turn: AsyncMock,
    mock_resolve_environment: AsyncMock,
    mock_resolve_agent: AsyncMock,
    mock_auto_name: AsyncMock,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    enabled: bool,
) -> None:
    """The thread opens under the placeholder, then the rename is spawned in
    the background with the turn's tenant, author and settings — and only
    when ``DAIMON_THREAD_NAMING__ENABLED`` is on.

    ``auto_name_thread`` itself is covered above against a real DB; here it
    is patched because the test session factory shares ONE connection, which
    a concurrent background write would trip over (production pools).
    """
    guild_id = "801000777" if enabled else "801000778"
    await provision_tenant(
        db_session_factory,
        platform="discord",
        workspace_id=guild_id,
        signup_credit=Decimal("5.00"),
    )
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
    mock_resolve_config.return_value = ResolvedConfig(
        agent_name="test-agent",
        agent_name_tier="tenant",
        environment_name="test-env",
        environment_name_tier="tenant",
    )
    mock_create_session.return_value = _fake_session()
    mock_resolve_agent.return_value = "ag_test"
    mock_resolve_environment.return_value = "env_test"

    runtime = _runtime(
        db_session_factory,
        router=_ma_router(),
        thread_naming=ThreadNamingSettings(enabled=enabled, max_input_chars=321),
    )
    bot = _bot(runtime)
    message = _channel_message(guild_id=int(guild_id), content="<@999> my PyMC model diverges")
    thread = _thread(thread_id=8001)
    thread.send = AsyncMock()
    message.create_thread.return_value = thread

    await bot.on_message(message)
    await asyncio.gather(*bot._bg_tasks)  # pyright: ignore[reportPrivateUsage]

    message.create_thread.assert_awaited_once()
    assert message.create_thread.await_args.kwargs["name"] == "Chat with test-agent", (
        "the thread must open instantly under the placeholder; the title arrives later"
    )
    if not enabled:
        mock_auto_name.assert_not_awaited()
        return
    mock_auto_name.assert_awaited_once()
    spawned = mock_auto_name.await_args.kwargs
    assert spawned["thread"] is thread, "the rename must target the thread just created"
    assert spawned["message_text"] == "my PyMC model diverges", (
        "the opening message, minus the bot mention, is what gets titled"
    )
    assert spawned["expected_name"] == "Chat with test-agent", (
        "the task must know the placeholder so it never overwrites a deliberate rename"
    )
    assert (spawned["tenant_id"], spawned["platform_user_id"]) == (tenant_id, "555"), (
        "the naming call must be billed to the turn's tenant under the message author"
    )
    assert spawned["max_input_chars"] == 321, "settings must reach the task unchanged"
    assert spawned["anthropic"] is runtime.anthropic, "the runtime client is reused"
