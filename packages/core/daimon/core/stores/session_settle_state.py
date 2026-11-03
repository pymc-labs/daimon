"""Store for per-session settle state: pending settle requests, the backlog cutoff,
and which turn an agent-chat send started (`unattended_turn`).

Per `guideline:architecture`: this module does NOT swallow exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from daimon.core._models import SessionSettleState
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

BACKLOG_CUTOFF_ID = "backlog-cutoff"
"""The row whose `covered_through` is when this settle path was deployed.

Written once by the migration that introduced settle requests. Calls Managed
Agents made before it are the backlog: no automatic settle reads them; only
the operator's backlog settle does.
"""


async def backlog_cutoff(session: AsyncSession) -> datetime | None:
    """When this settle path was deployed (None on a database that never ran it)."""
    return (
        await session.execute(
            select(SessionSettleState.covered_through).where(
                SessionSettleState.managed_session_id == BACKLOG_CUTOFF_ID
            )
        )
    ).scalar_one_or_none()


@dataclass(frozen=True)
class PendingSettle:
    """A headless send's settle request: its turn (None until the send names it)."""

    turn_event_id: str | None
    requested_at: datetime


async def settle_request(session: AsyncSession, *, managed_session_id: str) -> PendingSettle | None:
    """The session's pending settle request, if any."""
    row = (
        await session.execute(
            select(
                SessionSettleState.settle_turn_event_id, SessionSettleState.settle_requested_at
            ).where(
                SessionSettleState.managed_session_id == managed_session_id,
                SessionSettleState.settle_requested_at.is_not(None),
            )
        )
    ).one_or_none()
    if row is None or row[1] is None:
        return None
    return PendingSettle(turn_event_id=row[0], requested_at=row[1])


async def request_settle(
    session: AsyncSession, *, managed_session_id: str
) -> tuple[PendingSettle | None, datetime]:
    """Record, before a send, that the session must be settled once that turn ends.

    The request names no turn yet (`name_settle_turn` does once the send is
    accepted). Returns the request it replaced, for `withdraw_settle_request`
    if the send fails, and its own `requested_at`, which versions it.
    """
    previous = await settle_request(session, managed_session_id=managed_session_id)
    stmt = insert(SessionSettleState).values(
        managed_session_id=managed_session_id,
        settle_turn_event_id=None,
        settle_requested_at=func.clock_timestamp(),
    )
    requested_at = (
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[SessionSettleState.managed_session_id],
                set_={
                    "settle_turn_event_id": None,
                    "settle_requested_at": stmt.excluded.settle_requested_at,
                    "updated_at": func.now(),
                },
            ).returning(SessionSettleState.settle_requested_at)
        )
    ).scalar_one()
    assert requested_at is not None  # just written
    return previous, requested_at


async def name_settle_turn(
    session: AsyncSession, *, managed_session_id: str, requested_at: datetime, turn_event_id: str
) -> None:
    """Name the accepted send's `user.message` on the request written before it."""
    await session.execute(
        update(SessionSettleState)
        .where(
            SessionSettleState.managed_session_id == managed_session_id,
            SessionSettleState.settle_requested_at == requested_at,
        )
        .values(settle_turn_event_id=turn_event_id)
    )


async def withdraw_settle_request(
    session: AsyncSession,
    *,
    managed_session_id: str,
    requested_at: datetime,
    previous: PendingSettle | None,
) -> None:
    """Undo a request whose send failed, restoring the one it replaced."""
    await session.execute(
        update(SessionSettleState)
        .where(
            SessionSettleState.managed_session_id == managed_session_id,
            SessionSettleState.settle_requested_at == requested_at,
        )
        .values(
            settle_turn_event_id=previous.turn_event_id if previous else None,
            settle_requested_at=previous.requested_at if previous else None,
            updated_at=func.now(),
        )
    )


async def clear_settle_request(
    session: AsyncSession, *, managed_session_id: str, request: PendingSettle
) -> None:
    """Clear `request` only if it is still the session's request, unchanged.

    A newer send's request (or the naming of this one) leaves it in place.
    """
    await session.execute(
        update(SessionSettleState)
        .where(
            SessionSettleState.managed_session_id == managed_session_id,
            SessionSettleState.settle_requested_at == request.requested_at,
            SessionSettleState.settle_turn_event_id.is_not_distinct_from(request.turn_event_id),
        )
        .values(settle_turn_event_id=None, settle_requested_at=None, updated_at=func.now())
    )


async def list_settle_requests(session: AsyncSession, *, limit: int) -> list[str]:
    """Up to `limit` sessions with a pending request, least recently visited first."""
    rows = await session.execute(
        select(SessionSettleState.managed_session_id)
        .where(SessionSettleState.settle_requested_at.is_not(None))
        .order_by(SessionSettleState.updated_at)
        .limit(limit)
    )
    return list(rows.scalars())


async def touch_settle_request(session: AsyncSession, *, managed_session_id: str) -> None:
    """Send a request the drain visited without clearing it to the back of the queue."""
    await session.execute(
        update(SessionSettleState)
        .where(
            SessionSettleState.managed_session_id == managed_session_id,
            SessionSettleState.settle_requested_at.is_not(None),
        )
        .values(updated_at=func.now())
    )


@dataclass(frozen=True)
class UnattendedTurn:
    """The latest turn an agent-chat send started on a session."""

    turn_event_id: str
    rules_channel_id: str | None


async def record_unattended_turn(
    session: AsyncSession,
    *,
    managed_session_id: str,
    turn_event_id: str,
    rules_channel_id: str | None,
) -> None:
    """Record, after its send was accepted, the turn agent chat just started."""
    stmt = insert(SessionSettleState).values(
        managed_session_id=managed_session_id,
        unattended_turn_event_id=turn_event_id,
        unattended_rules_channel_id=rules_channel_id,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[SessionSettleState.managed_session_id],
            set_={
                "unattended_turn_event_id": stmt.excluded.unattended_turn_event_id,
                "unattended_rules_channel_id": stmt.excluded.unattended_rules_channel_id,
                "updated_at": func.now(),
            },
        )
    )


async def unattended_turn(
    session: AsyncSession, *, managed_session_id: str
) -> UnattendedTurn | None:
    """The latest turn agent chat started on the session, if any."""
    row = (
        await session.execute(
            select(
                SessionSettleState.unattended_turn_event_id,
                SessionSettleState.unattended_rules_channel_id,
            ).where(SessionSettleState.managed_session_id == managed_session_id)
        )
    ).one_or_none()
    if row is None or row[0] is None:
        return None
    return UnattendedTurn(turn_event_id=row[0], rules_channel_id=row[1])
