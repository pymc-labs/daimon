"""Tests for the Discord thread title and its wiring in ``DaimonBot``.

``generate_thread_name`` runs against a real ``AsyncAnthropic`` over
``httpx.MockTransport`` and a real Postgres, so the metering path is the
production one. The ``discord.Thread`` returned by ``create_thread`` is a
``MagicMock(spec=...)`` — discord.py glue at the system boundary, like
``create_thread`` in the sibling bot tests.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types import Message, TextBlock, Usage
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.discord.thread_naming import generate_thread_name
from daimon.core.config import McpSettings, ThreadNamingSettings
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault, ResolvedConfig
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.thread_naming import THREAD_NAMING_MODEL
from daimon.core.turn.deps import build_turn_deps
from daimon.testing import ma_session, resolved_agent_env_router
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


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


_FALLBACK = "Chat with test-agent"


def _thread(thread_id: int = 4242) -> Any:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.send = AsyncMock()
    return thread


# ---------------------------------------------------------------------------
# generate_thread_name
# ---------------------------------------------------------------------------


async def test_generate_thread_name_returns_title_and_meters_haiku_call_to_author(
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

    name = await generate_thread_name(
        fallback=_FALLBACK,
        message_text="why does my PyMC model diverge on the M2 Mac?",
        message_id=4242,
        anthropic=build_fake_anthropic(_naming_router("PyMC Divergences on M2 Mac").dispatch),
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        platform_user_id="555",
        markup=Decimal("1.0"),
        max_input_chars=2000,
        timeout_seconds=5.0,
        channel_id="chan-1",
    )

    assert name == "PyMC Divergences on M2 Mac", "the model's title is what the thread opens under"
    rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant.id)
    assert [r.channel_id for r in rows] == ["chan-1"], "the title counts toward the channel"
    assert [(r.model, r.platform_user_id, r.input_tokens, r.managed_session_id) for r in rows] == [
        (THREAD_NAMING_MODEL, "555", 120, "thread-naming:4242")
    ], "the naming call must be metered to the tenant under the author, keyed on the message"
    balance = await tenant_ledger.get_balance(db_session, tenant_id=tenant.id)
    assert balance < Decimal("5.00"), "the tenant ledger must carry the naming debit"


async def test_generate_thread_name_falls_back_but_still_meters_when_model_answers_blank(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()

    name = await generate_thread_name(
        fallback=_FALLBACK,
        message_text="hey",
        message_id=4242,
        anthropic=build_fake_anthropic(_naming_router("").dispatch),
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        platform_user_id="555",
        markup=Decimal("1.0"),
        max_input_chars=2000,
        timeout_seconds=5.0,
    )

    assert name == _FALLBACK, "a blank answer leaves the static title"
    rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant.id)
    assert len(rows) == 1, "tokens were spent even though no title came back; meter them"


async def test_generate_thread_name_falls_back_without_metering_on_api_error(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    router = MARouter()
    router.add(
        "POST",
        r"/v1/messages",
        lambda _req, _m: httpx.Response(400, json={"type": "error", "error": {"message": "x"}}),
    )

    name = await generate_thread_name(
        fallback=_FALLBACK,
        message_text="help with sampling",
        message_id=4242,
        anthropic=build_fake_anthropic(router.dispatch),
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        platform_user_id="555",
        markup=Decimal("1.0"),
        max_input_chars=2000,
        timeout_seconds=5.0,
    )

    assert name == _FALLBACK, "a failed call leaves the static title"
    rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant.id)
    assert rows == [], "a failed call produced no usage and must not be billed"


async def test_generate_thread_name_falls_back_without_metering_when_model_is_too_slow(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The thread waits on this call, so a stalled API yields the static title
    instead of a thread that appears minutes late. The cancelled call cannot
    be metered; that is the accepted, bounded cost of the cap."""
    tenant = await make_tenant(db_session)
    await db_session.commit()

    async def stall(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=_message_payload("Too Late"))

    anthropic = AsyncAnthropic(
        api_key="test",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(stall), base_url="https://api.anthropic.com"
        ),
    )

    name = await generate_thread_name(
        fallback=_FALLBACK,
        message_text="help with sampling",
        message_id=4242,
        anthropic=anthropic,
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        platform_user_id="555",
        markup=Decimal("1.0"),
        max_input_chars=2000,
        timeout_seconds=0.05,
    )

    assert name == _FALLBACK, "past the timeout the static title wins"
    rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant.id)
    assert rows == [], "no answer arrived, so there is nothing to meter"


