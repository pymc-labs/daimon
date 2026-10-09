"""Carry a turn's slot ticket from admission to the wait after its card.

The adapter admits a turn into its `TurnQueue` before it has a card, and the
card is posted deep inside its turn path. `holding` binds the ticket to the
task for the thread's whole run (its first turn and the follow-ups drained
after it) and releases whatever is still held on every exit.
`wait_for_slot` is called once each turn's card is up, before it binds its
session. A slot covers one turn: `release_turn_slot` returns it when the turn
ends, and the next follow-up's `wait_for_slot` takes a fresh ticket through
admission, so it queues behind other tenants' turns like a new one.

A turn that waited passed `admit()`'s balance gate before the wait, so the
gate runs again once it holds a slot: without that, every queued turn of a
tenant whose balance ran out meanwhile would still run, and the overdraft
bound in docs/billing.md would grow from the cap to the cap plus the queue.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

import structlog
from daimon.core.tenant_balance import is_over_balance
from daimon.core.turn.outcomes import current_outcome
from daimon.core.turn.termination import TerminationReason
from daimon.core.turn_queue import TurnTicket
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "QUEUE_TIMED_OUT_TEXT",
    "SlotResult",
    "holding",
    "release_turn_slot",
    "wait_for_slot",
    "wait_for_ticket",
]

log = structlog.get_logger(__name__)

SlotResult = Literal["started", "cancelled", "timed_out", "balance_depleted", "queue_full"]

_REASONS = {
    "cancelled": TerminationReason.INTERRUPTED,
    "timed_out": TerminationReason.ADMISSION_CONCURRENCY_SHED,
    "balance_depleted": TerminationReason.ADMISSION_BALANCE_DEPLETED,
    "queue_full": TerminationReason.ADMISSION_CONCURRENCY_SHED,
}

# The ordinary approved error: nothing about queues.
QUEUE_TIMED_OUT_TEXT = "Something went wrong. Mention me to try again."


class _Holder:
    """A thread run's slot state: the ticket admission gave its first turn,
    the one the running turn holds, and the template follow-ups re-admit from."""

    def __init__(self, ticket: TurnTicket) -> None:
        self.first: TurnTicket | None = ticket
        self.current: TurnTicket | None = None
        self.template = ticket

    def release(self) -> None:
        for ticket in (self.first, self.current):
            if ticket is not None:
                ticket.release()
        self.first = self.current = None


_current_holder: ContextVar[_Holder | None] = ContextVar("turn_slot_holder", default=None)


@contextmanager
def holding(ticket: TurnTicket) -> Iterator[TurnTicket]:
    """Hold slots for this task's turns, starting with `ticket`; release on every exit."""
    holder = _Holder(ticket)
    token = _current_holder.set(holder)
    try:
        yield ticket
    finally:
        _current_holder.reset(token)
        holder.release()


def release_turn_slot() -> None:
    """The turn ended: return its slot (or its unused first ticket) now, so the
    next follow-up re-enters admission instead of keeping the slot."""
    holder = _current_holder.get()
    if holder is not None:
        holder.release()


async def wait_for_slot(
    cancel: asyncio.Event,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> SlotResult:
    """Wait, after the card is up, until this turn holds a slot.

    "started" at once when it already holds one or when no ticket is held
    (paths that claim no slot). A follow-up takes a fresh ticket; when the
    queue is full that is "queue_full". The adapter ends the card as Stopped
    on "cancelled", with `QUEUE_TIMED_OUT_TEXT` on "timed_out", and with its
    balance or capacity refusal on "balance_depleted" or "queue_full". Each
    records the turn's outcome.
    """
    holder = _current_holder.get()
    if holder is None:
        return "started"
    if holder.current is not None:  # a turn that ended without releasing
        holder.current.release()
        holder.current = None
    ticket, holder.first = holder.first, None
    if ticket is None:
        ticket = holder.template.successor()
        if ticket is None:
            if (observation := current_outcome.get()) is not None:
                observation.finish(reason=_REASONS["queue_full"])
            return "queue_full"
    holder.current = ticket
    result = await wait_for_ticket(ticket, cancel, sessionmaker=sessionmaker, tenant_id=tenant_id)
    if result != "started":
        ticket.release()  # a Stop that raced the grant, or a refusal after it
        holder.current = None
    return result


async def wait_for_ticket(
    ticket: TurnTicket,
    cancel: asyncio.Event,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> SlotResult:
    """`wait_for_slot` for a caller that holds its ticket directly."""
    result: SlotResult = await ticket.wait(cancel)
    if (
        result == "started"
        and ticket.waited
        and await is_over_balance(sessionmaker=sessionmaker, tenant_id=tenant_id)
    ):
        log.info("turn.queue.balance_depleted", tenant_id=str(tenant_id))
        result = "balance_depleted"
    if result != "started" and (observation := current_outcome.get()) is not None:
        observation.finish(reason=_REASONS[result])
    return result
