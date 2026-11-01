"""Store for web_people: one row per person per workspace.

The person is what a browser session acts as. A member shares the account of their
platform principal (or of a `web` principal until they link one); the account keeps
spend, memory and ownership together whichever surface they use.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import datetime
from typing import Any, cast

from daimon.core._models import WebPerson, WebSession, WebSessionPerson
from daimon.core.cursor import Cursor
from daimon.core.stores.domain import WebPersonKind, WebPersonRow, WebPersonStatus
from sqlalchemy import CursorResult, delete, func, literal, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def get_person(
    session: AsyncSession, *, tenant_id: uuid.UUID, person_id: uuid.UUID
) -> WebPersonRow | None:
    orm = (
        await session.execute(
            select(WebPerson).where(WebPerson.id == person_id, WebPerson.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    return None if orm is None else WebPersonRow.model_validate(orm)


async def get_person_for_account(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> WebPersonRow | None:
    orm = (
        await session.execute(
            select(WebPerson).where(
                WebPerson.tenant_id == tenant_id, WebPerson.account_id == account_id
            )
        )
    ).scalar_one_or_none()
    return None if orm is None else WebPersonRow.model_validate(orm)


async def get_people(session: AsyncSession, *, person_ids: list[uuid.UUID]) -> list[WebPersonRow]:
    """The people with these ids, in no particular order; unknown ids are skipped."""
    rows = (await session.execute(select(WebPerson).where(WebPerson.id.in_(person_ids)))).scalars()
    return [WebPersonRow.model_validate(orm) for orm in rows]


async def upsert_member(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    display_name: str,
    workspace_name: str,
    email: str | None,
    new_person_id: uuid.UUID,
) -> WebPersonRow:
    """Create the member row for `(tenant, account)`, or refresh its name and email.

    A sign-in that carries no email (a chat provider) leaves the stored one alone.

    `new_person_id` is used only when the row is new, so a caller can name the person
    in advance (a `web` principal's external id is its person id). A visitor who later
    becomes a member keeps their row and kind: promotion is an admin action.
    """
    new = insert(WebPerson).values(
        id=new_person_id,
        tenant_id=tenant_id,
        account_id=account_id,
        kind="member",
        display_name=display_name,
        workspace_name=workspace_name,
        email=email,
    )
    stmt = new.on_conflict_do_update(
        constraint="uq_web_people_tenant_account",
        set_={
            "display_name": display_name,
            "workspace_name": workspace_name,
            "email": func.coalesce(new.excluded.email, WebPerson.email),
        },
    ).returning(WebPerson)
    return WebPersonRow.model_validate((await session.execute(stmt)).scalar_one())


async def create_invited(
    session: AsyncSession,
    *,
    person_id: uuid.UUID,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    kind: WebPersonKind,
    display_name: str,
    workspace_name: str,
    email: str | None,
    invite_id: uuid.UUID,
    invited_by_account_id: uuid.UUID | None,
) -> WebPersonRow:
    """The person an accepted invite brings in, on an account of their own."""
    orm = WebPerson(
        id=person_id,
        tenant_id=tenant_id,
        account_id=account_id,
        kind=kind,
        display_name=display_name,
        workspace_name=workspace_name,
        email=email,
        invite_id=invite_id,
        invited_by_account_id=invited_by_account_id,
    )
    session.add(orm)
    await session.flush()
    await session.refresh(orm)
    return WebPersonRow.model_validate(orm)


async def list_people(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    limit: int | None = None,
    after: Cursor | None = None,
    search: str | None = None,
) -> list[WebPersonRow]:
    """The workspace's people, oldest first; `limit` and `after` (the `(created_at, id)` of
    the last person already seen) page them. `search` keeps those whose name or email
    contains it, ignoring case."""
    query = select(WebPerson).where(WebPerson.tenant_id == tenant_id)
    if search:
        query = query.where(
            or_(
                WebPerson.display_name.icontains(search, autoescape=True),
                WebPerson.email.icontains(search, autoescape=True),
            )
        )
    if after is not None:
        query = query.where(
            tuple_(WebPerson.created_at, WebPerson.id)
            > tuple_(literal(after[0]), literal(after[1]))
        )
    query = query.order_by(WebPerson.created_at, WebPerson.id)
    if limit is not None:
        query = query.limit(limit)
    return [WebPersonRow.model_validate(orm) for orm in await session.scalars(query)]


async def count_by_tenant(
    session: AsyncSession, *, tenant_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """How many people each workspace has, in one grouped read."""
    if not tenant_ids:
        return {}
    rows = await session.execute(
        select(WebPerson.tenant_id, func.count())
        .where(WebPerson.tenant_id.in_(list(tenant_ids)))
        .group_by(WebPerson.tenant_id)
    )
    return {tenant_id: count for tenant_id, count in rows.all()}


async def count_members(session: AsyncSession, *, tenant_id: uuid.UUID) -> int:
    """How many of the workspace's people are members; visitors are in no group."""
    count = await session.scalar(
        select(func.count())
        .select_from(WebPerson)
        .where(WebPerson.tenant_id == tenant_id, WebPerson.kind == "member")
    )
    return count or 0


async def count_people(session: AsyncSession, *, tenant_id: uuid.UUID) -> int:
    count = await session.scalar(
        select(func.count()).select_from(WebPerson).where(WebPerson.tenant_id == tenant_id)
    )
    return count or 0


async def update_person(
    session: AsyncSession,
    *,
    person_id: uuid.UUID,
    status: WebPersonStatus | None = None,
    last_seen_at: datetime | None = None,
    account_id: uuid.UUID | None = None,
    kind: WebPersonKind | None = None,
) -> None:
    values: dict[str, Any] = {}
    if account_id is not None:
        values["account_id"] = account_id
    if kind is not None:
        values["kind"] = kind
    if status is not None:
        values["status"] = status
    if last_seen_at is not None:
        values["last_seen_at"] = last_seen_at
    if values:
        await session.execute(update(WebPerson).where(WebPerson.id == person_id).values(**values))


async def count_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    count = await session.scalar(
        select(func.count()).select_from(WebPerson).where(WebPerson.account_id == account_id)
    )
    return count or 0


async def delete_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    """Delete the account's people, with their identities, tokens and browser sessions.

    A session lists every person its sign-in reached, so it goes when any of them is
    erased: its subject names the person. Returns the number of people removed.
    """
    person_ids = select(WebPerson.id).where(WebPerson.account_id == account_id)
    session_ids = select(WebSessionPerson.session_id).where(
        WebSessionPerson.person_id.in_(person_ids)
    )
    await session.execute(delete(WebSession).where(WebSession.id.in_(session_ids)))
    result = await session.execute(delete(WebPerson).where(WebPerson.account_id == account_id))
    return cast(CursorResult[Any], result).rowcount
