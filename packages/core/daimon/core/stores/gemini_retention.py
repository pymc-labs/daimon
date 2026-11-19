"""SQL for the Gemini records retention sweep (`daimon.core.turn.gemini_retention`)."""

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from daimon.core._models import (
    AccountingOutbox,
    GeminiRecord,
    GeminiRuntimeState,
    ProviderBinding,
    ProviderBindingSlot,
    UsageObservation,
)
from mux.contracts.resources import ProviderBinding as BindingContract
from pydantic import BaseModel, ConfigDict
from sqlalchemy import (
    ColumnElement,
    and_,
    case,
    delete,
    func,
    literal,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy.engine import Row
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Owner = tuple[UUID, UUID, str]
RowId = tuple[str, str]


class StoredRow(BaseModel):
    """One `gemini_records` row with its owner, as the sweep reads it."""

    model_config = ConfigDict(frozen=True)

    owner: Owner
    kind: str
    id: str
    body: str
    version: int
    touched_at: datetime
    refs: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[UUID, UUID, str, str, str]:
        return (*self.owner, self.kind, self.id)


class SessionFacts(BaseModel):
    """What outside the records decides a session's retention."""

    model_config = ConfigDict(frozen=True)

    binding: BindingContract | None
    """The slot's current binding for the session's binding id; None: no slot."""
    settled: dict[str, int]
    """Observation id -> highest applied revision, for ids with nothing pending."""


_COLUMNS = (
    GeminiRecord.tenant_id,
    GeminiRecord.account_id,
    GeminiRecord.project,
    GeminiRecord.kind,
    GeminiRecord.id,
    GeminiRecord.body,
    GeminiRecord.version,
    GeminiRecord.touched_at,
    GeminiRecord.refs,
)


def _stored(r: Row[Any]) -> StoredRow:
    return StoredRow(
        owner=(r.tenant_id, r.account_id, r.project),
        kind=r.kind,
        id=r.id,
        body=r.body,
        version=r.version,
        touched_at=r.touched_at,
        refs=tuple(r.refs),
    )


def _of(owner: Owner) -> tuple[ColumnElement[bool], ...]:
    tenant, account, project = owner
    return (
        GeminiRecord.tenant_id == tenant,
        GeminiRecord.account_id == account,
        GeminiRecord.project == project,
    )


@asynccontextmanager
async def single_runner(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[bool]:
    """True while this caller is the one sweep runner (a transaction advisory lock)."""
    async with sessions() as db, db.begin():
        yield bool(
            await db.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "gemini_records_retention"},
            )
        )


async def stale_rows(
    sessions: async_sessionmaker[AsyncSession],
    *,
    kinds: Sequence[str],
    cutoff: datetime,
    after: tuple[UUID, UUID, str, str, str] | None,
    limit: int,
) -> list[StoredRow]:
    """Rows of `kinds` untouched since `cutoff`, in key order after `after`."""
    key = tuple_(
        GeminiRecord.tenant_id,
        GeminiRecord.account_id,
        GeminiRecord.project,
        GeminiRecord.kind,
        GeminiRecord.id,
    )
    statement = select(*_COLUMNS).where(
        GeminiRecord.kind.in_(kinds), GeminiRecord.touched_at < cutoff
    )
    if after is not None:
        statement = statement.where(key > tuple_(*map(literal, after)))
    async with sessions() as db:
        found = await db.execute(statement.order_by(*key.clauses).limit(limit))
        return [_stored(row) for row in found]


async def rows(
    sessions: async_sessionmaker[AsyncSession], owner: Owner, ids: Sequence[RowId]
) -> dict[RowId, StoredRow]:
    if not ids:
        return {}
    async with sessions() as db:
        found = await db.execute(
            select(*_COLUMNS).where(
                *_of(owner), tuple_(GeminiRecord.kind, GeminiRecord.id).in_(list(ids))
            )
        )
        return {(r.kind, r.id): r for r in map(_stored, found)}


async def session_artifacts(
    sessions: async_sessionmaker[AsyncSession], owner: Owner, session_id: str
) -> list[StoredRow]:
    async with sessions() as db:
        found = await db.execute(
            select(*_COLUMNS).where(
                *_of(owner), GeminiRecord.kind == "artifact", GeminiRecord.session_id == session_id
            )
        )
        return [_stored(row) for row in found]


