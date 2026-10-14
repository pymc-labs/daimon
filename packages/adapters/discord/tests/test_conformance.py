"""Actual Discord queue/drain controls; synthetic callbacks are not native C01 certification."""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from collections.abc import Callable, Mapping
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import daimon.adapters.discord.bot as bot_module
import daimon.testing.adapters.discord.conformance as conformance_module
import discord
import pytest
import pytest_asyncio
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.config import McpSettings, TurnQueueSettings
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.testing.adapters.discord.conformance import (
    AdmissionBarrier,
    MentionTurn,
    MultiHumanEnvelope,
    ThreadInputs,
    multi_human_evidence,
)
from daimon.testing.turn_fakes import deps_for
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from .harness import make_bot

if TYPE_CHECKING:
    from discord.types.message import Message as MessagePayload
    from discord.types.threads import Thread as ThreadPayload

pytestmark = pytest.mark.asyncio

# Use the adapter conftest's shared schema, engine and cleanup lifetimes.
# These callbacks still need separate pooled connections and committed rows;
# a module-local fixture alias must not replace the schema admission dependency.


@pytest_asyncio.fixture
async def bot(db_engine: AsyncEngine, db_clean: None) -> DaimonBot:
    del db_clean
    # Admission reads can overlap while the first real ingress task is held.
    # Give each production session its own pooled connection.
    sessions = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    await provision_tenant(
        sessions, platform="discord", workspace_id="60", signup_credit=Decimal("10")
    )
    settings = MagicMock()
    settings.mcp = McpSettings()
    settings.turn_queue = TurnQueueSettings()
    settings.discord.max_concurrent_turns_per_tenant = 100
    settings.discord.max_concurrent_turns = None
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=AsyncMock(),
        sessionmaker=sessions,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=replace(deps_for(), turn_path=None),
    )
    return make_bot(runtime)


def _message(message_id: int, *, thread: int, human: int, text: str) -> discord.Message:
    message = MagicMock(spec=discord.Message)
    message.id = message_id
    message.channel = MagicMock(spec=discord.Thread)
    message.channel.id = thread
    message.channel.parent_id = 50
    message.guild = MagicMock(spec=discord.Guild)
    message.guild.id = 60
    message.author = MagicMock(spec=discord.Member)
    message.author.id = human
    message.author.bot = False
    message.author.display_name = f"Human {human}"
    message.mentions = [SimpleNamespace(id=999)]
    message.channel.send = AsyncMock()
    message.content = text
    attachment = MagicMock(spec=discord.Attachment)
    attachment.id = message_id + 1000
    message.attachments = [attachment]
    message.add_reaction = AsyncMock()
    return cast(discord.Message, message)


async def _run(_turn: MentionTurn, barrier: AdmissionBarrier | None) -> None:
    if barrier is not None:
        # The real adapter admission has selected this callback. Native
        # preparation/root acceptance remains provider-envelope evidence.
        barrier.started.set()
        await barrier.release.wait()


def _envelope(bot: DaimonBot) -> MultiHumanEnvelope:
    return MultiHumanEnvelope(
        bot=bot,
        tenant_id=derive_tenant_uuid(platform="discord", workspace_id="60"),
        guild_id="60",
        channel_id=50,
        threads=(
            ThreadInputs(
                _message(1, thread=70, human=10, text="initial-a"),
                (
                    _message(2, thread=70, human=20, text="b-first"),
                    _message(3, thread=70, human=10, text="a-followup"),
                    _message(4, thread=70, human=20, text="b-second"),
                ),
            ),
            ThreadInputs(
                _message(5, thread=80, human=20, text="initial-b"),
                (_message(6, thread=80, human=10, text="other-thread"),),
            ),
        ),
        run=_run,
        timeout_s=30.0,
    )


def _assert_platform_oracle(evidence: Mapping[str, object]) -> None:
    assert evidence["threads"] == (
        {
            "thread_id": "70",
            "received_message_ids": ("1", "2", "3", "4"),
            "queued_message_ids": ("2", "3", "4"),
            "claimed_while_queued": True,
            "first_callback_finished_while_queued": False,
        },
        {
            "thread_id": "80",
            "received_message_ids": ("5", "6"),
            "queued_message_ids": ("6",),
            "claimed_while_queued": True,
            "first_callback_finished_while_queued": False,
        },
    )
    batches = cast(tuple[dict[str, object], ...], evidence["batches"])
    assert [
        (b["thread_id"], b["trigger_message_id"], b["caller_id"], b["content"], b["attachment_ids"])
        for b in batches
    ] == [
        ("70", "1", "10", "initial-a", ("1001",)),
        ("70", "2", "20", "b-first\n\nb-second", ("1002", "1004")),
        ("70", "3", "10", "a-followup", ("1003",)),
        ("80", "5", "20", "initial-b", ("1005",)),
        ("80", "6", "10", "other-thread", ("1006",)),
    ]
    assert [b["content_overridden"] for b in batches] == [False, True, True, False, True]
    assert evidence["peak_callbacks_by_thread"] == {"70": 1, "80": 1}
    assert evidence["queue_empty_after"] is evidence["processing_empty_after"] is True


