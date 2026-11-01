"""`/api/invites/*`: the page an invite link opens, and accepting it.

One table holds three kinds of link: an invite brings a new person in, a sign-in link
signs a visitor back in, and an owner link makes a signed-in person the workspace's
owner. Reading a link never uses it; accepting does, once.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.audit import AuditTarget, audit, invite_target, person_target
from daimon.adapters.web.caller import WebCaller, current_caller, ttl_of
from daimon.adapters.web.chat_json import agent_json
from daimon.adapters.web.cookies import new_session_token, session_cookie_name, set_cookie
from daimon.adapters.web.cookies import token_sha256 as sha256_of
from daimon.adapters.web.faces import avatars_of
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.config import WebSettings
from daimon.core.stores import spend_allowances, web_identities, web_invites, web_people
from daimon.core.stores import web_roles as roles_store
from daimon.core.stores import web_sessions as sessions_store
from daimon.core.stores.domain import (
    WebInviteKind,
    WebInviteRole,
    WebInviteRow,
    WebPersonRow,
)
from daimon.core.stores.identity import get_or_create_platform_principal
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

#: How long a sign-in link made for a visitor stays usable.
SIGN_IN_LINK_TTL = timedelta(days=7)
#: The provider a session opened by a link is recorded under.
INVITE_PROVIDER = "invite"


def link_url(web: WebSettings, token: str) -> str:
    return f"{str(web.public_url).rstrip('/')}/invite/{token}"


async def issue_link(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kind: WebInviteKind,
    expires_at: datetime,
    email: str = "",
    role: WebInviteRole = "visitor",
    credit_usd: Decimal = Decimal(0),
    agent_ids: Sequence[str] = (),
    person_id: uuid.UUID | None = None,
    created_by_account_id: uuid.UUID | None = None,
) -> tuple[WebInviteRow, str]:
    """Store a new link and return it with its token, which is shown once."""
    token = secrets.token_urlsafe(24)
    row = await web_invites.create_invite(
        db,
        tenant_id=tenant_id,
        token_sha256=sha256_of(token),
        kind=kind,
        expires_at=expires_at,
        email=email,
        role=role,
        credit_usd=credit_usd,
        agent_ids=agent_ids,
        person_id=person_id,
        created_by_account_id=created_by_account_id,
    )
    return row, token


def _gone() -> HTTPException:
    return HTTPException(status_code=404, detail="This link has expired.")


def _usable(invite: WebInviteRow | None, now: datetime) -> WebInviteRow:
    if (
        invite is None
        or invite.accepted_at is not None
        or invite.revoked_at is not None
        or invite.expires_at <= now
    ):
        raise _gone()
    return invite


async def _workspace_name(db: AsyncSession, invite: WebInviteRow) -> str:
    """A workspace has no name of its own on the web; its people carry the platform's."""
    people = await web_people.list_people(db, tenant_id=invite.tenant_id)
    return next((p.workspace_name for p in people if p.workspace_name), "Daimon")


async def _sender(db: AsyncSession, invite: WebInviteRow) -> str:
    if invite.created_by_account_id is None:
        return "Daimon"
    person = await web_people.get_person_for_account(
        db, tenant_id=invite.tenant_id, account_id=invite.created_by_account_id
    )
    return person.display_name if person is not None else "An admin"


async def _opened_by(
    db: AsyncSession, invite: WebInviteRow
) -> tuple[WebInviteRow, WebPersonRow | None]:
    """The invite whose agents and credit a link stands for: a sign-in link, its visitor's."""
    if invite.kind != "sign_in" or invite.person_id is None:
        return invite, None
    person = await web_people.get_person(db, tenant_id=invite.tenant_id, person_id=invite.person_id)
    if person is None or person.invite_id is None:
        return invite, person
    origin = await web_invites.get_invite(
        db, tenant_id=invite.tenant_id, invite_id=person.invite_id
    )
    return origin or invite, person


