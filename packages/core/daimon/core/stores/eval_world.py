"""Store for the eval fake world: an item's world calls, its overlay and the simulator cache."""

from __future__ import annotations

import uuid

from daimon.core._models import (
    EvalCase,
    EvalCaseRevision,
    EvalRun,
    EvalRunItem,
    EvalSimCache,
    EvalWorldCall,
    EvalWorldOverlay,
)
from pydantic import BaseModel, ConfigDict, JsonValue
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

Document = dict[str, JsonValue]
RUNNING = "running"


class _Row(BaseModel):
    model_config = ConfigDict(frozen=True)


class WorldItemRow(_Row):
    """A running item and the case revision it runs."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    case_document: Document
    case_sha256: str


class OverlayRow(_Row):
    service: str
    resource_key: str
    version: int
    data: Document | None


class CallStats(_Row):
    next_seq: int
    total: int
    written: int  # simulator model answers, the ones `simulator.max_calls` counts
    used_exchanges: frozenset[str]


async def running_item(
    session: AsyncSession, *, item_id: uuid.UUID | None = None, token_sha256: str | None = None
) -> WorldItemRow | None:
    """A running item by id or by its world token's hash, with its case; None otherwise."""
    query = (
        select(EvalRunItem.id, EvalRunItem.tenant_id, EvalCaseRevision.document,
               EvalCaseRevision.sha256)
        .join(EvalRun, EvalRun.id == EvalRunItem.run_id)
        .join(EvalCase, (EvalCase.suite_id == EvalRun.suite_id)
              & (EvalCase.key == EvalRunItem.case_key))
        .join(EvalCaseRevision, (EvalCaseRevision.case_id == EvalCase.id)
              & (EvalCaseRevision.revision == EvalRunItem.case_revision))
        .where(EvalRunItem.status == RUNNING)
    )  # fmt: skip
    if item_id is not None:
        query = query.where(EvalRunItem.id == item_id)
    elif token_sha256 is not None:
        query = query.where(EvalRunItem.world_token_sha256 == token_sha256)
    else:
        return None
    row = (await session.execute(query)).one_or_none()
    if row is None:
        return None
    return WorldItemRow(id=row[0], tenant_id=row[1], case_document=row[2], case_sha256=row[3])


async def set_world_token(session: AsyncSession, *, item_id: uuid.UUID, token_sha256: str) -> None:
    await session.execute(
        update(EvalRunItem).where(EvalRunItem.id == item_id).values(world_token_sha256=token_sha256)
    )


async def lock_running_item(session: AsyncSession, *, item_id: uuid.UUID) -> bool:
    """Take the item's row lock, which orders its world calls; False when it is not running."""
    found = await session.scalar(
        select(EvalRunItem.id)
        .where(EvalRunItem.id == item_id, EvalRunItem.status == RUNNING)
        .with_for_update()
    )
    return found is not None


async def call_stats(session: AsyncSession, *, item_id: uuid.UUID) -> CallStats:
    total, last, written = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.max(EvalWorldCall.seq), 0),
                func.count().filter(EvalWorldCall.reason == "written"),
            ).where(EvalWorldCall.item_id == item_id)
        )
    ).one()
    used = await session.scalars(
        select(EvalWorldCall.exchange_id).where(
            EvalWorldCall.item_id == item_id, EvalWorldCall.exchange_id.is_not(None)
        )
    )
    return CallStats(
        next_seq=last + 1, total=total, written=written,
        used_exchanges=frozenset(u for u in used if u is not None),
    )  # fmt: skip


async def record_call(
    session: AsyncSession, *, item_id: uuid.UUID, tenant_id: uuid.UUID, document: Document
) -> None:
    def text(key: str) -> str | None:
        value = document.get(key)
        return value if isinstance(value, str) else None

    seq = document["seq"]
    assert isinstance(seq, int)
    session.add(
        EvalWorldCall(
            item_id=item_id, seq=seq, tenant_id=tenant_id, surface=text("surface"),
            service=text("service"), origin=text("origin"), reason=text("reason"),
            exchange_id=text("exchange_id"), sim_key=text("sim_key"), document=document,
        )
    )  # fmt: skip
    await session.flush()


async def list_calls(
    session: AsyncSession, *, item_id: uuid.UUID, origin: str | None = None
) -> list[Document]:
    """An item's world call documents in seq order, optionally of one origin."""
    query = select(EvalWorldCall.document).where(EvalWorldCall.item_id == item_id)
    if origin is not None:
        query = query.where(EvalWorldCall.origin == origin)
    return list(await session.scalars(query.order_by(EvalWorldCall.seq)))


async def list_overlay(session: AsyncSession, *, item_id: uuid.UUID) -> list[OverlayRow]:
    rows = await session.scalars(
        select(EvalWorldOverlay).where(EvalWorldOverlay.item_id == item_id)
    )
    return [
        OverlayRow(service=r.service, resource_key=r.resource_key, version=r.version, data=r.data)
        for r in rows
    ]


async def upsert_overlay(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    tenant_id: uuid.UUID,
    service: str,
    resource_key: str,
    data: Document | None,
) -> None:
    """Write one resource's new state (None: deleted), bumping its version."""
    statement = insert(EvalWorldOverlay).values(
        item_id=item_id, tenant_id=tenant_id, service=service, resource_key=resource_key,
        version=1, data=data,
    )  # fmt: skip
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[
                EvalWorldOverlay.item_id,
                EvalWorldOverlay.service,
                EvalWorldOverlay.resource_key,
            ],
            set_={"version": EvalWorldOverlay.version + 1, "data": statement.excluded.data},
        )
    )


async def get_sim(
    session: AsyncSession, *, tenant_id: uuid.UUID, case_sha256: str, key: str
) -> Document | None:
    row = await session.get(EvalSimCache, (tenant_id, case_sha256, key))
    return row and row.document


async def put_sim(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    case_sha256: str,
    key: str,
    model: str,
    document: Document,
) -> None:
    """Keep a simulator answer; the first answer under a key stays."""
    await session.execute(
        insert(EvalSimCache)
        .values(tenant_id=tenant_id, case_sha256=case_sha256, key=key, model=model,
                document=document)
        .on_conflict_do_nothing()
    )  # fmt: skip


async def list_sims(
    session: AsyncSession, *, tenant_id: uuid.UUID, case_sha256: str
) -> list[Document]:
    """A case revision's cached answers, to export with the case (`world.sim_cache`)."""
    rows = await session.scalars(
        select(EvalSimCache.document)
        .where(EvalSimCache.tenant_id == tenant_id, EvalSimCache.case_sha256 == case_sha256)
        .order_by(EvalSimCache.created_at, EvalSimCache.key)
    )
    return list(rows)
