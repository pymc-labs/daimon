"""Asks from a running turn to its helpers, and their replies (`daimon.core.helpers`)."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Collection, Sequence
from datetime import datetime, timedelta
from typing import Any, cast

from daimon.core._models import FileUpload, HelperAsk
from daimon.core.stores.domain import HelperAskRow, HelperAskStatus, UploadedFile
from sqlalchemy import BigInteger, CursorResult, delete, func, select, true, type_coerce, update
from sqlalchemy import cast as sa_cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

OPEN: tuple[HelperAskStatus, ...] = ("queued", "running")
#: How long an ask's file links work, from the ask: past any turn that can use them.
FILES_TTL = timedelta(hours=1)
#: Most files one ask carries each way.
MAX_FILES = 10
#: Most bytes of files a helper may send back on one ask, in all.
MAX_REPLY_BYTES = 50_000_000


async def create_ask(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    origin_id: uuid.UUID,
    helper_agent_id: str,
    helper_name: str,
    message: str,
    now: datetime,
    files: Sequence[UploadedFile] = (),
) -> HelperAskRow:
    ask = HelperAsk(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        origin_id=origin_id,
        helper_agent_id=helper_agent_id,
        helper_name=helper_name,
        message=message,
        status="queued",
        created_at=now,
        files=[file.model_dump() for file in files],
        reply_files=[],
        files_token=secrets.token_urlsafe(24),
    )
    session.add(ask)
    await session.flush()
    return HelperAskRow.model_validate(ask)


async def count_asks(session: AsyncSession, *, origin_id: uuid.UUID) -> int:
    """Every ask this turn has made, finished or not."""
    total = await session.scalar(
        select(func.count()).select_from(HelperAsk).where(HelperAsk.origin_id == origin_id)
    )
    return total or 0


async def list_asks(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    origin_id: uuid.UUID,
    ids: Collection[uuid.UUID] | None = None,
) -> list[HelperAskRow]:
    """This turn's asks, oldest first; only `ids` when given."""
    query = select(HelperAsk).where(
        HelperAsk.tenant_id == tenant_id, HelperAsk.origin_id == origin_id
    )
    if ids is not None:
        query = query.where(HelperAsk.id.in_(list(ids)))
    rows = await session.scalars(query.order_by(HelperAsk.created_at, HelperAsk.id))
    return [HelperAskRow.model_validate(row) for row in rows]


async def claim_queued(
    session: AsyncSession, *, origin_id: uuid.UUID, skip_helpers: Collection[str], limit: int
) -> list[HelperAskRow]:
    """Mark up to `limit` queued asks running, oldest first, one per helper.

    A helper not in `skip_helpers` (those already running an ask) gets its oldest
    queued ask; its later ones wait, so a helper's conversation stays in order.
    """
    if limit <= 0:
        return []
    queued = await session.scalars(
        select(HelperAsk)
        .where(HelperAsk.origin_id == origin_id, HelperAsk.status == "queued")
        .order_by(HelperAsk.created_at, HelperAsk.id)
        .with_for_update(skip_locked=True)
    )
    claimed: list[HelperAsk] = []
    busy = set(skip_helpers)
    for ask in queued:
        if len(claimed) == limit:
            break
        if ask.helper_agent_id in busy:
            continue
        busy.add(ask.helper_agent_id)
        claimed.append(ask)
    if claimed:
        await session.execute(
            update(HelperAsk)
            .where(HelperAsk.id.in_([ask.id for ask in claimed]))
            .values(status="running")
        )
    return [
        HelperAskRow.model_validate(ask).model_copy(update={"status": "running"}) for ask in claimed
    ]


async def finish_ask(
    session: AsyncSession,
    *,
    ask_id: uuid.UUID,
    status: HelperAskStatus,
    reply: str | None,
    now: datetime,
) -> None:
    await session.execute(
        update(HelperAsk)
        .where(HelperAsk.id == ask_id, HelperAsk.status.in_(OPEN))
        .values(status=status, reply=reply, finished_at=now)
    )


async def stop_open_asks(session: AsyncSession, *, origin_id: uuid.UUID, now: datetime) -> None:
    """End every ask still open for this turn: the turn that asked is over."""
    await session.execute(
        update(HelperAsk)
        .where(HelperAsk.origin_id == origin_id, HelperAsk.status.in_(OPEN))
        .values(status="stopped", finished_at=now)
    )


async def get_ask_by_files_token(
    session: AsyncSession, *, token: str, now: datetime
) -> HelperAskRow | None:
    """The ask whose file links carry `token`, while they still work."""
    ask = await session.scalar(
        select(HelperAsk).where(
            HelperAsk.files_token == token, HelperAsk.created_at > now - FILES_TTL
        )
    )
    return None if ask is None else HelperAskRow.model_validate(ask)


async def add_reply_file(
    session: AsyncSession,
    *,
    ask_id: uuid.UUID,
    file: UploadedFile,
    max_files: int,
    max_bytes: int,
) -> bool:
    """Add a file to a running ask's reply; False once it ended, holds `max_files`, or
    would hold more than `max_bytes`. The checks and the add are one statement, so
    parallel uploads cannot both squeeze in past a cap."""
    sent = func.jsonb_array_elements(HelperAsk.reply_files).column_valued("sent")
    held = (
        select(func.coalesce(func.sum(sa_cast(sent.op("->>")("size"), BigInteger)), 0))
        .correlate(HelperAsk)
        .scalar_subquery()
    )
    added = await session.scalar(
        update(HelperAsk)
        .where(
            HelperAsk.id == ask_id,
            HelperAsk.status == "running",
            func.jsonb_array_length(HelperAsk.reply_files) < max_files,
            held + file.size <= max_bytes,
        )
        .values(
            reply_files=HelperAsk.reply_files.op("||", return_type=JSONB)(
                type_coerce([file.model_dump()], JSONB)
            )
        )
        .returning(HelperAsk.id)
    )
    return added is not None


async def delete_expired_reply_uploads(session: AsyncSession, *, now: datetime) -> int:
    """Delete the files helpers sent back on asks whose links have expired; returns how many.

    A reply file is the asking agent's to use within the turn, so past `FILES_TTL` it is
    only stored bytes, unless the agent passed it on to an ask whose links still work.
    """
    sent = func.jsonb_array_elements(HelperAsk.reply_files).table_valued("value")
    expired = (
        select(sent.c.value.op("->>")("id"))
        .select_from(HelperAsk)
        .join(sent, true())
        .where(HelperAsk.created_at <= now - FILES_TTL)
    )
    passed = func.jsonb_array_elements(HelperAsk.files).table_valued("value")
    passed_id = passed.c.value.op("->>")("id")
    live = (
        select(passed_id)
        .select_from(HelperAsk)
        .join(passed, true())
        .where(HelperAsk.created_at > now - FILES_TTL, passed_id.is_not(None))
    )
    result = await session.execute(
        delete(FileUpload).where(FileUpload.id.in_(expired), FileUpload.id.not_in(live))
    )
    return cast(CursorResult[Any], result).rowcount
