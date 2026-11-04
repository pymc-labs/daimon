"""Store for web_logins: the sign-in accounts one member connected, across platforms.

An account (provider and subject) belongs to one login; connecting an account that is in
another login merges the two.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from daimon.core._models import WebLogin, WebLoginAccount, WebSession
from daimon.core.stores.domain import WebLoginAccountRow
from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def get_account(
    session: AsyncSession, *, provider: str, subject: str
) -> WebLoginAccountRow | None:
    orm = await session.get(WebLoginAccount, (provider, subject))
    return None if orm is None else WebLoginAccountRow.model_validate(orm)


async def list_accounts(session: AsyncSession, *, login_id: uuid.UUID) -> list[WebLoginAccountRow]:
    """The login's accounts, most recently proven first."""
    rows = await session.scalars(
        select(WebLoginAccount)
        .where(WebLoginAccount.login_id == login_id)
        .order_by(WebLoginAccount.last_proven_at.desc(), WebLoginAccount.provider)
    )
    return [WebLoginAccountRow.model_validate(orm) for orm in rows]


async def create_login(session: AsyncSession) -> uuid.UUID:
    login = WebLogin()
    session.add(login)
    await session.flush()
    return login.id


async def prove_account(
    session: AsyncSession, *, provider: str, subject: str, display_name: str, now: datetime
) -> WebLoginAccountRow:
    """Record that the provider vouched for the account now; it gets a login of its own
    the first time."""
    existing = await get_account(session, provider=provider, subject=subject)
    login_id = existing.login_id if existing is not None else await create_login(session)
    stmt = (
        insert(WebLoginAccount)
        .values(
            login_id=login_id,
            provider=provider,
            subject=subject,
            display_name=display_name,
            last_proven_at=now,
        )
        .on_conflict_do_update(
            index_elements=["provider", "subject"],
            set_={"display_name": display_name, "last_proven_at": now},
        )
        .returning(WebLoginAccount)
    )
    row = WebLoginAccountRow.model_validate((await session.execute(stmt)).scalar_one())
    if row.login_id != login_id:
        # Lost a race to another sign-in of the same account; drop the spare login.
        await session.execute(delete(WebLogin).where(WebLogin.id == login_id))
    return row


async def attach_session(
    session: AsyncSession, *, session_id: uuid.UUID, login_id: uuid.UUID
) -> None:
    await session.execute(
        update(WebSession).where(WebSession.id == session_id).values(login_id=login_id)
    )


async def add_account(
    session: AsyncSession,
    *,
    login_id: uuid.UUID,
    provider: str,
    subject: str,
    display_name: str,
    now: datetime,
) -> uuid.UUID | None:
    """Put the account, proven now, in the login. One that is in another login brings that
    whole login along: its accounts and sessions move over, and its id is returned."""
    existing = await get_account(session, provider=provider, subject=subject)
    merged: uuid.UUID | None = None
    if existing is not None and existing.login_id != login_id:
        merged = existing.login_id
        await session.execute(
            update(WebLoginAccount)
            .where(WebLoginAccount.login_id == merged)
            .values(login_id=login_id)
        )
        await session.execute(
            update(WebSession).where(WebSession.login_id == merged).values(login_id=login_id)
        )
        await session.execute(delete(WebLogin).where(WebLogin.id == merged))
    stmt = (
        insert(WebLoginAccount)
        .values(
            login_id=login_id,
            provider=provider,
            subject=subject,
            display_name=display_name,
            last_proven_at=now,
        )
        .on_conflict_do_update(
            index_elements=["provider", "subject"],
            set_={"display_name": display_name, "last_proven_at": now},
        )
    )
    await session.execute(stmt)
    return merged


async def remove_account(session: AsyncSession, *, provider: str, subject: str) -> int:
    """Take the account out of its login; its next sign-in starts a login of its own."""
    result = await session.execute(
        delete(WebLoginAccount).where(
            WebLoginAccount.provider == provider, WebLoginAccount.subject == subject
        )
    )
    return cast(CursorResult[Any], result).rowcount
