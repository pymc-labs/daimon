"""Store for running eval items: the queue the workers claim from, item results and caps."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from daimon.core._models import EvalRun, EvalRunItem
from daimon.core.stores.evals import Document, ItemRow
from sqlalchemy import ColumnElement, CursorResult, func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

#: Run statuses whose items the workers may start.
OPEN_RUNS = ("approved", "running")
MAX_ATTEMPTS = 2
"""An item is tried at most twice: once more after its worker was lost."""


def _merged(fields: Document) -> ColumnElement[Document]:
    """The item document with `fields` set, in SQL."""
    return EvalRunItem.document.op("||")(literal(fields, JSONB))


async def set_cap(session: AsyncSession, *, run_id: uuid.UUID, cap_usd: float | None) -> None:
    await session.execute(
        update(EvalRun)
        .where(EvalRun.id == run_id)
        .values(cap_usd=None if cap_usd is None else Decimal(str(cap_usd)))
    )


async def open_caps(session: AsyncSession, *, tenant_id: uuid.UUID) -> float:
    """What a workspace's open runs are capped at together. Holds the workspace's run lock
    until the transaction ends, so two starts never both pass a ceiling."""
    key = func.hashtextextended(f"eval-runs:{tenant_id}", 0)
    await session.execute(select(func.pg_advisory_xact_lock(key)))
    total = await session.scalar(
        select(func.coalesce(func.sum(EvalRun.cap_usd), 0)).where(
            EvalRun.tenant_id == tenant_id, EvalRun.status.in_(OPEN_RUNS)
        )
    )
    return float(total or 0)


async def cap_of(session: AsyncSession, *, run_id: uuid.UUID) -> float | None:
    cap = await session.scalar(select(EvalRun.cap_usd).where(EvalRun.id == run_id))
    return None if cap is None else float(cap)


async def prior_item_costs(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    arm_id: str,
    config_hash: str,
    case_keys: Iterable[str],
) -> list[float]:
    """What finished items of earlier local runs of this arm config cost on these cases."""
    cost = EvalRunItem.document["usage"]["cost_usd"].astext
    rows = await session.scalars(
        select(cost)
        .join(EvalRun, EvalRun.id == EvalRunItem.run_id)
        .where(
            EvalRunItem.tenant_id == tenant_id,
            EvalRun.origin == "local",
            EvalRunItem.arm_key == arm_id,
            EvalRunItem.status == "done",
            EvalRunItem.case_key.in_(list(case_keys)),
            EvalRun.document["arms"].contains([{"id": arm_id, "config_hash": config_hash}]),
            cost.is_not(None),
        )
    )
    return [float(c) for c in rows]


async def fail_lost_items(
    session: AsyncSession, *, stale_before: datetime
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """End in error the running items whose worker went quiet before `stale_before` with no
    try left; their run and item ids, so their holds are released and their runs ended."""
    lost = (EvalRunItem.status == "running") & (EvalRunItem.claimed_at < stale_before)
    rows = await session.execute(
        update(EvalRunItem)
        .where(lost, EvalRunItem.attempts >= MAX_ATTEMPTS)
        .values(
            status="error",
            document=_merged(
                {"status": "error", "error": {"kind": "other", "message": "worker lost"}}
            ),
        )
        .returning(EvalRunItem.run_id, EvalRunItem.id)
    )
    return [(run_id, item_id) for run_id, item_id in rows.tuples()]


async def claim_item(
    session: AsyncSession, *, now: datetime, stale_before: datetime
) -> ItemRow | None:
    """Claim one item to run, skipping rows other workers hold: a pending item of an open
    local run, or a running one whose worker went quiet before `stale_before` and has a try
    left (`fail_lost_items` ends the others first)."""
    lost = (EvalRunItem.status == "running") & (EvalRunItem.claimed_at < stale_before)
    row = await session.scalar(
        select(EvalRunItem)
        .join(EvalRun, EvalRun.id == EvalRunItem.run_id)
        .where(
            EvalRun.origin == "local",
            EvalRun.status.in_(OPEN_RUNS),
            (EvalRunItem.status == "pending") | (lost & (EvalRunItem.attempts < MAX_ATTEMPTS)),
        )
        .order_by(EvalRunItem.created_at, EvalRunItem.key)
        .limit(1)
        .with_for_update(of=EvalRunItem, skip_locked=True)
    )
    if row is None:
        return None
    row.status = "running"
    row.attempts += 1
    row.claimed_at = now
    row.document = {**row.document, "status": "running"}
    await session.flush()
    return ItemRow.model_validate(row)


async def heartbeat(
    session: AsyncSession, *, item_id: uuid.UUID, attempt: int, now: datetime
) -> bool:
    """Keep an item's claim fresh; False when another worker has taken it over."""
    done = await session.execute(
        update(EvalRunItem)
        .where(EvalRunItem.id == item_id, EvalRunItem.attempts == attempt,
               EvalRunItem.status == "running")
        .values(claimed_at=now)
    )  # fmt: skip
    return cast(CursorResult[Any], done).rowcount > 0


async def save_item(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    document: Document,
    blob_shas: Iterable[str] = (),
    attempt: int | None = None,
) -> bool:
    """An item's result; its status column follows the document. With `attempt`, only while
    that try still holds the item: False when another worker has taken it over."""
    query = update(EvalRunItem).where(EvalRunItem.id == item_id)
    if attempt is not None:
        query = query.where(EvalRunItem.attempts == attempt, EvalRunItem.status == "running")
    done = await session.execute(
        query.values(document=document, status=str(document["status"]),
                     blob_shas=sorted(set(blob_shas)))
    )  # fmt: skip
    return cast(CursorResult[Any], done).rowcount > 0


async def close_items(
    session: AsyncSession, *, run_id: uuid.UUID, status: str, error: Document
) -> None:
    """End every pending item of a run with `status` and `error`."""
    await session.execute(
        update(EvalRunItem)
        .where(EvalRunItem.run_id == run_id, EvalRunItem.status == "pending")
        .values(
            status=status,
            document=_merged({"status": status, "error": error}),
        )
    )


async def status_counts(session: AsyncSession, *, run_id: uuid.UUID) -> dict[str, int]:
    rows = await session.execute(
        select(EvalRunItem.status, func.count())
        .where(EvalRunItem.run_id == run_id)
        .group_by(EvalRunItem.status)
    )
    return {status: count for status, count in rows.tuples()}


async def item_status(session: AsyncSession, *, item_id: uuid.UUID) -> str | None:
    return await session.scalar(select(EvalRunItem.status).where(EvalRunItem.id == item_id))
