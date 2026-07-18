"""TC-1: Discord→turn→MA E2E test.

Drives the real DaimonBot.on_message → _orchestrate → run_turn path against a
transport-level fake MA (MARouter + SSE). run_turn is NOT stubbed — that is the
non-negotiable seam that catches routing/threading/wiring regressions.

Assertion surface: on terminal success DiscordTurnLifecycle.on_terminal_success
edits the thinking-embed message via message_ref.edit(content=..., embed=None) — it is
NOT a fresh thread.send. The final agent text must appear in message_ref.edit's
content kwarg.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from anthropic.types.beta.beta_managed_agents_session_agent import BetaManagedAgentsSessionAgent
from anthropic.types.beta.beta_managed_agents_session_stats import BetaManagedAgentsSessionStats
from anthropic.types.beta.beta_managed_agents_session_usage import BetaManagedAgentsSessionUsage
from anthropic.types.beta.sessions.beta_managed_agents_agent_message_event import (
    BetaManagedAgentsAgentMessageEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_end_turn import (
    BetaManagedAgentsSessionEndTurn,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import (
    BetaManagedAgentsSessionStatusIdleEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_text_block import (
    BetaManagedAgentsTextBlock,
)
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.config import McpSettings
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import tenant_ledger
from daimon.testing.factories import make_tenant
from daimon.testing.ma import (
    MARouter,
    build_fake_anthropic,
    list_response,
    send_events_response,
    sse_response,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio


_AGENT_TEXT = "Hello from the agent!"
_AGENT_ID = "ag_e2e_test"
_AGENT_ID_2 = "ag_e2e_retrieve"
_ENV_ID = "env_e2e_test"
_SESSION_ID = "sess_e2e_test"


def _make_fake_session() -> BetaManagedAgentsSession:
    """Construct a real BetaManagedAgentsSession for the create_session stub."""
    return BetaManagedAgentsSession(
        id=_SESSION_ID,
        agent=BetaManagedAgentsSessionAgent(
            id=_AGENT_ID_2,
            mcp_servers=[],
            model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6"),
            name="test-agent",
            skills=[],
            tools=[],
            type="agent",
            version=1,
        ),
        created_at="2026-06-14T00:00:00Z",
        environment_id=_ENV_ID,
        metadata={},
        resources=[],
        stats=BetaManagedAgentsSessionStats(),
        status="idle",
        type="session",
        updated_at="2026-06-14T00:00:00Z",
        usage=BetaManagedAgentsSessionUsage(),
        vault_ids=[],
        outcome_evaluations=[],  # pyright: ignore[reportCallIssue]
    )


def _build_router(tenant_id_str: str) -> MARouter:
    """Build a MARouter handling all routes the E2E exercises.

    Routes registered:
      GET  /v1/agents               — list for resolver tag lookup
      GET  /v1/agents/{id}          — retrieve for re-fetch after resolve
      GET  /v1/environments         — list for resolver tag lookup
      GET  /v1/environments/{id}    — retrieve for re-fetch after resolve
      POST /v1/sessions/{id}/events — send-initial event (run_turn)
      GET  /v1/sessions/{id}/events/stream — SSE turn stream (run_turn)
    """
    from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
    from anthropic.types.beta.beta_cloud_config import BetaCloudConfig
    from anthropic.types.beta.beta_packages import BetaPackages
    from anthropic.types.beta.beta_unrestricted_network import BetaUnrestrictedNetwork

    empty_cloud_config = BetaCloudConfig(
        type="cloud",
        networking=BetaUnrestrictedNetwork(type="unrestricted"),
        packages=BetaPackages(apt=[], cargo=[], gem=[], go=[], npm=[], pip=[]),
    )

    agent_item = BetaManagedAgentsAgent(
        id=_AGENT_ID,
        type="agent",
        name="test-agent",
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6"),
        metadata={
            MA_METADATA_KEY_TENANT: tenant_id_str,
            MA_METADATA_KEY_NAME: "test-agent",
        },
        description=None,
        created_at="2026-06-14T00:00:00Z",  # pyright: ignore[reportArgumentType]
        updated_at="2026-06-14T00:00:00Z",  # pyright: ignore[reportArgumentType]
        version=1,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    ).model_dump(mode="json")

    env_item = BetaEnvironment(
        id=_ENV_ID,
        type="environment",
        name="test-env",
        config=empty_cloud_config,
        metadata={
            MA_METADATA_KEY_TENANT: tenant_id_str,
            MA_METADATA_KEY_NAME: "test-env",
        },
        description="",
        created_at="2026-06-14T00:00:00Z",
        updated_at="2026-06-14T00:00:00Z",
    ).model_dump(mode="json")

    agent_message_event = BetaManagedAgentsAgentMessageEvent(
        id="evt_msg_e2e",
        type="agent.message",
        processed_at=datetime.now(UTC),
        content=[BetaManagedAgentsTextBlock(type="text", text=_AGENT_TEXT)],
    ).model_dump(mode="json")

    idle_event = BetaManagedAgentsSessionStatusIdleEvent(
        id="evt_idle_e2e",
        type="session.status_idle",
        processed_at=datetime.now(UTC),
        stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
    ).model_dump(mode="json")

    router = MARouter()

    # Resolver list endpoints
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([agent_item]))
    router.add(
        "GET",
        r"/v1/agents/[^/]+",
        lambda req, _m: httpx.Response(200, json=agent_item),
    )
    router.add("GET", r"/v1/environments", lambda req, _m: list_response([env_item]))
    router.add(
        "GET",
        r"/v1/environments/[^/]+",
        lambda req, _m: httpx.Response(200, json=env_item),
    )

    # run_turn endpoints
    router.add(
        "POST",
        r"/v1/sessions/[^/]+/events",
        lambda req, _m: send_events_response(),
    )
    router.add(
        "GET",
        r"/v1/sessions/[^/]+/events/stream",
        lambda req, _m: sse_response([agent_message_event, idle_event]),
    )

    return router


def _make_runtime(
    sessionmaker: async_sessionmaker[AsyncSession],
    router: MARouter,
) -> DiscordRuntime:
    """Build a DiscordRuntime wired to the MARouter-backed transport-level fake.

    Uses build_fake_anthropic so the real run_turn pumps the fake SSE instead
    of an AsyncMock client. This is the key difference from test_orchestration.py
    which uses AsyncMock — here run_turn is exercised for real.
    """
    settings = MagicMock()
    settings.mcp = McpSettings()
    settings.billing.markup = Decimal("1.0")
    settings.billing.signup_credit = Decimal("0")
    settings.crypto.keys = []  # no crypto keys → fernet=None → no repo injection
    discord_settings = MagicMock()
    discord_settings.max_concurrent_turns_per_tenant = 100
    settings.discord = discord_settings

    return DiscordRuntime(
        settings=settings,
        anthropic=build_fake_anthropic(router.dispatch),
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,  # billing disabled → is_over_cap always False
        deployment_default=DeploymentDefault(
            agent_name="test-agent",
            environment_name="test-env",
        ),
        resolver_cache=new_resolver_cache(),
    )


def _make_bot(runtime: DiscordRuntime) -> DaimonBot:
    """Build a DaimonBot with bot user id=999 and mentioned_in→True."""
    intents = discord.Intents.default()
    intents.message_content = True
    bot = DaimonBot(runtime=runtime, intents=intents)
    bot._connection.user = MagicMock(spec=discord.ClientUser)  # pyright: ignore[reportPrivateUsage]
    bot._connection.user.id = 999  # pyright: ignore[reportPrivateUsage]
    bot._connection.user.mentioned_in = MagicMock(return_value=True)  # pyright: ignore[reportPrivateUsage]
    return bot


def _make_thread_message(*, guild_id: int = 123456) -> discord.Message:
    """Mock a mention arriving inside an existing Discord thread.

    thread.send returns a MagicMock with a real .id=42 so that
    DiscordTurnLifecycle._message_ref is not None and the final edit can land
    on it (Pitfall 2 from 63-PATTERNS.md).
    """
    message = MagicMock(spec=discord.Message)
    message.content = "<@999> hello"
    message.author = MagicMock()
    message.author.bot = False
    message.author.id = 111
    message.author.display_name = "testuser"
    message.guild = MagicMock(spec=discord.Guild)
    message.guild.id = guild_id
    message.guild.owner_id = 222

    # Channel is a Discord Thread
    thread = MagicMock(spec=discord.Thread)
    thread.id = 5555
    thread.parent_id = 789

    # message_ref must carry a real .id so lifecycle._message_ref is non-None.
    # message_ref.edit must be an AsyncMock because _edit_message does `await msg.edit(...)`.
    message_ref = MagicMock()
    message_ref.id = 42
    message_ref.edit = AsyncMock()
    thread.send = AsyncMock(return_value=message_ref)

    message.channel = thread
    message.add_reaction = AsyncMock()
    message.attachments = []
    message.mentions = [SimpleNamespace(id=999)]
    # created_at needed by build_context_xml — not called (patched), but set for safety
    message.created_at = datetime(2026, 6, 14, tzinfo=UTC)

    return message


@patch("daimon.adapters.discord.bot.create_session")
@patch("daimon.adapters.discord.bot.build_context_xml")
async def test_discord_mention_delivers_agent_reply_via_edit(
    mock_build_context_xml: AsyncMock,
    mock_create_session: AsyncMock,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Discord mention drives real on_message→run_turn→lifecycle with fake MA SSE.

    The agent's final text "Hello from the agent!" is delivered by editing the
    thinking-embed message (message_ref.edit), NOT by a fresh thread.send. This
    asserts the complete on_message→_orchestrate→run_turn→on_terminal_success
    chain without stubbing run_turn (TC-1).
    """
    # Seed the tenant so the liveness gate passes (provision_status='ready' by default).
    tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")

    # Seed a balance credit so the is_over_balance gate passes.
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    await db_session.flush()
    await db_session.commit()

    router = _build_router(str(tenant.id))
    runtime = _make_runtime(db_session_factory, router)
    bot = _make_bot(runtime)
    message = _make_thread_message(guild_id=123456)
    thread = cast(MagicMock, message.channel)
    message_ref = cast(MagicMock, thread.send.return_value)

    # build_context_xml is patched to return a simple user message; the real
    # path we care about is run_turn, not XML context construction.
    mock_build_context_xml.return_value = ("<user_query>hello</user_query>", [])

    # create_session is boundary-stubbed: the heavy vault/cred
    # provisioning in sessions.py is not what TC-1 tests. run_turn is NOT stubbed.
    mock_create_session.return_value = _make_fake_session()

    await bot.on_message(message)

    # Assertion: the final agent text arrives via message_ref.edit(content=...)
    # not via a fresh thread.send (verified lifecycle.py:215-220).
    edit_calls = [c for c in message_ref.edit.call_args_list if "content" in c.kwargs]
    assert edit_calls, (
        "expected at least one message_ref.edit call with content kwarg "
        "(terminal success should edit the thinking embed)"
    )
    assert edit_calls[-1].kwargs["content"] == _AGENT_TEXT, (
        "terminal success edits the embed message to the agent's final text"
    )