# ---------------------------------------------------------------------------
# Bot wiring: on_message titles the thread before create_thread
# ---------------------------------------------------------------------------


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


@pytest.mark.parametrize(
    ("enabled", "content", "expects_naming"),
    [
        (True, "<@999> my PyMC model diverges", True),
        (False, "<@999> my PyMC model diverges", False),
        (True, "<@999>", False),
    ],
    ids=["enabled", "disabled", "attachment_only"],
)
@patch("daimon.adapters.discord.bot.generate_thread_name", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
@patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock)
@patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
async def test_on_message_creates_thread_under_generated_title_only_when_there_is_text_to_name(
    mock_resolve_config: AsyncMock,
    mock_create_session: AsyncMock,
    mock_run_turn: AsyncMock,
    mock_resolve_environment: AsyncMock,
    mock_resolve_agent: AsyncMock,
    mock_generate_name: AsyncMock,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    enabled: bool,
    content: str,
    expects_naming: bool,
) -> None:
    """The title is generated with the turn's tenant, author and settings and
    the thread is created under it — no rename, so no "renamed the thread"
    notice. With ``DAIMON_THREAD_NAMING__ENABLED`` off, or a mention that
    carries no text (attachment-only), the metered call is skipped and the
    static title is used.

    ``generate_thread_name`` itself is covered above against a real DB; here
    it is patched so this test asserts only what ``on_message`` hands it and
    does with the answer.
    """
    guild_id = {
        (True, True): "801000777",
        (False, False): "801000778",
        (True, False): "801000779",
    }[(enabled, expects_naming)]
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
    mock_create_session.return_value = ma_session(id="sess_naming")
    mock_resolve_agent.return_value = "ag_test"
    mock_resolve_environment.return_value = "env_test"
    mock_generate_name.return_value = "PyMC Divergences"

    runtime = _runtime(
        db_session_factory,
        router=resolved_agent_env_router(),
        thread_naming=ThreadNamingSettings(
            enabled=enabled, max_input_chars=321, timeout_seconds=2.5
        ),
    )
    bot = _bot(runtime)
    message = _channel_message(guild_id=int(guild_id), content=content)
    message.id = 8001
    message.create_thread.return_value = _thread(thread_id=8001)

    await bot.on_message(message)
    await asyncio.gather(*bot._bg_tasks)  # pyright: ignore[reportPrivateUsage]

    message.create_thread.assert_awaited_once()
    created_name = message.create_thread.await_args.kwargs["name"]
    if not expects_naming:
        mock_generate_name.assert_not_awaited()
        assert created_name == "Chat with test-agent", (
            "naming off or nothing to title keeps the static title without a model call"
        )
        return
    assert created_name == "PyMC Divergences", (
        "the thread must be created under the generated title, not renamed into it"
    )
    mock_generate_name.assert_awaited_once()
    assert mock_generate_name.await_args is not None, "asserted awaited above"
    handed = mock_generate_name.await_args.kwargs
    assert handed["fallback"] == "Chat with test-agent", "the static title is the fallback"
    assert handed["message_text"] == "my PyMC model diverges", (
        "the opening message, minus the bot mention, is what gets titled"
    )
    assert handed["message_id"] == 8001, "metering is keyed on the opening message"
    assert (handed["tenant_id"], handed["platform_user_id"]) == (tenant_id, "555"), (
        "the naming call must be billed to the turn's tenant under the message author"
    )
    assert (handed["max_input_chars"], handed["timeout_seconds"]) == (321, 2.5), (
        "settings must reach the call unchanged"
    )
    assert handed["anthropic"] is runtime.anthropic, "the runtime client is reused"