def invites_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/invites")

    async def agents_of(tenant_id: uuid.UUID, agent_ids: Sequence[str]) -> list[dict[str, Any]]:
        """The invite's agents as cards; the workspace's default when it names none."""
        if runtime.turns is None:
            return []
        deps = runtime.turns.deps
        listed = await runtime.agents.agents(deps.anthropic, tenant_id)
        picked: list[BetaManagedAgentsAgent] = [a for a in listed if a.id in agent_ids]
        if not picked:
            default = deps.deployment_default.agent_name
            picked = [a for a in listed if a.name == default][:1]
        avatars = await avatars_of(runtime, tenant_id, picked)
        return [agent_json(a, avatar=avatars.get(a.id, "")) for a in picked]

    @router.get("/{token}")
    async def preview(token: str) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        async with runtime.sessionmaker() as db:
            invite = _usable(
                await web_invites.find_by_token(db, token_sha256=sha256_of(token)), runtime.now()
            )
            origin, person = await _opened_by(db, invite)
            workspace, sender = await _workspace_name(db, invite), await _sender(db, invite)
        email = person.email or "" if person is not None else invite.email
        return {
            "workspace": workspace,
            "from": sender,
            "email": email,
            "kind": invite.kind,
            "agents": await agents_of(invite.tenant_id, origin.agent_ids),
            "credit_usd": float(origin.credit_usd),
            "expires_at": invite.expires_at.isoformat(),
        }

    @router.post("/{token}/accept")
    async def accept(token: str, request: Request, response: Response) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        now, digest = runtime.now(), sha256_of(token)
        async with runtime.sessionmaker() as db:
            found = _usable(await web_invites.find_by_token(db, token_sha256=digest), now)
        caller = await _signed_in(request) if found.kind == "owner" else None
        if found.kind == "owner" and (caller is None or caller.person.tenant_id != found.tenant_id):
            raise HTTPException(
                status_code=401, detail="Sign in to this workspace, then open the link again."
            )
        # Refused before the link is claimed, so it stays usable by the person it was meant for.
        if caller is not None and caller.person.kind == "visitor":
            raise HTTPException(status_code=403, detail="A visitor can't become an owner.")
        async with runtime.sessionmaker.begin() as db:
            invite = await web_invites.claim_invite(db, token_sha256=digest, now=now)
            if invite is None:
                raise _gone()
            if invite.kind == "owner":
                assert caller is not None
                await roles_store.set_workspace_role(
                    db,
                    tenant_id=invite.tenant_id,
                    account_id=caller.person.account_id,
                    role="owner",
                    granted_by_account_id=None,
                )
                await web_invites.set_accepted_by(
                    db, invite_id=invite.id, person_id=caller.person.id
                )
                await audit(
                    db,
                    tenant_id=invite.tenant_id,
                    actor_account_id=caller.person.account_id,
                    action="Accepted owner link",
                    target=person_target(caller.person.id),
                    at=now,
                )
                log.info("web.invite.owner", tenant_id=str(invite.tenant_id))
                return {"ok": True}
            person = await (
                _sign_in(db, invite) if invite.kind == "sign_in" else _join(db, invite, now)
            )
            await web_invites.set_accepted_by(db, invite_id=invite.id, person_id=person.id)
            if invite.kind == "sign_in":
                await audit(
                    db,
                    tenant_id=invite.tenant_id,
                    actor_account_id=person.account_id,
                    action="Used sign-in link",
                    target=person_target(person.id),
                    at=now,
                )
            if invite.kind == "invite":
                agents = _agent_names(invite)
                await audit(
                    db,
                    tenant_id=invite.tenant_id,
                    actor_account_id=person.account_id,
                    action="Accepted invite",
                    target=AuditTarget(agents) if agents else invite_target(invite.id),
                    at=now,
                )
            session_token = new_session_token()
            await sessions_store.create_session(
                db,
                token_sha256=sha256_of(session_token),
                provider=INVITE_PROVIDER,
                issuer="",
                subject=str(person.invite_id or person.id),
                people=[person],
                ttl=ttl_of(runtime),
                now=now,
            )
        set_cookie(
            response,
            runtime.web,
            name=session_cookie_name(runtime.web),
            value=session_token,
            max_age=ttl_of(runtime),
        )
        log.info("web.invite.accepted", tenant_id=str(invite.tenant_id), kind=invite.kind)
        return {"ok": True}

    def _agent_names(invite: WebInviteRow) -> str:
        agents = (runtime.agents.cached(invite.tenant_id, a) for a in invite.agent_ids)
        return ", ".join(a.name for a in agents if a is not None)

    async def _sign_in(db: AsyncSession, invite: WebInviteRow) -> WebPersonRow:
        person = (
            None
            if invite.person_id is None
            else await web_people.get_person(
                db, tenant_id=invite.tenant_id, person_id=invite.person_id
            )
        )
        if person is None:
            raise _gone()
        if person.status == "suspended":
            raise HTTPException(status_code=403, detail="This account is suspended.")
        return person

    async def _join(db: AsyncSession, invite: WebInviteRow, now: datetime) -> WebPersonRow:
        """The person an invite brings in: their own account, sign-in and allowance."""
        person_id = uuid.uuid4()
        principal = await get_or_create_platform_principal(
            db, tenant_id=invite.tenant_id, platform="web", external_id=str(person_id)
        )
        person = await web_people.create_invited(
            db,
            person_id=person_id,
            tenant_id=invite.tenant_id,
            account_id=principal.account_id,
            kind=invite.role,
            display_name=invite.email.split("@")[0] or "Guest",
            workspace_name=await _workspace_name(db, invite),
            email=invite.email or None,
            invite_id=invite.id,
            invited_by_account_id=invite.created_by_account_id,
        )
        await web_identities.link_identity(
            db,
            tenant_id=invite.tenant_id,
            person_id=person.id,
            provider=INVITE_PROVIDER,
            issuer="",
            subject=str(invite.id),
            label=invite.email,
            groups=(),
        )
        if invite.credit_usd > 0:
            await spend_allowances.create_allowance(
                db,
                tenant_id=invite.tenant_id,
                subject_kind="invite",
                subject_id=str(invite.id),
                agent_id=None,
                amount_usd=invite.credit_usd,
                window="total" if invite.role == "visitor" else "month",
                starts_at=now,
                created_by_account_id=invite.created_by_account_id,
            )
        return person

    async def _signed_in(request: Request) -> WebCaller | None:
        try:
            return await current_caller(request, Response())
        except HTTPException:
            return None

    return router
