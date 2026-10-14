"""Explicit host probe over the existing Teams per-thread orchestration."""

from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.identity import TeamsInbound


@dataclass(frozen=True)
class AdmissionBarrier:
    started: asyncio.Event
    release: asyncio.Event


@dataclass(frozen=True)
class MentionTurn:
    inbound: TeamsInbound
    tenant_id: uuid.UUID


@dataclass(frozen=True)
class ThreadInputs:
    first: TeamsInbound
    queued: tuple[TeamsInbound, ...]


@dataclass(frozen=True)
class MultiHumanEnvelope:
    app: TeamsApp
    tenant_id: uuid.UUID
    entra_tenant_id: str
    channel_id: str
    threads: tuple[ThreadInputs, ...]
    run: Callable[[MentionTurn, AdmissionBarrier | None], Awaitable[None]]
    timeout_s: float = 30.0


async def multi_human_evidence(envelope: MultiHumanEnvelope) -> Mapping[str, object]:
    """Run actual routing/queue/composition with owner-controlled root barriers.

    Prepared bindings, journal roots and actual charges are independent host
    evidence. This probe returns only platform facts and never a verdict.
    Dedicated app, database and offline transport are supplied by the caller;
    no application imports this module. Every callback performs normal host
    admission/binding/execution, preserving setup routing and continuation tails.
    """
    app = envelope.app
    if not math.isfinite(envelope.timeout_s) or envelope.timeout_s <= 0:
        raise ValueError("C01 requires a finite positive timeout")
    if len(envelope.threads) < 2 or not envelope.channel_id or not envelope.entra_tenant_id:
        raise ValueError("C01 requires two channel threads and an Entra tenant")
    if app._processing or app._pending:  # pyright: ignore[reportPrivateUsage]
        raise ValueError("C01 requires a dedicated idle app")
    threads_seen: set[str] = set()
    messages_seen: set[str] = set()
    humans: set[str] = set()
    for inputs in envelope.threads:
        key = inputs.first.conversation_id
        if not key or key in threads_seen or not inputs.queued:
            raise ValueError("C01 requires distinct threads with queued follow-ups")
        threads_seen.add(key)
        for inbound in (inputs.first, *inputs.queued):
            if (
                inbound.kind != "channel"
                or inbound.channel_id != envelope.channel_id
                or inbound.entra_tenant_id != envelope.entra_tenant_id
                or inbound.conversation_id != key
                or not inbound.user_id
                or not inbound.activity_id
                or inbound.activity_id in messages_seen
                or inbound.unprompted
            ):
                raise ValueError(
                    "C01 needs unique addressed human inputs in the declared channel/thread"
                )
            messages_seen.add(inbound.activity_id)
            humans.add(inbound.user_id)
    if len(humans) < 2:
        raise ValueError("C01 requires two distinct human callers")

    prior = app.__dict__.get("_run_turn_guarded")
    batches: list[Mapping[str, object]] = []
    threads: list[Mapping[str, object]] = []
    finished_callbacks: set[str] = set()
    active: dict[str, int] = {}
    peak: dict[str, int] = {}
    barrier: AdmissionBarrier | None = None
    first_id: str | None = None

    async def turn(inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        key = inbound.conversation_id
        active[key] = active.get(key, 0) + 1
        peak[key] = max(peak.get(key, 0), active[key])
        batch: dict[str, object] = {
            "conversation_id": key,
            "thread_id": inbound.thread_id,
            "reply_message_id": inbound.activity_id,
            "message_ids": inbound.message_ids,
            "caller_id": inbound.user_id,
            "content": inbound.text,
            "composed_ids": inbound.composed_ids,
            "files": tuple({"kind": file.kind, "name": file.name} for file in inbound.files),
            "callback_returned": False,
        }
        batches.append(batch)
        try:
            await envelope.run(
                MentionTurn(inbound, tenant_id),
                barrier if inbound.activity_id == first_id else None,
            )
            batch["callback_returned"] = True
        finally:
            finished_callbacks.add(inbound.activity_id)
            active[key] -= 1

    app._run_turn_guarded = turn  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    try:
        async with asyncio.timeout(envelope.timeout_s):
            for inputs in envelope.threads:
                key = inputs.first.conversation_id
                first_id = inputs.first.activity_id
                barrier = AdmissionBarrier(asyncio.Event(), asyncio.Event())
                task = asyncio.create_task(
                    app._orchestrate(  # pyright: ignore[reportPrivateUsage]
                        inputs.first, envelope.tenant_id
                    )
                )
                waiter = asyncio.create_task(barrier.started.wait())
                try:
                    await asyncio.wait((task, waiter), return_when=asyncio.FIRST_COMPLETED)
                    if task.done():
                        await task
                        raise RuntimeError("C01 first callback finished before queued inputs")
                    if active.get(key, 0) != 1:
                        raise RuntimeError("C01 first callback returned before queued inputs")
                    for inbound in inputs.queued:
                        await app._orchestrate(inbound, envelope.tenant_id)  # pyright: ignore[reportPrivateUsage]
                    pending = app._pending.get(key, [])  # pyright: ignore[reportPrivateUsage]
                    threads.append(
                        {
                            "conversation_id": key,
                            "received_message_ids": tuple(
                                i.activity_id for i in (inputs.first, *inputs.queued)
                            ),
                            "queued_message_ids": tuple(i.activity_id for i in pending),
                            "claimed_while_queued": key in app._processing,  # pyright: ignore[reportPrivateUsage]
                            "first_callback_finished_while_queued": first_id in finished_callbacks,
                        }
                    )
                    barrier.release.set()
                    await task
                    barrier = None
                finally:
                    if barrier is not None:
                        barrier.release.set()
                    waiter.cancel()
                    task.cancel()
                    await asyncio.gather(task, waiter, return_exceptions=True)
    finally:
        if prior is None:
            del app.__dict__["_run_turn_guarded"]
        else:
            app.__dict__["_run_turn_guarded"] = prior
    return {
        "platform": "teams",
        "threads": tuple(threads),
        "batches": tuple(batches),
        "peak_callbacks_by_thread": dict(peak),
        "queue_empty_after": not app._pending,  # pyright: ignore[reportPrivateUsage]
        "processing_empty_after": not app._processing,  # pyright: ignore[reportPrivateUsage]
    }
