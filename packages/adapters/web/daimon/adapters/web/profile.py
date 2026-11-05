"""`/api/profile/*`: the signed-in person's own data, for every role.

Memory, connected accounts, sign-ins, coding keys and deleting everything. The checks live
in `daimon.core.profile`, `daimon.core.coding_keys` and `daimon.core.privacy`, the ones the
chat privacy panels use; each call reads the account from the session, so none can name
another person.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from anthropic import AsyncAnthropic
from daimon.adapters.web.audit import audit
from daimon.adapters.web.caller import WebCaller, current_caller
from daimon.adapters.web.cookies import clear_cookie, session_cookie_name
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import coding_keys, profile
from daimon.core.authz import Subject
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.privacy import PurgePreview, collect_purge_preview, held_lines
from daimon.core.purge import purge_account
from daimon.core.stores import identity as identity_store
from daimon.core.stores import platform_names
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

Caller = Annotated[WebCaller, Depends(current_caller)]

GONE = "That isn't here any more."
UNAVAILABLE = "This isn't available right now."
DELETE_OFF = "Deleting your data is paused. Ask an admin."
NAME_MISMATCH = "Type your name exactly to confirm."
REMOVED_AGENT = "Removed agent"

_TARGET = "Profile"


class AgentRef(BaseModel):
    id: str
    name: str


class MemoryOut(BaseModel):
    agent: AgentRef
    files: list[str]


class ConnectionOut(BaseModel):
    kind: Literal["microsoft", "slack", "mcp"]
    label: str
    agent: AgentRef | None
    key: str


class SignInOut(BaseModel):
    id: str
    provider: str
    label: str


class ChatAccountOut(BaseModel):
    platform: str
    name: str | None


class KeyOut(BaseModel):
    id: str
    agent: AgentRef | None
    created_at: str
    expires_at: str | None
    bound: bool
    client: str | None


class ProfileOut(BaseModel):
    name: str
    memory: list[MemoryOut]
    connections: list[ConnectionOut]
    sign_ins: list[SignInOut]
    chat_accounts: list[ChatAccountOut]
    keys: list[KeyOut]


class ForgetOut(BaseModel):
    removed: int


class DisconnectBody(BaseModel):
    kind: Literal["microsoft", "slack", "mcp"]
    agent_id: str | None = None
    key: str | None = None


class DeletePreviewOut(BaseModel):
    enabled: bool
    held: list[str]
    kept: list[str]


class DeleteBody(BaseModel):
    confirm: str


def _kept(preview: PurgePreview) -> list[str]:
    """What stays after deleting, as counted phrases."""
    pairs = (
        (preview.retained_shared_conversations.count, "conversation shared with other people"),
        (preview.retained_shared_operations.count, "request in a shared conversation"),
        (preview.retained_accounting_records.count, "billing record"),
    )
    return [f"{n} {label if n == 1 else label + 's'}" for n, label in pairs if n > 0]


def _is_uuid(raw: str | None) -> bool:
    try:
        uuid.UUID(raw or "")
    except ValueError:
        return False
    return True


def _uuid(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(status_code=404, detail=GONE) from None


def profile_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/profile")

    def client() -> AsyncAnthropic:
        found = runtime.anthropic or (runtime.turns.deps.anthropic if runtime.turns else None)
        if found is None:
            raise HTTPException(status_code=503, detail=UNAVAILABLE)
        return found

    async def acting(db: AsyncSession, caller: WebCaller) -> Subject:
        return (await caller.principal(db)).subject

    async def agent_names(caller: WebCaller) -> dict[uuid.UUID, str]:
        tenant_id = caller.person.tenant_id
        found = runtime.anthropic or (runtime.turns.deps.anthropic if runtime.turns else None)
        if found is None:
            return {}
        return {
            derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=a.id): str(
                a.metadata.get(MA_METADATA_KEY_NAME) or a.name
            )
            for a in await runtime.agents.agents(found, tenant_id)
        }

    def ref(names: dict[uuid.UUID, str], agent_id: uuid.UUID) -> AgentRef:
        return AgentRef(id=str(agent_id), name=names.get(agent_id, REMOVED_AGENT))

    async def record(db: AsyncSession, caller: WebCaller, action: str) -> None:
        await audit(
            db,
            tenant_id=caller.person.tenant_id,
            actor_account_id=caller.person.account_id,
            action=action,
            target=_TARGET,
            at=runtime.now(),
        )

    @router.get("")
    async def read(caller: Caller) -> ProfileOut:  # pyright: ignore[reportUnusedFunction]
        person = caller.person
        names = await agent_names(caller)
        found = runtime.anthropic or (runtime.turns.deps.anthropic if runtime.turns else None)
        async with runtime.sessionmaker() as db:
            subject = await acting(db, caller)
            memory = (
                await profile.list_personal_memory(
                    db,
                    found,
                    subject=subject,
                    tenant_id=person.tenant_id,
                    account_id=person.account_id,
                )
                if found is not None
                else []
            )
            connections = await profile.list_connections(
                db, subject=subject, tenant_id=person.tenant_id, account_id=person.account_id
            )
            keys = await coding_keys.list_my_coding_keys(
                db, tenant_id=person.tenant_id, account_id=person.account_id, now=runtime.now()
            )
            principals = [
                p
                for p in await identity_store.list_platform_principals_for_account(
                    db, account_id=person.account_id
                )
                if p.tenant_id == person.tenant_id
            ]
            chat_accounts: list[ChatAccountOut] = []
            for p in principals:
                names_here = await platform_names.get_user_names(
                    db, tenant_id=person.tenant_id, platform=p.platform, user_ids={p.external_id}
                )
                known = names_here.get(p.external_id)
                chat_accounts.append(
                    ChatAccountOut(platform=p.platform, name=known.label if known else None)
                )
        return ProfileOut(
            name=person.display_name,
            memory=[MemoryOut(agent=ref(names, m.agent_id), files=list(m.paths)) for m in memory],
            connections=[
                ConnectionOut(
                    kind=c.kind,
                    label=c.label,
                    agent=ref(names, c.agent_id) if c.agent_id is not None else None,
                    key=c.key,
                )
                for c in connections
            ],
            sign_ins=[
                SignInOut(id=str(i.id), provider=i.provider, label=i.label)
                for i in caller.identities
            ],
            chat_accounts=chat_accounts,
            keys=[
                KeyOut(
                    id=str(k.jti),
                    agent=ref(names, uuid.UUID(k.agent_id)) if _is_uuid(k.agent_id) else None,
                    created_at=k.created_at.isoformat(),
                    expires_at=k.expires_at.isoformat() if k.expires_at else None,
                    bound=k.bound,
                    client=k.client,
                )
                for k in keys
            ],
        )

    @router.post("/memory/{agent_id}/forget")
    async def forget(agent_id: str, caller: Caller) -> ForgetOut:  # pyright: ignore[reportUnusedFunction]
        person = caller.person
        async with runtime.sessionmaker() as db, db.begin():
            try:
                removed = await profile.forget_personal_memory(
                    db,
                    client(),
                    subject=await acting(db, caller),
                    tenant_id=person.tenant_id,
                    account_id=person.account_id,
                    agent_id=_uuid(agent_id),
                )
            except profile.ProfileRefused as err:
                raise HTTPException(status_code=404, detail=str(err)) from err
            await record(db, caller, "Forgot personal memory")
        return ForgetOut(removed=removed)

    @router.post("/connections/disconnect")
    async def disconnect(body: DisconnectBody, caller: Caller) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        person = caller.person
        if body.kind == "mcp":
            if body.agent_id is None or not body.key:
                raise HTTPException(status_code=422, detail=GONE)
            async with runtime.sessionmaker() as db:
                subject = await acting(db, caller)
            gone = await profile.disconnect_mcp_sign_in(
                client(),
                sessionmaker=runtime.sessionmaker,
                subject=subject,
                tenant_id=person.tenant_id,
                account_id=person.account_id,
                agent_id=_uuid(body.agent_id),
                server_url=body.key,
            )
        else:
            async with runtime.sessionmaker() as db, db.begin():
                gone = (
                    await profile.disconnect_token(
                        db,
                        subject=await acting(db, caller),
                        tenant_id=person.tenant_id,
                        account_id=person.account_id,
                        kind=body.kind,
                    )
                    > 0
                )
        if not gone:
            raise HTTPException(status_code=404, detail=GONE)
        async with runtime.sessionmaker() as db, db.begin():
            await record(db, caller, "Disconnected an account")
        return {"ok": True}

    @router.delete("/sign-ins/{identity_id}")
    async def unlink(identity_id: str, caller: Caller) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        async with runtime.sessionmaker() as db, db.begin():
            try:
                done = await profile.unlink_web_sign_in(
                    db,
                    subject=await acting(db, caller),
                    tenant_id=caller.person.tenant_id,
                    person_id=caller.person.id,
                    identity_id=_uuid(identity_id),
                )
            except profile.ProfileRefused as err:
                raise HTTPException(status_code=409, detail=str(err)) from err
            if not done:
                raise HTTPException(status_code=404, detail=GONE)
            await record(db, caller, "Removed a sign-in")
        return {"ok": True}

    @router.delete("/keys/{key_id}")
    async def revoke(key_id: str, caller: Caller) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        async with runtime.sessionmaker() as db, db.begin():
            done = await coding_keys.revoke_my_coding_key(
                db,
                jti=_uuid(key_id),
                tenant_id=caller.person.tenant_id,
                account_id=caller.person.account_id,
                now=runtime.now(),
            )
            if not done:
                raise HTTPException(status_code=404, detail=GONE)
            await record(db, caller, "Revoked a coding key")
        return {"ok": True}

    @router.get("/delete")
    async def delete_preview(caller: Caller) -> DeletePreviewOut:  # pyright: ignore[reportUnusedFunction]
        preview = await collect_purge_preview(
            sm=runtime.sessionmaker, account_id=caller.person.account_id
        )
        return DeletePreviewOut(
            enabled=runtime.settings.privacy.delete_enabled,
            held=held_lines(preview),
            kept=_kept(preview),
        )

    @router.post("/delete")
    async def delete(body: DeleteBody, caller: Caller, response: Response) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        if not runtime.settings.privacy.delete_enabled:
            raise HTTPException(status_code=403, detail=DELETE_OFF)
        if body.confirm.strip().casefold() != caller.person.display_name.strip().casefold():
            raise HTTPException(status_code=422, detail=NAME_MISMATCH)
        async with runtime.sessionmaker() as db:
            subject = await acting(db, caller)
        if subject.via_agent_key:
            raise HTTPException(status_code=403, detail=GONE)
        await purge_account(
            sm=runtime.sessionmaker,
            account_id=caller.person.account_id,
            anthropic=runtime.anthropic,
        )
        clear_cookie(response, runtime.web, name=session_cookie_name(runtime.web))
        return {"ok": True}

    return router
