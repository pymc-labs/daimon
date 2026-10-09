"""Per-thread mention queueing in DaimonBot.

Covers the queue-and-drain behavior: mentions arriving during an in-flight
turn for the same thread accumulate in self._pending and are drained after
the current turn completes into a single composite follow-up turn.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import pytest_asyncio
from daimon.adapters.discord.bot import (  # pyright: ignore[reportPrivateUsage]
    GLOBAL_CAP_NOTICE,
    DaimonBot,
    _compose_queued_content,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.config import McpSettings
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.turn.slots import wait_for_slot
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import make_bot


def _make_runtime(
    tenant_id: uuid.UUID,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> DiscordRuntime:
    _ = tenant_id  # runtime no longer carries tenant_id
    settings = MagicMock()
    settings.mcp = McpSettings()
    discord_settings = MagicMock()
    discord_settings.max_concurrent_turns_per_tenant = 100  # effectively uncapped in tests
    discord_settings.thread_open_notice_after_s = 3.0
    settings.discord = discord_settings
    return DiscordRuntime(
        settings=settings,
        anthropic=AsyncMock(),
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # _handle_mention/_orchestrate stubbed per-test
    )


def _make_channel_message(
    *,
    content: str = "<@999> hello",
    guild_id: int = 123456,
    channel_id: int = 789,
    author_id: int = 111,
    display_name: str = "Alice",
) -> discord.Message:
    message = MagicMock(spec=discord.Message)
    message.content = content
    message.author = MagicMock()
    message.author.bot = False
    message.author.id = author_id
    message.author.display_name = display_name
    message.guild = MagicMock(spec=discord.Guild)
    message.guild.id = guild_id
    message.channel = MagicMock()
    message.channel.__class__ = discord.TextChannel
    message.channel.id = channel_id
    message.channel.send = AsyncMock()
    message.create_thread = AsyncMock()
    message.add_reaction = AsyncMock()
    message.attachments = []
    message.mentions = [SimpleNamespace(id=999)]
    return message


def _make_thread_message(
    *,
    content: str = "<@999> hello",
    guild_id: int = 123456,
    thread_id: int = 789,
    author_id: int = 111,
    display_name: str = "Alice",
) -> discord.Message:
    """A mention sent inside an existing thread (channel is a discord.Thread).

    Thread follow-ups are the only mentions that queue+coalesce — a thread is
    one conversation on one MA session, so overlapping turns must serialize.
    """
    message = MagicMock(spec=discord.Message)
    message.content = content
    message.author = MagicMock()
    message.author.bot = False
    message.author.id = author_id
    message.author.display_name = display_name
    message.guild = MagicMock(spec=discord.Guild)
    message.guild.id = guild_id
    message.channel = MagicMock()
    message.channel.__class__ = discord.Thread
    message.channel.id = thread_id
    message.channel.send = AsyncMock()
    message.add_reaction = AsyncMock()
    message.attachments = []
    message.mentions = [SimpleNamespace(id=999)]
    return message


# ---------------------------------------------------------------------------
# _compose_queued_content pure unit tests
# ---------------------------------------------------------------------------


def test_compose_single_author_joins_contents_with_blank_lines() -> None:
    m1 = _make_channel_message(content="hello", author_id=42, display_name="Alice")
    m2 = _make_channel_message(content="anyone there?", author_id=42, display_name="Alice")
    m3 = _make_channel_message(content="please respond", author_id=42, display_name="Alice")
    assert _compose_queued_content([m1, m2, m3]) == "hello\n\nanyone there?\n\nplease respond"


def test_compose_multi_author_prefixes_display_name() -> None:
    m1 = _make_channel_message(content="foo", author_id=1, display_name="Alice")
    m2 = _make_channel_message(content="bar", author_id=2, display_name="Bob")
    assert _compose_queued_content([m1, m2]) == "[Alice]: foo\n\n[Bob]: bar"


def test_compose_empty_list_returns_empty_string() -> None:
    assert _compose_queued_content([]) == ""


# ---------------------------------------------------------------------------
# Drain-behavior tests. Each test installs a stub `_handle_mention` that
# signals via an `entered` event when it starts and waits on a `release` event
# before returning. This lets the test deterministically interleave concurrent
# `on_message` calls without polling loops.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def queued_bot(
    db_session_factory: async_sessionmaker[AsyncSession],
):
    """Bot with tenant provisioned. _handle_mention is left intact; tests
    install per-test stubs."""
    # Funded: a turn that queued re-checks the balance once it holds a slot.
    result = await provision_tenant(
        db_session_factory, platform="discord", workspace_id="123456", signup_credit=Decimal("10")
    )

    runtime = _make_runtime(result.tenant_id, db_session_factory)
    return make_bot(runtime)


@pytest.mark.asyncio
async def test_no_queueing_when_serial_mentions(queued_bot: DaimonBot) -> None:
    """Two mentions that don't overlap → 2 calls, no content_override."""
    calls: list[tuple[discord.Message, str | None]] = []

    async def stub(
        message: discord.Message,
        guild_id: str,
        tenant_id: uuid.UUID,
        *,
        content_override: str | None = None,
        created_thread_ids: list[int] | None = None,
        attachments_override: list[discord.Attachment] | None = None,
    ) -> None:
        calls.append((message, content_override))

    queued_bot._handle_mention = stub  # type: ignore[method-assign]

    m1 = _make_channel_message(content="first")
    m2 = _make_channel_message(content="second")

    await queued_bot.on_message(m1)
    await queued_bot.on_message(m2)

    assert len(calls) == 2
    assert calls[0][1] is None
    assert calls[1][1] is None
    m1.add_reaction.assert_not_called()  # type: ignore[attr-defined]
    m2.add_reaction.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_over_the_tenant_cap_a_mention_queues_instead_of_refusing(
    queued_bot: DaimonBot,
) -> None:
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id="123456")
    cap = queued_bot.runtime.settings.discord.max_concurrent_turns_per_tenant
    held = [queued_bot.turn_queue.claim(tenant_id) for _ in range(cap)]
    slots: list[str] = []
    waiting = asyncio.Event()

    async def stub(*_args: object, **_kwargs: object) -> None:
        # Stands in for _orchestrate: the card is up, now wait for a slot.
        waiting.set()
        slots.append(
            await wait_for_slot(
                asyncio.Event(),
                sessionmaker=queued_bot.runtime.sessionmaker,
                tenant_id=tenant_id,
            )
        )

    queued_bot._orchestrate = stub  # type: ignore[method-assign]
    message = _make_channel_message()
    turn = asyncio.create_task(queued_bot.on_message(message))
    await waiting.wait()
    assert queued_bot.turn_queue.depth(tenant_id) == 1
    held[0].release()
    await turn
    assert slots == ["started"]
    message.channel.send.assert_not_awaited()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
    for ticket in held:
        ticket.release()
    assert queued_bot.turn_queue.in_flight() == 0


