"""Store for web chats: threads and their messages.

A thread belongs to one person and is answered by the agent it was started with. A
deleted thread keeps its row (so its id is never reused) and loses its messages.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import datetime
from decimal import Decimal
from typing import Literal

from daimon.core._models import WebMessage, WebThread
from daimon.core.cursor import Cursor
from daimon.core.stores import file_uploads
from daimon.core.stores.domain import (
    WebMessageBlocks,
    WebMessageRow,
    WebMessageStatus,
    WebThreadRow,
)
from sqlalchemy import delete, literal, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def create_thread(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    person_id: uuid.UUID,
    agent_id: str,
    agent_name: str,
    title: str,
) -> WebThreadRow:
    orm = WebThread(
        tenant_id=tenant_id,
        person_id=person_id,
        agent_id=agent_id,
        agent_name=agent_name,
        title=title,
    )
    session.add(orm)
    await session.flush()
    await session.refresh(orm)
    return WebThreadRow.model_validate(orm)


async def get_thread(
    session: AsyncSession, *, person_id: uuid.UUID, thread_id: uuid.UUID
) -> WebThreadRow | None:
    """The person's live thread, or None (someone else's, deleted or unknown)."""
    orm = (
        await session.execute(
            select(WebThread).where(
                WebThread.id == thread_id,
                WebThread.person_id == person_id,
                WebThread.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    return None if orm is None else WebThreadRow.model_validate(orm)


async def list_threads(
    session: AsyncSession,
    *,
    person_id: uuid.UUID,
    limit: int | None = None,
    before: Cursor | None = None,
) -> list[WebThreadRow]:
    """The person's live threads, most recently active first.

    `limit` and `before` (the `(updated_at, id)` of the last thread already seen) page it.
    """
    query = select(WebThread).where(
        WebThread.person_id == person_id, WebThread.deleted_at.is_(None)
    )
    if before is not None:
        query = query.where(
            tuple_(WebThread.updated_at, WebThread.id)
            < tuple_(literal(before[0]), literal(before[1]))
        )
    query = query.order_by(WebThread.updated_at.desc(), WebThread.id.desc())
    if limit is not None:
        query = query.limit(limit)
    return [WebThreadRow.model_validate(orm) for orm in await session.scalars(query)]


async def list_titles(
    session: AsyncSession, *, tenant_id: uuid.UUID, thread_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """The title of each of the workspace's threads among `thread_ids`, deleted ones too."""
    if not thread_ids:
        return {}
    rows = await session.execute(
        select(WebThread.id, WebThread.title).where(
            WebThread.tenant_id == tenant_id, WebThread.id.in_(list(thread_ids))
        )
    )
    return {thread_id: title for thread_id, title in rows.all()}


async def update_thread(
    session: AsyncSession,
    *,
    thread_id: uuid.UUID,
    title: str | None = None,
    updated_at: datetime | None = None,
) -> None:
    values: dict[str, object] = {}
    if title is not None:
        values["title"] = title
    if updated_at is not None:
        values["updated_at"] = updated_at
    if values:
        await session.execute(update(WebThread).where(WebThread.id == thread_id).values(**values))


async def move_threads(
    session: AsyncSession, *, from_person_id: uuid.UUID, to_person_id: uuid.UUID
) -> None:
    """Hand one person's chats, and the files they uploaded, to another in the same workspace."""
    await session.execute(
        update(WebThread)
        .where(WebThread.person_id == from_person_id)
        .values(person_id=to_person_id)
    )
    await file_uploads.move_person_uploads(
        session, from_person_id=from_person_id, to_person_id=to_person_id
    )


async def delete_thread(session: AsyncSession, *, thread_id: uuid.UUID, now: datetime) -> None:
    """Soft-delete the thread and erase its messages and the files only they carried.

    The thread goes first: its row lock makes a reply being saved either land before the
    messages are erased or find the thread deleted (`upsert_assistant_message`).
    """
    person_id = await session.scalar(
        update(WebThread)
        .where(WebThread.id == thread_id)
        .values(deleted_at=now, title="")
        .returning(WebThread.person_id)
    )
    rows = (
        await session.execute(
            delete(WebMessage).where(WebMessage.thread_id == thread_id).returning(WebMessage)
        )
    ).scalars()
    files = {
        file.id
        for message in map(WebMessageRow.model_validate, rows)
        for file in (*message.blocks.attachments, *message.blocks.files)
    }
    if person_id is not None and files:
        await file_uploads.delete_unreferenced_web_uploads(
            session, person_id=person_id, handle_ids=files
        )


async def insert_user_message(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    thread_id: uuid.UUID,
    message_id: uuid.UUID,
    content: str,
    blocks: WebMessageBlocks,
) -> tuple[WebMessageRow, bool]:
    """Insert the message, or return the one already sent under its id; True when new.

    The browser chooses the id, so a retried post never sends a message twice. An id
    already used in another thread is refused (`LookupError`).
    """
    inserted = (
        await session.execute(
            insert(WebMessage)
            .values(
                id=message_id,
                tenant_id=tenant_id,
                thread_id=thread_id,
                role="user",
                content=content,
                blocks=blocks.model_dump(mode="json"),
                status="done",
            )
            .on_conflict_do_nothing(index_elements=[WebMessage.id])
            .returning(WebMessage)
        )
    ).scalar_one_or_none()
    if inserted is not None:
        return WebMessageRow.model_validate(inserted), True
    existing = await session.get(WebMessage, message_id)
    if existing is None or existing.thread_id != thread_id or existing.role != "user":
        raise LookupError("message id already used")
    return WebMessageRow.model_validate(existing), False


