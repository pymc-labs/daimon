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

import asyncio
import json
import os
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import httpx
from daimon.adapters.discord import theme
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.agent_post_identity import fallback_name_prefix
from daimon.core.config import McpSettings, RecruitmentSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import tenant_ledger
from daimon.core.stores.thread_sessions import list_orphaned_turns
from daimon.core.turn.deps import build_turn_deps
from daimon.testing import build_turn_router
from daimon.testing.db import build_test_engine
from daimon.testing.factories import make_tenant
from daimon.testing.membership import FixtureMembershipResolver
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic
from mux.testing.drivers.anthropic.ma_models import ma_session
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_AGENT_TEXT = "Hello from the agent!"
_AGENT_ID = "ag_e2e_test"
_AGENT_ID_2 = "ag_e2e_retrieve"
_ENV_ID = "env_e2e_test"
_SESSION_ID = "sess_e2e_test"


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
    settings.recruitment = RecruitmentSettings()
    settings.agent_identity.enabled = True
    settings.mcp = McpSettings()
    settings.billing.markup = Decimal("1.0")
    settings.billing.signup_credit = Decimal("0")
    settings.crypto.keys = []  # no crypto keys → fernet=None → no repo injection
    discord_settings = MagicMock()
    discord_settings.max_concurrent_turns_per_tenant = 100
    discord_settings.thread_open_notice_after_s = 3.0
    settings.discord = discord_settings

    anthropic = build_fake_anthropic(router.dispatch)
    resolver_cache = new_resolver_cache()
    deployment_default = DeploymentDefault(
        agent_name="test-agent",
        environment_name="test-env",
    )
    return DiscordRuntime(
        settings=settings,
        anthropic=anthropic,
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,  # billing disabled → is_over_cap always False
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


def _make_bot(runtime: DiscordRuntime) -> DaimonBot:
    """Build a DaimonBot with bot user id=999 and mentioned_in→True."""
    intents = discord.Intents.default()
    intents.message_content = True
    bot = DaimonBot(runtime=runtime, intents=intents)
    bot.membership_resolver = FixtureMembershipResolver("111")  # type: ignore[assignment]
    bot.runtime = replace(
        bot.runtime,
        turn_deps=replace(bot.runtime.turn_deps, membership_resolver=bot.membership_resolver),
    )
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
    message_ref.add_reaction = AsyncMock()
    thread.send = AsyncMock(return_value=message_ref)

    message.channel = thread
    message.add_reaction = AsyncMock()
    message.attachments = []
    message.mentions = [SimpleNamespace(id=999)]
    # created_at needed by build_context_xml — not called (patched), but set for safety
    message.created_at = datetime(2026, 6, 14, tzinfo=UTC)

    return message


@patch("daimon.core.turn.prepare.create_session")
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

    router = build_turn_router(
        str(tenant.id),
        agent_id=_AGENT_ID,
        env_id=_ENV_ID,
        agent_text=_AGENT_TEXT,
        usage_event_id=None,
    )
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
    mock_create_session.return_value = ma_session(
        id=_SESSION_ID, agent_id=_AGENT_ID_2, environment_id=_ENV_ID
    )

    tracked_intents: list[uuid.UUID] = []
    original_track = bot._track_live_turn_card  # pyright: ignore[reportPrivateUsage]

    def track_live(intent_id: uuid.UUID) -> None:
        original_track(intent_id)
        assert intent_id in bot._live_turn_card_intent_ids  # pyright: ignore[reportPrivateUsage]
        tracked_intents.append(intent_id)

    with patch.object(bot, "_track_live_turn_card", side_effect=track_live):
        await bot.on_message(message)
    assert len(tracked_intents) == 1

    # Assertion: the final agent text arrives via message_ref.edit(content=...)
    # not via a fresh thread.send (verified lifecycle.py:215-220).
    edit_calls = [c for c in message_ref.edit.call_args_list if "content" in c.kwargs]
    assert edit_calls, (
        "expected at least one message_ref.edit call with content kwarg "
        "(terminal success should edit the thinking embed)"
    )
    assert edit_calls[-1].kwargs["content"] == fallback_name_prefix("test-agent", _AGENT_TEXT), (
        "terminal success edits the embed message to the agent's labelled final text"
    )


class _HeldStream(httpx.AsyncByteStream):
    """An SSE stream MA opened and never writes to: the turn is running."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        await asyncio.Event().wait()
        yield b""  # pragma: no cover - never reached


_GONE = {"type": "error", "error": {"type": "not_found_error", "message": "session gone"}}


@patch("daimon.adapters.discord.bot.build_context_xml")
async def test_a_drain_during_mention_recovery_leaves_the_replacement_recoverable(
    mock_build_context_xml: AsyncMock,
    db_session: AsyncSession,
    db_schema: str,
) -> None:
    """Re-review #823, mention path (the continuation path shares its
    bookkeeping): the first session is gone, the replacement accepts the turn
    and keeps running, and the drain cuts it. The marker must name the
    replacement so the next boot interrupts the session actually running."""
    engine = build_test_engine(
        os.environ["DAIMON_DATABASE__TEST_URL"], db_schema, pool_size=4, max_overflow=0
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    await db_session.commit()

    attempts: list[tuple[str, list[str]]] = []
    accepted = asyncio.Event()
    router = build_turn_router(
        str(tenant.id), agent_id=_AGENT_ID, env_id=_ENV_ID, agent_text=_AGENT_TEXT
    )
    router.add("GET", r"/v1/sessions/[^/]+/events", lambda _r, _m: httpx.Response(404, json=_GONE))
    for index, (method, pattern, handler) in enumerate(router.routes):
        if method == "GET" and pattern.pattern.endswith("/events/stream"):
            router.routes[index] = (
                method,
                pattern,
                lambda request, _m: (
                    httpx.Response(404, json=_GONE)
                    if "/sess_gone/" in request.url.path
                    else httpx.Response(
                        200, headers={"content-type": "text/event-stream"}, stream=_HeldStream()
                    )
                ),
            )
        elif method == "POST" and pattern.pattern.endswith("/events"):

            def record(request: httpx.Request, match: re.Match[str], handler=handler):
                attempts.append(
                    (
                        request.url.path.split("/")[-2],
                        [event["type"] for event in json.loads(request.content)["events"]],
                    )
                )
                if "/sess_gone/" in request.url.path:
                    return httpx.Response(404, json=_GONE)
                response = handler(request, match)
                accepted.set()
                return response

            router.routes[index] = (method, pattern, record)

    runtime = _make_runtime(factory, router)
    bot = _make_bot(runtime)
    bot.close = AsyncMock()  # type: ignore[method-assign]
    message = _make_thread_message(guild_id=123456)
    thread = cast(MagicMock, message.channel)
    mock_build_context_xml.return_value = ("<user_query>hello</user_query>", [])
    try:
        with (
            patch(
                "daimon.core.turn.prepare.create_session",
                side_effect=[
                    ma_session(id=sid, agent_id=_AGENT_ID_2, environment_id=_ENV_ID)
                    for sid in ("sess_gone", "sess_replacement")
                ],
            ),
            patch("daimon.adapters.discord.bot._DRAIN_GRACE_S", 0.1),
        ):
            turn = asyncio.create_task(bot.on_message(message))
            await asyncio.wait_for(accepted.wait(), timeout=10.0)
            await asyncio.wait_for(bot._drain_and_close(), timeout=15.0)  # pyright: ignore[reportPrivateUsage]
            _, still_running = await asyncio.wait({turn}, timeout=5.0)
        assert not still_running, "the drain cancels the mention turn"

        async with factory() as session:
            markers = await list_orphaned_turns(session, platform="discord")
        assert [(m.ma_session_id, m.active_turn_message_id) for m in markers] == [
            ("sess_replacement", "42")
        ], "the marker moved onto the replacement before its turn was sent"
        assert markers[0].active_turn_owner_key == runtime.owner_key

        card = cast(MagicMock, thread.send.return_value)
        card.webhook_id = None
        card.embeds = [
            discord.Embed(title="Working on it…", color=discord.Color(theme.COLOR_IN_PROGRESS))
        ]
        card.attachments = []
        card.edit = AsyncMock(return_value=card)
        thread.fetch_message = AsyncMock(return_value=card)
        successor = _make_bot(runtime)
        successor._orphan_recovery_armed = True  # pyright: ignore[reportPrivateUsage]
        successor.get_channel = MagicMock(return_value=thread)  # type: ignore[method-assign]
        await successor._retire_orphaned_turns()  # pyright: ignore[reportPrivateUsage]
        background = list(successor._bg_tasks)  # pyright: ignore[reportPrivateUsage]
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)

        assert attempts[-2:] == [
            ("sess_replacement", ["user.message"]),
            ("sess_replacement", ["user.interrupt"]),
        ], "boot interrupts the running replacement, not its dead predecessor"
        assert ("sess_gone", ["user.interrupt"]) not in attempts
        async with factory() as session:
            assert await list_orphaned_turns(session, platform="discord") == []
    finally:
        await engine.dispose()
