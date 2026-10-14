"""A turn whose own thread is deleted or closed mid-answer (#809).

Production (f5c665762, 2026-10-11): a channel mention opened a thread, the thread
was removed on Discord's side 80 s into the turn, every write to it failed with
10003 Unknown Channel, and the bot ended by posting an error into the PARENT
channel. These tests run the real `on_message` -> orchestration -> driver path
against a transport-level fake of Managed Agents, so a Stop is a real
`user.interrupt` and billing is a real usage row. Only Discord is faked.
"""

from __future__ import annotations

import asyncio
import types
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import discord
import httpx
import pytest
import structlog
from anthropic.types.beta import BetaManagedAgentsSession
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.post_transport import DiscordPostTransport
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.discord.thread_gone import (
    ThreadStopped,
    ThreadStops,
    TurnThreadWatch,
    stop_reason_of,
)
from daimon.core.config import McpSettings, ThreadNamingSettings
from daimon.core.ma import interrupt_orphaned_session
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault, ResolvedConfig
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.stores.thread_sessions import list_live_thread_sessions
from daimon.core.stores.turn_outcomes import list_for_tenant as list_outcomes
from daimon.core.turn.deps import build_turn_deps
from daimon.core.turn.outcomes import drain_outcomes
from daimon.core.turn.termination import TerminationReason
from daimon.testing import MARouter, build_fake_anthropic, json_body, ma_session
from daimon.testing.db import db_nullpool_engine as db_nullpool_engine
from daimon.testing.turn_fakes import deps_for
from daimon.testing.turn_router import build_turn_router, turn_events
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .conftest import make_tenant
from .harness import make_bot

_GUILD_ID = 123456
_THREAD_ID = 9999
_PARENT_ID = 789
_MA_SESSION = "sess-809"
_USAGE_EVENT = "evt_usage_809"


def _http(code: int, *, status: int = 400, cls: type[discord.HTTPException] | None = None):
    """A Discord API error carrying `code`; production saw 10003 as a 400 and as a 404."""
    response = Mock(status=status, reason="Discord")
    return (cls or discord.HTTPException)(response, {"code": code, "message": f"code {code}"})


class _AsyncIter:
    def __init__(self, items: list[object]) -> None:
        self._items = iter(items)

    def __aiter__(self) -> _AsyncIter:
        return self

    async def __anext__(self) -> object:
        try:
            return next(self._items)
        except StopIteration as err:
            raise StopAsyncIteration from err


class _SSE(httpx.AsyncByteStream):
    """An SSE body that stays open until `release` is set, like a turn still running."""

    def __init__(
        self,
        events: list[dict[str, Any]],
        release: asyncio.Event | None,
        drained: asyncio.Event | None = None,
        after_release: list[dict[str, Any]] | None = None,
    ) -> None:
        self._after_release = after_release or []
        self._events = events
        self._release = release
        self._drained = drained

    async def __aiter__(self):
        import json

        for event in self._events:
            yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
        if self._drained is not None:
            self._drained.set()  # the turn's events are in; it now keeps running
        if self._release is not None:
            await self._release.wait()
            for event in self._after_release:
                yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


@dataclass
class _World:
    """One bot, one thread and one scripted Managed Agents turn."""

    bot: DaimonBot
    sessionmaker: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID
    channel_message: Any
    thread: Any
    card: Any
    release_stream: asyncio.Event
    stream_opened: asyncio.Event
    sent_to_ma: list[dict[str, Any]]
    #: Called when the question reaches MA, before the driver has read any event.
    on_user_message: list[Callable[[], None]]
    #: What the fake thread answers a write with, or None while it is healthy.
    thread_error: Callable[[bool], Exception | None] = lambda _is_send: None
    edit_attempts: int = 0
    failed_writes: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def break_thread(self, error: Callable[[bool], Exception | None]) -> None:
        self.thread_error = error

    def interrupts(self) -> int:
        return sum(
            1
            for body in self.sent_to_ma
            for event in body.get("events", [])
            if event.get("type") == "user.interrupt"
        )

    def user_messages(self) -> int:
        return sum(
            1
            for body in self.sent_to_ma
            for event in body.get("events", [])
            if event.get("type") == "user.message"
        )


def _gone(is_send: bool) -> Exception:
    # The two shapes production logged: a 400 on the card edit, a 404 on a send.
    return _http(10003, status=404, cls=discord.NotFound) if is_send else _http(10003)


def _closed_for_sends(is_send: bool) -> Exception:
    """The card is missing (10008), so its replacement send runs, and that send is refused 50083."""
    return _http(50083) if is_send else _http(10008, status=404, cls=discord.NotFound)


