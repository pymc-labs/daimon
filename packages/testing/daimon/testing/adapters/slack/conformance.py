"""Explicit host envelopes over the unchanged Slack orchestration and drain."""

from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from daimon.adapters.slack.app import SlackApp
from daimon.adapters.slack.vision import SlackFile
from slack_sdk.web.async_client import AsyncWebClient


@dataclass(frozen=True)
class AdmissionBarrier:
    started: asyncio.Event
    release: asyncio.Event


@dataclass(frozen=True)
class MentionTurn:
    event: dict[str, Any]
    channel: str
    tenant_id: uuid.UUID
    thread_id: str
    team_id: str
    web_client: AsyncWebClient
    content_override: str | None
    files: tuple[SlackFile, ...] | None


@dataclass(frozen=True)
class ThreadInputs:
    first: dict[str, Any]
    queued: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class MultiHumanEnvelope:
    app: SlackApp
    tenant_id: uuid.UUID
    channel: str
    team_id: str
    web_client: AsyncWebClient
    threads: tuple[ThreadInputs, ...]
    run: Callable[[MentionTurn, AdmissionBarrier | None], Awaitable[None]]
    timeout_s: float = 30.0


async def multi_human_evidence(envelope: MultiHumanEnvelope) -> Mapping[str, object]:
    """Observe actual per-caller turns; provider/ledger oracle stays with owners.

    The normal orchestration performs real cap/slot reads. First native root
    choreography supplies the started/release barrier. All follow-ups traverse
    normal busy-thread orchestration, pending reactions, grouping/composition
    and settlement. This module is only explicitly imported by a conformance
    host, never a live application; use a dedicated idle app and offline client.
    """
    app = envelope.app
    if not math.isfinite(envelope.timeout_s) or envelope.timeout_s <= 0:
        raise ValueError("C01 requires a finite positive timeout")
    if len(envelope.threads) < 2 or not envelope.channel or not envelope.team_id:
        raise ValueError("C01 requires two threads in one channel and team")
    if app._processing or app._pending:  # pyright: ignore[reportPrivateUsage]
        raise ValueError("C01 requires a dedicated idle app")
    thread_ids: set[str] = set()
    message_ids: set[str] = set()
    humans: set[str] = set()
    for inputs in envelope.threads:
        thread_id = str(inputs.first.get("thread_ts") or inputs.first.get("ts") or "")
        if not thread_id or thread_id in thread_ids or not inputs.queued:
            raise ValueError("C01 requires distinct threads with queued follow-ups")
        thread_ids.add(thread_id)
        for event in (inputs.first, *inputs.queued):
            message_id = str(event.get("ts") or "")
            user = str(event.get("user") or "")
            if (
                not message_id
                or message_id in message_ids
                or not user
                or event.get("bot_id")
                or event.get("channel") != envelope.channel
                or str(event.get("thread_ts") or event.get("ts") or "") != thread_id
                or not event.get("event_ts")
            ):
                raise ValueError("C01 needs unique human events in the specified channel/thread")
            message_ids.add(message_id)
            humans.add(user)
    if len(humans) < 2:
        raise ValueError("C01 requires two distinct human callers")

    prior = app.__dict__.get("_run_thread_turn")
    batches: list[Mapping[str, object]] = []
    threads: list[Mapping[str, object]] = []
    finished_callbacks: set[str] = set()
    active: dict[str, int] = {}
    peak: dict[str, int] = {}
    barrier: AdmissionBarrier | None = None
    first_ts: str | None = None

    async def turn(
        event: dict[str, Any],
        *,
        channel: str,
        web_client: AsyncWebClient,
        tenant_id: uuid.UUID,
        thread_id: str,
        team_id: str,
        content_override: str | None = None,
        files: list[SlackFile] | None = None,
    ) -> None:
        active[thread_id] = active.get(thread_id, 0) + 1
        peak[thread_id] = max(peak.get(thread_id, 0), active[thread_id])
        batch: dict[str, object] = {
            "thread_id": thread_id,
            "trigger_message_id": str(event.get("ts") or ""),
            "caller_id": str(event.get("user") or ""),
            "content": str(event.get("text") or "")
            if content_override is None
            else content_override,
            "content_overridden": content_override is not None,
            "file_ids": tuple(file["id"] for file in files or ()),
            "callback_returned": False,
        }
        batches.append(batch)
        try:
            await envelope.run(
                MentionTurn(
                    event,
                    channel,
                    tenant_id,
                    thread_id,
                    team_id,
                    web_client,
                    content_override,
                    None if files is None else tuple(files),
                ),
                barrier if event.get("ts") == first_ts else None,
            )
            batch["callback_returned"] = True
        finally:
            finished_callbacks.add(str(event.get("ts") or ""))
            active[thread_id] -= 1

    app._run_thread_turn = turn  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    try:
        async with asyncio.timeout(envelope.timeout_s):
            for inputs in envelope.threads:
                thread_id = str(inputs.first.get("thread_ts") or inputs.first["ts"])
                first_ts = str(inputs.first["ts"])
                barrier = AdmissionBarrier(asyncio.Event(), asyncio.Event())
                task = asyncio.create_task(
                    app._orchestrate(  # pyright: ignore[reportPrivateUsage]
                        inputs.first,
                        team_id=envelope.team_id,
                        channel=envelope.channel,
                        event_ts=str(inputs.first["event_ts"]),
                        web_client=envelope.web_client,
                        tenant_id=envelope.tenant_id,
                    )
                )
                waiter = asyncio.create_task(barrier.started.wait())
                try:
                    await asyncio.wait((task, waiter), return_when=asyncio.FIRST_COMPLETED)
                    if task.done():
                        await task
                        raise RuntimeError("C01 first callback finished before queued inputs")
                    if active.get(thread_id, 0) != 1:
                        raise RuntimeError("C01 first callback returned before queued inputs")
                    for event in inputs.queued:
                        await app._orchestrate(  # pyright: ignore[reportPrivateUsage]
                            event,
                            team_id=envelope.team_id,
                            channel=envelope.channel,
                            event_ts=str(event["event_ts"]),
                            web_client=envelope.web_client,
                            tenant_id=envelope.tenant_id,
                        )
                    thread_key = (envelope.team_id, envelope.channel, thread_id)
                    pending = app._pending.get(thread_key, [])  # pyright: ignore[reportPrivateUsage]
                    threads.append(
                        {
                            "thread_id": thread_id,
                            "received_message_ids": tuple(
                                str(e["ts"]) for e in (inputs.first, *inputs.queued)
                            ),
                            "queued_message_ids": tuple(str(e["ts"]) for e in pending),
                            "claimed_while_queued": thread_key in app._processing,  # pyright: ignore[reportPrivateUsage]
                            "first_callback_finished_while_queued": first_ts in finished_callbacks,
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
            del app.__dict__["_run_thread_turn"]
        else:
            app.__dict__["_run_thread_turn"] = prior
    return {
        "platform": "slack",
        "threads": tuple(threads),
        "batches": tuple(batches),
        "peak_callbacks_by_thread": dict(peak),
        "queue_empty_after": not app._pending,  # pyright: ignore[reportPrivateUsage]
        "processing_empty_after": not app._processing,  # pyright: ignore[reportPrivateUsage]
    }