@pytest.mark.asyncio
async def test_a_full_tenant_queue_refuses_with_the_plain_notice(queued_bot: DaimonBot) -> None:
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id="123456")
    queued_bot.turn_queue.max_queued_per_tenant = 0
    cap = queued_bot.runtime.settings.discord.max_concurrent_turns_per_tenant
    for _ in range(cap):
        queued_bot.turn_queue.claim(tenant_id)
    message = _make_channel_message()

    await queued_bot.on_message(message)

    message.channel.send.assert_awaited_once_with(  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        "This server has too many chats in flight right now — try again in a moment."
    )


@pytest.mark.asyncio
async def test_over_the_global_cap_a_mention_waits_for_the_first_to_finish(
    queued_bot: DaimonBot,
) -> None:
    queued_bot.turn_queue.global_cap = 1
    entered = asyncio.Event()
    release = asyncio.Event()
    started: list[int] = []

    async def stub(message: discord.Message, *_args: object, **_kwargs: object) -> None:
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id="123456")
        result = await wait_for_slot(
            asyncio.Event(), sessionmaker=queued_bot.runtime.sessionmaker, tenant_id=tenant_id
        )
        assert result == "started"
        started.append(message.id)
        entered.set()
        if len(started) == 1:
            await release.wait()

    queued_bot._orchestrate = stub  # type: ignore[method-assign]
    first_message = _make_channel_message(channel_id=789)
    first_message.id = 1
    second_message = _make_channel_message(channel_id=790)
    second_message.id = 2
    first = asyncio.create_task(queued_bot.on_message(first_message))
    await entered.wait()
    second = asyncio.create_task(queued_bot.on_message(second_message))
    async with asyncio.timeout(5):
        while not queued_bot.turn_queue.depth():
            await asyncio.sleep(0.01)
    assert started == [1]
    assert queued_bot.turn_queue.in_flight() == 1
    assert queued_bot.turn_queue.depth() == 1
    release.set()
    await asyncio.gather(first, second)
    assert started == [1, 2]
    second_message.channel.send.assert_not_awaited()  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
    assert queued_bot.turn_queue.in_flight() == 0


@pytest.mark.asyncio
async def test_a_full_queue_at_the_global_cap_refuses_with_the_global_notice(
    queued_bot: DaimonBot,
) -> None:
    queued_bot.turn_queue.global_cap = 1
    queued_bot.turn_queue.max_queued = 0
    queued_bot.turn_queue.claim(uuid.uuid4())
    message = _make_channel_message()

    await queued_bot.on_message(message)

    message.channel.send.assert_awaited_once_with(GLOBAL_CAP_NOTICE)  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]


@pytest.mark.asyncio
async def test_global_slot_released_when_turn_fails(queued_bot: DaimonBot) -> None:
    queued_bot.turn_queue.global_cap = 1

    async def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("turn failed")

    queued_bot._orchestrate = fail  # type: ignore[method-assign]
    message = _make_channel_message()

    await queued_bot.on_message(message)

    assert queued_bot.turn_queue.in_flight() == 0
    assert queued_bot.turn_queue.depth() == 0


@pytest.mark.asyncio
async def test_overlapping_channel_mentions_run_in_parallel(queued_bot: DaimonBot) -> None:
    """Two mentions overlapping in the SAME channel each open their own thread,
    so both turns run concurrently — the second is NOT queued behind the first.

    Regression guard: serializing channel mentions by channel id let a single
    stalled turn wedge the entire channel.

    This also closes #163: because each channel mention opens its own thread and
    its own MA session, two channel mentions never share a session — so the
    cross-session "second turn on the same session" race #163 describes cannot
    occur. There is no shared-session turn to register in ``_processing`` or
    serialize on the channel path; only thread mentions (one conversation on one
    session) queue and drain.
    """
    calls: list[discord.Message] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def stub(
        message: discord.Message,
        guild_id: str,
        tenant_id: uuid.UUID,
        *,
        content_override: str | None = None,
        created_thread_ids: list[int] | None = None,
        attachments_override: list[discord.Attachment] | None = None,
    ) -> None:
        calls.append(message)
        entered.set()
        await release.wait()

    queued_bot._handle_mention = stub  # type: ignore[method-assign]

    m1 = _make_channel_message(content="first", author_id=1)
    m2 = _make_channel_message(content="second", author_id=2)

    t1 = asyncio.create_task(queued_bot.on_message(m1))
    await entered.wait()  # turn 1 is now mid-call (parked on release)
    entered.clear()

    t2 = asyncio.create_task(queued_bot.on_message(m2))
    await entered.wait()  # turn 2 ENTERS too → proves it ran in parallel

    assert len(calls) == 2, "both channel mentions must run concurrently, not queue"
    m2.add_reaction.assert_not_called()  # type: ignore[attr-defined]  # no ⌛ queue marker

    release.set()
    await asyncio.gather(t1, t2)


