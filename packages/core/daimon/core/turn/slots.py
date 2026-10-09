"""Carry a turn's slot ticket from admission to the wait after its card.

The adapter admits a turn into its `TurnQueue` before it has a card, and the
card is posted deep inside its turn path. `holding` binds the ticket to the
task for that span and releases it on every exit; `wait_for_slot` is called
once the card is up and the turn is about to bind its session.

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

__all__ = ["QUEUE_TIMED_OUT_TEXT", "SlotResult", "holding", "wait_for_slot", "wait_for_ticket"]

log = structlog.get_logger(__name__)

SlotResult = Literal["started", "cancelled", "timed_out", "balance_depleted"]

_REASONS = {
    "cancelled": TerminationReason.INTERRUPTED,
    "timed_out": TerminationReason.ADMISSION_CONCURRENCY_SHED,
    "balance_depleted": TerminationReason.ADMISSION_BALANCE_DEPLETED,
}

# The ordinary approved error: nothing about queues.
QUEUE_TIMED_OUT_TEXT = "Something went wrong. Mention me to try again."

_current_ticket: ContextVar[TurnTicket | None] = ContextVar("turn_ticket", default=None)


@contextmanager
def holding(ticket: TurnTicket) -> Iterator[TurnTicket]:
    """Hold `ticket` for this task's turns and release it on every exit."""
    token = _current_ticket.set(ticket)
    try:
        yield ticket
    finally:
        _current_ticket.reset(token)
        ticket.release()


async def wait_for_slot(
    cancel: asyncio.Event,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> SlotResult:
    """Wait, after the card is up, until this task's turn holds a slot.

    "started" at once when it already holds one or when no ticket is held
    (paths that claim no slot). The adapter ends the card as Stopped on
    "cancelled", with `QUEUE_TIMED_OUT_TEXT` on "timed_out", and with its
    balance refusal on "balance_depleted". Each records the turn's outcome.
    """
    ticket = _current_ticket.get()
    if ticket is None:
        return "started"
    return await wait_for_ticket(ticket, cancel, sessionmaker=sessionmaker, tenant_id=tenant_id)


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
