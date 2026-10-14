"""Read-only rendering context for a Discord mention's admitted slot ticket."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from daimon.core.turn_queue import TurnQueue, TurnTicket

_ticket: ContextVar[tuple[TurnTicket, int] | None] = ContextVar(
    "discord_queue_card_ticket", default=None
)


@contextmanager
def queue_card_ticket(ticket: TurnTicket, *, cap: int) -> Iterator[None]:
    """Observe this task's ticket and actual admission cap without changing either."""
    token = _ticket.set((ticket, cap))
    try:
        yield
    finally:
        _ticket.reset(token)


def tenant_slot_waiter(queue: TurnQueue, tenant_id: uuid.UUID) -> Callable[[], bool]:
    """Only this mention's actual tenant-cap wait may change its card."""
    admitted = _ticket.get()
    if admitted is None:
        return lambda: False
    ticket, cap = admitted

    def waiting() -> bool:
        return ticket.tenant_id == tenant_id and ticket.queued and queue.in_flight(tenant_id) >= cap

    return waiting
