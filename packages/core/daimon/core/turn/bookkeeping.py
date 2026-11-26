"""Durable turn cleanup shared by adapters.

The callers retain their admission gates, snapshots and transport error
handling. Most store and provider calls are injected so each adapter keeps its
existing failure policy; `release_turn_markers` calls the marker stores itself.
"""

from collections.abc import Awaitable, Callable, Iterable, Mapping
from functools import partial
from typing import Protocol
from uuid import UUID

from anthropic import AsyncAnthropic
from daimon.core.ma import interrupt_orphaned_session
from daimon.core.stores.domain import ThreadSessionRow
from daimon.core.stores.thread_sessions import (
    clear_active_turn,
    clear_active_turn_if_message_id,
    mark_dead,
)
from daimon.core.turn.running import RunningTurns
from mux.contracts.ids import Scope
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


async def release_turn_markers(
    sessionmaker: async_sessionmaker[AsyncSession],
    running_turns: RunningTurns,
    markers: Mapping[UUID, str | None],
) -> None:
    """Clear a finished turn's thread markers, each in its own transaction.

    A marker with an expected message id is cleared only while it still names
    that message; one with None is cleared unconditionally. A turn unwinding
    from the shutdown drain's cancel keeps every marker: its session is still
    running, and the next process's orphan sweep recovers it through them.
    """
    if running_turns.is_drain_cancel():
        return
    for mapping_id, expected_message_id in markers.items():
        async with sessionmaker() as session:
            if expected_message_id is None:
                await clear_active_turn(session, id=mapping_id)
            else:
                await clear_active_turn_if_message_id(
                    session, id=mapping_id, expected_message_id=expected_message_id
                )
            await session.commit()


async def recover_orphan_marker(
    sessionmaker: async_sessionmaker[AsyncSession],
    row: ThreadSessionRow,
    *,
    clear: CompareClearMarker,
    interrupt: InterruptSession,
) -> bool:
    """Clear the snapshot marker only with an interrupt or a dead mapping.

    Callers edit the card first. A moved marker keeps its new owner and its
    session running. If the interrupt is unconfirmed, mark the old mapping
    dead so the next turn prepares a fresh MA session. Database errors
    propagate into the existing boot retry.
    """
    if row.active_turn_message_id is None:
        return False
    async with sessionmaker.begin() as session:
        cleared = await clear(session, id=row.id, expected_message_id=row.active_turn_message_id)
        if cleared and not await interrupt(session_id=row.ma_session_id):
            await mark_dead(session, id=row.id)
    return cleared


def orphan_scope(row: ThreadSessionRow, *, platform: str) -> Scope | None:
    """The scope a boot sweep interrupts an orphaned session under."""
    if row.account_id is None:
        return None
    return Scope(
        tenant_id=str(row.tenant_id),
        account_id=str(row.account_id),
        principal_id="daimon",
        authorization_id=f"{platform}-orphan-sweep",
    )


async def retire_orphan_marker(
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    row: ThreadSessionRow,
    *,
    platform: str,
) -> bool:
    """`recover_orphan_marker` with the stores' compare-and-clear and the MA interrupt."""
    return await recover_orphan_marker(
        sessionmaker,
        row,
        clear=clear_active_turn_if_message_id,
        interrupt=partial(
            interrupt_orphaned_session, anthropic, scope=orphan_scope(row, platform=platform)
        ),
    )


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
