"""Durable turn cleanup shared by adapters.

The callers retain their admission gates, snapshots, transaction boundaries
and transport error handling. Store and provider calls are injected so each
adapter keeps its existing failure policy.
"""

from collections.abc import Awaitable, Callable, Iterable
from typing import Protocol
from uuid import UUID

from daimon.core.stores.domain import ThreadSessionRow
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class ClearMarker(Protocol):
    async def __call__(self, session: AsyncSession, *, id: UUID) -> None: ...


class CompareClearMarker(Protocol):
    async def __call__(
        self, session: AsyncSession, *, id: UUID, expected_message_id: str
    ) -> bool: ...


class InterruptSession(Protocol):
    async def __call__(self, *, session_id: str) -> bool: ...


async def clear_turn_markers(
    session: AsyncSession, ids: Iterable[UUID], *, clear: ClearMarker
) -> None:
    """Clear touched mappings in the caller's transaction and iteration order."""
    for mapping_id in ids:
        await clear(session, id=mapping_id)


async def recover_orphan_marker(
    sessionmaker: async_sessionmaker[AsyncSession],
    row: ThreadSessionRow,
    *,
    clear: CompareClearMarker,
    interrupt: InterruptSession,
) -> bool:
    """Commit the snapshot CAS before interrupting its MA session.

    Callers edit the card first. A moved marker keeps its new owner and its
    session running. Database errors propagate into the existing boot retry.
    """
    if row.active_turn_message_id is None:
        return False
    async with sessionmaker() as session:
        cleared = await clear(session, id=row.id, expected_message_id=row.active_turn_message_id)
        await session.commit()
    if cleared:
        await interrupt(session_id=row.ma_session_id)
    return cleared


async def reconcile_found_card(
    *,
    expected_message_id: str | None,
    recovered_message_id: str,
    record: Callable[[str], Awaitable[bool]],
    edit: Callable[[], Awaitable[bool]],
    retire: Callable[[str], Awaitable[None]],
) -> None:
    """Record a recovered id before edits; retire only after every edit succeeds."""
    if expected_message_id is None:
        if not await record(recovered_message_id):
            return
        expected_message_id = recovered_message_id
    if await edit():
        await retire(expected_message_id)