async def _world(
    db_session: AsyncSession,
    db_nullpool_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> _World:
    # One connection per query: the turn and a queued message talk to the database at once.
    db_session_factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    tenant = await make_tenant(db_session, platform="discord", workspace_id=str(_GUILD_ID))
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    await db_session.commit()

    release = asyncio.Event()
    opened = asyncio.Event()
    sent: list[dict[str, Any]] = []
    on_user_message: list[Callable[[], None]] = []
    streams = 0
    events = turn_events(agent_text="partial answer", usage_event_id=_USAGE_EVENT)
    working, idle = events[:-1], events[-1:]

    router = MARouter()

    def send_events(request: httpx.Request, _match: Any) -> httpx.Response:
        body = json_body(request)
        sent.append(body)
        if any(event.get("type") == "user.message" for event in body.get("events", [])):
            for hook in on_user_message:
                hook()
        return httpx.Response(200, json={"data": None})

    def stream(_request: httpx.Request, _match: Any) -> httpx.Response:
        nonlocal streams
        streams += 1
        headers = {"content-type": "text/event-stream"}
        if streams == 1:
            # The turn: some work, then it keeps running until `release` (or a Stop).
            return httpx.Response(200, headers=headers, stream=_SSE(working, release, opened, idle))
        # A Stop's interrupt waits on this stream for the session to go idle; a
        # turn that was let run to its end reads the same idle from here.
        return httpx.Response(200, headers=headers, stream=_SSE(idle, None))

    router.add("POST", r"/v1/sessions/(?P<session_id>[^/]+)/events", send_events)
    router.add("GET", r"/v1/sessions/(?P<session_id>[^/]+)/events/stream", stream)
    build_turn_router(str(tenant.id), router=router)
    anthropic = build_fake_anthropic(router.dispatch)

    settings = MagicMock()
    settings.mcp = McpSettings()
    settings.billing.markup = Decimal("1.0")
    settings.billing.signup_credit = Decimal("0")
    discord_settings = MagicMock()
    discord_settings.max_concurrent_turns_per_tenant = 100
    discord_settings.thread_open_notice_after_s = 3.0
    settings.discord = discord_settings
    settings.thread_naming = ThreadNamingSettings(enabled=False)
    resolver_cache = new_resolver_cache()
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=anthropic,
        sessionmaker=db_session_factory,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=resolver_cache,
        turn_deps=build_turn_deps(
            settings,
            anthropic,
            db_session_factory,
            deployment_default=DeploymentDefault(),
            resolver_cache=resolver_cache,
            billing_config=None,
        ),
    )
    bot = make_bot(runtime)

    card = MagicMock(spec=discord.Message)
    card.id = 4242
    card.webhook_id = None
    card.application_id = None
    thread = MagicMock(spec=discord.Thread)
    thread.id = _THREAD_ID
    thread.parent_id = _PARENT_ID
    thread.guild = MagicMock(spec=discord.Guild)

    message = MagicMock(spec=discord.Message)
    message.id = 1000
    message.content = "<@999> hello"
    message.author = MagicMock()
    message.author.bot = False
    message.author.id = 111
    message.guild = MagicMock(spec=discord.Guild)
    message.guild.id = _GUILD_ID
    message.channel = MagicMock()
    message.channel.__class__ = discord.TextChannel
    message.channel.id = _PARENT_ID
    message.channel.send = AsyncMock()
    message.channel.history = MagicMock(return_value=_AsyncIter([]))
    message.create_thread = AsyncMock(return_value=thread)
    message.add_reaction = AsyncMock()
    message.remove_reaction = AsyncMock()
    message.attachments = []
    message.mentions = [types.SimpleNamespace(id=999)]

    world = _World(
        bot=bot,
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        channel_message=message,
        thread=thread,
        card=card,
        release_stream=release,
        stream_opened=opened,
        sent_to_ma=sent,
        on_user_message=on_user_message,
    )

    async def edit(**_kwargs: Any) -> discord.Message:
        world.edit_attempts += 1
        error = world.thread_error(False)
        if error is not None:
            world.failed_writes += 1
            raise error
        return card

    async def send(*_args: Any, **_kwargs: Any) -> discord.Message:
        error = world.thread_error(True)
        if error is not None:
            world.failed_writes += 1
            raise error
        return card

    card.edit = AsyncMock(side_effect=edit)
    thread.send = AsyncMock(side_effect=send)

    # `_handle_mention` keeps its contract: nothing escapes it, so `on_message` never
    # reaches the prologue handler that posts into the parent channel.
    real_handle_mention = bot._handle_mention  # pyright: ignore[reportPrivateUsage]

    async def watched_handle_mention(*args: Any, **kwargs: Any) -> None:
        try:
            await real_handle_mention(*args, **kwargs)
        except BaseException as exc:
            world.extra.setdefault("handle_mention_raised", []).append(exc)
            raise

    monkeypatch.setattr(bot, "_handle_mention", watched_handle_mention)

    # Progress edits on every render tick, instead of once every 10 s.
    monkeypatch.setattr("daimon.adapters.discord.lifecycle._DEBOUNCE_S", 0)
    return world


def _patch_admission(func: Callable[..., Any]) -> Callable[..., Any]:
    """Patch admission's resolvers and session creation; the mocks arrive in this order."""
    func = patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)(func)
    func = patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock)(func)
    func = patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)(func)
    return patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)(func)


async def _run_mention(world: _World, *, release_after_s: float = 8.0) -> None:
    """Run the channel mention to the end; an unfixed bot lets the turn finish on its own."""
    asyncio.get_running_loop().call_later(release_after_s, world.release_stream.set)
    await asyncio.wait_for(world.bot.on_message(world.channel_message), timeout=40)


