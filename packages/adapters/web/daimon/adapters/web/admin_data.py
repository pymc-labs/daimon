"""`/api/admin/data/*`: who may read what, and who is who, in the workspace's data sources.

Scopes, grants, source identities, data admins and redaction sets, for owners and admins,
who manage them as data admins do (`workspace_admin`); the store refuses anyone else with a
message. Every write leaves a row in the workspace's audit trail through the data package.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from daimon.adapters.web.admin_common import names_by_account
from daimon.adapters.web.caller import WebCaller
from daimon.adapters.web.data_common import (
    Manager,
    refused,
    schemas_of,
    store,
    workspace_admin,
)
from daimon.adapters.web.runtime import WebRuntime
from daimon.data import identity, manage, redaction
from daimon.data.identity import IdentityError, IdentityKey
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

ACCOUNT_PREFIX = "account:"


class GrantOut(BaseModel):
    holder: str
    name: str | None
    bound: bool


class ScopeOut(BaseModel):
    id: str
    kind: str
    name: str
    description: str
    bound: bool
    holders: list[GrantOut]


class NewScope(BaseModel):
    id: str = Field(max_length=64)
    kind: str = Field(max_length=20)
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=500)


class Holder(BaseModel):
    holder: str = Field(max_length=200)


class IdentityOut(BaseModel):
    provider: str
    workspace: str
    subject: str
    name: str
    email: str | None
    email_verified: bool
    account_id: uuid.UUID | None
    account_name: str | None
    proof: str | None
    linked_at: datetime | None
    redacted: bool


class CandidateOut(BaseModel):
    provider: str
    workspace: str
    subject: str
    name: str
    email: str
    account_id: uuid.UUID
    account_name: str | None


class IdentityRef(BaseModel):
    provider: str = Field(max_length=100)
    workspace: str = Field(max_length=300)
    subject: str = Field(max_length=300)


class Link(IdentityRef):
    account_id: uuid.UUID


class AdminOut(BaseModel):
    account_id: uuid.UUID
    name: str | None
    created_at: datetime


class Admins(BaseModel):
    admins: list[AdminOut]
    you: bool
    """Whether the caller manages data access: a workspace admin or a data admin."""
    can_grant: bool
    """Whether the caller may name data admins: the same people."""


class Account(BaseModel):
    account_id: uuid.UUID


class Redacted(BaseModel):
    redacted: bool


class SetOut(BaseModel):
    name: str
    repo: str | None
    path: str | None
    ref: str | None
    approved_sha: str | None
    pin_sha: str | None
    pending: bool
    usable: bool
    checked_at: datetime | None
    error: str | None


class SetDetailOut(BaseModel):
    info: SetOut
    rules: redaction.Rules | None
    pending_rules: redaction.Rules | None


class PersonOut(BaseModel):
    account_id: uuid.UUID
    name: str


class Approval(BaseModel):
    sha: str = Field(max_length=100)


def _set(info: redaction.SetInfo) -> SetOut:
    out = SetOut.model_validate(info, from_attributes=True)
    return out.model_copy(update={"error": None if out.error is None else out.error[:300]})


def _account_of(holder: str) -> uuid.UUID | None:
    if not holder.startswith(ACCOUNT_PREFIX):
        return None
    try:
        return uuid.UUID(holder.removeprefix(ACCOUNT_PREFIX))
    except ValueError:
        return None


def _holder_name(holder: str, names: dict[uuid.UUID, str]) -> str | None:
    account = _account_of(holder)
    return None if account is None else names.get(account)


def admin_data_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/admin/data")
    schemas = schemas_of(runtime)

    async def names(caller: WebCaller) -> dict[uuid.UUID, str]:
        async with runtime.sessionmaker() as db:
            return await names_by_account(db, caller.person.tenant_id)

    async def member(caller: WebCaller, account_id: uuid.UUID | None) -> None:
        """Refuse an account outside the caller's workspace."""
        if account_id is not None and account_id not in await names(caller):
            raise HTTPException(404, "No such person here.")

    @router.get("/people")
    async def people(  # pyright: ignore[reportUnusedFunction]
        caller: Manager, q: Annotated[str, Query(max_length=100)] = ""
    ) -> list[PersonOut]:
        """The workspace's people by name, for choosing who holds a scope or links an account."""
        who = await names(caller)
        needle = q.strip().casefold()
        found = sorted((n, a) for a, n in who.items() if needle in n.casefold())
        return [PersonOut(account_id=a, name=n) for n, a in found[:8]]

    # ---- scopes and grants ----------------------------------------------------------------

    @router.get("/scopes")
    async def scopes(caller: Manager) -> list[ScopeOut]:  # pyright: ignore[reportUnusedFunction]
        who = await names(caller)
        async with store(runtime) as conn:
            found = await manage.scopes(conn, schemas, caller.person.tenant_id)
        return [
            ScopeOut(
                id=s.id, kind=s.kind, name=s.name, description=s.description, bound=s.bound,
                holders=[
                    GrantOut(holder=g.holder, name=_holder_name(g.holder, who), bound=g.bound)
                    for g in s.holders
                ],
            )
            for s in found
        ]  # fmt: skip

    @router.post("/scopes", status_code=201)
    async def create_scope(caller: Manager, body: NewScope) -> None:  # pyright: ignore[reportUnusedFunction]
        p = caller.person
        async with store(runtime) as conn:
            try:
                await manage.create_scope(
                    conn, schemas, tenant_id=p.tenant_id, by=p.account_id, id=body.id,
                    kind=body.kind, name=body.name, description=body.description,
                    admin=workspace_admin(caller),
                )  # fmt: skip
            except IdentityError as e:
                raise refused(e) from None

    @router.delete("/scopes/{scope_id}", status_code=204)
    async def delete_scope(caller: Manager, scope_id: str) -> None:  # pyright: ignore[reportUnusedFunction]
        p = caller.person
        async with store(runtime) as conn:
            try:
                await manage.delete_scope(
                    conn,
                    schemas,
                    tenant_id=p.tenant_id,
                    by=p.account_id,
                    id=scope_id,
                    admin=workspace_admin(caller),
                )
            except IdentityError as e:
                raise refused(e) from None

    @router.post("/scopes/{scope_id}/grants", status_code=204)
    async def grant(caller: Manager, scope_id: str, body: Holder) -> None:  # pyright: ignore[reportUnusedFunction]
        p = caller.person
        await member(caller, _account_of(body.holder))
        async with store(runtime) as conn:
            try:
                await manage.grant(
                    conn, schemas, tenant_id=p.tenant_id, by=p.account_id, scope=scope_id,
                    holder=body.holder, admin=workspace_admin(caller),
                )  # fmt: skip
            except IdentityError as e:
                raise refused(e) from None

    @router.delete("/scopes/{scope_id}/grants", status_code=204)
    async def revoke(  # pyright: ignore[reportUnusedFunction]
        caller: Manager, scope_id: str, holder: Annotated[str, Query(max_length=200)]
    ) -> None:
        p = caller.person
        async with store(runtime) as conn:
            try:
                await manage.revoke(
                    conn, schemas, tenant_id=p.tenant_id, by=p.account_id, scope=scope_id,
                    holder=holder, admin=workspace_admin(caller),
                )  # fmt: skip
            except IdentityError as e:
                raise refused(e) from None

    # ---- identity links -------------------------------------------------------------------

    @router.get("/identities")
    async def identities(  # pyright: ignore[reportUnusedFunction]
        caller: Manager, linked: bool | None = None
    ) -> list[IdentityOut]:
        who = await names(caller)
        tenant = caller.person.tenant_id
        async with store(runtime) as conn:
            found = await manage.identities(conn, schemas, tenant, linked=linked)
            if linked is not True:
                # Every open candidate's identity is listed, past the page's limit too.
                have = {(i.provider, i.workspace, i.subject) for i in found}
                wanted = {
                    (c.key.provider, c.key.workspace, c.key.subject)
                    for c in await identity.list_candidates(conn, schemas, tenant)
                } - have
                if wanted:
                    found += [
                        i
                        for i in await manage.identities(
                            conn, schemas, tenant, linked=False, limit=1000
                        )
                        if (i.provider, i.workspace, i.subject) in wanted
                    ]
        return [
            IdentityOut(
                provider=i.provider, workspace=i.workspace, subject=i.subject, name=i.name,
                email=i.email, email_verified=i.email_verified, account_id=i.account_id,
                account_name=who.get(i.account_id) if i.account_id else None, proof=i.proof,
                linked_at=i.linked_at, redacted=i.redacted,
            )
            for i in found
        ]  # fmt: skip

    @router.get("/candidates")
    async def candidates(caller: Manager) -> list[CandidateOut]:  # pyright: ignore[reportUnusedFunction]
        who = await names(caller)
        tenant = caller.person.tenant_id
        async with store(runtime) as conn:
            found = await identity.list_candidates(conn, schemas, tenant)
            known = {
                (i.provider, i.workspace, i.subject): i.name
                for i in await manage.identities(conn, schemas, tenant, linked=False, limit=1000)
            }
        return [
            CandidateOut(
                provider=c.key.provider, workspace=c.key.workspace, subject=c.key.subject,
                name=known.get((c.key.provider, c.key.workspace, c.key.subject), c.key.subject),
                email=c.email, account_id=c.account_id, account_name=who.get(c.account_id),
            )
            for c in found
        ]  # fmt: skip

    @router.post("/links", status_code=204)
    async def link(caller: Manager, body: Link) -> None:  # pyright: ignore[reportUnusedFunction]
        p = caller.person
        await member(caller, body.account_id)
        async with store(runtime) as conn:
            try:
                await identity.admin_link(
                    conn, schemas, tenant_id=p.tenant_id, by=p.account_id,
                    key=IdentityKey(body.provider, body.workspace, body.subject),
                    account_id=body.account_id, admin=workspace_admin(caller),
                )  # fmt: skip
            except IdentityError as e:
                raise refused(e) from None

    @router.post("/links/reject", status_code=204)
    async def reject(caller: Manager, body: Link) -> None:  # pyright: ignore[reportUnusedFunction]
        p = caller.person
        async with store(runtime) as conn:
            try:
                await identity.reject_candidate(
                    conn, schemas, tenant_id=p.tenant_id, by=p.account_id,
                    key=IdentityKey(body.provider, body.workspace, body.subject),
                    account_id=body.account_id, admin=workspace_admin(caller),
                )  # fmt: skip
            except IdentityError as e:
                raise refused(e) from None

    @router.post("/links/unlink", status_code=204)
    async def unlink(caller: Manager, body: IdentityRef) -> None:  # pyright: ignore[reportUnusedFunction]
        p = caller.person
        async with store(runtime) as conn:
            try:
                await identity.unlink(
                    conn, schemas, tenant_id=p.tenant_id, by=p.account_id,
                    key=IdentityKey(body.provider, body.workspace, body.subject),
                    admin=workspace_admin(caller),
                )  # fmt: skip
            except IdentityError as e:
                raise refused(e) from None

    @router.put("/people/{account_id}/redacted", status_code=204)
    async def mark_redacted(  # pyright: ignore[reportUnusedFunction]
        caller: Manager, account_id: uuid.UUID, body: Redacted
    ) -> None:
        p = caller.person
        await member(caller, account_id)
        async with store(runtime) as conn:
            try:
                await redaction.set_redacted(
                    conn,
                    schemas,
                    p.tenant_id,
                    account_id,
                    body.redacted,
                    by=p.account_id,
                    admin=workspace_admin(caller),
                )
            except IdentityError as e:
                raise refused(e) from None

    # ---- data admins ----------------------------------------------------------------------

    @router.get("/admins")
    async def admins(caller: Manager) -> Admins:  # pyright: ignore[reportUnusedFunction]
        p = caller.person
        who = await names(caller)
        async with store(runtime) as conn:
            found = await manage.data_admins(conn, schemas, p.tenant_id)
        you = workspace_admin(caller) or any(a.account_id == p.account_id for a in found)
        return Admins(
            admins=[AdminOut(account_id=a.account_id, name=who.get(a.account_id),
                             created_at=a.created_at) for a in found],
            you=you,
            can_grant=you,
        )  # fmt: skip

    @router.post("/admins", status_code=204)
    async def add_admin(caller: Manager, body: Account) -> None:  # pyright: ignore[reportUnusedFunction]
        p = caller.person
        await member(caller, body.account_id)
        async with store(runtime) as conn:
            try:
                await identity.grant_data_admin(
                    conn, schemas, tenant_id=p.tenant_id, by=p.account_id,
                    account_id=body.account_id, admin=workspace_admin(caller),
                )  # fmt: skip
            except IdentityError as e:
                raise refused(e) from None

    @router.delete("/admins/{account_id}", status_code=204)
    async def remove_admin(caller: Manager, account_id: uuid.UUID) -> None:  # pyright: ignore[reportUnusedFunction]
        p = caller.person
        async with store(runtime) as conn:
            try:
                await identity.revoke_data_admin(
                    conn, schemas, tenant_id=p.tenant_id, by=p.account_id,
                    account_id=account_id, admin=workspace_admin(caller),
                )  # fmt: skip
            except IdentityError as e:
                raise refused(e) from None

    # ---- redaction sets -------------------------------------------------------------------

    @router.get("/redaction")
    async def sets(caller: Manager) -> list[SetOut]:  # pyright: ignore[reportUnusedFunction]
        p = caller.person
        async with store(runtime) as conn:
            try:
                found = await redaction.list_sets(
                    conn, schemas, p.tenant_id, by=p.account_id, admin=workspace_admin(caller)
                )
            except IdentityError as e:
                raise refused(e) from None
        return [_set(s) for s in found]

    @router.get("/redaction/{name}")
    async def show_set(caller: Manager, name: str) -> SetDetailOut:  # pyright: ignore[reportUnusedFunction]
        p = caller.person
        async with store(runtime) as conn:
            try:
                found = await redaction.show_set(
                    conn, schemas, p.tenant_id, name, by=p.account_id, admin=workspace_admin(caller)
                )
            except IdentityError as e:
                raise refused(e) from None
        return SetDetailOut(
            info=_set(found.info), rules=found.rules, pending_rules=found.pending_rules
        )

    @router.post("/redaction/{name}/approve")
    async def approve_set(caller: Manager, name: str, body: Approval) -> SetOut:  # pyright: ignore[reportUnusedFunction]
        p = caller.person
        async with store(runtime) as conn:
            try:
                done = await redaction.approve(
                    conn,
                    schemas,
                    p.tenant_id,
                    name,
                    body.sha,
                    by=p.account_id,
                    admin=workspace_admin(caller),
                )
            except IdentityError as e:
                raise refused(e) from None
        return _set(done)

    return router
