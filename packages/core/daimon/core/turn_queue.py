"""Turn slots and the queue in front of them, one per adapter process.

A turn over the global cap (`max_concurrent_turns`) or its tenant's cap waits
here for a slot instead of being refused. The queue is backstage: a queued turn
shows the same "Working on it…" card with Stop as a running one, and the only
difference a person sees is time.

- Admission (`TurnQueue.admit`) runs a turn at once when its tenant and the
  process both have a free slot and nobody of its tenant is already waiting;
  otherwise it joins its tenant's FIFO queue. A full queue (per tenant or in
  total) returns None, and the adapter keeps its plain refusal.
- A released slot is handed on in the same synchronous span, round-robin over
  the tenants that have a turn waiting and a free tenant slot, so one busy
  tenant cannot starve the others.
- `TurnTicket.wait` returns once the turn holds a slot, is stopped (its cancel
  event), or has waited `max_wait_s`. The last is a safeguard only; the adapter
  ends that card with its ordinary error.
- `TurnTicket.release` is the one exit, idempotent, on every end path: it
  returns a held slot or leaves the queue.

Everything here runs on the event loop without awaiting between a check and
its update, so no lock is needed. `daimon.core.turn.slots` carries a turn's
ticket from admission to the point after its card is posted. The queue is in
memory: a restart drops it, and the boot sweep retires the queued turns' cards
with the running ones' (their card intents are written before the wait). `formal/turn_queue/` models
this module.
"""

from __future__ import annotations

import asyncio
import math
import time
import uuid
import weakref
from collections import deque
from collections.abc import Callable
from typing import Literal, TypedDict

import structlog
from daimon.core.config import TurnQueueSettings

__all__ = [
    "QueueWindow",
    "TurnQueue",
    "TurnTicket",
    "WaitResult",
    "take_queue_window",
]

log = structlog.get_logger(__name__)

WaitResult = Literal["started", "cancelled", "timed_out"]

_live_queues: weakref.WeakSet[TurnQueue] = weakref.WeakSet()
# Waits kept for the heartbeat's percentiles between two windows.
_WAIT_SAMPLE = 4096
# The cap a follow-up re-admits with when its first turn claimed past the caps.
_UNCAPPED = 1_000_000


class QueueWindow(TypedDict):
    """Queue depth now and the waits of turns that started since the last window."""

    global_depth: int
    per_tenant_max: int
    started: int
    wait_ms_p50: float
    wait_ms_p95: float
    wait_ms_max: float


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * fraction) - 1]