def _arm(
    mock_resolve: AsyncMock,
    mock_create_session: AsyncMock,
    mock_find_env: AsyncMock,
    mock_find_agent: AsyncMock,
) -> None:
    mock_resolve.return_value = ResolvedConfig(
        agent_name="test-agent",
        agent_name_tier="tenant",
        environment_name="test-env",
        environment_name_tier="tenant",
    )
    session: BetaManagedAgentsSession = ma_session(id=_MA_SESSION)
    mock_create_session.return_value = session
    mock_find_agent.return_value = "ag_test"
    mock_find_env.return_value = "env_test"


async def _admission_handled(world: _World, *message_ids: str) -> dict[str, bool]:
    async with world.sessionmaker() as session:
        rows = await session.execute(
            text(
                "SELECT message_id, handled FROM discord_message_admissions"
                " WHERE message_id = ANY(:ids)"
            ),
            {"ids": list(message_ids)},
        )
        return {message_id: handled for message_id, handled in rows.all()}


async def _sessions(world: _World) -> tuple[int, int]:
    """(live, dead) thread-session rows in the test thread."""
    async with world.sessionmaker() as session:
        statuses = (
            await session.scalars(
                text("SELECT status FROM thread_sessions WHERE thread_id = :t"),
                {"t": str(_THREAD_ID)},
            )
        ).all()
    return statuses.count("live"), statuses.count("dead")


def _queued_follow_up(message_id: int = 1001) -> Any:
    message = MagicMock(spec=discord.Message)
    message.id = message_id
    message.content = "<@999> and one more thing"
    message.author = MagicMock()
    message.author.bot = False
    message.author.id = 222
    message.author.display_name = "Bob"
    message.guild = MagicMock(spec=discord.Guild)
    message.guild.id = _GUILD_ID
    message.channel = MagicMock()
    message.channel.__class__ = discord.Thread
    message.channel.id = _THREAD_ID
    message.channel.send = AsyncMock()
    message.add_reaction = AsyncMock()
    message.remove_reaction = AsyncMock()
    message.attachments = []
    message.mentions = [types.SimpleNamespace(id=999)]
    return message


class TestStopReason:
    def test_matches_the_code_not_the_status(self) -> None:
        assert stop_reason_of(_http(10003, status=400)) == ("gone", 10003)
        assert stop_reason_of(_http(10003, status=404, cls=discord.NotFound)) == ("gone", 10003)
        assert stop_reason_of(_http(50083), write="send") == ("closed", 50083)

    def test_a_missing_webhook_or_message_is_not_a_lost_thread(self) -> None:
        assert stop_reason_of(_http(10015, status=404, cls=discord.NotFound)) is None
        assert stop_reason_of(_http(10008, status=404, cls=discord.NotFound)) is None
        assert stop_reason_of(_http(50001, status=403, cls=discord.Forbidden)) is None
        assert stop_reason_of(RuntimeError("x")) is None


