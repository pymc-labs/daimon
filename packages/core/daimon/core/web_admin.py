"""Admin changes to a workspace's web access, shared by the web app and the chat tools.

Each change is audited in the transaction that makes it. A refusal is a `WebAdminError`
carrying the HTTP status the web app answers with and the text both surfaces show.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core import workspace_settings as access_settings
from daimon.core.authz import Subject, build_agent_ref
from daimon.core.config import WebSettings
from daimon.core.errors import DaimonError
from daimon.core.permissions import agent_permissions
from daimon.core.stores import tenants, web_audit, web_invites, web_people, web_roles
from daimon.core.stores import web_agent_grants as grants_store
from daimon.core.stores import web_workspace_settings as workspace_settings
from daimon.core.stores.access_policy import PolicyBusyError, load_access_policy
from daimon.core.stores.domain import (
    WebAgentWho,
    WebInviteKind,
    WebInviteRole,
    WebInviteRow,
    WebPersonRow,
    WebPersonStatus,
)
from daimon.core.stores.web_audit import INVITE_LABEL, PERSON_LABEL
from daimon.core.web_roles_rules import ROLE_WORDS, Role, at_or_above, is_admin, role_of
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SIGN_IN_LINK_TTL = timedelta(days=7)
MAX_INVITE_AGENTS = 50
MAX_ACCESS_GROUPS = 100
MAX_INVITE_CREDIT_USD = 100_000
MAX_INVITE_DAYS = 90
_BUSY = "Settings are being changed. Try again."
ModuleName = Literal["chat", "agents", "channels", "routines", "credits", "integrations"]


class WebAdminError(DaimonError):
    """A refused change. `status` is what the web app answers with."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class WebAdmin:
    """Who makes the change: an account in the workspace, with the web role it holds."""

    tenant_id: uuid.UUID
    account_id: uuid.UUID
    role: Role
    person_id: uuid.UUID | None = None
    """Their web person, when they have one: what a change checks to keep them from
    suspending themselves."""


@dataclass(frozen=True, slots=True)
class AuditTarget:
    """What a change was about. A person or invite is an id, so erasing them erases the
    name or address too; anything else is a label that names no one."""

    label: str
    person_id: uuid.UUID | None = None
    invite_id: uuid.UUID | None = None


def person_target(person_id: uuid.UUID) -> AuditTarget:
    return AuditTarget(PERSON_LABEL, person_id=person_id)


def invite_target(invite_id: uuid.UUID) -> AuditTarget:
    return AuditTarget(INVITE_LABEL, invite_id=invite_id)


def usd(amount: Decimal | float) -> str:
    return f"${Decimal(str(amount)):,.2f}"


async def audit(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_account_id: uuid.UUID | None,
    action: str,
    target: AuditTarget | str,
    at: datetime,
    detail: str | None = None,
) -> None:
    """Record a change in the same transaction as the change itself."""
    what = AuditTarget(target) if isinstance(target, str) else target
    await web_audit.record(
        db,
        tenant_id=tenant_id,
        actor_account_id=actor_account_id,
        action=action,
        target=what.label,
        target_person_id=what.person_id,
        target_invite_id=what.invite_id,
        detail=detail,
        at=at,
    )


def token_sha256(token: str) -> bytes:
    """The only form of a session or link token the database ever holds."""
    return hashlib.sha256(token.encode("utf-8")).digest()


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
    allow_files: bool = False,
    agent_ids: Sequence[str] = (),
    person_id: uuid.UUID | None = None,
    created_by_account_id: uuid.UUID | None = None,
) -> tuple[WebInviteRow, str]:
    """Store a new link and return it with its token, which is shown once."""
    token = secrets.token_urlsafe(24)
    row = await web_invites.create_invite(
        db,
        tenant_id=tenant_id,
        token_sha256=token_sha256(token),
        kind=kind,
        expires_at=expires_at,
        email=email,
        role=role,
        credit_usd=credit_usd,
        allow_files=allow_files,
        agent_ids=agent_ids,
        person_id=person_id,
        created_by_account_id=created_by_account_id,
    )
    return row, token