async def test_actual_drain_preserves_per_caller_order_bytes_attachments_and_thread_ownership(
    bot: DaimonBot,
) -> None:
    """Main #813 f84699cf6 drain keyword compatibility; original oracle unchanged."""
    envelope = _envelope(bot)
    original = envelope.bot._handle_mention  # pyright: ignore[reportPrivateUsage]
    seen: list[MentionTurn] = []

    async def run(turn: MentionTurn, barrier: AdmissionBarrier | None) -> None:
        seen.append(turn)
        await _run(turn, barrier)

    evidence = await multi_human_evidence(replace(envelope, run=run))
    _assert_platform_oracle(evidence)
    assert [t.message.id for t in seen] == [1, 2, 3, 5, 6]
    assert all(t.guild_id == "60" and t.tenant_id == envelope.tenant_id for t in seen)
    assert seen[1].attachments_override == (
        envelope.threads[0].queued[0].attachments[0],
        envelope.threads[0].queued[2].attachments[0],
    )
    assert envelope.bot._handle_mention == original  # pyright: ignore[reportPrivateUsage]
    assert "_handle_mention" not in envelope.bot.__dict__
    for thread in envelope.threads:
        for message in thread.queued:
            cast(AsyncMock, message.add_reaction).assert_awaited_once_with("⌛")


