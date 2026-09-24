"""The boot sweep that lays to rest turns whose process died mid-flight.

A turn's render loop lives in the process that started it, so a container
recreate freezes its embed on 'thinking' forever while MA completes the answer
server-side. These cover the sweep itself — an embed it can edit, one it
cannot, and the reconnect that must not trigger it. The marker's own semantics
live in the store tests.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
from anthropic import AsyncAnthropic
from daimon.adapters.discord import theme
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.config import McpSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.thread_sessions import (
    create_thread_session,
    list_orphaned_turns,
    mark_turn_active,
)
from daimon.testing import build_fake_anthropic, ma_session
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _make_bot(
    sessionmaker: async_sessionmaker[AsyncSession], *, anthropic: AsyncAnthropic | None = None
) -> DaimonBot:
    settings = MagicMock()
    settings.mcp = McpSettings()
    discord_settings = MagicMock()
    discord_settings.max_concurrent_turns_per_tenant = 3
    settings.discord = discord_settings
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=anthropic if anthropic is not None else AsyncMock(),
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # the sweep never runs a turn
    )
    intents = discord.Intents.default()
    intents.message_content = True
    return DaimonBot(runtime=runtime, intents=intents)


async def _make_orphan(
    session: AsyncSession,
    *,
    thread_id: str = "555",
    message_id: str = "777",
    ma_session_id: str = "sesn_test",
) -> uuid.UUID:
    tenant = await make_tenant(session)
    row = await create_thread_session(
        session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id=thread_id,
        account_id=uuid.uuid4(),
        ma_session_id=ma_session_id,
    )
    await mark_turn_active(
        session,
        id=row.id,
        active_turn_message_id=message_id,
        now=datetime.now(UTC),
    )
    await session.commit()
    return row.id


_EVENTS_PATH = re.compile(r"^/v1/sessions/(?P<sid>[^/]+)/events$")


class _InterruptRecorder:
    """An MA transport recording every event sent to a session; sessions in
    ``failing`` answer 404, as MA does for a session that no longer exists."""

    def __init__(self, *, failing: frozenset[str] = frozenset()) -> None:
        self.sent: list[tuple[str, str]] = []
        self._failing = failing

    def __call__(self, request: httpx.Request) -> httpx.Response:
        match = _EVENTS_PATH.match(request.url.path)
        not_found = {"type": "error", "error": {"type": "not_found_error", "message": "not found"}}
        if request.method != "POST" or match is None:
            return httpx.Response(404, json=not_found)
        sid = match["sid"]
        for event in json.loads(request.content)["events"]:
            self.sent.append((sid, event["type"]))
        if sid in self._failing:
            return httpx.Response(404, json=not_found)
        return httpx.Response(
            200,
            json={"data": [{"id": "sevt_1", "type": "user.interrupt", "processed_at": None}]},
        )


def _reachable_thread() -> MagicMock:
    message = MagicMock(spec=discord.Message)
    message.edit = AsyncMock()
    thread = MagicMock(spec=discord.Thread)
    thread.fetch_message = AsyncMock(return_value=message)
    return thread


async def test_sweep_interrupts_the_orphaned_turns_ma_session(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The embed says the turn was interrupted; the MA session must stop too.

    Left running, the dead turn keeps billing, and the next mention reuses the
    session and sends its user.message into a still-running session, which MA
    answers with 200 and ignores.
    """
    await _make_orphan(db_session, ma_session_id="sesn_orphan")
    recorder = _InterruptRecorder()
    bot = _make_bot(db_session_factory, anthropic=build_fake_anthropic(recorder))
    bot.get_channel = MagicMock(return_value=_reachable_thread())  # pyright: ignore[reportAttributeAccessIssue]

    await bot._retire_orphaned_turns()  # pyright: ignore[reportPrivateUsage]

    assert recorder.sent == [("sesn_orphan", "user.interrupt")], (
        "the orphaned turn's MA session must be interrupted exactly once"
    )
    assert await list_orphaned_turns(db_session, platform="discord") == []