class TurnTicket:
    """One turn's claim on a slot: queued, running, or done.

    A ticket covers one turn. A follow-up turn in the same thread takes a
    fresh one (`TurnQueue.readmit`), so it queues behind other tenants like
    any new turn and never inherits a ticket that already ended.
    """

    def __init__(
        self,
        queue: TurnQueue,
        tenant_id: uuid.UUID,
        *,
        state: Literal["queued", "running"],
        now: float,
        cap: int | None,
        fields: dict[str, object],
    ) -> None:
        self._queue = queue
        self.tenant_id = tenant_id
        self.cap = cap
        self.state: Literal["queued", "running", "done"] = state
        # Whether this turn ever waited: its admission gates ran before the wait.
        self.waited = state == "queued"
        self.queued_at = now
        self._granted = state == "running"
        # Set when the ticket stops waiting: granted a slot, or expired by dispatch.
        self._settled = asyncio.Event()
        self._fields = fields
        if state == "running":
            self._settled.set()

    @property
    def queued(self) -> bool:
        return self.state == "queued"

    def waited_ms(self) -> float:
        return round((self._queue.clock() - self.queued_at) * 1000, 1)

    def overdue(self) -> bool:
        return self._queue.clock() - self.queued_at >= self._queue.max_wait_s

    async def wait(self, cancel: asyncio.Event) -> WaitResult:
        """Wait for a slot; returns at once for a ticket that already holds one.

        "cancelled" when Stop came first, including a Stop that raced the
        grant: the slot is then still held, and `release` returns it.
        "timed_out" after the queue's max wait, whether this waiter's timer or
        the dispatcher noticed first; the ticket has left the queue.
        """
        if self._granted and not cancel.is_set():
            return "started"
        if self.state == "queued":
            remaining = self.queued_at + self._queue.max_wait_s - self._queue.clock()
            settled = asyncio.ensure_future(self._settled.wait())
            stopped = asyncio.ensure_future(cancel.wait())
            try:
                await asyncio.wait(
                    {settled, stopped},
                    timeout=max(0.0, remaining),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                settled.cancel()
                stopped.cancel()
        if cancel.is_set():
            if self.state == "queued":
                log.info("turn.queue.cancelled", waited_ms=self.waited_ms(), **self._fields)
                self._queue.leave(self)
            return "cancelled"
        if self._granted:
            return "started"
        if self.state == "queued":
            log.warning("turn.queue.timed_out", waited_ms=self.waited_ms(), **self._fields)
            self._queue.leave(self)
        return "timed_out"

    def successor(self) -> TurnTicket | None:
        """A fresh ticket for the next turn in this thread (`TurnQueue.readmit`)."""
        return self._queue.readmit(self)

    def release(self) -> None:
        """Return the slot, or leave the queue. Safe to call more than once."""
        if self.state == "running":
            self.state = "done"
            self._queue.free(self)
        elif self.state == "queued":
            self._queue.leave(self)


class TurnQueue:
    """Per-process turn slots with a bounded, round-robin queue in front."""

    def __init__(
        self,
        *,
        platform: str,
        global_cap: int | None,
        max_queued_per_tenant: int,
        max_queued: int,
        max_wait_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.platform = platform
        self.global_cap = global_cap
        self.max_queued_per_tenant = max_queued_per_tenant
        self.max_queued = max_queued
        self.max_wait_s = max_wait_s
        self.clock = clock
        self._running: dict[uuid.UUID, int] = {}
        self._running_total = 0
        self._caps: dict[uuid.UUID, int] = {}
        self._queues: dict[uuid.UUID, deque[TurnTicket]] = {}
        # Tenants with a waiting turn, in round-robin order.
        self._rotation: deque[uuid.UUID] = deque()
        self._depth = 0
        # A bounded sample: an adapter with no heartbeat never drains it.
        self._waits_ms: deque[float] = deque(maxlen=_WAIT_SAMPLE)
        self._started = 0
        _live_queues.add(self)

    @classmethod
    def from_settings(
        cls, settings: TurnQueueSettings, *, platform: str, global_cap: int | None = None
    ) -> TurnQueue:
        """The adapter's queue; `enabled=false` keeps the slots and refuses at the cap."""
        return cls(
            platform=platform,
            global_cap=global_cap,
            max_queued_per_tenant=settings.max_per_tenant if settings.enabled else 0,
            max_queued=settings.max_total if settings.enabled else 0,
            max_wait_s=settings.max_wait_s,
        )

    # --- counts ---------------------------------------------------------

    def in_flight(self, tenant_id: uuid.UUID | None = None) -> int:
        """Turns holding a slot, for one tenant or in total."""
        if tenant_id is None:
            return self._running_total
        return self._running.get(tenant_id, 0)

    def in_flight_max(self) -> int:
        return max(self._running.values(), default=0)

    def depth(self, tenant_id: uuid.UUID | None = None) -> int:
        """Turns waiting, for one tenant or in total."""
        if tenant_id is None:
            return self._depth
        return len(self._queues.get(tenant_id, ()))

    def take_window(self) -> QueueWindow:
        waits = list(self._waits_ms)
        started, self._started = self._started, 0
        self._waits_ms.clear()
        return {
            "global_depth": self._depth,
            "per_tenant_max": max((len(q) for q in self._queues.values()), default=0),
            "started": started,
            "wait_ms_p50": _percentile(waits, 0.5),
            "wait_ms_p95": _percentile(waits, 0.95),
            "wait_ms_max": max(waits, default=0.0),
        }

    # --- admission ------------------------------------------------------

    def _can_run(self, tenant_id: uuid.UUID) -> bool:
        return (
            self._running.get(tenant_id, 0) < self._caps[tenant_id]
            and (self.global_cap is None or self._running_total < self.global_cap)
            and not self._queues.get(tenant_id)
        )

    def try_claim(self, tenant_id: uuid.UUID, /, *, cap: int) -> TurnTicket | None:
        """A slot now, or None. Never queues (for turns nobody is waiting on)."""
        self._caps[tenant_id] = cap
        self._dispatch()  # a raised cap may have freed a waiting turn first
        if not self._can_run(tenant_id):
            return None
        return self._start(
            TurnTicket(self, tenant_id, state="running", now=self.clock(), cap=cap, fields={})
        )

    def claim(self, tenant_id: uuid.UUID, /) -> TurnTicket:
        """A slot now, past both caps: for a turn whose admission ignores them
        (a Teams continuation wake) but must still count as running."""
        ticket = TurnTicket(self, tenant_id, state="running", now=self.clock(), cap=None, fields={})
        return self._start(ticket)

    def admit(self, tenant_id: uuid.UUID, /, *, cap: int, **fields: object) -> TurnTicket | None:
        """A slot now, else a place in the queue; None only when the queue is full.

        `fields` are added to this turn's queue log lines (channel, thread).
        """
        claimed = self.try_claim(tenant_id, cap=cap)
        if claimed is not None:
            return claimed
        fields = {"tenant_id": str(tenant_id), "platform": self.platform, **fields}
        tenant_depth = self.depth(tenant_id)
        if tenant_depth >= self.max_queued_per_tenant or self._depth >= self.max_queued:
            log.warning(
                "turn.queue.full",
                reason="tenant_queue_full"
                if tenant_depth >= self.max_queued_per_tenant
                else "queue_full",
                depth=self._depth,
                tenant_depth=tenant_depth,
                **fields,
            )
            return None
        ticket = TurnTicket(
            self, tenant_id, state="queued", now=self.clock(), cap=cap, fields=fields
        )
        self._queues.setdefault(tenant_id, deque()).append(ticket)
        if tenant_id not in self._rotation:
            self._rotation.append(tenant_id)
        self._depth += 1
        log.info(
            "turn.queue.enqueued",
            position=tenant_depth + 1,
            depth=self._depth,
            in_flight=self._running_total,
            tenant_in_flight=self._running.get(tenant_id, 0),
            cap=cap,
            global_cap=self.global_cap,
            **fields,
        )
        return ticket

    def readmit(self, previous: TurnTicket) -> TurnTicket | None:
        """A fresh ticket for the next turn in `previous`'s thread, through the
        same admission as a new turn: a slot now, else the back of the queue."""
        cap = previous.cap if previous.cap is not None else self._caps.get(previous.tenant_id)
        return self.admit(
            previous.tenant_id,
            cap=cap if cap is not None else _UNCAPPED,
            **previous._fields,  # pyright: ignore[reportPrivateUsage]
        )

    # --- slot hand-off (one synchronous span each) ----------------------

    def _start(self, ticket: TurnTicket) -> TurnTicket:
        ticket.state = "running"
        self._running[ticket.tenant_id] = self._running.get(ticket.tenant_id, 0) + 1
        self._running_total += 1
        ticket._granted = True  # pyright: ignore[reportPrivateUsage]
        ticket._settled.set()  # pyright: ignore[reportPrivateUsage]
        return ticket

    def _dispatch(self) -> None:
        """Start waiting turns while slots are free: the first eligible tenant in
        rotation order goes next and moves to the back."""
        while self.global_cap is None or self._running_total < self.global_cap:
            tenant_id = next(
                (t for t in self._rotation if self._running.get(t, 0) < self._caps[t]), None
            )
            if tenant_id is None:
                return
            waiting = self._queues[tenant_id]
            ticket = waiting.popleft()
            self._depth -= 1
            if ticket.overdue():
                # Past its max wait: it times out here rather than starting
                # late, and the slot goes to the next waiting turn. The tenant
                # keeps its place in the rotation; it was not served.
                if not waiting:
                    del self._queues[tenant_id]
                    self._rotation.remove(tenant_id)
                self._expire(ticket)
                continue
            self._rotation.remove(tenant_id)
            if waiting:
                self._rotation.append(tenant_id)
            else:
                del self._queues[tenant_id]
            waited = ticket.waited_ms()
            self._waits_ms.append(waited)
            self._started += 1
            self._start(ticket)
            log.info(
                "turn.queue.started",
                waited_ms=waited,
                depth=self._depth,
                **ticket._fields,  # pyright: ignore[reportPrivateUsage]
            )

    def free(self, ticket: TurnTicket) -> None:
        """Return `ticket`'s slot and hand it on."""
        tenant_id = ticket.tenant_id
        remaining = self._running.get(tenant_id, 1) - 1
        if remaining > 0:
            self._running[tenant_id] = remaining
        else:
            self._running.pop(tenant_id, None)
        self._running_total -= 1
        self._dispatch()

    def _expire(self, ticket: TurnTicket) -> None:
        """End a popped ticket that outwaited the max wait; its waiter wakes timed out."""
        log.warning(
            "turn.queue.timed_out",
            waited_ms=ticket.waited_ms(),
            **ticket._fields,  # pyright: ignore[reportPrivateUsage]
        )
        ticket.state = "done"
        ticket._settled.set()  # pyright: ignore[reportPrivateUsage]

    def leave(self, ticket: TurnTicket) -> None:
        """Take a waiting `ticket` out of the queue without a slot."""
        if ticket.state != "queued":
            return
        ticket.state = "done"
        waiting = self._queues.get(ticket.tenant_id)
        if waiting is None or ticket not in waiting:
            return
        waiting.remove(ticket)
        self._depth -= 1
        if not waiting:
            del self._queues[ticket.tenant_id]
            self._rotation.remove(ticket.tenant_id)


def take_queue_window() -> QueueWindow:
    """This process's queue window for the health heartbeat; zeros without a queue.

    An adapter process holds one queue; the wait percentiles of several are
    combined by taking the largest."""
    windows = [queue.take_window() for queue in list(_live_queues)]
    if not windows:
        return {
            "global_depth": 0,
            "per_tenant_max": 0,
            "started": 0,
            "wait_ms_p50": 0.0,
            "wait_ms_p95": 0.0,
            "wait_ms_max": 0.0,
        }
    return {
        "global_depth": sum(w["global_depth"] for w in windows),
        "per_tenant_max": max(w["per_tenant_max"] for w in windows),
        "started": sum(w["started"] for w in windows),
        "wait_ms_p50": max(w["wait_ms_p50"] for w in windows),
        "wait_ms_p95": max(w["wait_ms_p95"] for w in windows),
        "wait_ms_max": max(w["wait_ms_max"] for w in windows),
    }
