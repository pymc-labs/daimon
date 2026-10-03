"""Organic thread participation on Teams: quiet-timer batches over the shared gates.

Mirrors Discord's path in `bot.py`. An unmentioned human reply in a channel
thread costs one cascade read; only a followed thread (`on`) with a live tenant
and readable Graph history starts a batch. The batch is judged once the thread
has been quiet for `quiet_seconds`: the shared gates
(`daimon.core.participation_gates`), then the classifier over the thread Graph
returns. Cascade keys are the thread's conversation id (`19:…;messageid=<root>`),
its channel id and the tenant. Every failure here is silent.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.thread_reader import ThreadReader
from daimon.core.config import ThreadParticipationSettings
from daimon.core.observability import capture_exception_with_scope
from daimon.core.participation_gates import (
    BATCH_MAX_MESSAGES,
    BATCH_MAX_QUIET_PERIODS,
    ParticipationGates,
)
from daimon.core.teams_graph import GraphUnavailable
from daimon.core.thread_participation import ClassifierMessage, ParticipationMode

log = structlog.get_logger()


class Spawn(Protocol):
    """`TeamsApp.spawn`: a tracked task, so drain waits for (or cancels) it."""

    def __call__(self, coro: Coroutine[Any, Any, None], *, name: str) -> asyncio.Task[None]: ...


# Runs the admitted turn for the burst's newest message, as `TeamsApp` does for a mention.
Fire = Callable[[TeamsInbound, uuid.UUID], Awaitable[None]]


@dataclass
class _Batch:
    """Unmentioned replies piling up in one followed thread, and the timer watching them."""

    messages: list[TeamsInbound]
    first_at: float
    timer: asyncio.Task[None] | None = field(default=None)


class TeamsParticipation:
    """Per-thread batches keyed by conversation id; `fire` runs the turn a batch earns."""

    def __init__(
        self,
        *,
        gates: ParticipationGates,
        settings: ThreadParticipationSettings,
        reader: ThreadReader | None,
        spawn: Spawn,
        fire: Fire,
        is_busy: Callable[[str], bool],
    ) -> None:
        self._gates = gates
        self._settings = settings
        self._reader = reader
        self._spawn = spawn
        self._fire = fire
        self._is_busy = is_busy
        self._pending: dict[str, _Batch] = {}

    def cancel(self, conversation_id: str) -> None:
        """Drop a thread's batch: a mention there supersedes it, its turn replays them."""
        batch = self._pending.pop(conversation_id, None)
        if batch is not None and batch.timer is not None:
            batch.timer.cancel()

    def cancel_all(self) -> None:
        for conversation_id in list(self._pending):
            self.cancel(conversation_id)

    async def observe(
        self,
        inbound: TeamsInbound,
        tenant_id: uuid.UUID,
        *,
        admitted: Callable[[], Awaitable[TeamsInbound | None]],
    ) -> None:
        """Add one unmentioned reply to its thread's batch, if the thread is followed.

        A thread the cascade resolves to anything but `on` costs one indexed
        read: no liveness read, no sender lookup, no timer, no classifier, no
        turn. `admitted` then gives the reply as classified, or None to drop it.
        """
        key = inbound.conversation_id
        if self._is_busy(key):
            return  # the in-flight turn's successor replays it from Graph
        try:
            resolved = await self._gates.resolve(
                tenant_id=tenant_id, channel_id=inbound.channel_id, thread_id=key
            )
            if resolved.mode is not ParticipationMode.ON:
                log.debug(
                    "thread_participation.not_following",
                    thread_id=key,
                    mode=resolved.mode.value,
                    tier=resolved.tier,
                )
                return
            if self._reader is None:
                # No Graph, no thread to judge: a followed thread stays mention-only.
                log.info("thread_participation.skipped", reason="no_history", thread_id=key)
                return
            classified = await admitted()
            if classified is None:
                return
        except Exception as exc:  # unasked-for turn: log, stay silent
            log.exception("thread_participation.decision_failed", thread_id=key)
            capture_exception_with_scope(exc)
            return

        now = asyncio.get_running_loop().time()
        quiet_seconds = self._settings.quiet_seconds
        batch = self._pending.setdefault(key, _Batch(messages=[], first_at=now))
        batch.messages.append(classified)
        del batch.messages[:-BATCH_MAX_MESSAGES]
        if batch.timer is not None:
            if now - batch.first_at >= quiet_seconds * BATCH_MAX_QUIET_PERIODS:
                return  # waited long enough: let the running timer fire as scheduled
            batch.timer.cancel()
        batch.timer = self._spawn(
            self._quiet_timer(quiet_seconds, key, tenant_id), name="teams.participation"
        )

    async def _quiet_timer(self, quiet_seconds: float, key: str, tenant_id: uuid.UUID) -> None:
        """Wait out the quiet period, then judge the batch. Cancelled = a newer message won."""
        await asyncio.sleep(quiet_seconds)
        await self.judge(key, tenant_id)

    async def judge(self, key: str, tenant_id: uuid.UUID) -> None:
        """The thread went quiet: judge the whole batch once, then run at most one turn."""
        batch = self._pending.pop(key, None)
        if batch is None or not batch.messages or self._is_busy(key):
            return
        # One turn = one caller: the newest message's author, and only their
        # messages are judged, so nobody's words run on another person's session.
        trigger = batch.messages[-1]
        candidates = [m for m in batch.messages if m.user_id == trigger.user_id]
        reader = self._reader
        if reader is None:
            return
        exclude_ids = frozenset(m.activity_id for m in candidates)

        async def recent() -> list[ClassifierMessage]:
            limit = self._settings.recent_messages_window
            return await reader.read_window(trigger, exclude_ids=exclude_ids, limit=limit)

        try:
            # Re-resolved: the quiet window is long enough to turn the thread off mid-burst.
            resolved = await self._gates.resolve(
                tenant_id=tenant_id, channel_id=trigger.channel_id, thread_id=key
            )
            respond = await self._gates.should_respond(
                tenant_id=tenant_id,
                channel_id=trigger.channel_id,
                thread_id=key,
                caller_id=trigger.user_id,
                candidates=[
                    ClassifierMessage(
                        author_name=m.user_name or "unknown", content=m.text, is_bot=False
                    )
                    for m in candidates
                ],
                recent=recent,
                resolved=resolved,
            )
        except GraphUnavailable as err:
            log.info(
                "thread_participation.skipped",
                reason="history_unavailable",
                thread_id=key,
                status=err.status,
            )
            return
        except Exception as exc:  # unasked-for turn: log, stay silent
            log.exception("thread_participation.decision_failed", thread_id=key)
            capture_exception_with_scope(exc)
            return
        if respond:
            await self._fire(trigger, tenant_id)

    async def record(self, trigger: TeamsInbound, tenant_id: uuid.UUID) -> None:
        """The ledger row for an admitted turn. Best effort: a miss loosens the cap by one."""
        try:
            await self._gates.record(
                tenant_id=tenant_id,
                thread_id=trigger.conversation_id,
                message_id=trigger.activity_id,
            )
        except Exception:  # best-effort ledger
            log.exception("thread_participation.record_failed", thread_id=trigger.conversation_id)
