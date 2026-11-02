"""Store for web_audit: what admins changed in the web app, newest first."""

from __future__ import annotations

import uuid
from datetime import datetime

from daimon.core._models import WebAudit
from daimon.core.cursor import Cursor
from daimon.core.stores.domain import WebAuditRow
from sqlalchemy import literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

#: What a row about a person or invite says once they are erased.
PERSON_LABEL = "A person"
INVITE_LABEL = "An invite"


async def record(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_account_id: uuid.UUID | None,
    action: str,
    target: str,
    at: datetime,
    detail: str | None = None,
    target_person_id: uuid.UUID | None = None,
    target_invite_id: uuid.UUID | None = None,
) -> None:
    """Add a row. `target` is shown when no person or invite id names the target, so
    with an id it must be a generic label, never the name or email."""
    session.add(
        WebAudit(
            tenant_id=tenant_id,
            actor_account_id=actor_account_id,
            action=action,
            target=target,
            target_person_id=target_person_id,
            target_invite_id=target_invite_id,
            detail=detail,
            at=at,
        )
    )
    await session.flush()


async def list_recent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    limit: int = 200,
    before: Cursor | None = None,
) -> list[WebAuditRow]:
    """The workspace's changes, newest first; `before` is the `(at, id)` of the last one seen."""
    query = select(WebAudit).where(WebAudit.tenant_id == tenant_id)
    if before is not None:
        query = query.where(
            tuple_(WebAudit.at, WebAudit.id) < tuple_(literal(before[0]), literal(before[1]))
        )
    rows = await session.scalars(
        query.order_by(WebAudit.at.desc(), WebAudit.id.desc()).limit(limit)
    )
    return [WebAuditRow.model_validate(r) for r in rows]
