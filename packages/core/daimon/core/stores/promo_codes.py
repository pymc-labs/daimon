"""Promo codes, their per-tenant redemptions, and refused-attempt counts.

Codes are deployment-level; redemptions and failures are tenant-scoped. The
caller owns the transaction. Per `guideline:architecture` nothing is
swallowed here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from daimon.core._models import PromoCode, PromoRedeemFailure, PromoRedemption, Tenant
from daimon.core.promo_codes import PromoCodeTerms
from daimon.core.stores.domain import PromoCodeRow, PromoRedemptionRow, TimedPromoGrantRow
from sqlalchemy import ColumnElement, Select, delete, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


async def insert_promo_code(
    session: AsyncSession, *, code_hash: str, terms: PromoCodeTerms
) -> PromoCodeRow | None:
    """Store a new code. None when a code with the same hash already exists."""
    stmt = (
        pg_insert(PromoCode)
        .values(
            code_hash=code_hash,
            note=terms.note,
            amount_usd=terms.amount_usd,
            kind=terms.kind,
            credit_starts_at=terms.credit_starts_at,
            credit_ends_at=terms.credit_ends_at,
            redeem_starts_at=terms.redeem_starts_at,
            redeem_ends_at=terms.redeem_ends_at,
            max_redemptions=terms.max_redemptions,
        )
        .on_conflict_do_nothing(index_elements=["code_hash"])
        .returning(PromoCode)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else PromoCodeRow.model_validate(orm)


async def list_promo_codes(session: AsyncSession) -> list[PromoCodeRow]:
    stmt = select(PromoCode).order_by(PromoCode.created_at.desc(), PromoCode.id)
    return [PromoCodeRow.model_validate(r) for r in (await session.execute(stmt)).scalars()]


async def get_promo_code(session: AsyncSession, promo_code_id: uuid.UUID) -> PromoCodeRow | None:
    orm = await session.get(PromoCode, promo_code_id)
    return None if orm is None else PromoCodeRow.model_validate(orm)


async def lock_promo_code_by_hash(session: AsyncSession, code_hash: str) -> PromoCodeRow | None:
    """Row-lock the code so its redemption count and limit are checked serially."""
    stmt = select(PromoCode).where(PromoCode.code_hash == code_hash).with_for_update()
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else PromoCodeRow.model_validate(orm)


async def revoke_promo_code(
    session: AsyncSession, *, promo_code_id: uuid.UUID, now: datetime
) -> PromoCodeRow | None:
    """Stop new redemptions. Idempotent: the first revocation time is kept."""
    stmt = (
        update(PromoCode)
        .where(PromoCode.id == promo_code_id)
        .values(revoked_at=func.coalesce(PromoCode.revoked_at, now))
        .returning(PromoCode)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else PromoCodeRow.model_validate(orm)


async def has_redemption(
    session: AsyncSession, *, promo_code_id: uuid.UUID, tenant_id: uuid.UUID
) -> bool:
    stmt = select(
        exists().where(
            PromoRedemption.promo_code_id == promo_code_id,
            PromoRedemption.tenant_id == tenant_id,
        )
    )
    return (await session.execute(stmt)).scalar_one()


async def insert_redemption(
    session: AsyncSession,
    *,
    promo_code_id: uuid.UUID,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
    now: datetime,
    granted: bool,
) -> bool:
    """Record the tenant's redemption and count it. False when it already redeemed."""
    stmt = (
        pg_insert(PromoRedemption)
        .values(
            promo_code_id=promo_code_id,
            tenant_id=tenant_id,
            redeemed_by_account_id=account_id,
            redeemed_at=now,
            granted_at=now if granted else None,
        )
        .on_conflict_do_nothing(index_elements=["promo_code_id", "tenant_id"])
    )
    result = cast(CursorResult[Any], await session.execute(stmt))
    if result.rowcount == 0:
        return False
    await session.execute(
        update(PromoCode)
        .where(PromoCode.id == promo_code_id)
        .values(redeemed_count=PromoCode.redeemed_count + 1)
    )
    return True