@pytest.mark.asyncio
async def test_one_queued_mention_runs_one_composite_followup(queued_bot: DaimonBot) -> None:
    calls: list[tuple[discord.Message, str | None]] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def stub(
        message: discord.Message,
        guild_id: str,
        tenant_id: uuid.UUID,
        *,
        content_override: str | None = None,
        created_thread_ids: list[int] | None = None,
        attachments_override: list[discord.Attachment] | None = None,
    ) -> None:
        calls.append((message, content_override))
        entered.set()
        await release.wait()

    queued_bot._handle_mention = stub  # type: ignore[method-assign]

    m1 = _make_thread_message(content="first")
    m2 = _make_thread_message(content="queued")

    task = asyncio.create_task(queued_bot.on_message(m1))
    await entered.wait()  # turn 1 is now mid-call
    entered.clear()

    await queued_bot.on_message(m2)  # queues; returns immediately
    m2.add_reaction.assert_awaited_with("⌛")  # type: ignore[attr-defined]
    assert len(calls) == 1, "queued mention must not enter the handler yet"

    release.set()
    await task

    assert len(calls) == 2
    drain_message, drain_override = calls[1]
    assert drain_message is m2
    assert drain_override == "queued"


@pytest.mark.asyncio
async def test_three_queued_mentions_merge_into_single_composite_turn(
    queued_bot: DaimonBot,
) -> None:
    calls: list[tuple[discord.Message, str | None]] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def stub(
        message: discord.Message,
        guild_id: str,
        tenant_id: uuid.UUID,
        *,
        content_override: str | None = None,
        created_thread_ids: list[int] | None = None,
        attachments_override: list[discord.Attachment] | None = None,
    ) -> None:
        calls.append((message, content_override))
        entered.set()
        await release.wait()

    queued_bot._handle_mention = stub  # type: ignore[method-assign]

    m1 = _make_thread_message(content="first")
    q1 = _make_thread_message(content="A", author_id=42, display_name="Alice")
    q2 = _make_thread_message(content="B", author_id=42, display_name="Alice")
    q3 = _make_thread_message(content="C", author_id=42, display_name="Alice")

    task = asyncio.create_task(queued_bot.on_message(m1))
    await entered.wait()
    entered.clear()

    await queued_bot.on_message(q1)
    await queued_bot.on_message(q2)
    await queued_bot.on_message(q3)
    assert len(calls) == 1

    release.set()
    await task

    assert len(calls) == 2, "three queued mentions must merge into ONE follow-up turn"
    _, composite = calls[1]
    assert composite == "A\n\nB\n\nC"


@pytest.mark.asyncio
async def test_queue_drains_repeatedly_if_new_mention_during_drain(
    queued_bot: DaimonBot,
) -> None:
    """A mention that arrives during the drain turn gets queued and produces a third turn."""
    calls: list[tuple[discord.Message, str | None]] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def stub(
        message: discord.Message,
        guild_id: str,
        tenant_id: uuid.UUID,
        *,
        content_override: str | None = None,
        created_thread_ids: list[int] | None = None,
        attachments_override: list[discord.Attachment] | None = None,
    ) -> None:
        calls.append((message, content_override))
        entered.set()
        await release.wait()
        release.clear()

    queued_bot._handle_mention = stub  # type: ignore[method-assign]

    m1 = _make_thread_message(content="first")
    q1 = _make_thread_message(content="during-turn-1")
    q2 = _make_thread_message(content="during-drain")

    task = asyncio.create_task(queued_bot.on_message(m1))
    await entered.wait()
    entered.clear()

    await queued_bot.on_message(q1)  # queued behind turn 1

    release.set()  # release turn 1 → drain turn starts with q1
    await entered.wait()
    entered.clear()

    await queued_bot.on_message(q2)  # queued behind the drain turn

    release.set()  # release drain turn 1 → second drain starts with q2
    await entered.wait()
    entered.clear()

    release.set()  # release final drain turn
    await task

    assert len(calls) == 3, (
        "the drain loop must keep going while new mentions arrive during drain turns"
    )
    assert calls[1][0] is q1
    assert calls[2][0] is q2


@pytest.mark.asyncio
async def test_multi_author_queue_partitions_into_per_author_turns(
    queued_bot: DaimonBot,
) -> None:
    """G1: queued mentions from different authors drain as separate turns.

    The drain loop partitions the queue by author.id so one author's messages
    can never ride another author's session (confused-deputy). Alice and Bob
    each get their own _handle_mention call with their own single-author
    composite — never a cross-author prefixed composite.
    """
    calls: list[tuple[discord.Message, str | None]] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def stub(
        message: discord.Message,
        guild_id: str,
        tenant_id: uuid.UUID,
        *,
        content_override: str | None = None,
        created_thread_ids: list[int] | None = None,
        attachments_override: list[discord.Attachment] | None = None,
    ) -> None:
        calls.append((message, content_override))
        entered.set()
        await release.wait()

    queued_bot._handle_mention = stub  # type: ignore[method-assign]

    m1 = _make_thread_message(content="first", author_id=1)
    qa = _make_thread_message(content="foo", author_id=10, display_name="Alice")
    qb = _make_thread_message(content="bar", author_id=20, display_name="Bob")

    task = asyncio.create_task(queued_bot.on_message(m1))
    await entered.wait()

    await queued_bot.on_message(qa)
    await queued_bot.on_message(qb)

    release.set()
    await task

    assert len(calls) == 3, "in-flight turn + one drain turn per distinct author"
    assert calls[1] == (qa, "foo"), "Alice drains as her own single-author turn"
    assert calls[2] == (qb, "bar"), "Bob drains as his own single-author turn"