# Invites


async def create_invite(
    db: AsyncSession,
    admin: WebAdmin,
    now: datetime,
    *,
    email: str,
    role: WebInviteRole,
    agent_ids: Sequence[str],
    agents: Mapping[str, str],
    credit_usd: Decimal,
    days: int,
    allow_files: bool,
) -> tuple[WebInviteRow, str]:
    """Invite someone by email. `agents` maps the workspace's agent ids to their names."""
    picked = list(dict.fromkeys(agent_ids))
    if len(agent_ids) > MAX_INVITE_AGENTS:
        raise WebAdminError(f"Pick at most {MAX_INVITE_AGENTS} agents.", status=422)
    if not 1 <= days <= MAX_INVITE_DAYS:
        raise WebAdminError(f"Give 1 to {MAX_INVITE_DAYS} days.", status=422)
    if not 0 <= credit_usd <= MAX_INVITE_CREDIT_USD or credit_usd != credit_usd.quantize(
        Decimal("0.01")
    ):
        raise WebAdminError(
            f"Give a credit from 0 to {MAX_INVITE_CREDIT_USD:,} dollars, in cents.", status=422
        )
    if any(a not in agents for a in picked):
        raise WebAdminError("Pick agents from this workspace.", status=422)
    if role == "visitor" and not picked:
        raise WebAdminError("Pick at least one agent.", status=422)
    invite, token = await issue_link(
        db,
        tenant_id=admin.tenant_id,
        kind="invite",
        expires_at=now + timedelta(days=days),
        email=email,
        role=role,
        credit_usd=credit_usd,
        allow_files=allow_files,
        agent_ids=picked,
        created_by_account_id=admin.account_id,
    )
    await audit(
        db,
        tenant_id=admin.tenant_id,
        actor_account_id=admin.account_id,
        action="Created invite",
        target=invite_target(invite.id),
        detail=", ".join(
            [
                ROLE_WORDS[role],
                *(agents[a] for a in picked),
                usd(credit_usd),
                *(["Files on"] if invite.allow_files else []),
            ]
        ),
        at=now,
    )
    return invite, token


async def set_invite_files(
    db: AsyncSession, admin: WebAdmin, now: datetime, invite_id: uuid.UUID, *, allow: bool
) -> WebInviteRow:
    """Turn files on or off for a visitor invite, whether or not it was accepted."""
    before = await web_invites.get_invite(db, tenant_id=admin.tenant_id, invite_id=invite_id)
    invite = await web_invites.set_allow_files(
        db, tenant_id=admin.tenant_id, invite_id=invite_id, allow=allow
    )
    if invite is None:
        raise WebAdminError("This invite no longer exists.", status=404)
    if before is not None and before.allow_files != invite.allow_files:
        await audit(
            db,
            tenant_id=admin.tenant_id,
            actor_account_id=admin.account_id,
            action="Turned files on" if invite.allow_files else "Turned files off",
            target=invite_target(invite.id),
            at=now,
        )
    return invite


async def revoke_invite(
    db: AsyncSession, admin: WebAdmin, now: datetime, invite_id: uuid.UUID
) -> None:
    invite = await web_invites.revoke_invite(
        db, tenant_id=admin.tenant_id, invite_id=invite_id, now=now
    )
    if invite is None:
        raise WebAdminError("This invite no longer exists.", status=404)
    await audit(
        db,
        tenant_id=invite.tenant_id,
        actor_account_id=admin.account_id,
        action="Revoked invite",
        target=invite_target(invite.id),
        at=now,
    )


# People


async def person_in(db: AsyncSession, tenant_id: uuid.UUID, person_id: uuid.UUID) -> WebPersonRow:
    person = await web_people.get_person(db, tenant_id=tenant_id, person_id=person_id)
    if person is None:
        raise WebAdminError("This person isn't in the workspace.", status=404)
    return person


