"""Drive cold platform entry points and DM delivery with existing fake builders."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import httpx
import pytest
from aioresponses import aioresponses as AioResponsesMock
from anthropic import AsyncAnthropic
from cryptography.fernet import Fernet
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.commands.direct_messages import DirectMessageCog
from daimon.adapters.slack.app import SlackApp
from daimon.core.config import McpSettings
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.core.turn import driver as turn_driver
from daimon.core.turn.ceiling import CEILING_MESSAGE
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.termination import TerminationReason
from daimon.testing.effect_recorder import FakeClock
from daimon.testing.factories import make_ledger_entry, make_tenant
from daimon.testing.ma import combine_handlers, make_fake_memory_store_handler
from daimon.testing.ma_models import ma_session
from daimon.testing.turn_fakes import BlockForever, FakeAnthropic, RecordingLifecycle
from daimon.testing.turn_router import build_turn_router
from http_turn import EventBytes, HttpTurnFixtures

NOW = datetime(2026, 10, 9, tzinfo=UTC)


async def test_ceiling_after_http_setup_closes_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """A virtual deadline expires only after actual SDK setup and first body read.

    The timeout provider cancels and drains the awaited coroutine exactly as
    wait_for does. The production driver owns ceiling classification/delivery.
    No race against SDK import latency or machine wall-clock scheduling exists.
    """
    if "oracle_plugin" not in sys.modules:
        HttpTurnFixtures(monkeypatch)
    fake = FakeAnthropic()
    fake.beta.sessions.events.stream_scripts = [[BlockForever()]]
    lifecycle = RecordingLifecycle()
    clock = FakeClock(NOW)
    reading = asyncio.Event()
    original_iteration = EventBytes.__aiter__

    async def read_started(self: EventBytes) -> AsyncIterator[bytes]:
        reading.set()
        async for chunk in original_iteration(self):
            yield chunk

    monkeypatch.setattr(EventBytes, "__aiter__", read_started)
    original_asyncio = cast(Any, turn_driver).asyncio
    timeout_calls: list[float] = []

    class DeadlineClock:
        def __getattr__(self, name: str) -> Any:
            return getattr(original_asyncio, name)

        async def wait_for(self, coroutine: Any, *, timeout: float) -> Any:
            timeout_calls.append(timeout)
            task = asyncio.create_task(coroutine)
            ready = asyncio.create_task(reading.wait())
            try:
                done, _ = await asyncio.wait({task, ready}, return_when=asyncio.FIRST_COMPLETED)
                if task in done:
                    return task.result()
                clock.advance(timeout)
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                raise TimeoutError
            finally:
                ready.cancel()
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await ready
                with suppress(asyncio.CancelledError):
                    await task

    monkeypatch.setattr(turn_driver, "asyncio", DeadlineClock())
    final = await asyncio.wait_for(
        turn_driver.run_turn(
            anthropic=cast(AsyncAnthropic, fake),
            session_id="sess_ceiling",
            user_message="hi",
            lifecycle=lifecycle,
            cancel=asyncio.Event(),
            billing=BillingExempt(reason="cli-operator-run"),
            now=clock.now,
            deadline=NOW + timedelta(seconds=60),
        ),
        timeout=30,
    )
    assert timeout_calls == [60]
    assert clock.current == NOW + timedelta(seconds=60)
    assert final.error is not None and final.error.kind == "ceiling"
    assert final.error.message == CEILING_MESSAGE
    assert final.termination == TerminationReason.CEILING
    assert len(lifecycle.terminal_failures) == 1
    assert lifecycle.terminal_success == []
    assert fake.beta.sessions.events.stream_calls == 1
    assert fake.beta.sessions.events.streams[0].closed
    assert len(fake.beta.sessions.events.sent_events) == 1
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name() in {"turn.stream_open", "turn.send_initial", "turn.stream_next"}
        and not task.done()
    ]


@pytest.mark.parametrize("platform", ["discord", "slack"])
async def test_cold_thread_feedback_before_session_create(
    platform: Literal["discord", "slack"],
    db_session: Any,
    db_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module: Any = importlib.import_module(f"parity.drivers.{platform}_driver")
    driver: Any = getattr(module, f"{platform.title()}Driver")()
    workspace = "900001001" if platform == "discord" else "T900001001"
    user = "555000111" if platform == "discord" else "U555000111"
    channel = "100000" if platform == "discord" else "C100000"
    tenant = await make_tenant(db_session, platform=platform, workspace_id=workspace)
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("100"))
    await db_session.commit()
    router = build_turn_router(str(tenant.id), session_id="sess_cold_thread")
    turn_router: Any = importlib.import_module("daimon.testing.turn_router")
    original_events = turn_router.turn_events

    def dated_events(**kwargs: Any) -> Any:
        return original_events(**kwargs, now=NOW - timedelta(seconds=90))

    monkeypatch.setattr(turn_router, "turn_events", dated_events)
    created: list[dict[str, Any]] = []

    def create(request: httpx.Request, match: Any) -> httpx.Response:
        body = json.loads(request.content)
        created.append(body)
        session = ma_session(
            id="sess_cold_thread",
            agent_id="ag_parity_test",
            environment_id="env_parity_test",
            resources=body.get("resources", []),
            metadata=body.get("metadata", {}),
        )
        router.add_session(session, with_archive=True)
        return httpx.Response(200, json=session.model_dump(mode="json"))

    router.add("POST", r"/v1/sessions", create)
    boundary = SimpleNamespace(
        dispatch=combine_handlers(make_fake_memory_store_handler(), router.dispatch)
    )
    if platform == "discord":
        runtime = driver._make_runtime(db_session_factory, boundary)
    else:
        key = Fernet.generate_key().decode()
        fernet = build_multifernet((key,))
        async with db_session_factory() as session:
            await upsert_slack_bot_token(
                session,
                team_id=workspace,
                encrypted_token=encrypt_token(fernet, "xoxb-parity-test"),
            )
            await session.commit()
        runtime = driver._make_runtime(db_session_factory, boundary, fernet_key=key)
    runtime.settings.completion_pings = {tenant.id: True}
    deps = replace(
        runtime.turn_deps,
        defaults_root=Path("/nonexistent"),
        mcp=McpSettings(),
        public_url=None,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        agent_github_app=None,
    )
    runtime = replace(runtime, turn_deps=deps)
    if platform == "discord":
        bot = driver._make_bot(runtime)
        message = driver._make_message(
            workspace_id=workspace, channel_id=channel, user_id=user, text="cold hello"
        )
        message.id = 12345
        message.remove_reaction = AsyncMock(return_value=None)
        message.guild.me = SimpleNamespace(id=999)
        with patch(
            "daimon.adapters.discord.bot.build_context_xml",
            return_value=("<user_query>cold hello</user_query>", []),
        ):
            await bot.on_message(message)
    else:
        app: Any = SlackApp(runtime=runtime)
        with AioResponsesMock() as mock:
            module._register_slack_defaults(mock)
            await app._handle_app_mention(
                {
                    "type": "app_mention",
                    "channel": channel,
                    "event_ts": "1791504000.000000",
                    "ts": "1791504000.000000",
                    "user": user,
                    "text": "<@U_BOT> cold hello",
                },
                team_id=workspace,
            )
    assert len(created) == 1


async def test_dm_reply_delivery_and_deduplication(
    db_session: Any, db_session_factory: Any
) -> None:
    existing: Any = importlib.import_module("integration.test_direct_messages")
    tenant, deps, admission, _sent, streams, created = await existing._setup(
        db_session, db_session_factory
    )
    await existing._start(tenant, deps, admission)
    runtime = MagicMock()
    runtime.sessionmaker = db_session_factory
    runtime.turn_deps = deps
    runtime.settings.discord.max_concurrent_turns = 10
    bot: Any = DaimonBot(runtime=runtime, intents=discord.Intents.default())
    guild = MagicMock(spec=discord.Guild)
    guild.owner_id = 1
    member = MagicMock(spec=discord.Member)
    member.id = 42
    member.roles = []
    member.guild_permissions.administrator = False
    member.guild_permissions.manage_guild = False
    guild.fetch_member = AsyncMock(return_value=member)
    channel = MagicMock(spec=discord.DMChannel)
    channel.id = "dm-42"
    channel.send = AsyncMock(return_value=None)

    @asynccontextmanager
    async def typing() -> AsyncIterator[None]:
        yield

    channel.typing = typing

    def message(message_id: int, content: str) -> Any:
        result = MagicMock(spec=discord.Message)
        result.author.bot = False
        result.author.id = 42
        result.channel = channel
        result.id = message_id
        result.content = content
        return result

    with patch.object(bot, "get_guild", return_value=guild):
        cog = DirectMessageCog(bot)
        await cog.on_message(message(1, "private first question"))
        await cog.on_message(message(2, "private follow-up"))
        await cog.on_message(message(2, "private follow-up"))
    assert len(streams) == 2
    assert len(created) == 1
    assert channel.send.await_count == 2
    assert bot._global_inflight == 0
