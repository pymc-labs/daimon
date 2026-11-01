"""`/api/admin/people`: the workspace's people, their roles and sign-in links.

Every change is audited in the transaction that makes it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Annotated

from daimon.adapters.web.access import Role, at_or_above, is_admin
from daimon.adapters.web.admin_common import (
    ROLE_WORDS,
    AdminPerson,
    Credit,
    PeopleAdmin,
    Pickers,
    conflict,
    no_person,
    person_of,
)
from daimon.adapters.web.audit import audit, person_target
from daimon.adapters.web.caller import WebCaller, role_of, roles_of
from daimon.adapters.web.credits import person_meter
from daimon.adapters.web.invites import SIGN_IN_LINK_TTL, issue_link, link_url
from daimon.adapters.web.me import identity_json
from daimon.adapters.web.paging import PageRequest, cut, page_of
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.allowances import EVERYONE
from daimon.core.stores import web_identities as identities_store
from daimon.core.stores import web_invites
from daimon.core.stores import web_people as people_store
from daimon.core.stores import web_roles as roles_store
from daimon.core.stores.domain import WebIdentityRow, WebPersonRow, WebPersonStatus
from fastapi import APIRouter, Depends, HTTPException, Query, Response
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

    async def person_detail(db: AsyncSession, person: WebPersonRow) -> AdminPerson:
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
        return person_json(person, role, identities, credit)

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
    async def person(person_id: uuid.UUID, caller: PeopleAdmin) -> AdminPerson:  # pyright: ignore[reportUnusedFunction]
        async with runtime.sessionmaker() as db:
            return await person_detail(db, await person_of(db, caller, person_id))

    @router.patch("/people/{person_id}")
    async def change_person(  # pyright: ignore[reportUnusedFunction]
        person_id: uuid.UUID, body: PersonChange, caller: PeopleAdmin
    ) -> AdminPerson:
        now, tenant_id = runtime.now(), caller.person.tenant_id
        async with runtime.sessionmaker.begin() as db:
            target = await person_of(db, caller, person_id)
            was = await role_of(db, target)
            if "owner" in (was, body.role) and caller.role != "owner":
                raise HTTPException(status_code=403, detail="Only an owner can do that.")
            if body.role is not None and body.role != was:
                await _set_role(db, caller, target, was, body.role)
                target = await person_of(db, caller, person_id)
                now_role = await role_of(db, target)
                if now_role != body.role:
                    raise conflict("This role comes from their chat workspace.")
                if was == "owner" and not await roles_store.has_active_owner(
                    db, tenant_id=tenant_id
                ):
                    raise conflict("A workspace needs an owner.")
                await audit(
                    db,
                    tenant_id=tenant_id,
                    actor_account_id=caller.person.account_id,
                    action="Changed role",
                    target=person_target(target.id),
                    detail=f"{ROLE_WORDS[was]} to {ROLE_WORDS[body.role]}",
                    at=now,
                )
            if body.status is not None and body.status != target.status:
                if target.id == caller.person.id:
                    raise conflict("You can't suspend yourself.")
                await people_store.update_person(db, person_id=target.id, status=body.status)
                if body.status == "suspended":
                    # A suspended person's open sign-in links must not outlive the suspension.
                    await web_invites.revoke_open_links(
                        db, tenant_id=tenant_id, kind="sign_in", now=now, person_id=target.id
                    )
                await audit(
                    db,
                    tenant_id=tenant_id,
                    actor_account_id=caller.person.account_id,
                    action="Suspended" if body.status == "suspended" else "Restored",
                    target=person_target(target.id),
                    at=now,
                )
            changed = await person_of(db, caller, person_id)
        async with runtime.sessionmaker() as db:
            return await person_detail(db, changed)

    async def _set_role(
        db: AsyncSession, caller: WebCaller, target: WebPersonRow, was: Role, role: Role
    ) -> None:
        if role == "visitor":
            raise conflict("A member can't become a visitor. Suspend them instead.")
        if was == "visitor":
            await people_store.update_person(db, person_id=target.id, kind="member")
        if is_admin(role):
            await roles_store.set_workspace_role(
                db,
                tenant_id=target.tenant_id,
                account_id=target.account_id,
                role="owner" if role == "owner" else "admin",
                granted_by_account_id=caller.person.account_id,
            )
        else:
            await roles_store.delete_workspace_role(
                db, tenant_id=target.tenant_id, account_id=target.account_id
            )

    @router.post("/people/{person_id}/link")
    async def sign_in_link(person_id: uuid.UUID, caller: PeopleAdmin) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """A one-time sign-in link for a visitor, who has no other way back in."""
        now = runtime.now()
        async with runtime.sessionmaker.begin() as db:
            person = await person_of(db, caller, person_id)
            if person.kind != "visitor":
                raise no_person()
            # A link signs in as the person, so it must never hand a caller a higher role.
            if at_or_above(await role_of(db, person), caller.role):
                raise HTTPException(status_code=403, detail="Ask an owner.")
            _, token = await issue_link(
                db,
                tenant_id=person.tenant_id,
                kind="sign_in",
                expires_at=now + SIGN_IN_LINK_TTL,
                email=person.email or "",
                person_id=person.id,
                created_by_account_id=caller.person.account_id,
            )
            await audit(
                db,
                tenant_id=person.tenant_id,
                actor_account_id=caller.person.account_id,
                action="Created sign-in link",
                target=person_target(person.id),
                at=now,
            )
        return {"link": link_url(runtime.web, token)}

    @router.delete("/people/{person_id}/link")
    async def revoke_sign_in_link(person_id: uuid.UUID, caller: PeopleAdmin) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        """Revoke a person's open sign-in links, as when one was sent to the wrong place."""
        now = runtime.now()
        async with runtime.sessionmaker.begin() as db:
            person = await person_of(db, caller, person_id)
            revoked = await web_invites.revoke_open_links(
                db, tenant_id=person.tenant_id, kind="sign_in", now=now, person_id=person.id
            )
            if not revoked:
                raise HTTPException(status_code=404, detail="This person has no open link.")
            await audit(
                db,
                tenant_id=person.tenant_id,
                actor_account_id=caller.person.account_id,
                action="Revoked sign-in link",
                target=person_target(person.id),
                at=now,
            )
        return {"ok": True}

    return router
