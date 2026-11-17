"""Store for monitored agents: the questions, answers and MCP tool calls kept for admins.

Rows are written only through `daimon.core.monitoring`, which decides whether the
answering agent is monitored. A person's rows go with their account (`delete_for_account`)
and every row with its tenant (the foreign key); `prune_before` ends retention.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from daimon.core._models import MonitoredEvent
from daimon.core.stores.domain import (
    MonitoredConversationRow,
    MonitoredEventRow,
    MonitoredKind,
    MonitoredPlatform,
)
from sqlalchemy import String, cast, delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, aliased
from sqlalchemy.sql.elements import ColumnElement


@dataclass(frozen=True)
class MonitoredSubject:
    """Who asked which agent, and where: shared by a turn's question and answer."""

    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    platform: MonitoredPlatform
    account_id: uuid.UUID
    conversation: str | None


@dataclass(frozen=True)
class MonitoredEntry:
    kind: MonitoredKind
    content: str | None = None
    tool_name: str | None = None
    outcome: str | None = None
    turn_ref: str | None = None
    created_at: datetime | None = None


async def record_events(
    session: AsyncSession, subject: MonitoredSubject, entries: Sequence[MonitoredEntry]
) -> int:
    """Insert `entries`; a turn's question or answer already kept is skipped. Returns
    how many were inserted."""
    if not entries:
        return 0
    rows = [
        {
            "tenant_id": subject.tenant_id,
            "agent_id": subject.agent_id,
            "platform": subject.platform,
            "account_id": subject.account_id,
            "conversation": subject.conversation,
            "turn_ref": entry.turn_ref,
            "kind": entry.kind,
            "content": entry.content,
            "tool_name": entry.tool_name,
            "outcome": entry.outcome,
            "created_at": entry.created_at or func.now(),
        }
        for entry in entries
    ]
    result = await session.execute(
        insert(MonitoredEvent).values(rows).on_conflict_do_nothing().returning(MonitoredEvent.id)
    )
    return len(result.all())


async def get_open_question(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation: str, turn_ref: str
) -> MonitoredEventRow | None:
    """The turn's kept question while it has no kept answer, else None."""
    rows = (
        await session.scalars(
            select(MonitoredEvent).where(
                MonitoredEvent.tenant_id == tenant_id,
                MonitoredEvent.conversation == conversation,
                MonitoredEvent.turn_ref == turn_ref,
            )
        )
    ).all()
    kinds = {row.kind: row for row in rows}
    question = kinds.get("question")
    if question is None or "answer" in kinds:
        return None
    return MonitoredEventRow.model_validate(question)


async def open_question_refs(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation: str
) -> list[str]:
    """The turn refs of a conversation's kept questions that have no kept answer yet."""
    answer = aliased(MonitoredEvent)
    answered = select(answer.id).where(
        answer.tenant_id == MonitoredEvent.tenant_id,
        answer.conversation == MonitoredEvent.conversation,
        answer.turn_ref == MonitoredEvent.turn_ref,
        answer.kind == "answer",
    )
    stmt = select(MonitoredEvent.turn_ref).where(
        MonitoredEvent.tenant_id == tenant_id,
        MonitoredEvent.conversation == conversation,
        MonitoredEvent.kind == "question",
        MonitoredEvent.turn_ref.is_not(None),
        ~answered.exists(),
    )
    return [ref for ref in await session.scalars(stmt) if ref is not None]


async def list_events(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    since: datetime | None = None,
    until: datetime | None = None,
    platforms: Sequence[MonitoredPlatform] | None = None,
    account_id: uuid.UUID | None = None,
    conversation: str | None = None,
    before: tuple[datetime, uuid.UUID] | None = None,
    after: tuple[datetime, uuid.UUID] | None = None,
    day: str | None = None,
    tool_calls_only: bool = False,
    oldest_first: bool = False,
    limit: int = 100,
) -> list[MonitoredEventRow]:
    """One agent's kept rows, newest first; `before` is the last row's (created_at, id).
    With `after` they run oldest first, from that row on (a transcript). `day` keeps the
    rows of one UTC date (`YYYY-MM-DD`); `tool_calls_only` those with no conversation."""
    stmt = select(MonitoredEvent).where(
        MonitoredEvent.tenant_id == tenant_id, MonitoredEvent.agent_id == agent_id
    )
    if since is not None:
        stmt = stmt.where(MonitoredEvent.created_at >= since)
    if until is not None:
        stmt = stmt.where(MonitoredEvent.created_at < until)
    if platforms is not None:
        stmt = stmt.where(MonitoredEvent.platform.in_(platforms))
    if account_id is not None:
        stmt = stmt.where(MonitoredEvent.account_id == account_id)
    if conversation is not None:
        stmt = stmt.where(MonitoredEvent.conversation == conversation)
    if tool_calls_only:
        stmt = stmt.where(MonitoredEvent.conversation.is_(None))
    if day is not None:
        stmt = stmt.where(_day_of(MonitoredEvent.created_at) == day)
    if before is not None:
        stmt = stmt.where(tuple_(MonitoredEvent.created_at, MonitoredEvent.id) < before)
    if after is not None:
        stmt = stmt.where(tuple_(MonitoredEvent.created_at, MonitoredEvent.id) > after)
    if before is None and (after is not None or oldest_first):
        stmt = stmt.order_by(MonitoredEvent.created_at, MonitoredEvent.id)
    else:
        stmt = stmt.order_by(MonitoredEvent.created_at.desc(), MonitoredEvent.id.desc())
    return [
        MonitoredEventRow.model_validate(row) for row in await session.scalars(stmt.limit(limit))
    ]