# ---------------------------------------------------------------------------
# Error boundaries must never let a mention drop silently.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_mention_catches_sqlalchemy_error_from_orchestrate(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A DB error inside _orchestrate must never escape _handle_mention (#170).

    On main, _handle_mention's except tuple is (DaimonError, anthropic.APIError,
    discord.HTTPException) -- SQLAlchemyError is NOT in it, so this test fails
    (the exception propagates) on pre-fix code.
    """
    tenant_id = uuid.uuid4()
    runtime = _make_runtime(tenant_id, db_session_factory)
    bot = make_bot(runtime)

    async def _raise_db_error(*args: object, **kwargs: object) -> None:
        raise SQLAlchemyError("db down")

    bot._orchestrate = _raise_db_error  # type: ignore[method-assign]

    message = _make_channel_message()

    await bot._handle_mention(message, "123456", tenant_id)  # pyright: ignore[reportPrivateUsage]

    message.channel.send.assert_called_once()  # type: ignore[attr-defined]
    error_text: str = message.channel.send.call_args[0][0]  # type: ignore[attr-defined]
    assert "rid:" not in error_text, "trace ids stay in logs"


@pytest.mark.asyncio
async def test_handle_mention_catches_unexpected_exception_from_orchestrate(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A bare, unclassified exception inside _orchestrate must also never escape
    _handle_mention (#170) -- the catch-all boundary is the backstop for bugs
    that don't fit any named exception type.

    On main there is no catch-all clause, so this RuntimeError propagates
    (test fails) on pre-fix code.
    """
    tenant_id = uuid.uuid4()
    runtime = _make_runtime(tenant_id, db_session_factory)
    bot = make_bot(runtime)

    async def _raise_unexpected(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    bot._orchestrate = _raise_unexpected  # type: ignore[method-assign]

    message = _make_channel_message()

    await bot._handle_mention(message, "123456", tenant_id)  # pyright: ignore[reportPrivateUsage]

    message.channel.send.assert_called_once()  # type: ignore[attr-defined]
    error_text: str = message.channel.send.call_args[0][0]  # type: ignore[attr-defined]
    assert "rid:" not in error_text, "trace ids stay in logs"


@pytest.mark.asyncio
async def test_on_message_prologue_failure_never_escapes_and_sends_error(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB error in the on_message prologue (e.g. the liveness read) must never
    raise out of on_message, and must still post a best-effort error message.

    On main, get_tenant_liveness's SQLAlchemyError is unguarded and escapes
    on_message (test fails) on pre-fix code.
    """
    monkeypatch.setattr(
        "daimon.adapters.discord.bot.get_tenant_liveness",
        AsyncMock(side_effect=SQLAlchemyError("liveness read failed")),
    )

    tenant_id = uuid.uuid4()
    runtime = _make_runtime(tenant_id, db_session_factory)
    bot = make_bot(runtime)
    message = _make_channel_message()

    await bot.on_message(message)  # must not raise

    message.channel.send.assert_called_once()  # type: ignore[attr-defined]
    error_text: str = message.channel.send.call_args[0][0]  # type: ignore[attr-defined]
    assert "rid:" not in error_text, "trace ids stay in logs"


# ---------------------------------------------------------------------------
# A bot-created thread must be mutex-registered from the
# instant it's created (inside _orchestrate) through drain, so an in-thread
# follow-up mention that arrives during the originating channel-mention turn
# queues instead of racing a second turn onto the same thread's session.
#
# These stubs replace `_handle_mention` with a `created_thread_ids` KEYWORD
# parameter that DEFAULTS to None -- exactly mirroring the real signature so
# pre-fix `on_message` (which calls `_handle_mention(message, guild_id,
# tenant_id)` with no such kwarg) exercises the real `:553`-style
# `thread_id in self._processing` guard un-doctored. On pre-fix code the stub
# never registers the thread (the kwarg is never passed), so the guard misses
# it and both calls run concurrently.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_mention_thread_registers_and_followup_queues(
    queued_bot: DaimonBot,
) -> None:
    """Failure mode (a): concurrent turn on the same session.

    On main, the bot-created thread never enters self._processing (it's only
    known inside _orchestrate's local `thread` variable), so a follow-up
    mention posted in that thread while the originating turn is still running
    passes the `:553` guard and starts a SECOND concurrent turn -- this test
    fails pre-fix (both calls enter before release).
    """
    thread_id = 42424242
    calls: list[discord.Message] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def stub(
        message: discord.Message,
        guild_id: str,
        tenant_id: uuid.UUID,
        *,
        content_override: str | None = None,
        created_thread_ids: list[int] | None = None,
        attachments_override: list[discord.Attachment] | None = None,
    ) -> None:
        calls.append(message)
        if created_thread_ids is not None:
            # Mirrors real _orchestrate: register the bot-created thread
            # immediately and report it back via the out-param.
            queued_bot._processing.add(thread_id)  # pyright: ignore[reportPrivateUsage]
            created_thread_ids.append(thread_id)
        entered.set()
        await release.wait()

    queued_bot._handle_mention = stub  # type: ignore[method-assign]

    channel_msg = _make_channel_message(content="first")
    thread_followup = _make_thread_message(content="follow-up", thread_id=thread_id)

    task = asyncio.create_task(queued_bot.on_message(channel_msg))
    await entered.wait()
    entered.clear()

    try:
        # Bounded, not indefinite: on pre-fix code the follow-up dives into the
        # handler and blocks on `release.wait()` (never queues), which would
        # otherwise hang the test forever instead of failing cleanly.
        await asyncio.wait_for(queued_bot.on_message(thread_followup), timeout=2.0)

        assert len(calls) == 1, (
            "an in-thread follow-up during the originating channel-mention turn "
            "must queue, not start a second concurrent turn on the same thread"
        )
        thread_followup.add_reaction.assert_awaited_with("⌛")  # type: ignore[attr-defined]
        assert queued_bot._pending[thread_id] == [thread_followup]  # pyright: ignore[reportPrivateUsage]
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_queued_followup_after_channel_mention_drains_with_content_override(
    queued_bot: DaimonBot,
) -> None:
    """After the originating channel-mention turn completes, the queued
    in-thread follow-up must drain as its own composite turn (content_override
    set), and the registration must be fully cleaned up afterward.
    """
    thread_id = 42424243
    calls: list[tuple[discord.Message, str | None]] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def stub(
        message: discord.Message,
        guild_id: str,
        tenant_id: uuid.UUID,
        *,
        content_override: str | None = None,
        created_thread_ids: list[int] | None = None,
        attachments_override: list[discord.Attachment] | None = None,
    ) -> None:
        calls.append((message, content_override))
        if created_thread_ids is not None:
            queued_bot._processing.add(thread_id)  # pyright: ignore[reportPrivateUsage]
            created_thread_ids.append(thread_id)
        entered.set()
        await release.wait()

    queued_bot._handle_mention = stub  # type: ignore[method-assign]

    channel_msg = _make_channel_message(content="first")
    thread_followup = _make_thread_message(content="queued", thread_id=thread_id)

    task = asyncio.create_task(queued_bot.on_message(channel_msg))
    await entered.wait()
    entered.clear()

    # Bounded, not indefinite: on pre-fix code the follow-up dives straight
    # into the handler and blocks on `release.wait()` instead of queueing.
    await asyncio.wait_for(queued_bot.on_message(thread_followup), timeout=2.0)
    assert len(calls) == 1, "queued mention must not enter the handler yet"

    release.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert len(calls) == 2, "the queued follow-up must drain as its own turn"
    drain_message, drain_override = calls[1]
    assert drain_message is thread_followup
    assert drain_override == "queued"
    assert thread_id not in queued_bot._processing, (  # pyright: ignore[reportPrivateUsage]
        "registration must be fully released after drain"
    )
    assert thread_id not in queued_bot._pending  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_channel_branch_processing_registration_does_not_leak_on_exception(
    queued_bot: DaimonBot,
) -> None:
    """Defense-in-depth: even if an exception escapes _handle_mention (real
    code never lets this happen -- its own boundary catches
    everything), on_message's channel-branch finally must still discard/pop
    the registered thread id so self._processing never leaks a stale entry.
    """
    thread_id = 55555555

    async def stub(
        message: discord.Message,
        guild_id: str,
        tenant_id: uuid.UUID,
        *,
        content_override: str | None = None,
        created_thread_ids: list[int] | None = None,
        attachments_override: list[discord.Attachment] | None = None,
    ) -> None:
        if created_thread_ids is not None:
            queued_bot._processing.add(thread_id)  # pyright: ignore[reportPrivateUsage]
            created_thread_ids.append(thread_id)
        raise RuntimeError("simulated escape")

    queued_bot._handle_mention = stub  # type: ignore[method-assign]

    channel_msg = _make_channel_message(content="first")

    await queued_bot.on_message(channel_msg)  # must not raise (outer boundary)

    assert thread_id not in queued_bot._processing, (  # pyright: ignore[reportPrivateUsage]
        "a registered thread id must never leak into _processing"
    )
    assert thread_id not in queued_bot._pending  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_queued_followup_drains_even_when_originating_turn_fails(
    queued_bot: DaimonBot,
) -> None:
    """Failure mode (b) / drain-on-failure: a follow-up mention queued
    behind a bot-created thread whose originating turn subsequently FAILS
    must still get its drain turn -- never silently discarded.

    Drives the real `_handle_mention` boundary (only `_orchestrate` is
    stubbed) so the failure is genuinely absorbed the way production code
    absorbs it, and the drain-always behavior in on_message's channel branch
    is exercised for real.
    """
    thread_id = 77777777
    entered = asyncio.Event()
    release = asyncio.Event()
    orchestrate_calls: list[str | None] = []

    async def fake_orchestrate(
        message: discord.Message,
        guild_id: str,
        tenant_id: uuid.UUID,
        *,
        content_override: str | None = None,
        created_thread_ids: list[int] | None = None,
        attachments_override: list[discord.Attachment] | None = None,
        unprompted: bool = False,
        failure_surface: object = None,
    ) -> None:
        orchestrate_calls.append(content_override)
        if created_thread_ids is not None:
            # Mirrors real _orchestrate: register immediately, report back.
            queued_bot._processing.add(thread_id)  # pyright: ignore[reportPrivateUsage]
            created_thread_ids.append(thread_id)
        entered.set()
        await release.wait()
        raise SQLAlchemyError("db down mid-turn")

    queued_bot._orchestrate = fake_orchestrate  # type: ignore[method-assign]

    channel_msg = _make_channel_message(content="first")
    thread_followup = _make_thread_message(content="queued-during-failure", thread_id=thread_id)

    task = asyncio.create_task(queued_bot.on_message(channel_msg))
    await entered.wait()
    entered.clear()

    # Bounded, not indefinite: on pre-fix code the follow-up dives straight
    # into the handler and blocks on `release.wait()` instead of queueing.
    await asyncio.wait_for(queued_bot.on_message(thread_followup), timeout=2.0)
    assert queued_bot._pending[thread_id] == [thread_followup]  # pyright: ignore[reportPrivateUsage]

    release.set()
    # must not raise -- _handle_mention's own boundary absorbs the failure
    await asyncio.wait_for(task, timeout=2.0)

    assert orchestrate_calls == [None, "queued-during-failure"], (
        "the queued follow-up must still run its drain turn after the "
        "originating turn failed, never be silently discarded"
    )
    assert thread_id not in queued_bot._processing  # pyright: ignore[reportPrivateUsage]
    assert thread_id not in queued_bot._pending  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Attachments on ALL of an author's queued messages must reach the
# composite drain turn, not just author_msgs[0]'s.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_merges_attachments_from_all_of_authors_queued_messages(
    queued_bot: DaimonBot,
) -> None:
    """On main, the drain call only ever threads author_msgs[0].attachments
    through to _orchestrate (there is no attachments_override kwarg), so an
    attachment on a LATER queued message from the same author silently
    vanishes -- this test fails pre-fix (no attachments_override captured,
    or it doesn't carry the second message's attachment).
    """
    calls: list[tuple[discord.Message, str | None, list[discord.Attachment] | None]] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def stub(
        message: discord.Message,
        guild_id: str,
        tenant_id: uuid.UUID,
        *,
        content_override: str | None = None,
        created_thread_ids: list[int] | None = None,
        attachments_override: list[discord.Attachment] | None = None,
    ) -> None:
        calls.append((message, content_override, attachments_override))
        entered.set()
        await release.wait()

    queued_bot._handle_mention = stub  # type: ignore[method-assign]

    m1 = _make_thread_message(content="first")
    q1 = _make_thread_message(content="A", author_id=42, display_name="Alice")
    q2 = _make_thread_message(content="B", author_id=42, display_name="Alice")
    attachment = MagicMock(spec=discord.Attachment)
    q2.attachments = [attachment]

    task = asyncio.create_task(queued_bot.on_message(m1))
    await entered.wait()
    entered.clear()

    await queued_bot.on_message(q1)
    await queued_bot.on_message(q2)
    assert len(calls) == 1

    release.set()
    await task

    assert len(calls) == 2, "two queued mentions from one author must merge into ONE turn"
    _, drain_override, drain_attachments = calls[1]
    assert drain_override == "A\n\nB"
    assert drain_attachments == [attachment], (
        "attachments from ALL of the author's queued messages must reach the "
        "composite drain turn, not just the first message's"
    )


# ---------------------------------------------------------------------------
# Queue-before-reaction ordering (Discord twin of Slack WR-05).
# ---------------------------------------------------------------------------


def _recording_stub(
    calls: list[tuple[discord.Message, str | None]],
    entered: asyncio.Event,
    release: asyncio.Event,
):
    async def stub(
        message: discord.Message,
        guild_id: str,
        tenant_id: uuid.UUID,
        *,
        content_override: str | None = None,
        created_thread_ids: list[int] | None = None,
        attachments_override: list[discord.Attachment] | None = None,
    ) -> None:
        calls.append((message, content_override))
        entered.set()
        await release.wait()

    return stub


@pytest.mark.asyncio
async def test_queued_mention_survives_turn_ending_during_reaction(
    queued_bot: DaimonBot,
) -> None:
    """A mention queued while the ⌛ reaction is in flight must still be drained.

    If the in-flight turn finishes its drain loop and ``finally`` while the
    queued mention's ``add_reaction`` await is suspended, appending to
    ``_pending`` only after the reaction strands the mention: nothing drains
    that thread until an unrelated later mention arrives.
    """
    calls: list[tuple[discord.Message, str | None]] = []
    entered = asyncio.Event()
    release = asyncio.Event()
    queued_bot._handle_mention = _recording_stub(calls, entered, release)  # type: ignore[method-assign]

    m1 = _make_thread_message(content="first")
    q1 = _make_thread_message(content="queued")
    reaction_started = asyncio.Event()
    reaction_release = asyncio.Event()

    async def slow_reaction(_emoji: str) -> None:
        reaction_started.set()
        await reaction_release.wait()

    q1.add_reaction = AsyncMock(side_effect=slow_reaction)

    turn = asyncio.create_task(queued_bot.on_message(m1))
    await entered.wait()
    entered.clear()

    queued = asyncio.create_task(queued_bot.on_message(q1))
    await reaction_started.wait()  # q1 is suspended inside add_reaction

    release.set()  # turn 1 finishes: drain loop + finally run now
    done, _ = await asyncio.wait({turn}, timeout=5)
    assert turn in done

    reaction_release.set()
    await queued

    q1.remove_reaction.assert_awaited_with("⌛", queued_bot.user)
    assert not queued_bot._queued_reactions
    assert [c[0] for c in calls] == [m1, q1], "queued mention was stranded, not drained"
    assert calls[1][1] == "queued"
    assert 789 not in queued_bot._pending  # pyright: ignore[reportPrivateUsage]
    assert 789 not in queued_bot._processing  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_queued_mention_survives_reaction_failure(queued_bot: DaimonBot) -> None:
    """A failed ⌛ reaction (e.g. missing Add Reactions permission) is cosmetic.

    The mention must still be queued and drained, and no error message posted.
    """
    calls: list[tuple[discord.Message, str | None]] = []
    entered = asyncio.Event()
    release = asyncio.Event()
    queued_bot._handle_mention = _recording_stub(calls, entered, release)  # type: ignore[method-assign]

    m1 = _make_thread_message(content="first")
    q1 = _make_thread_message(content="queued")
    response = MagicMock(status=403, reason="Forbidden")
    q1.add_reaction = AsyncMock(side_effect=discord.Forbidden(response, "Missing Permissions"))

    turn = asyncio.create_task(queued_bot.on_message(m1))
    await entered.wait()
    entered.clear()

    await queued_bot.on_message(q1)

    release.set()
    await turn

    assert [c[0] for c in calls] == [m1, q1]
    q1.channel.send.assert_not_called()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# A mention queued behind an out-of-turn continuation dispatch.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mention_queued_during_a_continuation_dispatch_gets_its_own_turn(
    queued_bot: DaimonBot,
) -> None:
    """A mention that lands while a form's continuation turn holds the thread is drained.

    `dispatch_continuations_in_thread` holds `_processing`, so the mention
    queues behind it with ⌛ exactly as it would behind a mention turn. When
    the dispatch ends, the queued mention must get its own turn instead of
    waiting for the next mention in the thread.
    """
    calls: list[tuple[discord.Message, str | None]] = []

    async def stub(
        message: discord.Message,
        guild_id: str,
        tenant_id: uuid.UUID,
        *,
        content_override: str | None = None,
        created_thread_ids: list[int] | None = None,
        attachments_override: list[discord.Attachment] | None = None,
    ) -> None:
        calls.append((message, content_override))

    queued_bot._handle_mention = stub  # type: ignore[method-assign]
    q1 = _make_thread_message(content="and the chart too?")
    thread = MagicMock(spec=discord.Thread)
    thread.id = 789

    async def _continuation_turn_while_a_mention_arrives(**_kwargs: object) -> None:
        await queued_bot.on_message(q1)
        assert queued_bot._pending.get(789) == [q1], (  # pyright: ignore[reportPrivateUsage]
            "a mention during the dispatch must queue behind it, not run beside it"
        )

    with patch.object(
        queued_bot,
        "_dispatch_continuations",
        side_effect=_continuation_turn_while_a_mention_arrives,
    ):
        await queued_bot.dispatch_continuations_in_thread(
            tenant_id=derive_tenant_uuid(platform="discord", workspace_id="123456"),
            thread=thread,
            guild_id="123456",
        )

    q1.add_reaction.assert_awaited_once_with("⌛")  # type: ignore[attr-defined]
    assert calls == [(q1, "and the chart too?")], (
        f"the queued mention must get its own turn once the dispatch ends, got {calls}"
    )
    assert 789 not in queued_bot._pending  # pyright: ignore[reportPrivateUsage]
    assert 789 not in queued_bot._processing  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_mention_queued_during_a_raising_continuation_dispatch_still_gets_its_turn(
    queued_bot: DaimonBot,
) -> None:
    """A dispatch that raises still drains the mention queued behind it.

    `dispatch_pending_continuations` can raise (a DB error, say), and the
    spawned dispatch task only logs it. The mention that queued (⌛) during the
    dispatch must still get its own turn -- the same drain-always contract the
    mention path keeps when its originating turn fails -- rather than sitting
    under ⌛ until some later mention in the thread. The dispatch's error
    still propagates to the caller.
    """
    calls: list[tuple[discord.Message, str | None]] = []

    async def stub(
        message: discord.Message,
        guild_id: str,
        tenant_id: uuid.UUID,
        *,
        content_override: str | None = None,
        created_thread_ids: list[int] | None = None,
        attachments_override: list[discord.Attachment] | None = None,
    ) -> None:
        calls.append((message, content_override))

    queued_bot._handle_mention = stub  # type: ignore[method-assign]
    q1 = _make_thread_message(content="and the chart too?")
    thread = MagicMock(spec=discord.Thread)
    thread.id = 789

    async def _mention_arrives_then_dispatch_fails(**_kwargs: object) -> None:
        await queued_bot.on_message(q1)
        assert queued_bot._pending.get(789) == [q1]  # pyright: ignore[reportPrivateUsage]
        raise RuntimeError("continuation dispatch failed")

    with (
        patch.object(
            queued_bot,
            "_dispatch_continuations",
            side_effect=_mention_arrives_then_dispatch_fails,
        ),
        pytest.raises(RuntimeError, match="continuation dispatch failed"),
    ):
        await queued_bot.dispatch_continuations_in_thread(
            tenant_id=derive_tenant_uuid(platform="discord", workspace_id="123456"),
            thread=thread,
            guild_id="123456",
        )

    q1.add_reaction.assert_awaited_once_with("⌛")  # type: ignore[attr-defined]
    assert calls == [(q1, "and the chart too?")], (
        f"the queued mention must get its own turn even when the dispatch raises, got {calls}"
    )
    assert 789 not in queued_bot._pending  # pyright: ignore[reportPrivateUsage]
    assert 789 not in queued_bot._processing  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_a_drained_follow_up_re_queues_behind_another_tenants_waiting_turn(
    queued_bot: DaimonBot,
) -> None:
    """Global cap 1. A thread's follow-up must not inherit its first turn's
    slot: when the first turn ends, another tenant's waiting turn runs, and
    the follow-up queues behind it (review #4), with a fresh ticket (#2)."""
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id="123456")
    queued_bot.turn_queue.global_cap = 1
    order: list[str] = []
    first_running = asyncio.Event()
    release_first = asyncio.Event()

    async def stub(message: discord.Message, *_args: object, **_kwargs: object) -> None:
        slot = await wait_for_slot(
            asyncio.Event(), sessionmaker=queued_bot.runtime.sessionmaker, tenant_id=tenant_id
        )
        assert slot == "started"
        order.append("A-first" if not order else "A-follow-up")
        if len(order) == 1:
            first_running.set()
            await release_first.wait()

    queued_bot._orchestrate = stub  # type: ignore[method-assign]
    first = asyncio.create_task(queued_bot.on_message(_make_thread_message(content="one")))
    await first_running.wait()
    await queued_bot.on_message(_make_thread_message(content="two"))  # queues in the thread
    other = queued_bot.turn_queue.admit(uuid.uuid4(), cap=1)  # another server's turn
    assert other is not None and other.queued

    release_first.set()
    async with asyncio.timeout(5):
        while not queued_bot.turn_queue.depth(tenant_id):
            await asyncio.sleep(0.01)
    assert other.state == "running", "the other server's turn got the freed slot"
    assert order == ["A-first"], "the follow-up waits its turn in the queue"
    other.release()
    async with asyncio.timeout(5):
        await first
    assert order == ["A-first", "A-follow-up"]
    assert queued_bot.turn_queue.in_flight() == 0 and queued_bot.turn_queue.depth() == 0


@pytest.mark.parametrize("outcome", ["done", "failed", "cancelled"])
async def test_queue_markers_clear_for_every_message_on_settle(queued_bot, outcome):
    messages = [_make_thread_message(author_id=111), _make_thread_message(author_id=111)]
    queued_bot._processing.add(789)
    for message in messages:
        await queued_bot._queue_behind_inflight_turn(789, message)

    async def turn(*args, **kwargs):
        for message in messages:
            message.remove_reaction.assert_not_awaited()
        if outcome == "failed":
            raise RuntimeError("turn failed")
        if outcome == "cancelled":
            raise asyncio.CancelledError

    queued_bot._handle_mention = turn
    if outcome == "done":
        await queued_bot._drain_pending_mentions(789, "123456", uuid.uuid4())
    else:
        with pytest.raises(RuntimeError if outcome == "failed" else asyncio.CancelledError):
            await queued_bot._drain_pending_mentions(789, "123456", uuid.uuid4())
    for message in messages:
        message.add_reaction.assert_awaited_once_with("⌛")
        message.remove_reaction.assert_awaited_once_with("⌛", queued_bot.user)
    assert not queued_bot._queued_reactions


async def test_queue_cleanup_failure_does_not_skip_other_messages(queued_bot):
    messages = [_make_thread_message(), _make_thread_message()]
    messages[0].remove_reaction.side_effect = ConnectionResetError("disconnected")
    queued_bot._pending[789] = messages
    queued_bot._handle_mention = AsyncMock()
    await queued_bot._drain_pending_mentions(789, "123456", uuid.uuid4())
    messages[1].remove_reaction.assert_awaited_once_with("⌛", queued_bot.user)


async def test_early_eyes_precede_admission_and_clear_on_failure(queued_bot):
    message = _make_thread_message()
    admission_entered = asyncio.Event()
    release = asyncio.Event()

    async def admission(*args, **kwargs):
        message.add_reaction.assert_awaited_once_with("👀")
        message.remove_reaction.assert_not_awaited()
        admission_entered.set()
        await release.wait()
        raise RuntimeError("admission unavailable")

    with patch("daimon.adapters.discord.bot.admit", side_effect=admission):
        queued_bot._render_turn_error = AsyncMock()
        task = asyncio.create_task(queued_bot._handle_mention(message, "123456", uuid.uuid4()))
        await asyncio.wait_for(admission_entered.wait(), timeout=5)
        assert not task.done()
        release.set()
        await task
    message.remove_reaction.assert_awaited_once_with("👀", queued_bot.user)


@pytest.mark.parametrize("cancelled", [False, True])
async def test_early_eyes_clear_after_orchestration_and_add_failure_is_cosmetic(
    queued_bot, cancelled
):
    message = _make_thread_message()
    message.add_reaction.side_effect = ConnectionResetError("disconnected")
    queued_bot._orchestrate = AsyncMock(side_effect=asyncio.CancelledError if cancelled else None)
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await queued_bot._handle_mention(message, "123456", uuid.uuid4())
    else:
        await queued_bot._handle_mention(message, "123456", uuid.uuid4())
    queued_bot._orchestrate.assert_awaited_once()
    message.remove_reaction.assert_awaited_once_with("👀", queued_bot.user)


@pytest.mark.parametrize("unprompted,override", [(True, None), (False, "queued")])
async def test_unprompted_and_queued_turns_do_not_add_eyes(queued_bot, unprompted, override):
    message = _make_thread_message()
    queued_bot._orchestrate = AsyncMock()
    await queued_bot._handle_mention(
        message, "123456", uuid.uuid4(), content_override=override, unprompted=unprompted
    )
    message.add_reaction.assert_not_awaited()
    message.remove_reaction.assert_not_awaited()


async def test_cancelled_drain_clears_unrun_authors_in_popped_batch(queued_bot):
    messages = [_make_thread_message(author_id=111), _make_thread_message(author_id=222)]
    for message in messages:
        await queued_bot._queue_behind_inflight_turn(789, message)
    queued_bot._handle_mention = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await queued_bot._drain_pending_mentions(789, "123456", uuid.uuid4())
    assert queued_bot._handle_mention.await_count == 1
    for message in messages:
        message.remove_reaction.assert_awaited_with("⌛", queued_bot.user)
    assert not queued_bot._queued_reactions
