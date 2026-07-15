"""Per-thread mention queueing in DaimonBot.

Covers the queue-and-drain behavior: mentions arriving during an in-flight
turn for the same thread accumulate in self._pending and are drained after
the current turn completes into a single composite follow-up turn.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import pytest_asyncio
from daimon.adapters.discord.bot import (  # pyright: ignore[reportPrivateUsage]
    DaimonBot,
    _compose_queued_content,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.config import McpSettings
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _make_runtime(
    tenant_id: uuid.UUID,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> DiscordRuntime:
    _ = tenant_id  # runtime no longer carries tenant_id (D-06)
    settings = MagicMock()
    settings.mcp = McpSettings()
    discord_settings = MagicMock()
    discord_settings.max_concurrent_turns_per_tenant = 100  # effectively uncapped in tests
    settings.discord = discord_settings
    return DiscordRuntime(
        settings=settings,
        anthropic=AsyncMock(),
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )


def _make_bot(runtime: DiscordRuntime) -> DaimonBot:
    intents = discord.Intents.default()
    bot = DaimonBot(runtime=runtime, intents=intents)
    bot._connection.user = MagicMock()  # pyright: ignore[reportPrivateUsage]
    bot._connection.user.id = 999  # pyright: ignore[reportPrivateUsage]
    bot._connection.user.mentioned_in = MagicMock(return_value=True)  # pyright: ignore[reportPrivateUsage]
    return bot


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
    result = await provision_tenant(db_session_factory, platform="discord", workspace_id="123456")

    runtime = _make_runtime(result.tenant_id, db_session_factory)
    return _make_bot(runtime)


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
# CLB-02 (#170): error boundaries must never let a mention drop silently.
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
    bot = _make_bot(runtime)

    async def _raise_db_error(*args: object, **kwargs: object) -> None:
        raise SQLAlchemyError("db down")

    bot._orchestrate = _raise_db_error  # type: ignore[method-assign]

    message = _make_channel_message()

    await bot._handle_mention(message, "123456", tenant_id)  # pyright: ignore[reportPrivateUsage]

    message.channel.send.assert_called_once()  # type: ignore[attr-defined]
    error_text: str = message.channel.send.call_args[0][0]  # type: ignore[attr-defined]
    assert "rid:" in error_text, "boundary should render the error via render_error"


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
    bot = _make_bot(runtime)

    async def _raise_unexpected(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    bot._orchestrate = _raise_unexpected  # type: ignore[method-assign]

    message = _make_channel_message()

    await bot._handle_mention(message, "123456", tenant_id)  # pyright: ignore[reportPrivateUsage]

    message.channel.send.assert_called_once()  # type: ignore[attr-defined]
    error_text: str = message.channel.send.call_args[0][0]  # type: ignore[attr-defined]
    assert "rid:" in error_text, "catch-all boundary should also render via render_error"


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
    bot = _make_bot(runtime)
    message = _make_channel_message()

    await bot.on_message(message)  # must not raise

    message.channel.send.assert_called_once()  # type: ignore[attr-defined]
    error_text: str = message.channel.send.call_args[0][0]  # type: ignore[attr-defined]
    assert "rid:" in error_text, "prologue boundary should also render via render_error"


# ---------------------------------------------------------------------------
# CLB-03 (#163): a bot-created thread must be mutex-registered from the
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
    """CLB-03 failure mode (a): concurrent turn on the same session.

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
    code never lets this happen post-CLB-02 -- its own boundary catches
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

    await queued_bot.on_message(channel_msg)  # must not raise (CLB-02 outer boundary)

    assert thread_id not in queued_bot._processing, (  # pyright: ignore[reportPrivateUsage]
        "a registered thread id must never leak into _processing"
    )
    assert thread_id not in queued_bot._pending  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_queued_followup_drains_even_when_originating_turn_fails(
    queued_bot: DaimonBot,
) -> None:
    """CLB-03 failure mode (b) / drain-on-failure: a follow-up mention queued
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
# CLB-04: attachments on ALL of an author's queued messages must reach the
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
