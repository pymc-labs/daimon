"""Async store for `feature_values`: the features set at each tier of each place.

Unused place columns hold ''; rows come back with them as stored. Every write bumps
the tenant's `feature_revision`, which keys the resolver's cache. Callers own the
transaction; every write ends with `await session.flush()`.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from typing import Any, cast

from daimon.core._models import FeatureValue, Tenant
from daimon.core.stores.domain import FeatureSurface, FeatureTarget, FeatureValueRow
from sqlalchemy import CursorResult, and_, case, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement


def _ids(target: FeatureTarget) -> tuple[str, str, str]:
    channel = target.channel_id or ""
    thread = target.thread_key or ""
    if target.tier == "agent" and not target.agent_id:
        raise ValueError("the agent tier needs an agent_id")
    if target.tier == "channel" and not channel:
        raise ValueError("the channel tier needs a channel_id")
    if target.tier == "thread" and not thread:
        raise ValueError("the thread tier needs a thread_key")
    return target.agent_id or "", channel if target.tier != "workspace" else "", thread


def _match(target: FeatureTarget) -> ColumnElement[bool]:
    agent, channel, thread = _ids(target)
    if target.tier == "thread":
        return and_(
            FeatureValue.tier == "thread",
            FeatureValue.agent_id == agent,
            FeatureValue.thread_key == thread,
        )
    return and_(
        FeatureValue.tier == target.tier,
        FeatureValue.agent_id == agent,
        FeatureValue.channel_id == channel,
    )


async def _bump(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    await session.execute(
        update(Tenant)
        .where(Tenant.id == tenant_id)
        .values(feature_revision=Tenant.feature_revision + 1)
    )


async def feature_revision(session: AsyncSession, *, tenant_id: uuid.UUID) -> int:
    """The tenant's revision, 0 for an unknown tenant."""
    found = await session.scalar(select(Tenant.feature_revision).where(Tenant.id == tenant_id))
    return found or 0


async def place_values(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: str | None,
    channel_id: str | None,
    thread_key: str | None,
    keys: Collection[str] | None = None,
) -> list[FeatureValueRow]:
    """One read for a place: its workspace, agent, channel and thread rows."""
    targets = [FeatureTarget("workspace")]
    if agent_id:
        targets.append(FeatureTarget("agent", agent_id=agent_id))
    if channel_id:
        targets.append(FeatureTarget("channel", channel_id=channel_id))
    if thread_key:
        targets.append(FeatureTarget("thread", thread_key=thread_key))
    stmt = select(FeatureValue).where(
        FeatureValue.tenant_id == tenant_id, or_(*(_match(t) for t in targets))
    )
    if keys is not None:
        stmt = stmt.where(FeatureValue.key.in_(keys))
    rows = (await session.scalars(stmt)).all()
    return [FeatureValueRow.model_validate(row) for row in rows]


async def get_feature_value(
    session: AsyncSession, *, tenant_id: uuid.UUID, target: FeatureTarget, key: str
) -> FeatureValueRow | None:
    row = await session.scalar(
        select(FeatureValue).where(
            FeatureValue.tenant_id == tenant_id, FeatureValue.key == key, _match(target)
        )
    )
    return None if row is None else FeatureValueRow.model_validate(row)


async def set_feature_value(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    target: FeatureTarget,
    key: str,
    value: str,
    locked: bool = False,
    set_by: uuid.UUID | None,
    surface: FeatureSurface,
) -> FeatureValueRow:
    """Upsert one tier's value; a thread row keeps its known channel when given none."""
    agent, channel, thread = _ids(target)
    stmt = pg_insert(FeatureValue).values(
        tenant_id=tenant_id,
        tier=target.tier,
        agent_id=agent,
        channel_id=channel,
        thread_key=thread,
        key=key,
        value=value,
        locked=locked,
        set_by=set_by,
        surface=surface,
        set_at=func.now(),
    )
    excluded = stmt.excluded
    changes: dict[str, Any] = {
        "value": excluded.value,
        "locked": excluded.locked,
        "set_by": excluded.set_by,
        "surface": excluded.surface,
        "set_at": func.now(),
        "revision": FeatureValue.revision + 1,
    }
    if target.tier == "thread":
        changes["channel_id"] = case(
            (excluded.channel_id != "", excluded.channel_id), else_=FeatureValue.channel_id
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "agent_id", "thread_key", "key"],
            index_where=text("tier = 'thread'"),
            set_=changes,
        )
    else:
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "tier", "agent_id", "channel_id", "key"],
            index_where=text("tier <> 'thread'"),
            set_=changes,
        )
    row = (await session.scalars(stmt.returning(FeatureValue))).one()
    await _bump(session, tenant_id)
    await session.flush()
    return FeatureValueRow.model_validate(row)


async def clear_feature_value(
    session: AsyncSession, *, tenant_id: uuid.UUID, target: FeatureTarget, key: str
) -> bool:
    """Drop the tier's value so it inherits again. Returns whether a row existed."""
    result = await session.execute(
        delete(FeatureValue).where(
            FeatureValue.tenant_id == tenant_id, FeatureValue.key == key, _match(target)
        )
    )
    existed = cast(CursorResult[Any], result).rowcount == 1
    if existed:
        await _bump(session, tenant_id)
    await session.flush()
    return existed


async def list_feature_values(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    keys: Sequence[str],
    tier: str | None = None,
    channel_id: str | None = None,
    agent_id: str | None = None,
) -> list[FeatureValueRow]:
    """Every row for `keys`, narrowed to a tier, a channel (its threads too) or an agent."""
    stmt = select(FeatureValue).where(
        FeatureValue.tenant_id == tenant_id, FeatureValue.key.in_(keys)
    )
    if tier is not None:
        stmt = stmt.where(FeatureValue.tier == tier)
    if channel_id is not None:
        stmt = stmt.where(FeatureValue.channel_id == channel_id)
    if agent_id is not None:
        stmt = stmt.where(FeatureValue.agent_id == agent_id)
    rows = (await session.scalars(stmt.order_by(FeatureValue.id))).all()
    return [FeatureValueRow.model_validate(row) for row in rows]


async def replace_agent_values(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: str,
    values: dict[str, str],
) -> None:
    """Make an agent's spec-set rows match its spec's `features:`; a person's rows stay."""
    existing = await list_feature_values(
        session, tenant_id=tenant_id, keys=list(values) or [""], tier="agent", agent_id=agent_id
    )
    by_key = {row.key: row for row in existing}
    stale = await session.execute(
        delete(FeatureValue).where(
            FeatureValue.tenant_id == tenant_id,
            FeatureValue.tier == "agent",
            FeatureValue.agent_id == agent_id,
            FeatureValue.surface == "spec",
            FeatureValue.key.not_in(list(values) or [""]),
        )
    )
    changed = cast(CursorResult[Any], stale).rowcount > 0
    target = FeatureTarget("agent", agent_id=agent_id)
    for key, value in values.items():
        row = by_key.get(key)
        if row is not None and (row.surface != "spec" or row.value == value):
            continue
        await set_feature_value(
            session, tenant_id=tenant_id, target=target, key=key, value=value,
            set_by=None, surface="spec",
        )  # fmt: skip
    if changed:
        await _bump(session, tenant_id)
    await session.flush()
