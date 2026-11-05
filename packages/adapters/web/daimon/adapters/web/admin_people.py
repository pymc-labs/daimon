"""`/api/admin/people`: the workspace's people, their roles and sign-in links.

Every change is audited in the transaction that makes it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Annotated

from daimon.adapters.web.access import Role
from daimon.adapters.web.admin_common import (
    AdminPerson,
    AdminPersonDetail,
    Credit,
    PeopleAdmin,
    Pickers,
    admin_of,
    person_of,
    refused,
)
from daimon.adapters.web.caller import role_of, roles_of
from daimon.adapters.web.credits import person_meter
from daimon.adapters.web.me import identity_json
from daimon.adapters.web.paging import PageRequest, cut, page_of
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import web_admin
from daimon.core.allowances import EVERYONE
from daimon.core.stores import web_identities as identities_store
from daimon.core.stores import web_invites
from daimon.core.stores import web_people as people_store
from daimon.core.stores.domain import WebIdentityRow, WebPersonRow, WebPersonStatus
from daimon.core.web_admin import WebAdminError, link_url
from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession


class AdminGroup(BaseModel):
    id: str
    name: str
    members: int


class PersonChange(BaseModel):
    role: Role | None = None
    status: WebPersonStatus | None = None


def people_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/admin")

    def person_json(
        person: WebPersonRow, role: Role, identities: Sequence[WebIdentityRow], meter: Credit | None
    ) -> AdminPerson:
        groups = sorted({g for i in identities for g in i.groups})
        return AdminPerson(
            id=str(person.id),
            name=person.display_name,
            email=person.email or "",
            role=role,
            status=person.status,
            groups=groups,
            identities=[identity_json(row) for row in identities],
            credit=meter,
            last_seen=person.last_seen_at.isoformat(),
        )

    async def person_detail(db: AsyncSession, person: WebPersonRow) -> AdminPersonDetail:
        """One person with where they stand against their allowance, which costs the
        spend queries a listing leaves out."""
        identities = await identities_store.list_for_person(db, person_id=person.id)
        role = await role_of(db, person)
        meter = await person_meter(
            runtime, person, role, frozenset(g for i in identities for g in i.groups)
        )
        credit = (
            None if meter is None else Credit(left_usd=meter.left_usd, total_usd=meter.total_usd)
        )
        shown = AdminPersonDetail(**person_json(person, role, identities, credit).model_dump())
        invite = (
            await web_invites.get_invite(db, tenant_id=person.tenant_id, invite_id=person.invite_id)
            if role == "visitor" and person.invite_id is not None
            else None
        )
        if invite is not None and invite.revoked_at is None:
            shown = shown.model_copy(
                update={"invite_id": str(invite.id), "allow_files": invite.allow_files}
            )
        return shown

    # People

    @router.get("/people")
    async def people(  # pyright: ignore[reportUnusedFunction]
        caller: Pickers,
        response: Response,
        page: Annotated[PageRequest, Depends(page_of(200))],
        q: Annotated[str | None, Query(max_length=100)] = None,
    ) -> list[AdminPerson]:
        """The workspace's people, oldest first, without their credit (see the person).
        `q` keeps those whose name or email contains it, for a picker."""
        async with runtime.sessionmaker() as db:
            rows = await people_store.list_people(
                db,
                tenant_id=caller.person.tenant_id,
                limit=page.limit + 1,
                after=page.cursor,
                search=q.strip() if q else None,
            )
            shown = cut(rows, page, response, lambda p: (p.created_at, p.id))
            identities = await identities_store.list_for_people(
                db, person_ids=[p.id for p in shown]
            )
            roles = await roles_of(db, shown)
        return [person_json(p, roles[p.id], identities[p.id], None) for p in shown]

    @router.get("/groups")
    async def groups(caller: Pickers) -> list[AdminGroup]:  # pyright: ignore[reportUnusedFunction]
        """Everyone, then each group the workspace's sign-ins carry, with its head count."""
        tenant_id = caller.person.tenant_id
        async with runtime.sessionmaker() as db:
            counts = await identities_store.count_people_by_group(db, tenant_id=tenant_id)
            members = await people_store.count_members(db, tenant_id=tenant_id)
        return [
            AdminGroup(id=EVERYONE, name="Everyone", members=members),
            *(
                AdminGroup(id=name, name=name, members=count)
                for name, count in sorted(counts.items())
                if name != EVERYONE
            ),
        ]

    @router.get("/people/{person_id}")
    async def person(person_id: uuid.UUID, caller: PeopleAdmin) -> AdminPersonDetail:  # pyright: ignore[reportUnusedFunction]
        async with runtime.sessionmaker() as db:
            return await person_detail(db, await person_of(db, caller, person_id))

    @router.patch("/people/{person_id}")
    async def change_person(  # pyright: ignore[reportUnusedFunction]
        person_id: uuid.UUID, body: PersonChange, caller: PeopleAdmin
    ) -> AdminPersonDetail:
        try:
            async with runtime.sessionmaker.begin() as db:
                changed = await web_admin.change_person(
                    db,
                    admin_of(caller),
                    runtime.now(),
                    person_id,
                    role=body.role,
                    status=body.status,
                )
        except WebAdminError as error:
            raise refused(error) from error
        async with runtime.sessionmaker() as db:
            return await person_detail(db, changed)

    @router.post("/people/{person_id}/link")
    async def sign_in_link(person_id: uuid.UUID, caller: PeopleAdmin) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """A one-time sign-in link for a visitor, who has no other way back in."""
        try:
            async with runtime.sessionmaker.begin() as db:
                _, token, _ = await web_admin.issue_sign_in_link(
                    db, admin_of(caller), runtime.now(), person_id
                )
        except WebAdminError as error:
            raise refused(error) from error
        return {"link": link_url(runtime.web, token)}

    @router.delete("/people/{person_id}/link")
    async def revoke_sign_in_link(person_id: uuid.UUID, caller: PeopleAdmin) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        """Revoke a person's open sign-in links, as when one was sent to the wrong place."""
        try:
            async with runtime.sessionmaker.begin() as db:
                await web_admin.revoke_sign_in_link(db, admin_of(caller), runtime.now(), person_id)
        except WebAdminError as error:
            raise refused(error) from error
        return {"ok": True}

    return router
