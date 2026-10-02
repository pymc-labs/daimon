"""Per-channel spend budgets: one row per (tenant, platform, channel), no row = no limit.

Spend itself lives in the ledger (`tenant_ledger.get_channel_spend`); this
module only stores the limit and its window. Per `guideline:architecture`,
exceptions propagate.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from daimon.core._models import ChannelBudget
from daimon.core.stores.domain import BudgetWindow, ChannelBudgetRow
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


async def get_channel_budget(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, channel_id: str
) -> ChannelBudgetRow | None:
    orm = await session.scalar(
        select(ChannelBudget).where(
            ChannelBudget.tenant_id == tenant_id,
            ChannelBudget.platform == platform,
            ChannelBudget.channel_id == channel_id,
        )
    )
    return ChannelBudgetRow.model_validate(orm) if orm is not None else None


async def list_channel_budgets(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> list[ChannelBudgetRow]:
    rows = await session.scalars(
        select(ChannelBudget)
        .where(ChannelBudget.tenant_id == tenant_id)
        .order_by(ChannelBudget.platform, ChannelBudget.channel_id)
    )
    return [ChannelBudgetRow.model_validate(row) for row in rows]


async def set_channel_budget(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    limit_usd: Decimal,
    window: BudgetWindow,
    starts_at: datetime | None,
    ends_at: datetime | None,
    set_by_account_id: uuid.UUID | None,
) -> ChannelBudgetRow:
    """Insert or replace the channel's budget; the table's CHECKs refuse bad bounds."""
    values = {
        "limit_usd": limit_usd,
        "window": window,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "set_by_account_id": set_by_account_id,
    }
    stmt = (
        pg_insert(ChannelBudget)
        .values(tenant_id=tenant_id, platform=platform, channel_id=channel_id, **values)
        .on_conflict_do_update(
            constraint="uq_channel_budgets_tenant_channel",
            set_={**values, "updated_at": func.now()},
        )
        .returning(ChannelBudget)
        # A budget read earlier in this session must not shadow the new values.
        .execution_options(populate_existing=True)
    )
    orm = (await session.execute(stmt)).scalar_one()
    await session.flush()
    return ChannelBudgetRow.model_validate(orm)


async def delete_channel_budget(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, channel_id: str
) -> bool:
    """True iff a budget was removed."""
    result = await session.execute(
        delete(ChannelBudget).where(
            ChannelBudget.tenant_id == tenant_id,
            ChannelBudget.platform == platform,
            ChannelBudget.channel_id == channel_id,
        )
    )
    await session.flush()
    return cast(CursorResult[Any], result).rowcount > 0


async def raise_channel_budget(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    amount_usd: Decimal,
) -> ChannelBudgetRow | None:
    """Add ``amount_usd`` to the channel's limit in one statement; None when it has no budget."""
    stmt = (
        update(ChannelBudget)
        .where(
            ChannelBudget.tenant_id == tenant_id,
            ChannelBudget.platform == platform,
            ChannelBudget.channel_id == channel_id,
        )
        .values(limit_usd=ChannelBudget.limit_usd + amount_usd, updated_at=func.now())
        .returning(ChannelBudget)
        .execution_options(populate_existing=True)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    await session.flush()
    return None if orm is None else ChannelBudgetRow.model_validate(orm)