async def upsert_assistant_message(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    thread_id: uuid.UUID,
    message_id: uuid.UUID,
    turn_id: uuid.UUID,
    content: str,
    blocks: WebMessageBlocks,
    status: WebMessageStatus,
    cost_usd: Decimal | None = None,
    duration_ms: int | None = None,
) -> WebMessageRow | None:
    """Write a reply as it stands: first while it runs, then as it ended.

    None when the thread was deleted meanwhile: nothing is written into it.
    """
    alive = await session.scalar(
        select(WebThread.id)
        .where(WebThread.id == thread_id, WebThread.deleted_at.is_(None))
        .with_for_update()
    )
    if alive is None:
        return None
    values = {
        "content": content,
        "blocks": blocks.model_dump(mode="json"),
        "status": status,
        "cost_usd": cost_usd,
        "duration_ms": duration_ms,
    }
    orm = (
        await session.execute(
            insert(WebMessage)
            .values(
                id=message_id,
                tenant_id=tenant_id,
                thread_id=thread_id,
                role="assistant",
                turn_id=turn_id,
                **values,
            )
            .on_conflict_do_update(index_elements=[WebMessage.id], set_=values)
            .returning(WebMessage)
        )
    ).scalar_one()
    return WebMessageRow.model_validate(orm)


async def get_message(
    session: AsyncSession, *, person_id: uuid.UUID, message_id: uuid.UUID
) -> WebMessageRow | None:
    """A message in one of the person's live threads, or None."""
    orm = (
        await session.execute(
            select(WebMessage)
            .join(WebThread, WebThread.id == WebMessage.thread_id)
            .where(
                WebMessage.id == message_id,
                WebThread.person_id == person_id,
                WebThread.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    return None if orm is None else WebMessageRow.model_validate(orm)


async def list_messages(
    session: AsyncSession,
    *,
    thread_id: uuid.UUID,
    limit: int | None = None,
    before_seq: int | None = None,
) -> list[WebMessageRow]:
    """The thread's messages, oldest first.

    With `limit`, the newest that many (those before `before_seq`, if given), still oldest
    first, so a client reads the end of a long chat and pages back from there.
    """
    query = select(WebMessage).where(WebMessage.thread_id == thread_id)
    if before_seq is not None:
        query = query.where(WebMessage.seq < before_seq)
    if limit is None:
        rows = list(await session.scalars(query.order_by(WebMessage.seq)))
    else:
        newest = await session.scalars(query.order_by(WebMessage.seq.desc()).limit(limit))
        rows = list(reversed(list(newest)))
    return [WebMessageRow.model_validate(orm) for orm in rows]


async def get_card_message(
    session: AsyncSession, *, thread_id: uuid.UUID, card_id: str
) -> WebMessageRow | None:
    """The thread's message showing card `card_id`, or None."""
    orm = await session.scalar(
        select(WebMessage)
        .where(WebMessage.thread_id == thread_id, WebMessage.blocks["card"]["id"].astext == card_id)
        .order_by(WebMessage.seq.desc())
        .limit(1)
    )
    return None if orm is None else WebMessageRow.model_validate(orm)


async def holds_output_file(session: AsyncSession, *, person_id: uuid.UUID, file_id: str) -> bool:
    """Whether a reply in one of the person's live threads carries output file `file_id`."""
    found = await session.scalar(
        select(WebMessage.id)
        .join(WebThread, WebThread.id == WebMessage.thread_id)
        .where(
            WebThread.person_id == person_id,
            WebThread.deleted_at.is_(None),
            WebMessage.blocks["files"].contains([{"id": file_id}]),
        )
        .limit(1)
    )
    return found is not None


async def get_last_reply(session: AsyncSession, *, thread_id: uuid.UUID) -> WebMessageRow | None:
    """The thread's latest assistant message, or None."""
    orm = await session.scalar(
        select(WebMessage)
        .where(WebMessage.thread_id == thread_id, WebMessage.role == "assistant")
        .order_by(WebMessage.seq.desc())
        .limit(1)
    )
    return None if orm is None else WebMessageRow.model_validate(orm)


async def update_blocks(
    session: AsyncSession, *, message_id: uuid.UUID, blocks: WebMessageBlocks
) -> None:
    await session.execute(
        update(WebMessage)
        .where(WebMessage.id == message_id)
        .values(blocks=blocks.model_dump(mode="json"))
    )


async def set_feedback(
    session: AsyncSession, *, message_id: uuid.UUID, feedback: Literal["up", "down"] | None
) -> None:
    await session.execute(
        update(WebMessage).where(WebMessage.id == message_id).values(feedback=feedback)
    )


async def mark_running_interrupted(session: AsyncSession) -> list[WebMessageRow]:
    """End every reply still marked running: the process that ran it is gone."""
    rows = (
        await session.execute(
            update(WebMessage)
            .where(WebMessage.status == "running")
            .values(status="interrupted")
            .returning(WebMessage)
        )
    ).scalars()
    return [WebMessageRow.model_validate(orm) for orm in rows]
