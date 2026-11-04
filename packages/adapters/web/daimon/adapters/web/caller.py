"""Who is making a request: the session cookie, resolved to a person, role and rights."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from functools import cache
from typing import Annotated

from daimon.adapters.web.cookies import session_token, token_sha256
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.config import WebModuleKey
from daimon.core.stores import tenants as tenants_store
from daimon.core.stores import web_identities as identities_store
from daimon.core.stores import web_invites as invites_store
from daimon.core.stores import web_people as people_store
from daimon.core.stores import web_sessions as sessions_store
from daimon.core.stores import web_workspace_settings as workspace_settings
from daimon.core.stores.domain import WebIdentityRow, WebPersonRow, WebSessionRow
from daimon.core.web_identity import WebPrincipal, principal_of
from daimon.core.web_roles_rules import (
    Capability,
    Role,
    capabilities_for,
    chat_standings,
    has_left,
    modules_for,
    roles_of,
)
from daimon.core.web_roles_rules import (
    role_of as role_of,
)
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

# A visit is written at most this often.
_TOUCH_EVERY = timedelta(minutes=1)
# A sign-in is checked against the database again this often: its method still on for the
# workspace, the workspace not archived. Roles, suspension and chat membership are read
# on every request.
_RECHECK_EVERY = timedelta(minutes=15)
#: What a person who left the chat workspace reads there.
LEFT_WORKSPACE = "You're no longer in this workspace."


@dataclass(frozen=True)
class WebCaller:
    """The signed-in person for one request, with what they may do right now."""

    session: WebSessionRow
    person: WebPersonRow
    role: Role
    modules: tuple[WebModuleKey, ...]
    capabilities: frozenset[Capability]
    identities: tuple[WebIdentityRow, ...]
    # The person's chat account is gone from the workspace (only through the dependencies
    # that let them in to move elsewhere).
    left: bool = False
    _principals: list[WebPrincipal] = field(
        default_factory=list[WebPrincipal], compare=False, repr=False
    )

    async def principal(self, db: AsyncSession) -> WebPrincipal:
        """Who this person acts as, read once per request however often a route asks."""
        if not self._principals:
            groups = frozenset(g for identity in self.identities for g in identity.groups)
            self._principals.append(await principal_of(db, self.person, self.role, groups=groups))
        return self._principals[0]


def groups_of(caller: WebCaller) -> frozenset[str]:
    """The groups the person's sign-ins carry, which spend allowances can name."""
    return frozenset(g for identity in caller.identities for g in identity.groups)


def ttl_of(runtime: WebRuntime) -> timedelta:
    return timedelta(days=runtime.web.session_ttl_days)


async def may_attach(session: AsyncSession, person: WebPersonRow, role: Role) -> bool:
    """Whether the person may attach files in chat: a member always, a visitor only while
    their invite allows it. No invite, or a revoked one, is no files."""
    if role != "visitor":
        return True
    if person.invite_id is None:
        return False
    return await invites_store.invite_allows_files(
        session, tenant_id=person.tenant_id, invite_id=person.invite_id
    )


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="Sign in to continue.")


async def signin_allowed_in(db: AsyncSession, *, tenant_id: uuid.UUID) -> bool:
    """Whether a sign-in may work in the workspace now: it is still there."""
    tenant = await tenants_store.get_tenant(db, tenant_id)
    return tenant is not None and tenant.archived_at is None


#: What every refused capability reads as, whoever asks and whatever they asked for.
NOT_ALLOWED = "Ask an admin or an owner."
#: What a visitor whose invite leaves files off reads when they try to attach one.
FILES_OFF = "Files are off for your account."


@cache
def requires(capability: Capability) -> Callable[[WebCaller], Awaitable[WebCaller]]:
    """Dependency: the signed-in person, if their role and the workspace's modules give them
    `capability`; 403 naming who can help otherwise. One per capability, so a route that
    needs it twice resolves the caller once."""

    async def dependency(caller: Annotated[WebCaller, Depends(current_caller)]) -> WebCaller:
        if capability not in caller.capabilities:
            raise HTTPException(status_code=403, detail=NOT_ALLOWED)
        return caller

    return dependency


async def current_caller(request: Request, response: Response) -> WebCaller:
    """Dependency: the signed-in person, or 401 (no session) / 403 (suspended, or gone
    from the chat workspace)."""
    return await _caller(request, allow_suspended=False)


async def caller_who_may_be_suspended(request: Request, response: Response) -> WebCaller:
    """Dependency for what a suspended person, or one who left the chat workspace, must
    still do: leave for another workspace."""
    return await _caller(request, allow_suspended=True)


async def caller_who_may_have_left(request: Request, response: Response) -> WebCaller:
    """Dependency for the app's first read, which moves someone who left the chat
    workspace on to another; a suspended person is still refused."""
    return await _caller(request, allow_suspended=False, allow_left=True)


async def _caller(
    request: Request, *, allow_suspended: bool, allow_left: bool | None = None
) -> WebCaller:
    runtime: WebRuntime = request.app.state.runtime
    token = session_token(request, runtime.web)
    if not token:
        raise _unauthorized()
    now = runtime.now()
    digest = token_sha256(token)
    async with runtime.sessionmaker() as db, db.begin():
        found = await sessions_store.get_live_session_with_person(db, token_sha256=digest, now=now)
        if found is None:
            raise _unauthorized()
        live, person = found
        if person.status == "suspended" and not allow_suspended:
            raise HTTPException(status_code=403, detail="This account is suspended.")
        chosen = await workspace_settings.get_settings(db, tenant_id=person.tenant_id)
        # The lifetime counts from sign-in, so lowering the setting also ends older sessions.
        ended = now >= live.created_at + ttl_of(runtime)
        if not ended and now - live.roles_checked_at >= _RECHECK_EVERY:
            ended = not await signin_allowed_in(db, tenant_id=live.tenant_id)
            if not ended:
                await sessions_store.mark_checked(db, session_id=live.id, now=now)
        if not ended:
            if now - live.last_seen_at >= _TOUCH_EVERY:
                await sessions_store.touch(db, session_id=live.id, now=now)
                await people_store.update_person(db, person_id=person.id, last_seen_at=now)
            standings = await chat_standings(db, [person])
            left = has_left(standings, person)
            if left and not (allow_suspended if allow_left is None else allow_left):
                raise HTTPException(status_code=403, detail=LEFT_WORKSPACE)
            identities = await identities_store.list_for_person(db, person_id=person.id)
            role = (await roles_of(db, [person], standings))[person.id]
            # A workspace can turn a module off, never on; admin always stays.
            enabled: list[WebModuleKey] = [
                m for m in runtime.web.modules if m not in chosen.modules_off
            ]
            modules = tuple(modules_for(role, enabled))
            return WebCaller(
                session=live,
                person=person,
                role=role,
                modules=modules,
                capabilities=capabilities_for(
                    role, modules, files=await may_attach(db, person, role)
                ),
                identities=tuple(identities),
                left=left,
            )
        await sessions_store.delete_session(db, token_sha256=digest)
    # Raised after the block, which commits the delete; raising inside would roll it back.
    raise _unauthorized()
