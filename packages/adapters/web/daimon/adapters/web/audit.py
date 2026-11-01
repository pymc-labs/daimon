"""The admin audit trail: one row for every change an admin makes in the web app."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from daimon.core.stores import web_audit
from daimon.core.stores.web_audit import INVITE_LABEL, PERSON_LABEL
from sqlalchemy.ext.asyncio import AsyncSession


def usd(amount: Decimal | float) -> str:
    return f"${Decimal(str(amount)):,.2f}"


@dataclass(frozen=True, slots=True)
class AuditTarget:
    """What a change was about. A person or invite is an id, so erasing them erases the
    name or address too; anything else is a label that names no one."""

    label: str
    person_id: uuid.UUID | None = None
    invite_id: uuid.UUID | None = None


def person_target(person_id: uuid.UUID) -> AuditTarget:
    return AuditTarget(PERSON_LABEL, person_id=person_id)


def invite_target(invite_id: uuid.UUID) -> AuditTarget:
    return AuditTarget(INVITE_LABEL, invite_id=invite_id)


async def audit(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_account_id: uuid.UUID | None,
    action: str,
    target: AuditTarget | str,
    at: datetime,
    detail: str | None = None,
) -> None:
    """Record a change in the same transaction as the change itself."""
    what = AuditTarget(target) if isinstance(target, str) else target
    await web_audit.record(
        db,
        tenant_id=tenant_id,
        actor_account_id=actor_account_id,
        action=action,
        target=what.label,
        target_person_id=what.person_id,
        target_invite_id=what.invite_id,
        detail=detail,
        at=at,
    )