async def _set_role(db: AsyncSession, admin: WebAdmin, target: WebPersonRow, role: Role) -> None:
    if role == "visitor":
        raise WebAdminError("A member can't become a visitor. Suspend them instead.", status=409)
    if await role_of(db, target) == "visitor":
        await web_people.update_person(db, person_id=target.id, kind="member")
    if is_admin(role):
        await web_roles.set_workspace_role(
            db,
            tenant_id=target.tenant_id,
            account_id=target.account_id,
            role="owner" if role == "owner" else "admin",
            granted_by_account_id=admin.account_id,
        )
    else:
        await web_roles.delete_workspace_role(
            db, tenant_id=target.tenant_id, account_id=target.account_id
        )


async def change_person(
    db: AsyncSession,
    admin: WebAdmin,
    now: datetime,
    person_id: uuid.UUID,
    *,
    role: Role | None = None,
    status: WebPersonStatus | None = None,
) -> WebPersonRow:
    """Change a person's role or suspend or restore them. Only an owner touches an owner."""
    tenant_id = admin.tenant_id
    target = await person_in(db, tenant_id, person_id)
    was = await role_of(db, target)
    if "owner" in (was, role) and admin.role != "owner":
        raise WebAdminError("Only an owner can do that.", status=403)
    if role is not None and role != was:
        await _set_role(db, admin, target, role)
        target = await person_in(db, tenant_id, person_id)
        if await role_of(db, target) != role:
            raise WebAdminError("This role comes from their chat workspace.", status=409)
        if was == "owner" and not await web_roles.has_active_owner(db, tenant_id=tenant_id):
            raise WebAdminError("A workspace needs an owner.", status=409)
        await audit(
            db,
            tenant_id=tenant_id,
            actor_account_id=admin.account_id,
            action="Changed role",
            target=person_target(target.id),
            detail=f"{ROLE_WORDS[was]} to {ROLE_WORDS[role]}",
            at=now,
        )
    if status is not None and status != target.status:
        if target.id == admin.person_id:
            raise WebAdminError("You can't suspend yourself.", status=409)
        await web_people.update_person(db, person_id=target.id, status=status)
        if status == "suspended":
            # A suspended person's open sign-in links must not outlive the suspension.
            await web_invites.revoke_open_links(
                db, tenant_id=tenant_id, kind="sign_in", now=now, person_id=target.id
            )
        await audit(
            db,
            tenant_id=tenant_id,
            actor_account_id=admin.account_id,
            action="Suspended" if status == "suspended" else "Restored",
            target=person_target(target.id),
            at=now,
        )
    return await person_in(db, tenant_id, person_id)


async def issue_sign_in_link(
    db: AsyncSession, admin: WebAdmin, now: datetime, person_id: uuid.UUID
) -> tuple[WebPersonRow, str, uuid.UUID]:
    """A one-time sign-in link for a visitor, who has no other way back in. Returns the
    person, the token and the link's id."""
    person = await person_in(db, admin.tenant_id, person_id)
    if person.kind != "visitor":
        raise WebAdminError("This person isn't in the workspace.", status=404)
    # A link signs in as the person, so it must never hand a caller a higher role.
    if at_or_above(await role_of(db, person), admin.role):
        raise WebAdminError("Ask an owner.", status=403)
    link, token = await issue_link(
        db,
        tenant_id=person.tenant_id,
        kind="sign_in",
        expires_at=now + SIGN_IN_LINK_TTL,
        email=person.email or "",
        person_id=person.id,
        created_by_account_id=admin.account_id,
    )
    await audit(
        db,
        tenant_id=person.tenant_id,
        actor_account_id=admin.account_id,
        action="Created sign-in link",
        target=person_target(person.id),
        at=now,
    )
    return person, token, link.id