async def test_a_failed_interrupt_does_not_stop_the_sweep(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _make_orphan(db_session, thread_id="555", message_id="777", ma_session_id="sesn_gone")
    await _make_orphan(db_session, thread_id="556", message_id="778", ma_session_id="sesn_alive")
    recorder = _InterruptRecorder(failing=frozenset({"sesn_gone"}))
    bot = _make_bot(db_session_factory, anthropic=build_fake_anthropic(recorder))
    bot.get_channel = MagicMock(return_value=_reachable_thread())  # pyright: ignore[reportAttributeAccessIssue]

    await bot._retire_orphaned_turns()  # pyright: ignore[reportPrivateUsage]  # must not raise

    assert sorted(recorder.sent) == [
        ("sesn_alive", "user.interrupt"),
        ("sesn_gone", "user.interrupt"),
    ], "one session's failed interrupt must not skip the other's"
    assert await list_orphaned_turns(db_session, platform="discord") == []


async def test_sweep_marks_the_embed_failed_and_clears_the_row(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _make_orphan(db_session)
    bot = _make_bot(db_session_factory)
    message = MagicMock(spec=discord.Message)
    message.edit = AsyncMock()
    thread = MagicMock(spec=discord.Thread)
    thread.fetch_message = AsyncMock(return_value=message)
    bot.get_channel = MagicMock(return_value=thread)  # pyright: ignore[reportAttributeAccessIssue]

    await bot._retire_orphaned_turns()  # pyright: ignore[reportPrivateUsage]

    thread.fetch_message.assert_awaited_once_with(777)
    edited = message.edit.await_args.kwargs["embed"]  # pyright: ignore[reportAny]
    assert edited.color.value == theme.COLOR_RED, "an interrupted turn must render as an error"
    assert "interrupted" in edited.description, (
        "the user must be told the turn is dead, not left on a frozen spinner"
    )
    assert await list_orphaned_turns(db_session, platform="discord") == [], (
        "a retired turn must not be retired again on the next boot"
    )


async def test_sweep_clears_the_row_even_when_the_embed_is_unreachable(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A deleted message or lost permission must not make the sweep retry forever."""
    await _make_orphan(db_session)
    bot = _make_bot(db_session_factory)
    thread = MagicMock(spec=discord.Thread)
    thread.fetch_message = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "Unknown Message")
    )
    bot.get_channel = MagicMock(return_value=thread)  # pyright: ignore[reportAttributeAccessIssue]

    await bot._retire_orphaned_turns()  # pyright: ignore[reportPrivateUsage]

    assert await list_orphaned_turns(db_session, platform="discord") == [], (
        "an unreachable embed must still clear its marker"
    )


async def test_sweep_runs_once_per_process_not_once_per_reconnect(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """on_ready re-fires on a full gateway reconnect; a live turn is not an orphan."""
    await _make_orphan(db_session)
    bot = _make_bot(db_session_factory)
    message = MagicMock(spec=discord.Message)
    message.edit = AsyncMock()
    thread = MagicMock(spec=discord.Thread)
    thread.fetch_message = AsyncMock(return_value=message)
    bot.get_channel = MagicMock(return_value=thread)  # pyright: ignore[reportAttributeAccessIssue]

    await bot._retire_orphaned_turns()  # pyright: ignore[reportPrivateUsage]
    # A turn started after the sweep — its marker belongs to THIS process.
    mapping_id = await _make_orphan(db_session, thread_id="556", message_id="778")
    await bot._retire_orphaned_turns()  # pyright: ignore[reportPrivateUsage]

    assert [row.id for row in await list_orphaned_turns(db_session, platform="discord")] == [
        mapping_id
    ], "a reconnect must not reap the turns this process is still rendering"


async def test_sweep_does_not_clear_a_marker_rewritten_during_message_edit(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A turn that starts while the old card is being retired owns its new marker."""
    mapping_id = await _make_orphan(db_session)
    recorder = _InterruptRecorder()
    bot = _make_bot(db_session_factory, anthropic=build_fake_anthropic(recorder))
    message = MagicMock(spec=discord.Message)

    async def _start_new_turn(**_kwargs: object) -> None:
        await mark_turn_active(
            db_session,
            id=mapping_id,
            active_turn_message_id="778",
            now=datetime.now(UTC),
        )
        await db_session.commit()

    message.edit = AsyncMock(side_effect=_start_new_turn)
    thread = MagicMock(spec=discord.Thread)
    thread.fetch_message = AsyncMock(return_value=message)
    bot.get_channel = MagicMock(return_value=thread)  # pyright: ignore[reportAttributeAccessIssue]

    await bot._retire_orphaned_turns()  # pyright: ignore[reportPrivateUsage]

    assert recorder.sent == [], (
        "a session whose marker moved is running a live turn and must not be interrupted"
    )

    orphans = await list_orphaned_turns(db_session, platform="discord")
    assert [row.id for row in orphans] == [mapping_id], (
        "the boot sweep must not clear a marker written after its orphan snapshot"
    )
    assert orphans[0].active_turn_message_id == "778", (
        "the marker that belongs to the live turn must survive the old-card edit"
    )


async def test_ceiling_outcome_still_clears_the_active_turn_marker(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A `run_prepared_turn` outcome carrying a `"ceiling"` error takes the
    same `finally`-block path as any other outcome (T-19-04-A): the
    active-turn marker is cleared and no watermark is written. Core already
    marks the session mapping dead and renders the terminal-failure embed on
    a ceiling breach (19-03) -- the adapter no longer does any of that
    itself, it only owns clearing the marker.
    """
    from decimal import Decimal
    from unittest.mock import patch

    from daimon.core.errors import TurnError
    from daimon.core.stores import tenant_ledger
    from daimon.core.stores.thread_sessions import get_latest_thread_session
    from daimon.core.turn.run import RunOutcome
    from daimon.core.turn.state import TurnState
    from daimon.testing.factories import make_tenant

    from .harness import make_bot
    from .test_orchestration import (
        _make_channel_message,  # pyright: ignore[reportPrivateUsage]
        _make_runtime,  # pyright: ignore[reportPrivateUsage]
        _stub_resolved_config,  # pyright: ignore[reportPrivateUsage]
    )

    tenant = await make_tenant(db_session, platform="discord", workspace_id="123456")
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    await db_session.flush()
    await db_session.commit()

    runtime = _make_runtime(tenant.id, db_session_factory)
    bot = make_bot(runtime)
    message = _make_channel_message()
    mock_thread = MagicMock(spec=discord.Thread)
    mock_thread.id = 9999
    mock_thread.send = AsyncMock()
    message.create_thread.return_value = mock_thread  # pyright: ignore[reportAttributeAccessIssue]

    ceiling_outcome = RunOutcome(
        state=TurnState(error=TurnError(kind="ceiling", message="this turn stopped responding")),
        ma_session_id="sess-ceiling",
        mapping_id=None,
        recovered=False,
    )

    with (
        patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock) as mock_resolve,
        patch(
            "daimon.core.turn.prepare.create_session", new_callable=AsyncMock
        ) as mock_create_session,
        patch(
            "daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock
        ) as mock_find_agent,
        patch(
            "daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock
        ) as mock_find_env,
        patch(
            "daimon.adapters.discord.bot.run_prepared_turn", new_callable=AsyncMock
        ) as mock_run_prepared_turn,
    ):
        mock_resolve.return_value = _stub_resolved_config()
        mock_create_session.return_value = ma_session(id="sess-ceiling")
        mock_find_agent.return_value = "ag_test"
        mock_find_env.return_value = "env_test"
        mock_run_prepared_turn.return_value = ceiling_outcome

        await bot.on_message(message)

    mock_run_prepared_turn.assert_called_once()
    call_kwargs = mock_run_prepared_turn.call_args.kwargs
    assert "deadline" in call_kwargs, "run_prepared_turn must be given the shared core deadline"

    async with db_session_factory() as session:
        mapping = await get_latest_thread_session(
            session, tenant_id=tenant.id, platform="discord", thread_id="9999"
        )
    assert mapping is not None, "bind_session must still have created the mapping row"
    assert mapping.active_turn_message_id is None, (
        "a ceiling outcome must still clear the active-turn marker via the finally block"
    )
    assert mapping.watermark_message_id is None, (
        "a ceiling outcome carries state.error is not None, so the watermark must never be written"
    )