async def list_redemptions(
    session: AsyncSession, *, promo_code_id: uuid.UUID
) -> list[PromoRedemptionRow]:
    stmt = (
        select(
            PromoRedemption.id,
            PromoRedemption.promo_code_id,
            PromoRedemption.tenant_id,
            Tenant.platform.label("tenant_platform"),
            Tenant.external_id.label("tenant_external_id"),
            PromoRedemption.redeemed_by_account_id,
            PromoRedemption.redeemed_at,
            PromoRedemption.granted_at,
            PromoRedemption.expired_at,
            PromoRedemption.expired_usd,
        )
        .join(Tenant, Tenant.id == PromoRedemption.tenant_id)
        .where(PromoRedemption.promo_code_id == promo_code_id)
        .order_by(PromoRedemption.redeemed_at, PromoRedemption.id)
    )
    rows = (await session.execute(stmt)).mappings()
    return [PromoRedemptionRow.model_validate(dict(row)) for row in rows]


async def count_redeem_failures(
    session: AsyncSession, *, tenant_id: uuid.UUID, since: datetime
) -> int:
    stmt = select(func.count()).where(
        PromoRedeemFailure.tenant_id == tenant_id, PromoRedeemFailure.attempted_at >= since
    )
    return (await session.execute(stmt)).scalar_one()


async def record_redeem_failure(
    session: AsyncSession, *, tenant_id: uuid.UUID, now: datetime, prune_before: datetime
) -> None:
    """Count one refused attempt and drop this tenant's attempts outside the window."""
    await session.execute(
        delete(PromoRedeemFailure).where(
            PromoRedeemFailure.tenant_id == tenant_id,
            PromoRedeemFailure.attempted_at < prune_before,
        )
    )
    session.add(PromoRedeemFailure(tenant_id=tenant_id, attempted_at=now))
    await session.flush()


def _grant_select(*conditions: ColumnElement[bool]) -> Select[Any]:
    return (
        select(
            PromoRedemption.id.label("redemption_id"),
            PromoRedemption.promo_code_id,
            PromoRedemption.tenant_id,
            PromoCode.amount_usd,
            PromoCode.credit_starts_at,
            PromoCode.credit_ends_at,
            PromoRedemption.granted_at,
            PromoRedemption.expired_at,
        )
        .join(PromoCode, PromoCode.id == PromoRedemption.promo_code_id)
        .where(PromoCode.kind == "timed", *conditions)
    )


async def _grants(session: AsyncSession, stmt: Select[Any]) -> list[TimedPromoGrantRow]:
    rows = (await session.execute(stmt)).mappings()
    return [TimedPromoGrantRow.model_validate(dict(row)) for row in rows]


async def lock_due_grants(
    session: AsyncSession, *, now: datetime, limit: int
) -> list[TimedPromoGrantRow]:
    """Timed redemptions whose credit window has opened but whose credit is not yet granted."""
    stmt = (
        _grant_select(
            PromoRedemption.granted_at.is_(None),
            PromoRedemption.expired_at.is_(None),
            PromoCode.credit_starts_at <= now,
        )
        .order_by(PromoCode.credit_starts_at, PromoRedemption.id)
        .limit(limit)
        .with_for_update(of=PromoRedemption, skip_locked=True)
    )
    return await _grants(session, stmt)


async def lock_due_expiries(
    session: AsyncSession, *, now: datetime, limit: int
) -> list[TimedPromoGrantRow]:
    """Granted timed redemptions whose credit window has closed and not yet been settled."""
    stmt = (
        _grant_select(
            PromoRedemption.granted_at.is_not(None),
            PromoRedemption.expired_at.is_(None),
            PromoCode.credit_ends_at <= now,
        )
        .order_by(PromoCode.credit_ends_at, PromoRedemption.id)
        .limit(limit)
        .with_for_update(of=PromoRedemption, skip_locked=True)
    )
    return await _grants(session, stmt)


async def list_timed_grants(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> list[TimedPromoGrantRow]:
    """Every granted timed redemption of the tenant, settled or not."""
    stmt = _grant_select(
        PromoRedemption.tenant_id == tenant_id, PromoRedemption.granted_at.is_not(None)
    ).order_by(PromoRedemption.granted_at)
    return await _grants(session, stmt)


async def mark_granted(
    session: AsyncSession, *, redemption_id: uuid.UUID, granted_at: datetime
) -> None:
    await session.execute(
        update(PromoRedemption)
        .where(PromoRedemption.id == redemption_id, PromoRedemption.granted_at.is_(None))
        .values(granted_at=granted_at)
    )


async def mark_expired(
    session: AsyncSession, *, redemption_id: uuid.UUID, expired_at: datetime, expired_usd: Decimal
) -> None:
    await session.execute(
        update(PromoRedemption)
        .where(PromoRedemption.id == redemption_id, PromoRedemption.expired_at.is_(None))
        .values(expired_at=expired_at, expired_usd=expired_usd)
    )
