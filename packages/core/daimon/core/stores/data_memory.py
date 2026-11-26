"""What a chat session read from data sources, so a memory it saves carries that audience.

The data tools note each item a session read (`record:<id>`; a live answer or caller SQL,
which name no stored record, as an `audience_ref`: where and by whom). A memory saved later
in the session records them, and shows only where everyone the answer is for could read every
record (`daimon.data.reads.readable`) and was among each ref's readers there. Callers own
transactions.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any, Final, cast

from daimon.core._models import SessionDataRead
from sqlalchemy import CursorResult, literal, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def note_reads(
    session: AsyncSession, *, tenant_id: uuid.UUID, chat_session_id: str, refs: Iterable[str]
) -> None:
    """Record that the session read `refs`."""
    rows = [
        {"tenant_id": tenant_id, "chat_session_id": chat_session_id, "ref": ref}
        for ref in dict.fromkeys(refs)
    ]
    if rows:
        stmt = insert(SessionDataRead).values(rows)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["tenant_id", "chat_session_id", "ref"],
                set_={"read_at": stmt.excluded.read_at},
            )
        )


READS_RETENTION: Final = timedelta(days=90)


async def prune_reads(session: AsyncSession, *, cutoff: datetime, batch: int = 500) -> int:
    """Forget the reads of up to `batch` sessions that read nothing since `cutoff` and are
    no live chat session; the rows deleted. The caller commits."""
    result = await session.execute(
        text(
            "delete from session_data_reads r using (select tenant_id, chat_session_id"
            " from session_data_reads s group by tenant_id, chat_session_id"
            " having max(read_at) < :cutoff and not exists (select 1 from thread_sessions t"
            " where t.tenant_id = s.tenant_id and t.status = 'live'"
            " and t.effective_config ->> 'chat_session_id' = s.chat_session_id"
            ") limit :batch) old"
            " where r.tenant_id = old.tenant_id and r.chat_session_id = old.chat_session_id"
        ),
        {"cutoff": cutoff, "batch": batch},
    )
    return cast(CursorResult[Any], result).rowcount


async def reads_of(
    session: AsyncSession, *, tenant_id: uuid.UUID, chat_session_id: str
) -> list[str]:
    """Every data item the session read so far, sorted."""
    found = await session.scalars(
        select(SessionDataRead.ref)
        .where(
            SessionDataRead.tenant_id == tenant_id,
            SessionDataRead.chat_session_id == chat_session_id,
        )
        .order_by(SessionDataRead.ref)
    )
    return list(found)


async def has_read(session: AsyncSession, *, tenant_id: uuid.UUID, chat_session_id: str) -> bool:
    """Whether the session read any data item."""
    found = await session.scalar(
        select(literal(True))
        .where(
            SessionDataRead.tenant_id == tenant_id,
            SessionDataRead.chat_session_id == chat_session_id,
        )
        .limit(1)
    )
    return bool(found)


async def tenant_has_reads(session: AsyncSession, *, tenant_id: uuid.UUID) -> bool:
    """Whether any of the tenant's chat sessions read a data item."""
    found = await session.scalar(
        select(literal(True)).where(SessionDataRead.tenant_id == tenant_id).limit(1)
    )
    return bool(found)


async def carry_reads(
    session: AsyncSession, *, tenant_id: uuid.UUID, source: str, target: str
) -> None:
    """Give the session that continues `source` what it read, so a memory saved after the
    hand-over is tagged with it."""
    if source == target:
        return
    await session.execute(
        insert(SessionDataRead)
        .from_select(
            ["tenant_id", "chat_session_id", "ref", "read_at"],
            select(
                SessionDataRead.tenant_id,
                literal(target),
                SessionDataRead.ref,
                SessionDataRead.read_at,
            ).where(
                SessionDataRead.tenant_id == tenant_id, SessionDataRead.chat_session_id == source
            ),
        )
        .on_conflict_do_nothing()
    )