async def revoke_issued_link(
    db: AsyncSession, admin: WebAdmin, now: datetime, link_id: uuid.UUID
) -> None:
    """Revoke the one link just issued, as when it could not be delivered. The person's
    other open links stay."""
    link = await web_invites.revoke_link(db, tenant_id=admin.tenant_id, link_id=link_id, now=now)
    if link is None or link.person_id is None:
        return
    await audit(
        db,
        tenant_id=link.tenant_id,
        actor_account_id=admin.account_id,
        action="Revoked sign-in link",
        target=person_target(link.person_id),
        at=now,
    )


async def revoke_sign_in_link(
    db: AsyncSession, admin: WebAdmin, now: datetime, person_id: uuid.UUID
) -> None:
    """Revoke a person's open sign-in links, as when one was sent to the wrong place."""
    person = await person_in(db, admin.tenant_id, person_id)
    revoked = await web_invites.revoke_open_links(
        db, tenant_id=person.tenant_id, kind="sign_in", now=now, person_id=person.id
    )
    if not revoked:
        raise WebAdminError("This person has no open link.", status=404)
    await audit(
        db,
        tenant_id=person.tenant_id,
        actor_account_id=admin.account_id,
        action="Revoked sign-in link",
        target=person_target(person.id),
        at=now,
    )


# Agent access


@dataclass(frozen=True, slots=True)
class AgentAccess:
    agent_id: str
    name: str
    web: bool
    who: WebAgentWho
    groups: list[str]
    fixed_to: str | None
    """The channel an agent is kept to, which keeps it off the web."""


async def agent_access_rows(
    db: AsyncSession, tenant_id: uuid.UUID, agents: Sequence[BetaManagedAgentsAgent]
) -> list[AgentAccess]:
    """Each agent's web access, with the channel it is kept to, if any."""
    policy = await load_access_policy(db, tenant_id=tenant_id)
    grants = await grants_store.list_grants(db, tenant_id=tenant_id)
    rows: list[AgentAccess] = []
    for agent in agents:
        permissions = agent_permissions(policy, build_agent_ref(agent.name, agent.metadata).names)
        channels = sorted(c for rule in permissions.runs_in for c in rule)
        fixed = permissions.home or (channels[0] if channels else None)
        grant = grants.get(agent.id)
        rows.append(
            AgentAccess(
                agent_id=agent.id,
                name=agent.name,
                web=fixed is None and (grant is None or grant.web),
                who="everyone" if grant is None else grant.who,
                groups=[] if grant is None else list(grant.groups),
                fixed_to=fixed,
            )
        )
    return rows


async def set_agent_access(
    db: AsyncSession,
    admin: WebAdmin,
    now: datetime,
    current: AgentAccess,
    *,
    web: bool | None = None,
    who: WebAgentWho | None = None,
    groups: Sequence[str] | None = None,
) -> AgentAccess:
    """Turn web chat on or off for an agent and choose who may use it there."""
    if groups is not None and len(groups) > MAX_ACCESS_GROUPS:
        raise WebAdminError(f"Pick at most {MAX_ACCESS_GROUPS} groups.", status=422)
    if current.fixed_to is not None:
        raise WebAdminError("This agent is kept to its channel.", status=403)
    new_web = current.web if web is None else web
    new_who: WebAgentWho = current.who if who is None else who
    new_groups = current.groups if groups is None else list(groups)
    await grants_store.set_grant(
        db,
        tenant_id=admin.tenant_id,
        agent_id=current.agent_id,
        web=new_web,
        who=new_who,
        groups=new_groups,
        now=now,
    )
    if new_web != current.web:
        await audit(
            db,
            tenant_id=admin.tenant_id,
            actor_account_id=admin.account_id,
            action="Turned on web chat" if new_web else "Turned off web chat",
            target=current.name,
            at=now,
        )
    if (new_who, sorted(set(new_groups))) != (current.who, sorted(current.groups)):
        detail = {"everyone": "Everyone", "invites": "Invited people"}.get(new_who)
        await audit(
            db,
            tenant_id=admin.tenant_id,
            actor_account_id=admin.account_id,
            action="Changed web access",
            target=current.name,
            detail=detail or ", ".join(new_groups) or "No groups",
            at=now,
        )
    return AgentAccess(
        current.agent_id,
        current.name,
        new_web,
        new_who,
        list(dict.fromkeys(new_groups)),
        current.fixed_to,
    )


