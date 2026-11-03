"""A workspace's monthly limit per member, kept as caps in the allowance table.

The limit a CLI or a panel shows as "the cap" is a cap row (`is_cap`): one for the
built-in `everyone` group (the default) and one per person (an override). Setting one adds
or changes that row, so the web Credits page and the gate see the same rows. Reading a
person's limit follows the gate's own rules for which cap and allowance govern them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from daimon.core.allowances import EVERYONE, applicable, governing_cap
from daimon.core.spend_gate import resolve_spend_person
from daimon.core.stores import spend_allowances
from daimon.core.stores.domain import AllowanceSubjectKind, SpendAllowanceRow
from sqlalchemy.ext.asyncio import AsyncSession


async def _set(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kind: AllowanceSubjectKind,
    subject_id: str,
    amount: Decimal,
    now: datetime,
    account_id: uuid.UUID | None,
) -> SpendAllowanceRow:
    found = await spend_allowances.find_month_cap(
        session, tenant_id=tenant_id, subject_kind=kind, subject_id=subject_id
    )
    if found is not None:
        changed = await spend_allowances.set_amount(
            session, tenant_id=tenant_id, allowance_id=found.id, amount_usd=amount
        )
        if changed is not None:
            return changed
    return await spend_allowances.create_allowance(
        session,
        tenant_id=tenant_id,
        subject_kind=kind,
        subject_id=subject_id,
        agent_id=None,
        amount_usd=amount,
        window="month",
        starts_at=now,
        created_by_account_id=account_id,
        is_cap=True,
    )


async def set_default_limit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    amount: Decimal,
    now: datetime,
    account_id: uuid.UUID | None = None,
) -> SpendAllowanceRow:
    """What every member may spend a month, unless they have a limit of their own."""
    return await _set(
        session,
        tenant_id=tenant_id,
        kind="group",
        subject_id=EVERYONE,
        amount=amount,
        now=now,
        account_id=account_id,
    )


async def set_person_limit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
    amount: Decimal,
    now: datetime,
    account_id: uuid.UUID | None = None,
) -> SpendAllowanceRow:
    """What one person may spend a month, in place of the default."""
    return await _set(
        session,
        tenant_id=tenant_id,
        kind="person",
        subject_id=platform_user_id,
        amount=amount,
        now=now,
        account_id=account_id,
    )


async def monthly_limit(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform_user_id: str, now: datetime
) -> Decimal | None:
    """The monthly dollars the person's governing allowances and cap let them spend, None for none.

    Only monthly allowances for every agent count; where several govern, the largest does,
    as in the gate. The cap, if lower, is the limit.
    """
    rows = await spend_allowances.list_for_tenant(session, tenant_id=tenant_id)
    if not rows:
        return None
    person = await resolve_spend_person(
        session, tenant_id=tenant_id, platform_user_id=platform_user_id
    )
    governing = applicable(
        rows,
        person_ids=person.identities | {platform_user_id},
        groups=person.groups,
        invite_id=person.invite_id,
        agent_id=None,
        now=now,
        is_visitor=person.is_visitor,
    )
    amounts = [r.amount_usd for r in governing if r.window == "month" and r.agent_id is None]
    allowed = max(amounts, default=None)
    cap = governing_cap(rows, person_ids=person.identities | {platform_user_id}, now=now)
    if cap is None:
        return allowed
    return cap.amount_usd if allowed is None else min(allowed, cap.amount_usd)