async def referenced(
    sessions: async_sessionmaker[AsyncSession],
    owner: Owner,
    refs: Sequence[str],
    *,
    besides: str | None,
) -> set[str]:
    """Which of `refs` ("kind:id") a session row other than `besides` names."""
    if not refs:
        return set()
    async with sessions() as db:
        found = await db.scalars(
            select(GeminiRecord.refs).where(
                *_of(owner),
                GeminiRecord.kind == "session",
                GeminiRecord.id != (besides or ""),
                GeminiRecord.refs.overlap(list(refs)),
            )
        )
        return {ref for named in found for ref in named} & set(refs)


async def session_facts(
    sessions: async_sessionmaker[AsyncSession], owner: Owner, binding_id: str
) -> SessionFacts:
    tenant = owner[0]
    async with sessions() as db:
        body = await db.scalar(
            select(ProviderBinding.binding)
            .join(
                ProviderBindingSlot,
                and_(
                    ProviderBindingSlot.binding_id == ProviderBinding.binding_id,
                    ProviderBindingSlot.generation == ProviderBinding.generation,
                ),
            )
            .where(ProviderBinding.binding_id == binding_id, ProviderBinding.tenant_id == tenant)
        )
        applied = await db.execute(
            select(UsageObservation.observation_id, func.max(UsageObservation.revision))
            .where(UsageObservation.binding_id == binding_id, UsageObservation.tenant_id == tenant)
            .group_by(UsageObservation.observation_id)
        )
        pending = set(
            await db.scalars(
                select(AccountingOutbox.observation_id).where(
                    AccountingOutbox.binding_id == binding_id,
                    AccountingOutbox.tenant_id == tenant,
                    AccountingOutbox.applied_at.is_(None),
                )
            )
        )
    return SessionFacts(
        binding=BindingContract.model_validate(body) if body is not None else None,
        settled={id_: revision for id_, revision in applied.tuples() if id_ not in pending},
    )


async def holds(sessions: async_sessionmaker[AsyncSession], owner: Owner) -> str | None:
    """The owner's hold ledger blob."""
    tenant, account, project = owner
    async with sessions() as db:
        return await db.scalar(
            select(GeminiRuntimeState.body).where(
                GeminiRuntimeState.tenant_id == tenant,
                GeminiRuntimeState.account_id == account,
                GeminiRuntimeState.project == project,
                GeminiRuntimeState.kind == "holds",
            )
        )


async def prune(
    sessions: async_sessionmaker[AsyncSession],
    owner: Owner,
    *,
    expect: Mapping[RowId, int],
    writes: Mapping[RowId, str],
    deletes: Sequence[RowId],
) -> bool:
    """Rewrite and delete rows while each of `expect` still has its version.

    Locks with NOWAIT in the records lock order. False, writing nothing: a row
    is busy, gone or changed since it was read.
    """
    order = case((GeminiRecord.kind == "account", 0), (GeminiRecord.kind == "session", 2), else_=1)
    try:
        async with sessions() as db, db.begin():
            locked = await db.execute(
                select(GeminiRecord.kind, GeminiRecord.id, GeminiRecord.version)
                .where(*_of(owner), tuple_(GeminiRecord.kind, GeminiRecord.id).in_(list(expect)))
                .order_by(order, GeminiRecord.kind, GeminiRecord.id)
                .with_for_update(nowait=True)
            )
            if {(r.kind, r.id): r.version for r in locked} != dict(expect):
                return False
            for (kind, id_), body in writes.items():
                await db.execute(
                    update(GeminiRecord)
                    .where(*_of(owner), GeminiRecord.kind == kind, GeminiRecord.id == id_)
                    .values(body=body, version=GeminiRecord.version + 1, touched_at=func.now())
                )
            if deletes:
                await db.execute(
                    delete(GeminiRecord).where(
                        *_of(owner), tuple_(GeminiRecord.kind, GeminiRecord.id).in_(list(deletes))
                    )
                )
            return True
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) == "55P03":
            return False
        raise
