"""Support catalogs, plans and per-person allowances: tenant config, read at request time.

No row is the default: no plan is `settings.support.credits_per_user` each, no
catalog is `support_plans.DEFAULT_CATALOG`, no allowance is the plan's
`per_person`. Callers own the transaction and the tenant lock; exceptions propagate.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, cast

from daimon.core._models import SupportAllowance, SupportCatalog, SupportPlan
from daimon.core.stores.support_rows import SupportAllowanceRow, SupportCatalogRow, SupportPlanRow
from daimon.core.support_plans import PlanScope, SupportCatalogSpec, SupportPlace, plan_scope_id
from daimon.core.windows import WindowBounds
from sqlalchemy import (
    ColumnElement,
    CursorResult,
    and_,
    delete,
    false,
    func,
    select,
    text,
    tuple_,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

# ---------- Catalogs ----------


async def get_catalog(
    session: AsyncSession, *, tenant_id: uuid.UUID, key: str
) -> SupportCatalogRow | None:
    stmt = select(SupportCatalog).where(
        SupportCatalog.tenant_id == tenant_id, SupportCatalog.key == key
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else SupportCatalogRow.model_validate(orm)


async def get_catalog_by_id(
    session: AsyncSession, *, tenant_id: uuid.UUID, catalog_id: uuid.UUID
) -> SupportCatalogRow | None:
    stmt = select(SupportCatalog).where(
        SupportCatalog.tenant_id == tenant_id, SupportCatalog.id == catalog_id
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else SupportCatalogRow.model_validate(orm)


async def list_catalogs(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[SupportCatalogRow]:
    stmt = (
        select(SupportCatalog)
        .where(SupportCatalog.tenant_id == tenant_id)
        .order_by(SupportCatalog.key)
    )
    return [SupportCatalogRow.model_validate(o) for o in (await session.scalars(stmt)).all()]


async def set_catalog(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    key: str,
    spec: SupportCatalogSpec,
    set_by_account_id: uuid.UUID | None,
) -> SupportCatalogRow:
    """Insert or replace the menu under a new `revision`, so open menus go stale.

    Revisions count per tenant, not per menu: every set takes a number no menu
    of the tenant had, so a plan moved to another menu turns open menus stale
    too. Writers hold the tenant lock, which keeps the numbers distinct.
    """
    options = [o.model_dump(mode="json", exclude_none=True) for o in spec.options]
    others = aliased(SupportCatalog)
    revision = (
        select(func.coalesce(func.max(others.revision), 0) + 1)
        .where(others.tenant_id == tenant_id)
        .scalar_subquery()
    )
    values = {
        "name": spec.name,
        "options": options,
        "revision": revision,
        "set_by_account_id": set_by_account_id,
    }
    stmt = (
        pg_insert(SupportCatalog)
        .values(tenant_id=tenant_id, key=key, **values)
        .on_conflict_do_update(
            constraint="uq_support_catalogs_tenant_key",
            set_={**values, "updated_at": func.now()},
        )
        .returning(SupportCatalog)
        .execution_options(populate_existing=True)
    )
    orm = (await session.execute(stmt)).scalar_one()
    await session.flush()
    return SupportCatalogRow.model_validate(orm)


async def delete_catalog(
    session: AsyncSession, *, tenant_id: uuid.UUID, key: str
) -> SupportCatalogRow | None:
    """Remove the menu; the plans' RESTRICT key refuses one in use (check `plans_using`)."""
    stmt = (
        delete(SupportCatalog)
        .where(SupportCatalog.tenant_id == tenant_id, SupportCatalog.key == key)
        .returning(SupportCatalog)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    await session.flush()
    return None if orm is None else SupportCatalogRow.model_validate(orm)


async def plans_using(
    session: AsyncSession, *, tenant_id: uuid.UUID, catalog_id: uuid.UUID
) -> list[SupportPlanRow]:
    stmt = (
        select(SupportPlan)
        .where(SupportPlan.tenant_id == tenant_id, SupportPlan.catalog_id == catalog_id)
        .order_by(SupportPlan.scope, SupportPlan.scope_id)
    )
    return [SupportPlanRow.model_validate(o) for o in (await session.scalars(stmt)).all()]


# ---------- Plans ----------


async def get_plan(
    session: AsyncSession, *, tenant_id: uuid.UUID, scope: PlanScope, scope_id: str
) -> SupportPlanRow | None:
    stmt = select(SupportPlan).where(
        SupportPlan.tenant_id == tenant_id,
        SupportPlan.scope == scope,
        SupportPlan.scope_id == scope_id,
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else SupportPlanRow.model_validate(orm)


async def get_plan_by_id(
    session: AsyncSession, *, tenant_id: uuid.UUID, plan_id: uuid.UUID
) -> SupportPlanRow | None:
    stmt = select(SupportPlan).where(SupportPlan.tenant_id == tenant_id, SupportPlan.id == plan_id)
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else SupportPlanRow.model_validate(orm)


async def list_plans(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[SupportPlanRow]:
    stmt = (
        select(SupportPlan)
        .where(SupportPlan.tenant_id == tenant_id)
        .order_by(SupportPlan.scope, SupportPlan.scope_id)
    )
    return [SupportPlanRow.model_validate(o) for o in (await session.scalars(stmt)).all()]


async def plans_for_place(
    session: AsyncSession, *, tenant_id: uuid.UUID, place: SupportPlace
) -> list[SupportPlanRow]:
    """Every plan that could govern `place`; `governing_plan` picks the most specific."""
    keys = [
        (scope, scope_id)
        for scope in ("channel", "agent", "workspace")
        if (scope_id := plan_scope_id(scope, place)) is not None
    ]
    stmt = select(SupportPlan).where(
        SupportPlan.tenant_id == tenant_id,
        tuple_(SupportPlan.scope, SupportPlan.scope_id).in_(keys),
    )
    return [SupportPlanRow.model_validate(o) for o in (await session.scalars(stmt)).all()]


async def set_plan(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    scope: PlanScope,
    scope_id: str,
    catalog_id: uuid.UUID | None,
    pool: Decimal | None,
    per_person: Decimal | None,
    window: WindowBounds,
    set_by_account_id: uuid.UUID | None,
) -> SupportPlanRow:
    """Insert or replace the plan at (scope, scope_id); the table CHECKs refuse bad limits."""
    values = {
        "platform": platform,
        "catalog_id": catalog_id,
        "pool": pool,
        "per_person": per_person,
        "window": window.window,
        "starts_at": window.starts_at,
        "ends_at": window.ends_at,
        "set_by_account_id": set_by_account_id,
    }
    stmt = (
        pg_insert(SupportPlan)
        .values(tenant_id=tenant_id, scope=scope, scope_id=scope_id, **values)
        .on_conflict_do_update(
            constraint="uq_support_plans_scope", set_={**values, "updated_at": func.now()}
        )
        .returning(SupportPlan)
        .execution_options(populate_existing=True)
    )
    orm = (await session.execute(stmt)).scalar_one()
    await session.flush()
    return SupportPlanRow.model_validate(orm)


async def delete_plan(
    session: AsyncSession, *, tenant_id: uuid.UUID, scope: PlanScope, scope_id: str
) -> SupportPlanRow | None:
    """Remove the plan and its allowances; its requests keep their charges and count nowhere."""
    stmt = (
        delete(SupportPlan)
        .where(
            SupportPlan.tenant_id == tenant_id,
            SupportPlan.scope == scope,
            SupportPlan.scope_id == scope_id,
        )
        .returning(SupportPlan)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    await session.flush()
    return None if orm is None else SupportPlanRow.model_validate(orm)


# ---------- Allowances ----------


def _allowance_where(
    tenant_id: uuid.UUID, plan_id: uuid.UUID | None, platform_user_id: str | None
) -> ColumnElement[bool]:
    plan = (
        SupportAllowance.plan_id.is_(None)
        if plan_id is None
        else SupportAllowance.plan_id == plan_id
    )
    clauses = [SupportAllowance.tenant_id == tenant_id, plan]
    if platform_user_id is not None:
        clauses.append(SupportAllowance.platform_user_id == platform_user_id)
    return and_(*clauses)


async def get_allowance(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    plan_id: uuid.UUID | None,
    platform_user_id: str,
) -> SupportAllowanceRow | None:
    """`plan_id` None is the person's allowance under the default plan."""
    stmt = select(SupportAllowance).where(_allowance_where(tenant_id, plan_id, platform_user_id))
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else SupportAllowanceRow.model_validate(orm)


async def list_allowances(
    session: AsyncSession, *, tenant_id: uuid.UUID, plan_id: uuid.UUID | None
) -> list[SupportAllowanceRow]:
    stmt = (
        select(SupportAllowance)
        .where(_allowance_where(tenant_id, plan_id, None))
        .order_by(SupportAllowance.platform_user_id)
    )
    return [SupportAllowanceRow.model_validate(o) for o in (await session.scalars(stmt)).all()]


async def set_allowance(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    plan_id: uuid.UUID | None,
    platform_user_id: str,
    credits: Decimal,
    set_by_account_id: uuid.UUID | None,
) -> SupportAllowanceRow:
    values = {"credits": credits, "set_by_account_id": set_by_account_id}
    if plan_id is None:
        target = ["tenant_id", "platform_user_id"]
        where = text("plan_id IS NULL")
    else:
        target = ["tenant_id", "plan_id", "platform_user_id"]
        where = text("plan_id IS NOT NULL")
    stmt = (
        pg_insert(SupportAllowance)
        .values(tenant_id=tenant_id, plan_id=plan_id, platform_user_id=platform_user_id, **values)
        .on_conflict_do_update(
            index_elements=target,
            index_where=where,
            set_={**values, "updated_at": func.now()},
        )
        .returning(SupportAllowance)
        .execution_options(populate_existing=True)
    )
    orm = (await session.execute(stmt)).scalar_one()
    await session.flush()
    return SupportAllowanceRow.model_validate(orm)


async def delete_allowance(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    plan_id: uuid.UUID | None,
    platform_user_id: str,
) -> SupportAllowanceRow | None:
    stmt = (
        delete(SupportAllowance)
        .where(_allowance_where(tenant_id, plan_id, platform_user_id))
        .returning(SupportAllowance)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    await session.flush()
    return None if orm is None else SupportAllowanceRow.model_validate(orm)


def _people(platform_user_keys: Sequence[tuple[uuid.UUID, str]]) -> ColumnElement[bool]:
    if not platform_user_keys:
        return false()
    return tuple_(SupportAllowance.tenant_id, SupportAllowance.platform_user_id).in_(
        list(platform_user_keys)
    )


async def delete_allowances_for_people(
    session: AsyncSession, *, platform_user_keys: Sequence[tuple[uuid.UUID, str]]
) -> int:
    """Purge: every allowance of these (tenant, platform user) pairs. Idempotent."""
    result = await session.execute(delete(SupportAllowance).where(_people(platform_user_keys)))
    await session.flush()
    return cast(CursorResult[Any], result).rowcount


async def count_allowances_for_people(
    session: AsyncSession, *, platform_user_keys: Sequence[tuple[uuid.UUID, str]]
) -> int:
    """What `delete_allowances_for_people` would delete. Read-only."""
    stmt = select(func.count()).select_from(SupportAllowance).where(_people(platform_user_keys))
    return int((await session.execute(stmt)).scalar_one())


__all__ = [
    "count_allowances_for_people",
    "delete_allowance",
    "delete_allowances_for_people",
    "delete_catalog",
    "delete_plan",
    "get_allowance",
    "get_catalog",
    "get_catalog_by_id",
    "get_plan",
    "get_plan_by_id",
    "list_allowances",
    "list_catalogs",
    "list_plans",
    "plans_for_place",
    "plans_using",
    "set_allowance",
    "set_catalog",
    "set_plan",
]
