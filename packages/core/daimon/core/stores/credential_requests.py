"""Async store for credential_requests — the credential-button handshake.

No try/except anywhere in this module — exceptions propagate to the adapter
boundary. `consume_credential_request`'s single atomic UPDATE is the entire
single-use gate: its own row lock serializes concurrent clicks, so no
advisory lock (unlike the vault get-or-create critical section in
`mcp_vault.py`, which needs one because it spans a read-then-create) is
needed here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from daimon.core._models import CredentialRequest
from daimon.core.credential_requests import CredentialRequestKind, CredentialRequestOutcome
from daimon.core.stores.domain import CredentialRequestRow
from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession


async def create_credential_request(
    session: AsyncSession,
    *,
    token: str,
    kind: CredentialRequestKind,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
    target: str,
    mcp_server_url: str | None,
    requester_platform_user_id: str,
    channel_id: str,
    expires_at: datetime,
    idempotency_key: uuid.UUID,
    target_ma_agent_id: str,
    target_name: str,
    requested_work: str | None,
    responder_name: str | None = None,
    replaces_updated_at: datetime | None = None,
    platform: str | None = None,
    parent_channel_id: str | None = None,
    origin_thread_id: str | None = None,
    posted_message_id: str | None = None,
) -> CredentialRequestRow:
    """Insert a fresh, unused credential-request row and return it.

    `idempotency_key`, `target_ma_agent_id`, `target_name` and
    `requested_work` have no defaults: provenance must be stated, not
    defaulted into. A mint site that does not know which agent the control
    targets, or what work is waiting on it, is a mint site that should not be
    posting a control — and `requested_work=None` said explicitly is the
    legitimate way to record "nothing was waiting", the same discipline
    `agent_repo_binding.set_binding` applies to `proof`.
    """
    orm = CredentialRequest(
        token=token,
        kind=kind,
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=account_id,
        target=target,
        mcp_server_url=mcp_server_url,
        requester_platform_user_id=requester_platform_user_id,
        channel_id=channel_id,
        expires_at=expires_at,
        idempotency_key=idempotency_key,
        target_ma_agent_id=target_ma_agent_id,
        target_name=target_name,
        requested_work=requested_work,
        responder_name=responder_name,
        replaces_updated_at=replaces_updated_at,
        platform=platform,
        parent_channel_id=parent_channel_id,
        origin_thread_id=origin_thread_id,
        posted_message_id=posted_message_id,
    )
    session.add(orm)
    await session.flush()
    return CredentialRequestRow.model_validate(orm)


async def peek_credential_request(
    session: AsyncSession,
    *,
    token: str,
) -> CredentialRequestRow | None:
    """Return the row for `token`, or None if it was never minted.

    Deliberately applies no lifecycle filter (unused/unexpired) — mirrors
    `github_oauth_states.get_by_state`'s rationale: the caller needs to tell
    "expired", "already used", and "unknown token" apart, which requires the
    full row, not a filtered read that collapses all three into None.
    """
    orm = await session.get(CredentialRequest, token)
    if orm is None:
        return None
    return CredentialRequestRow.model_validate(orm)


async def consume_credential_request(
    session: AsyncSession,
    *,
    token: str,
    now: datetime,
) -> CredentialRequestRow | None:
    """Atomically mark `token` used, iff it is unused and unexpired.

    One statement is the authoritative single-use gate: the UPDATE's own row
    lock serializes concurrent consumers, so the loser's WHERE clause simply
    matches zero rows — no advisory lock needed. Returns None for "not
    consumable" (unknown token, already used, or expired); the caller cannot
    and must not distinguish those cases from this return value alone (use
    `peek_credential_request` for that).
    """
    stmt = (
        update(CredentialRequest)
        .where(
            CredentialRequest.token == token,
            CredentialRequest.used_at.is_(None),
            CredentialRequest.expires_at > now,
        )
        .values(used_at=now)
        .returning(CredentialRequest)
    )
    result = await session.execute(stmt)
    orm = result.scalar_one_or_none()
    await session.flush()
    if orm is None:
        return None
    return CredentialRequestRow.model_validate(orm)


async def list_live_credential_requests(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    requester_platform_user_id: str,
    origin_thread_id: str,
    now: datetime,
) -> list[CredentialRequestRow]:
    """Return the still-clickable requests one person left open in one thread.

    Live means exactly what `consume_credential_request` will accept: unused
    and unexpired. The four predicates together are the supersede scope — the
    same requester, the same thread, the same agent — so a second person's
    form, another agent's form, and a form in another thread are never
    returned and never retired.
    """
    stmt = (
        select(CredentialRequest)
        .where(
            CredentialRequest.tenant_id == tenant_id,
            CredentialRequest.agent_id == agent_id,
            CredentialRequest.requester_platform_user_id == requester_platform_user_id,
            CredentialRequest.origin_thread_id == origin_thread_id,
            CredentialRequest.used_at.is_(None),
            CredentialRequest.expires_at > now,
        )
        .order_by(CredentialRequest.created_at)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [CredentialRequestRow.model_validate(orm) for orm in rows]


async def supersede_credential_request(
    session: AsyncSession,
    *,
    token: str,
    now: datetime,
) -> CredentialRequestRow | None:
    """Retire an unclicked request because a newer one replaced it.

    Spends the row the same way a click would — one UPDATE, guarded by
    `used_at IS NULL`, so a form someone is submitting right now wins and this
    returns None. The recorded outcome says which of the two happened without
    a second read.
    """
    stmt = (
        update(CredentialRequest)
        .where(CredentialRequest.token == token, CredentialRequest.used_at.is_(None))
        .values(used_at=now, outcome="replaced_by_newer")
        .returning(CredentialRequest)
    )
    result = await session.execute(stmt)
    orm = result.scalar_one_or_none()
    await session.flush()
    if orm is None:
        return None
    return CredentialRequestRow.model_validate(orm)


async def set_credential_request_outcome(
    session: AsyncSession,
    *,
    token: str,
    outcome: CredentialRequestOutcome,
) -> None:
    """Record how the consumed request actually ended. Idempotent; no raise if absent.

    Written after `consume_credential_request` has already spent the row, so
    this never gates anything — it is the durable trace of what the click did
    ("applied", "stale_replacement", "write_failed"), which the posted card's
    final state is rendered from.
    """
    await session.execute(
        update(CredentialRequest).where(CredentialRequest.token == token).values(outcome=outcome)
    )


async def delete_credential_requests_for_platform_user(
    session: AsyncSession,
    *,
    platform_user_id: str,
    tenant_id: uuid.UUID | None = None,
) -> int:
    """Delete ALL credential-request rows for `platform_user_id`. Idempotent.

    Deliberately applies no lifecycle filter — used and expired rows still
    carry `requester_platform_user_id` PII and must be erased, mirroring
    `github_oauth_states.delete_states_for_platform_user`.

    Callers should always pass `tenant_id`: a platform user id is not
    globally unique (e.g. Discord snowflakes are scoped to the platform, not
    globally deduped across a hypothetical re-key), so a tenant-agnostic
    delete risks erasing another tenant's in-flight handshake rows.
    """
    predicates = [CredentialRequest.requester_platform_user_id == platform_user_id]
    if tenant_id is not None:
        predicates.append(CredentialRequest.tenant_id == tenant_id)
    result = await session.execute(delete(CredentialRequest).where(*predicates))
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


async def count_credential_requests_for_platform_user(
    session: AsyncSession,
    *,
    platform_user_id: str,
    tenant_id: uuid.UUID | None = None,
) -> int:
    """Count credential-request rows that `delete_credential_requests_for_platform_user`
    would delete. Read-only. Pass the SAME `tenant_id` the delete caller uses,
    or the preview diverges from the purge (parity contract in daimon.core.privacy).
    """
    predicates = [CredentialRequest.requester_platform_user_id == platform_user_id]
    if tenant_id is not None:
        predicates.append(CredentialRequest.tenant_id == tenant_id)
    stmt = select(func.count()).select_from(CredentialRequest).where(*predicates)
    return int((await session.execute(stmt)).scalar_one())


async def update_credential_request_message(
    session: AsyncSession, *, token: str, posted_message_id: str
) -> None:
    """Save the posted card identity for outcomes after the origin has expired."""
    await session.execute(
        update(CredentialRequest)
        .where(CredentialRequest.token == token)
        .values(posted_message_id=posted_message_id)
    )
