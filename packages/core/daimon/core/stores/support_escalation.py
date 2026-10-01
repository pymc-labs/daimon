"""Async store for support_escalations — human-support requests and their credit gate.

No try/except anywhere in this module — exceptions propagate to the adapter
boundary. Callers own the transaction; every write ends with `await
session.flush()`.

The credit gate lives in `record_escalation` rather than in the caller, and it
counts inside the caller's transaction, because a check performed before the
transaction is a TOCTOU: two fast clicks both read `used=2` against an
allowance of 3, both decide they are permitted, and both insert. Counting on
the same connection that inserts makes the check and the write atomic under
`REPEATABLE READ` or better, and under `READ COMMITTED` narrows the window to
the statement pair rather than to a round trip through Discord.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, cast

from daimon.core._models import SupportEscalation
from daimon.core.stores.domain import SupportEscalationRow
from daimon.core.support_escalation import has_credit
from sqlalchemy import (
    ColumnElement,
    CursorResult,
    delete,
    func,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession


async def count_escalations_for_user(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
) -> int:
    """How many escalations this person has already spent in this tenant.

    Credits are per user WITHIN a tenant, so both columns are always in the
    predicate — the same person in two installs has two separate allowances.
    """
    stmt = (
        select(func.count())
        .select_from(SupportEscalation)
        .where(
            SupportEscalation.tenant_id == tenant_id,
            SupportEscalation.platform_user_id == platform_user_id,
        )
    )
    return int((await session.execute(stmt)).scalar_one())


async def record_escalation(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
    platform: str,
    platform_user_id: str,
    channel_id: str,
    message_id: str,
    ma_session_id: str | None,
    note: str,
    allowance: int,
) -> SupportEscalationRow | None:
    """Insert one escalation if the person has a credit left, else `None`.

    `None` means "out of credits", which the caller renders as the contact-us
    message. It is not an error and must not be raised: running out is the
    designed end of a trial allowance, not a fault.

    `delivered_at` is left NULL. The caller attempts operator delivery AFTER
    this returns and calls `mark_delivered` only once a DM actually lands, so
    a request whose delivery fails entirely survives as an undelivered row.
    Committing the row first is deliberate and is the reason this function does
    not take a delivery callback.
    """
    used = await count_escalations_for_user(
        session, tenant_id=tenant_id, platform_user_id=platform_user_id
    )
    if not has_credit(allowance=allowance, used=used):
        return None

    orm = SupportEscalation(
        tenant_id=tenant_id,
        account_id=account_id,
        platform=platform,
        platform_user_id=platform_user_id,
        channel_id=channel_id,
        message_id=message_id,
        ma_session_id=ma_session_id,
        note=note,
    )
    session.add(orm)
    await session.flush()
    await session.refresh(orm)
    return SupportEscalationRow.model_validate(orm)


async def mark_delivered(
    session: AsyncSession,
    *,
    escalation_id: uuid.UUID,
) -> SupportEscalationRow | None:
    """Stamp `delivered_at` once an operator DM has actually landed.

    Idempotent in effect — re-stamping refreshes the timestamp rather than
    failing — because the alternative (predicating on `delivered_at IS NULL`)
    would make a retry look like a missing row to the caller.
    """
    stmt = (
        update(SupportEscalation)
        .where(SupportEscalation.id == escalation_id)
        .values(delivered_at=func.now())
        .returning(SupportEscalation)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    await session.flush()
    if orm is None:
        return None
    return SupportEscalationRow.model_validate(orm)


async def delete_support_escalations_for_platform_user(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
) -> int:
    """Delete one platform user's escalations in one tenant. Idempotent.

    Tenant-scoped for the same reason the message_feedback counterpart is:
    `platform_user_id` is not globally unique across platforms, and purging
    ONE principal must not erase the same person's rows in another install.
    """
    result = await session.execute(
        delete(SupportEscalation).where(
            SupportEscalation.tenant_id == tenant_id,
            SupportEscalation.platform_user_id == platform_user_id,
        )
    )
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


def _account_or_platform_user_predicate(
    *, account_id: uuid.UUID, platform_user_keys: Sequence[tuple[uuid.UUID, str]]
) -> ColumnElement[bool]:
    """Shared predicate for the account-scoped delete/count pair.

    They MUST stay identical, or the /privacy cascade preview lies about what
    erasure will remove. Same shape and same reason as the message_feedback
    pair: a request written before the person had an `accounts` row carries
    `account_id = NULL` and is unreachable by an account-keyed delete alone,
    so the `(tenant_id, platform_user_id)` membership check closes that gap.
    """
    predicates: list[ColumnElement[bool]] = [SupportEscalation.account_id == account_id]
    if platform_user_keys:
        predicates.append(
            tuple_(SupportEscalation.tenant_id, SupportEscalation.platform_user_id).in_(
                list(platform_user_keys)
            )
        )
    return or_(*predicates)


async def delete_support_escalations_for_account(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    platform_user_keys: Sequence[tuple[uuid.UUID, str]],
) -> int:
    """Delete every escalation attributable to `account_id`. Idempotent."""
    predicate = _account_or_platform_user_predicate(
        account_id=account_id, platform_user_keys=platform_user_keys
    )
    result = await session.execute(delete(SupportEscalation).where(predicate))
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


async def count_support_escalations_for_account(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    platform_user_keys: Sequence[tuple[uuid.UUID, str]],
) -> int:
    """Count what `delete_support_escalations_for_account` would delete. Read-only."""
    predicate = _account_or_platform_user_predicate(
        account_id=account_id, platform_user_keys=platform_user_keys
    )
    stmt = select(func.count()).select_from(SupportEscalation).where(predicate)
    return int((await session.execute(stmt)).scalar_one())
