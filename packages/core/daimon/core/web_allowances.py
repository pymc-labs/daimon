"""Spend allowances and caps an admin sets: the rules the web app and the chat tools share.

Every change is audited in the transaction that makes it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal

from daimon.core import credit_views
from daimon.core.allowances import EVERYONE
from daimon.core.stores import spend_allowances, web_invites
from daimon.core.stores import web_people as people_store
from daimon.core.stores.domain import AllowanceSubjectKind, AllowanceWindow, SpendAllowanceRow
from daimon.core.stores.web_audit import INVITE_LABEL, PERSON_LABEL
from daimon.core.web_admin import (
    AuditTarget,
    WebAdmin,
    WebAdminError,
    audit,
    invite_target,
    person_target,
    usd,
)
from daimon.core.web_identity import linked_identity
from sqlalchemy.ext.asyncio import AsyncSession

REMOVED_AGENT = "Removed agent"
_GONE = "This allowance no longer exists."


def allowance_words(row: SpendAllowanceRow, agent_names: Mapping[str, str]) -> str:
    """What an allowance lets: `$5.00 a month` or `$5.00 in total, <agent>`."""
    window = "a month" if row.window == "month" else "in total"
    words = f"{usd(row.amount_usd)} {window}"
    if row.agent_id is None:
        return words
    return f"{words}, {agent_names.get(row.agent_id, REMOVED_AGENT)}"


async def audit_target_of(db: AsyncSession, row: SpendAllowanceRow) -> AuditTarget:
    """A group by name; a person or invite by id, which the audit log shows by name while
    they exist. An id that matches no one (a chat-only person) stays a generic label."""
    if row.subject_kind == "group":
        return AuditTarget("Everyone" if row.subject_id == EVERYONE else f"{row.subject_id} group")
    if row.subject_kind == "person":
        label = (
            await credit_views.label_people(db, tenant_id=row.tenant_id, user_ids=[row.subject_id])
        ).get(row.subject_id)
        known = (
            await people_store.get_person(db, tenant_id=row.tenant_id, person_id=label.person_id)
            if label is not None and label.person_id is not None
            else None
        )
        return person_target(known.id) if known else AuditTarget(PERSON_LABEL)
    try:
        invite_id = uuid.UUID(row.subject_id)
    except ValueError:
        return AuditTarget(INVITE_LABEL)
    invite = await web_invites.get_invite(db, tenant_id=row.tenant_id, invite_id=invite_id)
    return invite_target(invite.id) if invite else AuditTarget(INVITE_LABEL)


async def _spend_subject_id(
    db: AsyncSession, tenant_id: uuid.UUID, kind: AllowanceSubjectKind, subject_id: str
) -> str:
    """What the spend gate matches the subject on. A person is the id their turns run as."""
    if kind != "person":
        return subject_id
    try:
        person_id = uuid.UUID(subject_id)
    except ValueError:
        raise WebAdminError("This person isn't in the workspace.", status=404) from None
    person = await people_store.get_person(db, tenant_id=tenant_id, person_id=person_id)
    if person is None:
        raise WebAdminError("This person isn't in the workspace.", status=404)
    return (await linked_identity(db, person))[1]


async def create_allowance(
    db: AsyncSession,
    admin: WebAdmin,
    now: datetime,
    *,
    subject_kind: AllowanceSubjectKind,
    subject_id: str,
    agent_id: str | None,
    agent_names: Mapping[str, str],
    amount_usd: Decimal,
    window: AllowanceWindow,
    expires_at: datetime | None,
    cap: bool,
) -> SpendAllowanceRow:
    """Add an allowance, or a cap: monthly, for everyone or one person, for every agent,
    with no end date. `subject_id` is a person's web id, a group's name or an invite's id."""
    if expires_at is not None and expires_at.astimezone(UTC) <= now:
        raise WebAdminError("Pick a date in the future.", status=422)
    if cap:
        everyone = subject_kind == "group" and subject_id == EVERYONE
        if not (everyone or subject_kind == "person"):
            raise WebAdminError("A cap is for everyone or one person.", status=422)
        if agent_id is not None or window != "month" or expires_at is not None:
            raise WebAdminError("A cap is monthly, for every agent, with no end date.", status=422)
    spend_id = await _spend_subject_id(db, admin.tenant_id, subject_kind, subject_id)
    if cap and await spend_allowances.find_month_cap(
        db, tenant_id=admin.tenant_id, subject_kind=subject_kind, subject_id=spend_id
    ):
        raise WebAdminError("This already has a cap. Remove it to set another.", status=409)
    row = await spend_allowances.create_allowance(
        db,
        tenant_id=admin.tenant_id,
        subject_kind=subject_kind,
        subject_id=spend_id,
        agent_id=agent_id,
        amount_usd=amount_usd,
        window=window,
        starts_at=now,
        expires_at=expires_at.astimezone(UTC) if expires_at else None,
        created_by_account_id=admin.account_id,
        is_cap=cap,
    )
    await audit(
        db,
        tenant_id=admin.tenant_id,
        actor_account_id=admin.account_id,
        action="Added cap" if row.is_cap else "Added allowance",
        target=await audit_target_of(db, row),
        detail=allowance_words(row, agent_names),
        at=now,
    )
    return row


async def set_allowance_amount(
    db: AsyncSession,
    admin: WebAdmin,
    now: datetime,
    allowance_id: uuid.UUID,
    *,
    amount_usd: Decimal,
    agent_names: Mapping[str, str],
) -> SpendAllowanceRow:
    row = await spend_allowances.set_amount(
        db, tenant_id=admin.tenant_id, allowance_id=allowance_id, amount_usd=amount_usd
    )
    if row is None:
        raise WebAdminError(_GONE, status=404)
    await audit(
        db,
        tenant_id=admin.tenant_id,
        actor_account_id=admin.account_id,
        action="Changed cap" if row.is_cap else "Changed allowance",
        target=await audit_target_of(db, row),
        detail=allowance_words(row, agent_names),
        at=now,
    )
    return row


async def delete_allowance(
    db: AsyncSession,
    admin: WebAdmin,
    now: datetime,
    allowance_id: uuid.UUID,
    *,
    agent_names: Mapping[str, str],
) -> SpendAllowanceRow:
    rows = await spend_allowances.list_for_tenant(db, tenant_id=admin.tenant_id)
    row = next((r for r in rows if r.id == allowance_id), None)
    if row is None or not await spend_allowances.delete_allowance(
        db, tenant_id=admin.tenant_id, allowance_id=allowance_id
    ):
        raise WebAdminError(_GONE, status=404)
    await audit(
        db,
        tenant_id=admin.tenant_id,
        actor_account_id=admin.account_id,
        action="Removed cap" if row.is_cap else "Removed allowance",
        target=await audit_target_of(db, row),
        detail=allowance_words(row, agent_names),
        at=now,
    )
    return row
