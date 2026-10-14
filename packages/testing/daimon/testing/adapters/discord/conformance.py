"""Explicit, offline host probes over Discord's existing admission, queue and drain paths.

Nothing imports this module in the application. A probe needs a fresh bot and
normal host callbacks; it reports facts, never conformance verdicts. The caller
owns independent expectations, provider choreography and ledger assertions.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

import discord
from daimon.adapters.discord.bot import DaimonBot
from daimon.core.ma_identity import derive_tenant_uuid


@dataclass(frozen=True)
class MentionTurn:
    """Exactly the inputs passed by the actual Discord mention/drain path."""

    message: discord.Message
    guild_id: str
    tenant_id: uuid.UUID
    content_override: str | None
    attachments_override: tuple[discord.Attachment, ...] | None


@dataclass(frozen=True)
class AdmissionBarrier:
    """Host callback signals started after actual admission/root acceptance.

    It then waits for release before finishing that root, while the probe
    routes follow-ups through normal ingress. Neither event is an acceptance receipt.
    """

    started: asyncio.Event
    release: asyncio.Event


@dataclass(frozen=True)
class ThreadInputs:
    first: discord.Message
    queued: tuple[discord.Message, ...]


@dataclass(frozen=True)
class MultiHumanEnvelope:
    bot: DaimonBot
    tenant_id: uuid.UUID
    guild_id: str
    channel_id: int
    threads: tuple[ThreadInputs, ...]
    run: Callable[[MentionTurn, AdmissionBarrier | None], Awaitable[None]]
    timeout_s: float = 30.0


async def multi_human_evidence(envelope: MultiHumanEnvelope) -> Mapping[str, object]:
    """Capture actual queue/drain inputs; normal callbacks retain host ownership.

    Every message enters through `on_message`: protection/liveness reads,
    busy-thread routing, tenant slots, queue/drain and release remain owned by
    the bot. The capture handler observes callbacks selected by that path and
    forwards their inputs to the owner. The first root waits at an owner-supplied
    barrier; queued IDs and ownership are read from production queue state.
    Ledger, workspace, native-root and credential evidence remain separate.

    Failure restores the handler and cancels only this probe's ingress task;
    `on_message` performs its own finally/release. Use a dedicated offline bot.
    """
    bot = envelope.bot
    if (
        not math.isfinite(envelope.timeout_s)
        or envelope.timeout_s <= 0
        or len(envelope.threads) < 2
    ):
        raise ValueError("C01 requires two threads and a finite positive timeout")
    if envelope.tenant_id != derive_tenant_uuid(platform="discord", workspace_id=envelope.guild_id):
        raise ValueError("C01 tenant must match the normal Discord guild identity")
    # Do not replace a live handler or reuse an existing queue.
    if bot._processing or bot._pending:  # pyright: ignore[reportPrivateUsage]
        raise ValueError("C01 requires a fresh idle bot")
    seen_threads: set[int] = set()
    seen_messages: set[int] = set()
    humans: set[int] = set()
    for inputs in envelope.threads:
        if (
            not isinstance(inputs.first.channel, discord.Thread)
            or inputs.first.channel.parent_id != envelope.channel_id
            or not inputs.queued
        ):
            raise ValueError("C01 requires thread messages")
        thread_id = inputs.first.channel.id
        if thread_id in seen_threads:
            raise ValueError("C01 requires distinct thread envelopes")
        seen_threads.add(thread_id)
        for message in (inputs.first, *inputs.queued):
            if (
                not isinstance(message.channel, discord.Thread)
                or message.channel.id != thread_id
                or message.channel.parent_id != envelope.channel_id
                or message.guild is None
                or str(message.guild.id) != envelope.guild_id
                or message.author.bot
                or message.id in seen_messages
            ):
                raise ValueError("C01 messages must be unique humans in the specified guild/thread")
            seen_messages.add(message.id)
            humans.add(message.author.id)
    if len(humans) < 2:
        raise ValueError("C01 requires two distinct human callers")

    prior = bot.__dict__.get("_handle_mention")
    batches: list[Mapping[str, object]] = []
    threads: list[Mapping[str, object]] = []
    callback_errors: list[Exception] = []
    finished_callbacks: set[int] = set()
    active: dict[int, int] = {}
    peak: dict[int, int] = {}
    barrier: AdmissionBarrier | None = None
    first_id: int | None = None

    async def handle(
        message: discord.Message,
        guild_id: str,
        tenant_id: uuid.UUID,
        *,
        content_override: str | None = None,
        created_thread_ids: list[int] | None = None,
        attachments_override: list[discord.Attachment] | None = None,
        admitted_message_ids: tuple[str, ...] = (),
        # The drain passes the authored messages for query rendering. This
        # offline callback observes queue selection only; mirror the exact
        # keyword without rendering.
        query_messages: tuple[discord.Message, ...] = (),
    ) -> None:
        del created_thread_ids, admitted_message_ids, query_messages
        thread_id = message.channel.id
        active[thread_id] = active.get(thread_id, 0) + 1
        peak[thread_id] = max(peak.get(thread_id, 0), active[thread_id])
        batches.append(
            {
                "thread_id": str(thread_id),
                "trigger_message_id": str(message.id),
                "caller_id": str(message.author.id),
                "content": message.content if content_override is None else content_override,
                "content_overridden": content_override is not None,
                "attachment_ids": tuple(
                    str(a.id)
                    for a in (
                        message.attachments
                        if attachments_override is None
                        else attachments_override
                    )
                ),
            }
        )
        try:
            await envelope.run(
                MentionTurn(
                    message,
                    guild_id,
                    tenant_id,
                    content_override,
                    None if attachments_override is None else tuple(attachments_override),
                ),
                barrier if message.id == first_id else None,
            )
        except Exception as exc:
            # on_message is an event-handler boundary and renders errors. A
            # conformance callback failure must also fail this explicit probe.
            callback_errors.append(exc)
            raise
        finally:
            finished_callbacks.add(message.id)
            active[thread_id] -= 1

    bot._handle_mention = handle  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    try:
        async with asyncio.timeout(envelope.timeout_s):
            for inputs in envelope.threads:
                thread_id = inputs.first.channel.id
                first_id = inputs.first.id
                barrier = AdmissionBarrier(asyncio.Event(), asyncio.Event())
                task = asyncio.create_task(bot.on_message(inputs.first))
                waiter = asyncio.create_task(barrier.started.wait())
                try:
                    # A callback failure must surface immediately rather than
                    # hanging until the barrier deadline.
                    await asyncio.wait((task, waiter), return_when=asyncio.FIRST_COMPLETED)
                    if task.done():
                        await task
                        if callback_errors:
                            raise callback_errors[0]
                        raise RuntimeError("C01 first callback finished before queued inputs")
                    if active.get(thread_id) != 1:
                        raise RuntimeError("C01 first callback finished before queued inputs")
                    for message in inputs.queued:
                        await bot.on_message(message)
                        if callback_errors:
                            raise callback_errors[0]
                    pending = bot._pending.get(thread_id, [])  # pyright: ignore[reportPrivateUsage]
                    threads.append(
                        {
                            "thread_id": str(thread_id),
                            "received_message_ids": tuple(
                                str(m.id) for m in (inputs.first, *inputs.queued)
                            ),
                            "queued_message_ids": tuple(str(m.id) for m in pending),
                            "claimed_while_queued": thread_id in bot._processing,  # pyright: ignore[reportPrivateUsage]
                            "first_callback_finished_while_queued": first_id in finished_callbacks,
                        }
                    )
                    barrier.release.set()
                    await task
                    if callback_errors:
                        raise callback_errors[0]
                    barrier = None
                finally:
                    if barrier is not None:
                        barrier.release.set()
                    waiter.cancel()
                    task.cancel()
                    await asyncio.gather(task, waiter, return_exceptions=True)
    finally:
        if prior is None:
            del bot.__dict__["_handle_mention"]
        else:
            bot.__dict__["_handle_mention"] = prior
    return {
        "platform": "discord",
        "threads": tuple(threads),
        "batches": tuple(batches),
        "peak_callbacks_by_thread": {str(key): value for key, value in peak.items()},
        "queue_empty_after": not bot._pending,  # pyright: ignore[reportPrivateUsage]
        "processing_empty_after": not bot._processing,  # pyright: ignore[reportPrivateUsage]
    }