def _day_of(created_at: InstrumentedAttribute[datetime]) -> ColumnElement[str]:
    """The UTC date of a row, as `YYYY-MM-DD`."""
    return func.to_char(func.timezone("UTC", created_at), "YYYY-MM-DD")


async def list_conversations(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    since: datetime | None = None,
    until: datetime | None = None,
    platforms: Sequence[MonitoredPlatform] | None = None,
    account_id: uuid.UUID | None = None,
    before: tuple[datetime, uuid.UUID] | None = None,
    limit: int = 100,
) -> list[MonitoredConversationRow]:
    """One agent's kept conversations, most recently active first; `before` is the last
    row's (last_at, cursor_id). A person's MCP tool calls have no conversation, so each
    UTC day of them is one row."""
    day = _day_of(MonitoredEvent.created_at)
    last_at = func.max(MonitoredEvent.created_at)
    cursor_id = func.min(cast(MonitoredEvent.id, String))
    stmt = (
        select(
            MonitoredEvent.platform,
            MonitoredEvent.account_id,
            MonitoredEvent.conversation,
            func.min(day).label("day"),
            func.count().filter(MonitoredEvent.kind == "question").label("questions"),
            func.count().filter(MonitoredEvent.kind == "tool").label("tools"),
            func.min(MonitoredEvent.created_at).label("first_at"),
            last_at.label("last_at"),
            cursor_id.label("cursor_id"),
        )
        .where(MonitoredEvent.tenant_id == tenant_id, MonitoredEvent.agent_id == agent_id)
        .group_by(
            MonitoredEvent.platform,
            MonitoredEvent.account_id,
            MonitoredEvent.conversation,
            # A tool row has no conversation: group its person's calls by day.
            func.coalesce(MonitoredEvent.conversation, day),
        )
    )
    if since is not None:
        stmt = stmt.where(MonitoredEvent.created_at >= since)
    if until is not None:
        stmt = stmt.where(MonitoredEvent.created_at < until)
    if platforms is not None:
        stmt = stmt.where(MonitoredEvent.platform.in_(platforms))
    if account_id is not None:
        stmt = stmt.where(MonitoredEvent.account_id == account_id)
    if before is not None:
        stmt = stmt.having(tuple_(last_at, cursor_id) < (before[0], str(before[1])))
    stmt = stmt.order_by(last_at.desc(), cursor_id.desc()).limit(limit)
    return [
        MonitoredConversationRow(
            platform=row.platform,
            account_id=row.account_id,
            conversation=row.conversation,
            day=row.day,
            questions=row.questions,
            tools=row.tools,
            first_at=row.first_at,
            last_at=row.last_at,
            cursor_id=uuid.UUID(row.cursor_id),
        )
        for row in await session.execute(stmt)
    ]


async def count_events(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    since: datetime | None = None,
) -> int:
    stmt = select(func.count()).where(
        MonitoredEvent.tenant_id == tenant_id, MonitoredEvent.agent_id == agent_id
    )
    if since is not None:
        stmt = stmt.where(MonitoredEvent.created_at >= since)
    return int(await session.scalar(stmt) or 0)


async def count_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    stmt = select(func.count()).where(MonitoredEvent.account_id == account_id)
    return int(await session.scalar(stmt) or 0)


async def delete_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    """Delete every row of the person's account, in every workspace; returns the count."""
    result = await session.execute(
        delete(MonitoredEvent)
        .where(MonitoredEvent.account_id == account_id)
        .returning(MonitoredEvent.id)
    )
    return len(result.all())


async def prune_before(session: AsyncSession, *, cutoff: datetime, batch: int) -> int:
    """Delete up to `batch` rows older than `cutoff`, oldest first; the caller commits.
    Rows another transaction holds are skipped for the next run, not waited on."""
    if batch <= 0:
        raise ValueError("batch must be positive")
    victims = (
        select(MonitoredEvent.id)
        .where(MonitoredEvent.created_at < cutoff)
        .order_by(MonitoredEvent.created_at)
        .limit(batch)
        .with_for_update(skip_locked=True)
        .cte("victims")
        .prefix_with("MATERIALIZED")
    )
    result = await session.execute(
        delete(MonitoredEvent)
        .where(MonitoredEvent.id.in_(select(victims.c.id)))
        .returning(MonitoredEvent.id)
    )
    return len(result.all())
