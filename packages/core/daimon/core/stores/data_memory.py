"""What a chat session read from data sources, so a memory it saves carries that audience.

The data tools note each item a session read (`record:<id>`; a live answer or caller SQL,
which name no stored record, as `live:<source id>` and `sql`; a person's own data read as
them as `person:<account id>`). A memory saved later in the
session records them, and shows only where everyone the answer is for could read every one
(`daimon.data.reads.readable`). Callers own transactions.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from daimon.core._models import SessionDataRead
from sqlalchemy import literal, select
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
        await session.execute(insert(SessionDataRead).values(rows).on_conflict_do_nothing())


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
