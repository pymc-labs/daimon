"""Store for per-session settle state: pending settle requests, the billing cutoffs,
which turn an agent-chat send started (`unattended_turn`), whether its driver is
a person signed in to the hub (`is_agent_chat_signed_in`) and which session a
credential's grant belongs to (`find_agent_chat_session`).

Per `guideline:architecture`: this module does NOT swallow exceptions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from daimon.core._models import Account, SessionSettleState
from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

BACKLOG_CUTOFF_ID = "backlog-cutoff"
"""The row whose `covered_through` is when this settle path was deployed.

Written once by the migration that introduced settle requests. Calls Managed
Agents made before it are the backlog: no automatic settle reads them; only
the operator's backlog settle does.
"""


WEB_SEARCH_CUTOFF_ID = "web-search-cutoff"
"""The row whose `covered_through` is when Claude web searches began to be billed.

Written once by the migration that added search billing. Searches before it
were never billed, so no settle bills them.
"""


async def backlog_cutoff(session: AsyncSession) -> datetime | None:
    """When this settle path was deployed (None on a database that never ran it)."""
    return await _cutoff(session, BACKLOG_CUTOFF_ID)


async def web_search_cutoff(session: AsyncSession) -> datetime | None:
    """When Claude web searches began to be billed (None on a database that never ran it)."""
    return await _cutoff(session, WEB_SEARCH_CUTOFF_ID)


async def _cutoff(session: AsyncSession, row_id: str) -> datetime | None:
    return (
        await session.execute(
            select(SessionSettleState.covered_through).where(
                SessionSettleState.managed_session_id == row_id
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
    send: int | None,
) -> None:
    """Record, after its send was accepted, the turn agent chat just started.

    `send` is that send's number from `record_agent_chat_driver`; None (a send recorded
    with no number) leaves the latest send unrecorded, which names nobody.
    """
    stmt = insert(SessionSettleState).values(
        managed_session_id=managed_session_id,
        unattended_turn_event_id=turn_event_id,
        unattended_rules_channel_id=rules_channel_id,
        agent_chat_recorded_send=send,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[SessionSettleState.managed_session_id],
            set_={
                "unattended_turn_event_id": stmt.excluded.unattended_turn_event_id,
                "unattended_rules_channel_id": stmt.excluded.unattended_rules_channel_id,
                "agent_chat_recorded_send": stmt.excluded.agent_chat_recorded_send,
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


@dataclass(frozen=True)
class AgentChatSends:
    """The agent-chat sends on one session, as written under its fence.

    `begun` counts the sends begun (`record_agent_chat_driver`); `recorded` is the number
    of the latest send whose turn was recorded, and `turn_event_id` that turn
    (`record_unattended_turn`).
    """

    begun: int | None
    recorded: int | None
    turn_event_id: str | None


async def count_agent_chat_sends(
    session: AsyncSession, *, managed_session_id: str
) -> AgentChatSends | None:
    """The session's agent-chat sends; None when it has no row."""
    row = (
        await session.execute(
            select(
                SessionSettleState.agent_chat_sends,
                SessionSettleState.agent_chat_recorded_send,
                SessionSettleState.unattended_turn_event_id,
            ).where(SessionSettleState.managed_session_id == managed_session_id)
        )
    ).one_or_none()
    if row is None:
        return None
    return AgentChatSends(begun=row[0], recorded=row[1], turn_event_id=row[2])


@dataclass(frozen=True)
class SessionProvenance:
    """Where an agent-chat session came from, as Daimon stamped its metadata at creation:
    the grant its Daimon credential signs (`chat_session_id`), the channel its tools were
    chosen for, and the account that created it. Each is None when the stamp has none."""

    grant: str | None
    channel_id: str | None
    account_id: uuid.UUID | None


@dataclass(frozen=True)
class AgentChatSession:
    """The agent-chat session a credential's grant belongs to (`find_agent_chat_session`)."""

    managed_session_id: str
    channel_id: str | None