# Settings


def _on(value: bool) -> str:
    return "On" if value else "Off"


async def change_settings(
    sessionmaker: async_sessionmaker[AsyncSession],
    admin: WebAdmin,
    now: datetime,
    *,
    agents: Mapping[str, str],
    modules: Mapping[ModuleName, bool] | None = None,
    default_agent: str | None = None,
    everyone_invokes: bool | None = None,
    invoker_people: Sequence[uuid.UUID] = (),
    dm_enabled: bool | None = None,
    dm_memory_read_only: bool | None = None,
    plain_text_mentions: bool | None = None,
) -> list[tuple[str, str]]:
    """Change the workspace's web modules, web chat agent and who may start turns, and the
    direct-message and mention rules. Returns what changed, as (setting, new value)."""
    tenant_id = admin.tenant_id
    subject = Subject(is_admin=is_admin(admin.role), platform_user_id=str(admin.account_id))
    changes: list[tuple[str, str]] = []
    try:
        async with sessionmaker.begin() as db:
            chosen = await workspace_settings.get_settings(db, tenant_id=tenant_id)
            modules_off = set(chosen.modules_off)
            for module, on in (modules or {}).items():
                if on == (module in modules_off):
                    (modules_off.discard if on else modules_off.add)(module)
                    changes.append(("Modules", f"{module.title()} {'on' if on else 'off'}"))
            default = chosen.default_agent
            if default_agent is not None and default_agent != default:
                if default_agent not in agents:
                    raise WebAdminError("Pick an agent from this workspace.", status=422)
                default = default_agent
                changes.append(("Web chat agent", agents[default_agent]))
            await workspace_settings.save_settings(
                db,
                tenant_id=tenant_id,
                modules_off=sorted(modules_off),
                default_agent=default,
                now=now,
            )
            tenant = await tenants.get_tenant(db, tenant_id)
            platform = tenant.platform if tenant else ""
            if everyone_invokes is not None:
                named = await access_settings.set_invoker_people(
                    db,
                    tenant_id=tenant_id,
                    subject=subject,
                    person_ids=None if everyone_invokes else list(invoker_people),
                )
                changes.append(
                    (
                        "Who can use the agents",
                        f"{len(named.people)} people" if named.limited else "Everyone",
                    )
                )
            if dm_enabled is not None:
                await access_settings.set_dms(
                    db, tenant_id=tenant_id, subject=subject, platform=platform, enabled=dm_enabled
                )
                changes.append(("Direct messages", _on(dm_enabled)))
            if dm_memory_read_only is not None:
                await access_settings.set_dm_memory_read_only(
                    db, tenant_id=tenant_id, subject=subject, enabled=dm_memory_read_only
                )
                changes.append(("Read-only memory in direct messages", _on(dm_memory_read_only)))
            if plain_text_mentions is not None:
                await access_settings.set_plain_text_mentions(
                    db,
                    tenant_id=tenant_id,
                    subject=subject,
                    platform=platform,
                    enabled=plain_text_mentions,
                )
                changes.append(("Typed mentions", _on(plain_text_mentions)))
            for target, detail in changes:
                await audit(
                    db,
                    tenant_id=tenant_id,
                    actor_account_id=admin.account_id,
                    action="Changed setting",
                    target=target,
                    detail=detail,
                    at=now,
                )
    except access_settings.SettingRefused as exc:
        raise WebAdminError(str(exc), status=422) from exc
    except PolicyBusyError as exc:
        raise WebAdminError(_BUSY, status=409) from exc
    return changes