@_patch_admission
async def test_thread_deleted_mid_turn_interrupts_and_posts_nothing_in_the_channel(
    mock_resolve: AsyncMock,
    mock_create_session: AsyncMock,
    mock_find_env: AsyncMock,
    mock_find_agent: AsyncMock,
    db_session: AsyncSession,
    db_nullpool_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The #809 sequence: the thread starts answering 10003 while the turn runs."""
    _arm(mock_resolve, mock_create_session, mock_find_env, mock_find_agent)
    world = await _world(db_session, db_nullpool_engine, monkeypatch)

    async def break_when_running() -> None:
        await world.stream_opened.wait()
        world.break_thread(_gone)

    breaker = asyncio.create_task(break_when_running())
    with structlog.testing.capture_logs() as logs:
        await _run_mention(world)
    await breaker
    await drain_outcomes()

    # Nothing anywhere else: not an error card, not a thread send, not the parent channel.
    world.channel_message.channel.send.assert_not_called()
    assert world.thread.send.await_count == 1, "only the turn's own card was ever sent"
    assert world.failed_writes == 1, "the first failed write is the last write attempted"
    assert not [e for e in logs if e["event"] == "mention_prologue_failed"]
    assert not [e for e in logs if e["event"] == "error.rendered"]
    assert world.extra.get("handle_mention_raised") is None, "_handle_mention never raises"

    # The turn was stopped through the normal path: a real user.interrupt, an
    # interrupted outcome, and the model calls it had already made still metered.
    assert 1 <= world.interrupts() <= 2, (
        "the driver's interrupt, plus at most one best-effort extra"
    )
    assert world.user_messages() == 1
    gone_logs = [e for e in logs if e["event"] == "turn.thread_gone"]
    assert len(gone_logs) == 1
    assert gone_logs[0]["reason"] == "gone"
    assert gone_logs[0]["error_code"] == 10003
    assert gone_logs[0]["thread_id"] == str(_THREAD_ID)
    assert gone_logs[0]["session_id"] == _MA_SESSION
    async with world.sessionmaker() as session:
        outcomes = await list_outcomes(session, world.tenant_id)
        usage = await usage_events.list_for_tenant(session, tenant_id=world.tenant_id)
        ledger = await tenant_ledger.list_for_tenant(session, tenant_id=world.tenant_id)
    assert [o.reason for o in outcomes] == [TerminationReason.INTERRUPTED]
    assert [row.event_id for row in usage] == [_USAGE_EVENT]
    assert any(e.idempotency_key.startswith("turn:") for e in ledger)

    # Bookkeeping: the admission row is finished, the session is dead and unmarked.
    assert await _admission_handled(world, "1000") == {"1000": True}
    assert await _sessions(world) == (0, 1)
    async with world.sessionmaker() as session:
        assert (
            await list_live_thread_sessions(
                session, tenant_id=world.tenant_id, platform="discord", thread_id=str(_THREAD_ID)
            )
            == []
        )
    assert _THREAD_ID not in world.bot._processing  # pyright: ignore[reportPrivateUsage]
    stop = world.bot._thread_stops.get(_THREAD_ID)  # pyright: ignore[reportPrivateUsage]
    assert stop is not None and stop.reason == "gone", "a gone thread stays gone after the release"


@_patch_admission
async def test_a_queued_follow_up_behind_a_gone_turn_never_starts(
    mock_resolve: AsyncMock,
    mock_create_session: AsyncMock,
    mock_find_env: AsyncMock,
    mock_find_agent: AsyncMock,
    db_session: AsyncSession,
    db_nullpool_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(mock_resolve, mock_create_session, mock_find_env, mock_find_agent)
    world = await _world(db_session, db_nullpool_engine, monkeypatch)
    follow_up = _queued_follow_up()
    other = _queued_follow_up(1002)
    other.author.id = 333  # another author's group, which the drain would run next

    async def queue_then_break() -> None:
        await world.stream_opened.wait()
        await world.bot.on_message(follow_up)
        await world.bot.on_message(other)
        world.break_thread(_gone)

    queuer = asyncio.create_task(queue_then_break())
    with structlog.testing.capture_logs() as logs:
        await _run_mention(world)
    await queuer

    assert world.user_messages() == 1, "no queued turn started"
    assert world.thread.send.await_count == 1, "no second card"
    world.channel_message.channel.send.assert_not_called()
    follow_up.channel.send.assert_not_called()
    assert await _admission_handled(world, "1000", "1001", "1002") == {
        "1000": True,
        "1001": True,
        "1002": True,
    }, "every dropped queued message's admission is finished, so replay never starts it"
    follow_up.add_reaction.assert_awaited_once()  # the hourglass went on while the thread lived
    follow_up.remove_reaction.assert_not_awaited()  # and no reaction call after it was lost
    other.remove_reaction.assert_not_awaited()
    assert world.bot._queued_reactions == set()  # pyright: ignore[reportPrivateUsage]
    assert world.bot._pending.get(_THREAD_ID) is None  # pyright: ignore[reportPrivateUsage]
    assert any(e["event"] == "thread_gone.queued_dropped" for e in logs)
    assert len([e for e in logs if e["event"] == "turn.thread_gone"]) == 1


@_patch_admission
async def test_a_message_queued_after_the_stop_is_dropped_before_its_group_runs(
    mock_resolve: AsyncMock,
    mock_create_session: AsyncMock,
    mock_find_env: AsyncMock,
    mock_find_agent: AsyncMock,
    db_session: AsyncSession,
    db_nullpool_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check is made before each group, so a group behind the first one stops too."""
    _arm(mock_resolve, mock_create_session, mock_find_env, mock_find_agent)
    world = await _world(db_session, db_nullpool_engine, monkeypatch)
    first = _queued_follow_up(1001)
    second = _queued_follow_up(1002)
    second.author.id = 333
    started: list[int] = []
    real = world.bot._handle_mention  # pyright: ignore[reportPrivateUsage]

    async def handle(message: Any, *args: Any, **kwargs: Any) -> None:
        if message is not world.channel_message:
            started.append(message.id)
            # The first drained group runs and, while it does, the thread is lost.
            world.bot._thread_stops.signal(  # pyright: ignore[reportPrivateUsage]
                _THREAD_ID, reason="gone", code=10003, phase="running"
            )
            return
        await real(message, *args, **kwargs)

    async def queue_while_running() -> None:
        await world.stream_opened.wait()
        await world.bot.on_message(first)
        await world.bot.on_message(second)
        world.release_stream.set()  # the originating turn ends normally

    monkeypatch.setattr(world.bot, "_handle_mention", handle)
    queuer = asyncio.create_task(queue_while_running())
    await _run_mention(world)
    await queuer

    assert started == [1001], "the second group is never started"
    assert await _admission_handled(world, "1002") == {"1002": True}
    second.remove_reaction.assert_not_awaited()


@_patch_admission
async def test_thread_delete_event_during_a_turn_interrupts_it_and_retires_sessions(
    mock_resolve: AsyncMock,
    mock_create_session: AsyncMock,
    mock_find_env: AsyncMock,
    mock_find_agent: AsyncMock,
    db_session: AsyncSession,
    db_nullpool_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway can say so before any write fails; the thread still answers writes here."""
    _arm(mock_resolve, mock_create_session, mock_find_env, mock_find_agent)
    world = await _world(db_session, db_nullpool_engine, monkeypatch)
    payload = types.SimpleNamespace(thread_id=_THREAD_ID, parent_id=_PARENT_ID, guild_id=_GUILD_ID)

    async def delete_when_running() -> None:
        await world.stream_opened.wait()
        await world.bot.on_raw_thread_delete(payload)  # pyright: ignore[reportArgumentType]

    deleter = asyncio.create_task(delete_when_running())
    with structlog.testing.capture_logs() as logs:
        await _run_mention(world)
    await deleter
    await drain_outcomes()

    assert 1 <= world.interrupts() <= 2  # the driver's, plus at most one best-effort extra
    world.channel_message.channel.send.assert_not_called()
    assert world.thread.send.await_count == 1
    assert world.failed_writes == 0, "no write was issued after the delete event"
    assert world.edit_attempts == 0, "no progress edit and no terminal card after the delete"
    deleted = [e for e in logs if e["event"] == "thread.deleted"]
    assert deleted == [
        {
            "event": "thread.deleted",
            "log_level": "info",
            "thread_id": str(_THREAD_ID),
            "parent_id": str(_PARENT_ID),
            "guild_id": str(_GUILD_ID),
            "had_running_turn": True,
        }
    ]
    gone_logs = [e for e in logs if e["event"] == "turn.thread_gone"]
    assert len(gone_logs) == 1 and gone_logs[0]["error_code"] is None
    assert gone_logs[0]["source"] == "gateway"
    async with world.sessionmaker() as session:
        outcomes = await list_outcomes(session, world.tenant_id)
    assert [o.reason for o in outcomes] == [TerminationReason.INTERRUPTED]
    assert await _sessions(world) == (0, 1)


async def test_thread_delete_event_with_no_turn_running_still_retires_the_sessions(
    db_session: AsyncSession,
    db_nullpool_engine: AsyncEngine,
) -> None:
    db_session_factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    from daimon.core.ma_identity import derive_tenant_uuid
    from daimon.core.stores.thread_sessions import mark_turn_active
    from daimon.testing import make_account, make_thread_session

    tenant = await make_tenant(db_session, platform="discord", workspace_id=str(_GUILD_ID))
    assert tenant.id == derive_tenant_uuid(platform="discord", workspace_id=str(_GUILD_ID))
    rows = []
    for _ in range(2):  # two callers, two accounts, one thread
        account = await make_account(db_session, tenant=tenant)
        rows.append(
            await make_thread_session(
                db_session, tenant=tenant, account=account, thread_id=str(_THREAD_ID)
            )
        )
    elsewhere = await make_thread_session(db_session, tenant=tenant, thread_id="5555")
    from datetime import UTC, datetime

    await mark_turn_active(
        db_session, id=rows[0].id, active_turn_message_id="777", now=datetime.now(UTC)
    )
    await db_session.commit()
    bot = make_bot(
        DiscordRuntime(
            settings=MagicMock(),
            anthropic=AsyncMock(),
            sessionmaker=db_session_factory,
            notebook_rate_limiter=RateLimiter(max_requests=999),
            billing_config=None,
            deployment_default=DeploymentDefault(),
            resolver_cache=new_resolver_cache(),
            turn_deps=replace(deps_for(), turn_path=None),
        )
    )
    payload = types.SimpleNamespace(thread_id=_THREAD_ID, parent_id=_PARENT_ID, guild_id=_GUILD_ID)

    with structlog.testing.capture_logs() as logs:
        await bot.on_raw_thread_delete(payload)  # pyright: ignore[reportArgumentType]

    assert [e["had_running_turn"] for e in logs if e["event"] == "thread.deleted"] == [False]
    assert not [e for e in logs if e["event"] == "turn.thread_gone"], "no turn, no turn log"
    async with db_session_factory() as session:
        result = await session.execute(
            text("SELECT id, status, active_turn_message_id FROM thread_sessions")
        )
        db_rows = {row.id: row for row in result.all()}
    assert {db_rows[row.id].status for row in rows} == {"dead"}
    assert all(db_rows[row.id].active_turn_message_id is None for row in rows)
    assert db_rows[elsewhere.id].status == "live", "another thread's session is untouched"


@_patch_admission
async def test_a_send_refused_as_archived_stops_the_turn_but_keeps_its_session(
    mock_resolve: AsyncMock,
    mock_create_session: AsyncMock,
    mock_find_env: AsyncMock,
    mock_find_agent: AsyncMock,
    db_session: AsyncSession,
    db_nullpool_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A send still 50083 after the unarchive-and-retry means closed: stop, keep the session."""
    _arm(mock_resolve, mock_create_session, mock_find_env, mock_find_agent)
    world = await _world(db_session, db_nullpool_engine, monkeypatch)
    world.thread.edit = AsyncMock(side_effect=_http(50083))  # the unarchive fails too

    async def archive_when_running() -> None:
        await world.stream_opened.wait()
        world.break_thread(_closed_for_sends)

    archiver = asyncio.create_task(archive_when_running())
    with structlog.testing.capture_logs() as logs:
        await _run_mention(world)
    await archiver
    await drain_outcomes()

    assert 1 <= world.interrupts() <= 2  # the driver's, plus at most one best-effort extra
    world.channel_message.channel.send.assert_not_called()
    assert world.thread.send.await_count == 2, "the card, then the one replacement that failed"
    world.thread.edit.assert_awaited_once()
    gone_logs = [e for e in logs if e["event"] == "turn.thread_gone"]
    assert [(e["reason"], e["error_code"]) for e in gone_logs] == [("closed", 50083)]
    assert await _sessions(world) == (1, 0), "a closed thread keeps its live session"
    assert await _admission_handled(world, "1000") == {"1000": True}


@_patch_admission
async def test_archived_edits_do_not_stop_a_turn_whose_answer_is_then_delivered(
    mock_resolve: AsyncMock,
    mock_create_session: AsyncMock,
    mock_find_env: AsyncMock,
    mock_find_agent: AsyncMock,
    db_session: AsyncSession,
    db_nullpool_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thread can auto-archive during a slow turn; progress edits fail 50083, the turn goes on."""
    _arm(mock_resolve, mock_create_session, mock_find_env, mock_find_agent)
    world = await _world(db_session, db_nullpool_engine, monkeypatch)
    archived_edits = iter([_http(50083), _http(50083)])

    def archived_then_open(is_send: bool) -> Exception | None:
        return None if is_send else next(archived_edits, None)

    async def archive_when_running() -> None:
        await world.stream_opened.wait()
        world.break_thread(archived_then_open)

    archiver = asyncio.create_task(archive_when_running())
    with structlog.testing.capture_logs() as logs:
        await _run_mention(world, release_after_s=6)
    await archiver
    await drain_outcomes()

    assert world.failed_writes == 2, "the archived progress edits really failed"
    assert world.interrupts() == 0, "and did not stop the turn"
    assert not [e for e in logs if e["event"] == "turn.thread_gone"]
    delivered = [c.kwargs.get("content") or "" for c in world.card.edit.await_args_list]
    assert any("partial answer" in content for content in delivered), "the answer arrived"
    async with world.sessionmaker() as session:
        outcomes = await list_outcomes(session, world.tenant_id)
    assert [o.reason for o in outcomes] == [TerminationReason.COMPLETED]
    assert await _sessions(world) == (1, 0)


@_patch_admission
async def test_an_unknown_webhook_does_not_stop_the_turn(
    mock_resolve: AsyncMock,
    mock_create_session: AsyncMock,
    mock_find_env: AsyncMock,
    mock_find_agent: AsyncMock,
    db_session: AsyncSession,
    db_nullpool_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """10015: the webhook can vanish while the thread lives; webhook recovery owns that."""
    _arm(mock_resolve, mock_create_session, mock_find_env, mock_find_agent)
    world = await _world(db_session, db_nullpool_engine, monkeypatch)
    failures = iter([_http(10015, status=404, cls=discord.NotFound)])

    def unknown_webhook_once(is_send: bool) -> Exception | None:
        return None if is_send else next(failures, None)

    async def fail_one_edit() -> None:
        await world.stream_opened.wait()
        world.break_thread(unknown_webhook_once)

    breaker = asyncio.create_task(fail_one_edit())
    with structlog.testing.capture_logs() as logs:
        await _run_mention(world, release_after_s=5)
    await breaker

    assert world.failed_writes == 1, "the 10015 edit really failed"
    assert world.interrupts() == 0, "and did not stop the turn"
    assert not [e for e in logs if e["event"] == "turn.thread_gone"]
    assert world.bot._thread_stops.get(_THREAD_ID) is None  # pyright: ignore[reportPrivateUsage]
    assert await _sessions(world) == (1, 0)


@_patch_admission
async def test_a_message_queued_after_the_stop_gets_no_reaction_and_is_finished(
    mock_resolve: AsyncMock,
    mock_create_session: AsyncMock,
    mock_find_env: AsyncMock,
    mock_find_agent: AsyncMock,
    db_session: AsyncSession,
    db_nullpool_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its claim committed, then the stop landed, then it reached the queue: the release finishes it."""
    _arm(mock_resolve, mock_create_session, mock_find_env, mock_find_agent)
    world = await _world(db_session, db_nullpool_engine, monkeypatch)
    late = _queued_follow_up(1003)
    stops = world.bot._thread_stops  # pyright: ignore[reportPrivateUsage]

    async def queue_after_stop() -> None:
        await world.stream_opened.wait()
        world.break_thread(_gone)
        while stops.get(_THREAD_ID) is None:
            await asyncio.sleep(0.05)
        await world.bot.on_message(late)

    queuer = asyncio.create_task(queue_after_stop())
    await _run_mention(world)
    await queuer

    late.add_reaction.assert_not_awaited()
    late.remove_reaction.assert_not_awaited()
    assert world.user_messages() == 1
    assert await _admission_handled(world, "1003") == {"1003": True}
    assert world.bot._pending.get(_THREAD_ID) is None  # pyright: ignore[reportPrivateUsage]


@_patch_admission
async def test_the_error_card_write_can_be_the_first_to_find_the_thread_gone(
    mock_resolve: AsyncMock,
    mock_create_session: AsyncMock,
    mock_find_env: AsyncMock,
    mock_find_agent: AsyncMock,
    db_session: AsyncSession,
    db_nullpool_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The turn fails for another reason, then the thread is deleted: the error says nothing."""
    _arm(mock_resolve, mock_create_session, mock_find_env, mock_find_agent)
    world = await _world(db_session, db_nullpool_engine, monkeypatch)

    async def fail_after_deletion(*_args: Any, **_kwargs: Any) -> None:
        world.break_thread(_gone)
        raise RuntimeError("unrelated failure")

    monkeypatch.setattr("daimon.adapters.discord.bot.run_prepared_turn", fail_after_deletion)
    with structlog.testing.capture_logs() as logs:
        await asyncio.wait_for(world.bot.on_message(world.channel_message), timeout=30)

    world.channel_message.channel.send.assert_not_called()
    assert world.thread.send.await_count == 1, "only the card: the error was never sent anywhere"
    assert world.extra.get("handle_mention_raised") is None
    assert not [e for e in logs if e["event"] == "mention_prologue_failed"]
    gone_logs = [e for e in logs if e["event"] == "turn.thread_gone"]
    assert [e["reason"] for e in gone_logs] == ["gone"]
    assert await _sessions(world) == (0, 1), "the deleted thread's session is retired"


async def test_a_failed_error_send_marks_the_thread_gone_and_never_raises(
    db_session: AsyncSession,
    db_nullpool_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no card left to turn into the error, the thread send is the first write to see 10003."""
    from daimon.adapters.discord.bot import (
        _TurnFailureSurface,  # pyright: ignore[reportPrivateUsage]
    )

    world = await _world(db_session, db_nullpool_engine, monkeypatch)
    world.break_thread(_gone)
    surface = _TurnFailureSurface(thread=world.thread, end_card=None)

    await world.bot._render_turn_error(  # pyright: ignore[reportPrivateUsage]
        world.channel_message,
        world.tenant_id,
        str(_GUILD_ID),
        "rid",
        RuntimeError("the turn failed"),
        surface=surface,
    )

    assert world.thread.send.await_count == 1, "the one attempt that found the thread gone"
    world.channel_message.channel.send.assert_not_called()
    stop = world.bot._thread_stops.get(_THREAD_ID)  # pyright: ignore[reportPrivateUsage]
    assert stop is not None and stop.reason == "gone"


async def test_an_error_for_a_thread_that_is_already_gone_is_not_posted(
    db_session: AsyncSession,
    db_nullpool_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.adapters.discord.bot import (
        _TurnFailureSurface,  # pyright: ignore[reportPrivateUsage]
    )

    world = await _world(db_session, db_nullpool_engine, monkeypatch)
    world.bot._thread_stops.signal(  # pyright: ignore[reportPrivateUsage]
        _THREAD_ID, reason="gone", code=10003, phase="running"
    )
    surface = _TurnFailureSurface(thread=world.thread, end_card=AsyncMock())

    await world.bot._render_turn_error(  # pyright: ignore[reportPrivateUsage]
        world.channel_message,
        world.tenant_id,
        str(_GUILD_ID),
        "rid",
        RuntimeError("x"),
        surface=surface,
    )

    surface.end_card.assert_not_awaited()  # pyright: ignore[reportOptionalMemberAccess]
    world.thread.send.assert_not_awaited()
    world.channel_message.channel.send.assert_not_called()


@_patch_admission
async def test_a_stop_during_send_initial_still_interrupts_the_session(
    mock_resolve: AsyncMock,
    mock_create_session: AsyncMock,
    mock_find_env: AsyncMock,
    mock_find_agent: AsyncMock,
    db_session: AsyncSession,
    db_nullpool_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The driver ends a Stop that lands while the question is being sent without an interrupt.

    MA would run that turn on for nobody, so the bot interrupts the stopped turn's
    session itself, before it retires the rows.
    """
    _arm(mock_resolve, mock_create_session, mock_find_env, mock_find_agent)
    world = await _world(db_session, db_nullpool_engine, monkeypatch)
    stops = world.bot._thread_stops  # pyright: ignore[reportPrivateUsage]
    world.on_user_message.append(
        lambda: stops.signal(_THREAD_ID, reason="gone", code=10003, phase="running")
    )
    sessions_interrupted: list[str] = []
    real_interrupt = interrupt_orphaned_session

    async def recording_interrupt(anthropic: Any, *, session_id: str, **kwargs: Any) -> bool:
        sessions_interrupted.append(session_id)
        return await real_interrupt(anthropic, session_id=session_id, **kwargs)

    monkeypatch.setattr(
        "daimon.adapters.discord.bot.interrupt_orphaned_session", recording_interrupt
    )

    await _run_mention(world, release_after_s=2)

    assert world.user_messages() == 1
    assert world.interrupts() >= 1, "MA was told to stop the turn it was just given"
    assert sessions_interrupted == [_MA_SESSION]
    world.channel_message.channel.send.assert_not_called()
    assert await _sessions(world) == (0, 1)


def _stale_world() -> tuple[ThreadStops, int]:
    return ThreadStops(), 4242


class TestThreadStopsLifetime:
    def test_a_closed_record_outlives_its_release_and_a_new_claim_clears_it(self) -> None:
        stops, thread_id = _stale_world()
        stops.claim(thread_id)
        stops.signal(thread_id, reason="closed", code=50083, phase="running")
        stops.release(thread_id)
        assert stops.get(thread_id) is not None, "a suspended reaction still sees the stop"
        stops.claim(thread_id)
        assert stops.get(thread_id) is None, "a new turn starts clean on an unarchived thread"

    def test_a_gone_record_is_never_cleared(self) -> None:
        stops, thread_id = _stale_world()
        stops.claim(thread_id)
        stops.signal(thread_id, reason="gone", code=10003, phase="running")
        stops.release(thread_id)
        stops.claim(thread_id)
        assert stops.get(thread_id) is not None

    def test_a_healthy_threads_bookkeeping_is_dropped_on_release(self) -> None:
        stops, thread_id = _stale_world()
        stops.claim(thread_id)
        stops.watch(thread_id, asyncio.Event())
        stops.release(thread_id)
        assert stops.generation(thread_id) == 0 and thread_id not in stops._cancels  # pyright: ignore[reportPrivateUsage]

    async def test_an_old_turns_late_archive_error_does_not_cancel_the_new_turn(self) -> None:
        stops, thread_id = _stale_world()
        thread = MagicMock(spec=discord.Thread)
        thread.id = thread_id
        thread.send = AsyncMock(side_effect=_http(50083))
        thread.edit = AsyncMock(side_effect=_http(50083))
        client = MagicMock()
        stops.claim(thread_id)
        old = DiscordPostTransport(
            client,
            thread,
            name="a",
            avatar_url=None,
            builtin=True,
            identity_enabled=False,
            watch=TurnThreadWatch(stops, thread_id),
        )
        stops.release(thread_id)
        stops.claim(thread_id)  # the next turn
        new_cancel = asyncio.Event()
        stops.watch(thread_id, new_cancel)

        with pytest.raises(discord.HTTPException):
            await old.send("late")  # the old turn's write fails 50083 after the new turn began

        assert stops.get(thread_id) is None and not new_cancel.is_set()

    async def test_an_old_turns_late_deletion_error_still_stops_the_thread(self) -> None:
        stops, thread_id = _stale_world()
        stale = TurnThreadWatch(stops, thread_id)
        stops.claim(thread_id)
        new_cancel = asyncio.Event()
        stops.watch(thread_id, new_cancel)
        stale.note_error(_http(10003), write="edit")
        assert stops.get(thread_id) is not None and new_cancel.is_set()


async def test_a_stop_signalled_while_the_webhook_lookup_is_suspended_sends_nothing() -> None:
    stops, thread_id = _stale_world()
    stops.claim(thread_id)
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.send = AsyncMock()
    transport = DiscordPostTransport(
        MagicMock(),
        thread,
        name="a",
        avatar_url=None,
        builtin=False,
        identity_enabled=True,
        watch=TurnThreadWatch(stops, thread_id),
    )
    hook = MagicMock()
    hook.send = AsyncMock()
    lookup_started = asyncio.Event()
    lookup_may_finish = asyncio.Event()

    async def slow_lookup(**_kwargs: Any) -> Any:
        lookup_started.set()
        await lookup_may_finish.wait()
        return hook

    monkeypatch_target = transport
    monkeypatch_target._webhook = slow_lookup  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
    sending = asyncio.create_task(transport.send("answer"))
    await lookup_started.wait()
    stops.signal(thread_id, reason="gone", code=10003, phase="running")
    lookup_may_finish.set()

    with pytest.raises(ThreadStopped):
        await sending
    hook.send.assert_not_awaited()
    thread.send.assert_not_awaited()


@_patch_admission
async def test_a_reaction_suspended_across_the_release_is_not_followed_by_a_removal(
    mock_resolve: AsyncMock,
    mock_create_session: AsyncMock,
    mock_find_env: AsyncMock,
    mock_find_agent: AsyncMock,
    db_session: AsyncSession,
    db_nullpool_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hourglass call is still in flight when the thread is deleted and the slot released."""
    _ = (mock_resolve, mock_create_session, mock_find_env, mock_find_agent)
    world = await _world(db_session, db_nullpool_engine, monkeypatch)
    bot = world.bot
    stops = bot._thread_stops  # pyright: ignore[reportPrivateUsage]
    message = _queued_follow_up(1004)
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def suspended_add(*_args: Any) -> None:
        entered.set()
        await finish.wait()

    message.add_reaction = AsyncMock(side_effect=suspended_add)
    bot._processing.add(_THREAD_ID)  # pyright: ignore[reportPrivateUsage]
    stops.claim(_THREAD_ID)
    queuing = asyncio.create_task(
        bot._queue_behind_inflight_turn(_THREAD_ID, message)  # pyright: ignore[reportPrivateUsage]
    )
    await entered.wait()

    stops.signal(_THREAD_ID, reason="gone", code=10003, phase="running")
    await bot._release_and_clear(  # pyright: ignore[reportPrivateUsage]
        _THREAD_ID, world.tenant_id, clear_reactions=True
    )
    assert _THREAD_ID not in bot._processing  # pyright: ignore[reportPrivateUsage]
    finish.set()
    await queuing

    message.remove_reaction.assert_not_awaited()
    assert await _admission_handled(world, "1004") == {}, "no claim row was ever made here"
