"""Store for web_sessions: browser sign-ins, keyed by the SHA-256 of the cookie token.

A session ends at `expires_at`, fixed when it is created: visits never extend it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, cast

from daimon.core._models import WebPerson, WebSession, WebSessionPerson
from daimon.core.stores.domain import WebPersonRow, WebSessionRow
from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession


async def create_session(
    session: AsyncSession,
    *,
    token_sha256: bytes,
    provider: str,
    issuer: str,
    subject: str,
    people: Sequence[WebPersonRow],
    ttl: timedelta,
    now: datetime,
) -> WebSessionRow:
    """Start a session on the first active of `people`; it may switch among all of them.

    A suspended person is refused on every request, so a session that started on one
    would lock the browser out of the workspaces where the person is active.
    """
    if not people:
        raise ValueError("a session needs at least one person")
    current = next((p for p in people if p.status == "active"), people[0])
    orm = WebSession(
        token_sha256=token_sha256,
        provider=provider,
        issuer=issuer,
        subject=subject,
        current_person_id=current.id,
        tenant_id=current.tenant_id,
        created_at=now,
        last_seen_at=now,
        expires_at=now + ttl,
        roles_checked_at=now,
    )
    session.add(orm)
    await session.flush()
    session.add_all(
        WebSessionPerson(session_id=orm.id, tenant_id=p.tenant_id, person_id=p.id) for p in people
    )
    await session.flush()
    return WebSessionRow.model_validate(orm)


async def get_live_session(
    session: AsyncSession, *, token_sha256: bytes, now: datetime
) -> WebSessionRow | None:
    orm = (
        await session.execute(
            select(WebSession).where(
                WebSession.token_sha256 == token_sha256, WebSession.expires_at > now
            )
        )
    ).scalar_one_or_none()
    return None if orm is None else WebSessionRow.model_validate(orm)


async def get_live_session_with_person(
    session: AsyncSession, *, token_sha256: bytes, now: datetime
) -> tuple[WebSessionRow, WebPersonRow] | None:
    """A live session and the person it is working as, in one read."""
    row = (
        await session.execute(
            select(WebSession, WebPerson)
            .join(
                WebPerson,
                (WebPerson.id == WebSession.current_person_id)
                & (WebPerson.tenant_id == WebSession.tenant_id),
            )
            .where(WebSession.token_sha256 == token_sha256, WebSession.expires_at > now)
        )
    ).one_or_none()
    if row is None:
        return None
    return WebSessionRow.model_validate(row[0]), WebPersonRow.model_validate(row[1])


async def list_session_people(session: AsyncSession, *, session_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await session.execute(
        select(WebSessionPerson.person_id).where(WebSessionPerson.session_id == session_id)
    )
    return list(rows.scalars())


async def switch_person(
    session: AsyncSession, *, session_id: uuid.UUID, tenant_id: uuid.UUID
) -> WebSessionRow | None:
    """Make the session's person in `tenant_id` current; None when it has none there."""
    person_id = (
        await session.execute(
            select(WebSessionPerson.person_id).where(
                WebSessionPerson.session_id == session_id, WebSessionPerson.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if person_id is None:
        return None
    orm = (
        await session.execute(
            update(WebSession)
            .where(WebSession.id == session_id)
            .values(current_person_id=person_id, tenant_id=tenant_id)
            .returning(WebSession)
        )
    ).scalar_one_or_none()
    return None if orm is None else WebSessionRow.model_validate(orm)


async def touch(session: AsyncSession, *, session_id: uuid.UUID, now: datetime) -> None:
    """Record a visit. The expiry is fixed at sign-in and never moves."""
    await session.execute(
        update(WebSession).where(WebSession.id == session_id).values(last_seen_at=now)
    )


async def mark_checked(session: AsyncSession, *, session_id: uuid.UUID, now: datetime) -> None:
    """Record that the sign-in was re-checked against the database at `now`."""
    await session.execute(
        update(WebSession).where(WebSession.id == session_id).values(roles_checked_at=now)
    )


async def delete_session(session: AsyncSession, *, token_sha256: bytes) -> int:
    result = await session.execute(
        delete(WebSession).where(WebSession.token_sha256 == token_sha256)
    )
    return cast(CursorResult[Any], result).rowcount


async def delete_expired(session: AsyncSession, *, now: datetime) -> int:
    result = await session.execute(delete(WebSession).where(WebSession.expires_at <= now))
    return cast(CursorResult[Any], result).rowcount
