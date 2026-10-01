"""Async store for wizard_session — the multi-step wizard's durable state.

No try/except anywhere in this module — exceptions propagate to the adapter
boundary. `try_claim_submit`'s single atomic UPDATE is the entire
double-submit gate: its own row lock serializes concurrent taps, so no
advisory lock (unlike the vault get-or-create critical section in
`mcp_vault.py`, which needs one because it spans a read-then-create) is
needed here — nothing in this module spans a read-then-write.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, cast

from daimon.core._models import WizardSession
from daimon.core.stores.domain import WizardSessionRow
from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession


async def create_wizard_session(
    session: AsyncSession,
    *,
    short_id: str,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
    requester_platform_user_id: str,
    channel_id: str,
    message_id: str,
    spec: dict[str, Any],
    answers: dict[str, list[str]],
    current_step: int,
    status: str,
    expires_at: datetime,
    now: datetime,
) -> WizardSessionRow:
    """Insert a fresh wizard_session row and return it. `now` is injected,
    never read from a clock inside the store."""
    orm = WizardSession(
        id=short_id,
        tenant_id=tenant_id,
        account_id=account_id,
        requester_platform_user_id=requester_platform_user_id,
        channel_id=channel_id,
        message_id=message_id,
        spec=spec,
        answers=answers,
        current_step=current_step,
        status=status,
        updated_at=now,
        expires_at=expires_at,
    )
    session.add(orm)
    await session.flush()
    return WizardSessionRow.model_validate(orm)


async def get_wizard_session(
    session: AsyncSession,
    *,
    short_id: str,
) -> WizardSessionRow | None:
    """Return the row for `short_id`, or None if it was never minted.

    Deliberately applies no lifecycle filter — mirrors
    `credential_requests.peek_credential_request`'s rationale: the caller
    needs to tell "unknown", "expired", and "already submitted" apart, which
    requires the full row, not a filtered read that collapses all three into
    None.
    """
    orm = await session.get(WizardSession, short_id)
    if orm is None:
        return None
    return WizardSessionRow.model_validate(orm)


async def update_wizard_state(
    session: AsyncSession,
    *,
    short_id: str,
    answers: dict[str, list[str]],
    current_step: int,
    expected_updated_at: datetime,
    now: datetime,
) -> int:
    """Persist a tap's new answers/step, iff the row is open, unexpired, and
    unchanged since the caller read it.

    Deliberately not a read-modify-write: one UPDATE statement predicated on
    `id == short_id AND status == "open" AND expires_at > now AND
    updated_at == expected_updated_at`. A tap on a row that was already
    purged, submitted, or expired therefore writes nothing and reports a
    rowcount of 0 — the caller decides what to tell the user, rather than
    this store assuming the row still exists.

    `expected_updated_at` is the optimistic-concurrency token, and it is what
    makes the predicate cover content as well as lifecycle: every caller
    computes its new answers/step from a row it read earlier, so without it
    two near-simultaneous taps both write from the same base row and the
    second silently erases the first's effect. `updated_at` advances on every
    write here (and on the submit claim), so a losing tap matches zero rows
    and gets the same rowcount 0 a lifecycle miss produces.
    """
    stmt = (
        update(WizardSession)
        .where(
            WizardSession.id == short_id,
            WizardSession.status == "open",
            WizardSession.expires_at > now,
            WizardSession.updated_at == expected_updated_at,
        )
        .values(
            answers=answers,
            current_step=current_step,
            updated_at=func.greatest(now, WizardSession.updated_at + timedelta(microseconds=1)),
        )
    )
    result = await session.execute(stmt)
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


async def try_claim_submit(
    session: AsyncSession,
    *,
    short_id: str,
    answers: dict[str, list[str]],
    current_step: int,
    expected_updated_at: datetime,
    now: datetime,
) -> WizardSessionRow | None:
    """Atomically flip `short_id` to submitted if its observed state is current.

    One statement is the authoritative single-use gate: the UPDATE's own row
    lock serializes concurrent Submit taps, so the loser's WHERE clause
    simply matches zero rows. `expected_updated_at` also rejects a submit
    computed from a stale read, so it cannot replace answers from an edit that
    committed in the meantime. Returns the claimed row, or None when its
    lifecycle or content changed before the claim.
    """
    stmt = (
        update(WizardSession)
        .where(
            WizardSession.id == short_id,
            WizardSession.status == "open",
            WizardSession.expires_at > now,
            WizardSession.updated_at == expected_updated_at,
        )
        .values(
            status="submitted",
            answers=answers,
            current_step=current_step,
            updated_at=func.greatest(now, WizardSession.updated_at + timedelta(microseconds=1)),
        )
        .returning(WizardSession)
    )
    result = await session.execute(stmt)
    orm = result.scalar_one_or_none()
    await session.flush()
    if orm is None:
        return None
    return WizardSessionRow.model_validate(orm)


async def abandon_expired_wizard_sessions(
    session: AsyncSession,
    *,
    now: datetime,
    limit: int = 500,
) -> int:
    """Flip up to `limit` open, past-expiry rows to `abandoned`. Returns the
    rowcount.

    Selects the target ids first with a `LIMIT`, then updates by that id
    set, so one sweep tick cannot lock the whole table.
    """
    id_stmt = (
        select(WizardSession.id)
        .where(WizardSession.status == "open", WizardSession.expires_at <= now)
        .limit(limit)
    )
    ids = (await session.execute(id_stmt)).scalars().all()
    if not ids:
        return 0
    stmt = (
        update(WizardSession)
        .where(
            WizardSession.id.in_(ids),
            WizardSession.status == "open",
            WizardSession.expires_at <= now,
        )
        .values(status="abandoned", updated_at=now)
    )
    result = await session.execute(stmt)
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


async def delete_wizard_sessions_for_platform_user(
    session: AsyncSession,
    *,
    platform_user_id: str,
    tenant_id: uuid.UUID | None = None,
) -> int:
    """Delete ALL wizard_session rows for `platform_user_id`. Idempotent.

    Deliberately applies no lifecycle filter — submitted and abandoned rows
    still carry the user's typed answers and must be erased, mirroring
    `credential_requests.delete_credential_requests_for_platform_user`.

    Callers should always pass `tenant_id`: a platform user id is not
    globally unique across installs, so a tenant-agnostic delete risks
    erasing another tenant's rows.
    """
    predicates = [WizardSession.requester_platform_user_id == platform_user_id]
    if tenant_id is not None:
        predicates.append(WizardSession.tenant_id == tenant_id)
    result = await session.execute(delete(WizardSession).where(*predicates))
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


async def count_wizard_sessions_for_platform_user(
    session: AsyncSession,
    *,
    platform_user_id: str,
    tenant_id: uuid.UUID | None = None,
) -> int:
    """Count wizard_session rows that `delete_wizard_sessions_for_platform_user`
    would delete. Read-only. Pass the SAME `tenant_id` the delete caller uses,
    or the preview diverges from the purge (parity contract in daimon.core.privacy).
    """
    predicates = [WizardSession.requester_platform_user_id == platform_user_id]
    if tenant_id is not None:
        predicates.append(WizardSession.tenant_id == tenant_id)
    stmt = select(func.count()).select_from(WizardSession).where(*predicates)
    return int((await session.execute(stmt)).scalar_one())