@pytest.mark.parametrize("failure", ["exception", "timeout", "cancel"])
async def test_failure_restores_handler_and_cleans_only_probe_threads(
    bot: DaimonBot, failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope = _envelope(bot)
    original = envelope.bot._handle_mention  # pyright: ignore[reportPrivateUsage]
    entered = asyncio.Event()
    deadline = asyncio.timeout(None)

    def controlled_timeout(delay: float) -> asyncio.Timeout:
        assert delay == 2.0
        return deadline

    # Only the explicit probe gets a controlled real Timeout. Keep admission
    # outside the timing race, then expire it at the actual callback boundary.
    monkeypatch.setattr(
        conformance_module,
        "asyncio",
        SimpleNamespace(**{**vars(asyncio), "timeout": controlled_timeout}),
    )

    async def run(_turn: MentionTurn, barrier: AdmissionBarrier | None) -> None:
        assert barrier is not None
        entered.set()
        if failure == "exception":
            raise LookupError("controlled callback failure")
        await asyncio.Event().wait()

    task = asyncio.create_task(multi_human_evidence(replace(envelope, run=run, timeout_s=2.0)))
    waiter = asyncio.create_task(entered.wait())
    expected = {
        "exception": LookupError,
        "timeout": TimeoutError,
        "cancel": asyncio.CancelledError,
    }[failure]
    try:
        async with asyncio.timeout(30):
            done, _ = await asyncio.wait((task, waiter), return_when=asyncio.FIRST_COMPLETED)
            if waiter not in done:
                await task
                pytest.fail("probe finished without entering the real callback")
            if failure == "timeout":
                deadline.reschedule(asyncio.get_running_loop().time())
            elif failure == "cancel":
                task.cancel()
            with pytest.raises(expected):
                await task
    finally:
        # Even a setup/entry/assertion failure must not strand admission tasks.
        task.cancel()
        waiter.cancel()
        await asyncio.gather(task, waiter, return_exceptions=True)
    assert deadline.expired() is (failure == "timeout")
    assert envelope.bot._handle_mention == original  # pyright: ignore[reportPrivateUsage]
    assert not envelope.bot._processing and not envelope.bot._pending  # pyright: ignore[reportPrivateUsage]
    assert envelope.bot.turn_queue.in_flight(envelope.tenant_id) == 0
    assert envelope.bot.turn_queue.depth() == 0


async def test_early_completed_callback_is_rejected_and_existing_instance_handler_restored(
    bot: DaimonBot,
) -> None:
    envelope = _envelope(bot)
    original = envelope.bot._handle_mention  # pyright: ignore[reportPrivateUsage]
    envelope.bot.__dict__["_handle_mention"] = original

    async def early(_turn: MentionTurn, barrier: AdmissionBarrier | None) -> None:
        assert barrier is not None
        barrier.started.set()

    with pytest.raises(RuntimeError, match="finished before queued inputs"):
        await multi_human_evidence(replace(envelope, run=early))
    assert envelope.bot.__dict__["_handle_mention"] is original


async def test_reaction_failure_retains_actual_queued_message(bot: DaimonBot) -> None:
    """Main #813 f84699cf6 drain keyword compatibility; queued bytes unchanged."""
    envelope = _envelope(bot)
    envelope.threads[0].queued[0].add_reaction = AsyncMock(
        side_effect=RuntimeError("offline reaction")
    )
    evidence = await multi_human_evidence(envelope)
    batches = cast(tuple[dict[str, object], ...], evidence["batches"])
    assert batches[1]["content"] == "b-first\n\nb-second"


@pytest.mark.parametrize(
    "invalid",
    [
        "busy",
        "pending",
        "thread",
        "guild",
        "channel",
        "duplicate",
        "robot",
        "one_thread",
        "one_human",
        "timeout",
        "tenant",
    ],
)
async def test_invalid_envelope_fails_before_callback_or_queue_side_effects(
    bot: DaimonBot, invalid: str
) -> None:
    envelope = _envelope(bot)
    if invalid == "busy":
        envelope.bot._processing.add(999)  # pyright: ignore[reportPrivateUsage]
    elif invalid == "pending":
        envelope.bot._pending[999] = []  # pyright: ignore[reportPrivateUsage]
    elif invalid == "thread":
        envelope.threads[0].queued[0].channel.id = 99
    elif invalid == "guild":
        cast(MagicMock, envelope.threads[0].queued[0].guild).id = 99
    elif invalid == "channel":
        cast(MagicMock, envelope.threads[1].first.channel).parent_id = 99
    elif invalid == "duplicate":
        envelope.threads[0].queued[0].id = 1
    elif invalid == "robot":
        envelope.threads[0].queued[0].author.bot = True
    elif invalid == "one_thread":
        envelope = replace(envelope, threads=envelope.threads[:1])
    elif invalid == "one_human":
        for thread in envelope.threads:
            for message in (thread.first, *thread.queued):
                message.author.id = 10
    elif invalid == "tenant":
        envelope = replace(
            envelope, tenant_id=derive_tenant_uuid(platform="discord", workspace_id="99")
        )
    else:
        envelope = replace(envelope, timeout_s=float("nan"))
    run = AsyncMock()
    original = envelope.bot._handle_mention  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(ValueError):
        await multi_human_evidence(replace(envelope, run=run))
    run.assert_not_awaited()
    assert envelope.bot._handle_mention == original  # pyright: ignore[reportPrivateUsage]
    if invalid == "busy":
        assert envelope.bot._processing == {999}  # pyright: ignore[reportPrivateUsage]
    if invalid == "pending":
        assert envelope.bot._pending == {999: []}  # pyright: ignore[reportPrivateUsage]


def _sdk_message(
    bot: DaimonBot, message_id: int, *, thread_id: int, human_id: int
) -> discord.Message:
    guild = MagicMock(spec=discord.Guild)
    guild.id = 60
    guild.get_member.return_value = None
    state = bot._connection  # pyright: ignore[reportPrivateUsage]
    channel = discord.Thread(
        guild=guild,
        state=state,
        data=cast(
            "ThreadPayload",
            {
                "id": str(thread_id),
                "parent_id": "50",
                "owner_id": str(human_id),
                "name": "offline-thread",
                "type": 11,
                "message_count": 0,
                "member_count": 0,
                "thread_metadata": {
                    "archived": False,
                    "auto_archive_duration": 60,
                    "archive_timestamp": "2026-10-10T00:00:00Z",
                    "locked": False,
                },
            },
        ),
    )
    return discord.Message(
        state=state,
        channel=channel,
        data=cast(
            "MessagePayload",
            {
                "id": str(message_id),
                "type": 0,
                "mentions": [
                    {
                        "id": "999",
                        "username": "offline-bot",
                        "discriminator": "0",
                        "avatar": None,
                        "bot": True,
                    }
                ],
                "content": f"native-sdk-{message_id} — exact",
                "author": {
                    "id": str(human_id),
                    "username": f"human-{human_id}",
                    "discriminator": "0",
                    "avatar": None,
                    "bot": False,
                },
                "attachments": [
                    {
                        "id": str(message_id + 1000),
                        "filename": "input.bin",
                        "size": 256,
                        "url": "https://offline.invalid/input.bin",
                        "proxy_url": "https://offline.invalid/input.bin",
                    }
                ],
            },
        ),
    )


async def test_real_discord_sdk_messages_threads_users_attachments_and_reaction_path(
    bot: DaimonBot,
) -> None:
    """Main #813 f84699cf6 drain keyword compatibility; SDK identities unchanged."""
    inputs = tuple(
        ThreadInputs(
            _sdk_message(bot, first, thread_id=thread, human_id=10),
            (_sdk_message(bot, first + 1, thread_id=thread, human_id=20),),
        )
        for first, thread in ((101, 70), (201, 80))
    )
    reactions = AsyncMock()
    with patch.object(bot.http, "add_reaction", reactions):
        evidence = await multi_human_evidence(
            MultiHumanEnvelope(
                bot=bot,
                tenant_id=derive_tenant_uuid(platform="discord", workspace_id="60"),
                guild_id="60",
                channel_id=50,
                threads=inputs,
                run=_run,
                timeout_s=30.0,
            )
        )
    assert isinstance(inputs[0].first, discord.Message)
    assert isinstance(inputs[0].first.channel, discord.Thread)
    assert isinstance(inputs[0].first.author, discord.User)
    assert isinstance(inputs[0].first.attachments[0], discord.Attachment)
    batches = cast(tuple[dict[str, object], ...], evidence["batches"])
    assert [batch["content"] for batch in batches] == [
        "native-sdk-101 — exact",
        "native-sdk-102 — exact",
        "native-sdk-201 — exact",
        "native-sdk-202 — exact",
    ]
    assert [batch["attachment_ids"] for batch in batches] == [
        ("1101",),
        ("1102",),
        ("1201",),
        ("1202",),
    ]
    assert reactions.await_count == 2
    assert [(c.args[0], c.args[1], c.args[2]) for c in reactions.await_args_list] == [
        (70, 102, "⌛"),
        (80, 202, "⌛"),
    ]


@pytest.mark.parametrize("mutation", ["busy_thread_admission", "cross_author_batching"])
async def test_production_mutation_fails_the_same_independent_platform_oracle(
    bot: DaimonBot, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    if mutation == "busy_thread_admission":
        # Mutate the exact production busy-thread branch reported in B1, in
        # process only. All protection/liveness/slot/finally code stays real.
        source = textwrap.dedent(inspect.getsource(DaimonBot.on_message))
        tree = ast.parse(source)
        matching = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and ast.unparse(node.test) == "thread_id in self._processing"
        ]
        assert len(matching) == 1, "production admission branch must exist"
        matching[0].test = ast.Constant(value=False)
        ast.fix_missing_locations(tree)
        namespace: dict[str, Any] = vars(bot_module).copy()
        exec(compile(tree, "<mutated-production-on_message>", "exec"), namespace)
        monkeypatch.setattr(DaimonBot, "on_message", namespace["on_message"])
    else:

        def flatten(
            pending: list[discord.Message], _author: Callable[[discord.Message], int]
        ) -> list[list[discord.Message]]:
            return [pending] if pending else []

        monkeypatch.setattr(bot_module, "group_by_author", flatten)
    evidence = await multi_human_evidence(_envelope(bot))
    if mutation == "busy_thread_admission":
        threads = cast(tuple[dict[str, object], ...], evidence["threads"])
        assert [thread["queued_message_ids"] for thread in threads] == [(), ()]
        assert evidence["peak_callbacks_by_thread"] == {"70": 2, "80": 2}
    with pytest.raises(AssertionError):
        _assert_platform_oracle(evidence)
    assert evidence["queue_empty_after"] is evidence["processing_empty_after"] is True


@pytest.mark.parametrize("exit_mode", ["normal", "cancel"])
async def test_production_release_resumes_continuation_deferred_during_callback(
    bot: DaimonBot,
    exit_mode: str,
) -> None:
    """Main #813 f84699cf6 drain keyword compatibility; release assertions unchanged."""
    envelope = _envelope(bot)
    resume = AsyncMock()
    entered = asyncio.Event()

    async def run(turn: MentionTurn, barrier: AdmissionBarrier | None) -> None:
        if barrier is not None:
            thread = cast(discord.Thread, turn.message.channel)
            bot._deferred_dispatch[thread.id] = (  # pyright: ignore[reportPrivateUsage]
                turn.tenant_id,
                thread,
                turn.guild_id,
            )
            if exit_mode == "cancel":
                entered.set()
                await asyncio.Event().wait()
        await _run(turn, barrier)

    with patch.object(bot, "dispatch_continuations_in_thread", resume):
        if exit_mode == "normal":
            evidence = await multi_human_evidence(replace(envelope, run=run))
            _assert_platform_oracle(evidence)
        else:
            task = asyncio.create_task(multi_human_evidence(replace(envelope, run=run)))
            async with asyncio.timeout(30):
                await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await asyncio.sleep(0)  # execute the bot's normal spawned resumes
    expected = [70, 80] if exit_mode == "normal" else [70]
    assert resume.await_count == len(expected)
    assert [call.kwargs["thread"].id for call in resume.await_args_list] == expected
    assert all(call.kwargs["tenant_id"] == envelope.tenant_id for call in resume.await_args_list)
    assert not bot._deferred_dispatch  # pyright: ignore[reportPrivateUsage]