async def record_agent_chat_driver(
    session: AsyncSession,
    *,
    managed_session_id: str,
    signed_in: bool,
    send: bool,
    provenance: SessionProvenance,
    new_session: bool = False,
    sends_read: int | None = None,
) -> int | None:
    """Record, before a send or resume, whether that session's driver is signed in.

    A send (`send`) also counts itself and returns its number, for
    `record_unattended_turn` once it is accepted. Only a send can name its driver: a
    resume carries on a turn someone else may have started. The driver is named only if
    nothing changed since the caller decided, checked in the same statement as the write: on a
    session its caller just created (`new_session`), only if this write creates the
    row; on any other, only if `agent_chat_sends` is still `sends_read`, the count the
    decision read, so another send begun in between names nobody.

    `provenance` is copied on each write, whoever drives, so a session created before it
    was recorded is found from its next send on (`find_agent_chat_session`).

    The grant is a fresh uuid4 for each session (`sessions.create_session`), so no two
    sessions share one. If two ever did, which would be a bug, the unique index refuses
    this write: the error is raised, nothing is recorded, and the send is not made.
    """
    where = {
        "agent_chat_grant": provenance.grant,
        "agent_chat_channel": provenance.channel_id,
        "agent_chat_account_id": provenance.account_id,
    }
    stmt = insert(SessionSettleState).values(
        managed_session_id=managed_session_id,
        agent_chat_signed_in=signed_in and send and new_session,
        agent_chat_sends=1 if send else None,
        **where,
    )
    named: object = False
    if signed_in and send and not new_session and sends_read is not None:
        # Read before this row is updated: the count as it stood when the send began.
        named = case((SessionSettleState.agent_chat_sends == sends_read, True), else_=False)
    set_: dict[str, object] = {"agent_chat_signed_in": named, "updated_at": func.now(), **where}
    if send:
        set_["agent_chat_sends"] = func.coalesce(SessionSettleState.agent_chat_sends, 0) + 1
    number = await session.scalar(
        stmt.on_conflict_do_update(
            index_elements=[SessionSettleState.managed_session_id], set_=set_
        ).returning(SessionSettleState.agent_chat_sends)
    )
    return number if send else None


async def record_agent_chat_provenance(
    session: AsyncSession, *, managed_session_id: str, provenance: SessionProvenance
) -> None:
    """Copy only where an agent-chat session came from, for a resume that keeps the driver
    record as it is (`agent_chat_requester.record_driver`): no driver, count or turn column
    is written, and a new row leaves them empty, as no row does. A shared grant is refused
    as in `record_agent_chat_driver`."""
    where = {
        "agent_chat_grant": provenance.grant,
        "agent_chat_channel": provenance.channel_id,
        "agent_chat_account_id": provenance.account_id,
    }
    stmt = insert(SessionSettleState).values(managed_session_id=managed_session_id, **where)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[SessionSettleState.managed_session_id],
            set_={**where, "updated_at": func.now()},
        )
    )


async def is_agent_chat_signed_in(session: AsyncSession, *, managed_session_id: str) -> bool:
    """Whether the latest recorded driver of the session is signed in; no record is no."""
    value = await session.scalar(
        select(SessionSettleState.agent_chat_signed_in).where(
            SessionSettleState.managed_session_id == managed_session_id
        )
    )
    return value is True


async def find_agent_chat_session(
    session: AsyncSession, *, grant: str, account_id: uuid.UUID
) -> AgentChatSession | None:
    """The agent-chat session whose Daimon credential signs `grant`, created by `account_id`.

    None for any other session: a chat thread's, one no send recorded since provenance was
    kept, or another account's."""
    row = (
        await session.execute(
            select(
                SessionSettleState.managed_session_id, SessionSettleState.agent_chat_channel
            ).where(
                SessionSettleState.agent_chat_grant == grant,
                SessionSettleState.agent_chat_account_id == account_id,
            )
        )
    ).one_or_none()
    return None if row is None else AgentChatSession(managed_session_id=row[0], channel_id=row[1])


async def count_agent_chat_provenance_for_account(
    session: AsyncSession, *, account_id: uuid.UUID
) -> int:
    """How many agent-chat sessions record `account_id` as the account that created them."""
    count = await session.scalar(
        select(func.count())
        .select_from(SessionSettleState)
        .where(SessionSettleState.agent_chat_account_id == account_id)
    )
    return count or 0


async def erase_agent_chat_provenance_for_account(
    session: AsyncSession, *, account_id: uuid.UUID
) -> int:
    """For an account's erasure: clear where its agent-chat sessions came from, the grant,
    channel and account. Such a session is then not found (`find_agent_chat_session`) and
    reads as one no send recorded. The driver record and the settle cursor stay: they name
    no one. Outside the session fence: no send decides from these columns."""
    cleared = await session.scalars(
        update(SessionSettleState)
        .where(SessionSettleState.agent_chat_account_id == account_id)
        .values(agent_chat_grant=None, agent_chat_channel=None, agent_chat_account_id=None)
        .returning(SessionSettleState.managed_session_id)
    )
    return len(list(cleared))


async def erase_agent_chat_provenance_for_tenant(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> int:
    """For a tenant's deletion, before its accounts cascade: the same as
    `erase_agent_chat_provenance_for_account` for each of its accounts."""
    accounts = select(Account.id).where(Account.tenant_id == tenant_id)
    cleared = await session.scalars(
        update(SessionSettleState)
        .where(SessionSettleState.agent_chat_account_id.in_(accounts))
        .values(agent_chat_grant=None, agent_chat_channel=None, agent_chat_account_id=None)
        .returning(SessionSettleState.managed_session_id)
    )
    return len(list(cleared))
