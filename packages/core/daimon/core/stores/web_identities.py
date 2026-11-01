"""Store for web_identities: how a person signs in to the web app."""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence

from daimon.core._models import WebIdentity, WebPerson
from daimon.core.stores.domain import WebIdentityRow
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def find_identity(
    session: AsyncSession, *, provider: str, issuer: str, subject: str
) -> list[WebIdentityRow]:
    """Every workspace's identity for this login; sign-in reaches all of them."""
    rows = await session.execute(
        select(WebIdentity).where(
            WebIdentity.provider == provider,
            WebIdentity.issuer == issuer,
            WebIdentity.subject == subject,
        )
    )
    return [WebIdentityRow.model_validate(orm) for orm in rows.scalars()]


async def link_identity(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    person_id: uuid.UUID,
    provider: str,
    issuer: str,
    subject: str,
    label: str,
    groups: Sequence[str],
) -> WebIdentityRow:
    """Attach the login to the person, or refresh its label and groups if already attached."""
    stmt = (
        insert(WebIdentity)
        .values(
            tenant_id=tenant_id,
            person_id=person_id,
            provider=provider,
            issuer=issuer,
            subject=subject,
            label=label,
            groups=list(groups),
        )
        .on_conflict_do_update(
            constraint="uq_web_identities_subject",
            set_={"label": label, "groups": list(groups)},
        )
        .returning(WebIdentity)
    )
    return WebIdentityRow.model_validate((await session.execute(stmt)).scalar_one())


async def list_for_person(session: AsyncSession, *, person_id: uuid.UUID) -> list[WebIdentityRow]:
    rows = await session.execute(
        select(WebIdentity)
        .where(WebIdentity.person_id == person_id)
        .order_by(WebIdentity.linked_at)
    )
    return [WebIdentityRow.model_validate(orm) for orm in rows.scalars()]


async def list_for_people(
    session: AsyncSession, *, person_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, list[WebIdentityRow]]:
    """`list_for_person` for many people in one read; everyone asked for has an entry."""
    found: dict[uuid.UUID, list[WebIdentityRow]] = {person_id: [] for person_id in person_ids}
    if not person_ids:
        return found
    rows = await session.scalars(
        select(WebIdentity)
        .where(WebIdentity.person_id.in_(list(person_ids)))
        .order_by(WebIdentity.linked_at)
    )
    for orm in rows:
        found[orm.person_id].append(WebIdentityRow.model_validate(orm))
    return found


async def count_people_in_group(session: AsyncSession, *, tenant_id: uuid.UUID, group: str) -> int:
    """How many of the workspace's members carry `group` on any of their sign-ins."""
    count = await session.scalar(
        select(func.count(func.distinct(WebIdentity.person_id)))
        .join(WebPerson, WebPerson.id == WebIdentity.person_id)
        .where(
            WebIdentity.tenant_id == tenant_id,
            WebIdentity.groups.contains([group]),
            WebPerson.kind == "member",
        )
    )
    return count or 0


async def count_people_by_group(session: AsyncSession, *, tenant_id: uuid.UUID) -> dict[str, int]:
    """Every group any of the workspace's sign-ins carries, with how many members carry it."""
    carried = (
        select(func.unnest(WebIdentity.groups).label("name"), WebIdentity.person_id)
        .join(WebPerson, WebPerson.id == WebIdentity.person_id)
        .where(WebIdentity.tenant_id == tenant_id, WebPerson.kind == "member")
        .subquery()
    )
    rows = await session.execute(
        select(carried.c.name, func.count(func.distinct(carried.c.person_id))).group_by(
            carried.c.name
        )
    )
    return {name: count for name, count in rows.all()}
