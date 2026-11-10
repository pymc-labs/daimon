"""Web roles, modules and capabilities: the rules the web app and the chat tools share."""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Literal

from daimon.core.config import WebModuleKey
from daimon.core.stores import accounts as accounts_store
from daimon.core.stores import web_people as people_store
from daimon.core.stores import web_roles as roles_store
from daimon.core.stores import workspace_members as members_store
from daimon.core.stores.domain import ChatStandingRow, WebPersonKind, WebPersonRow, WebRoleGrant
from daimon.core.stores.domain import Role as AccountRole
from daimon.core.web_identity import WebRoleName
from sqlalchemy.ext.asyncio import AsyncSession

Role = WebRoleName
Capability = Literal[
    "chat.use",
    "files.attach",
    "agents.view",
    "agents.edit",
    "agents.create",
    "channels.view",
    "channels.manage",
    "routines.view",
    "routines.manage",
    "credits.view_own",
    "credits.manage",
    "integrations.view",
    "integrations.manage",
    "people.manage",
    "audit.view",
    "workspace.manage",
]

_ALL: tuple[Capability, ...] = (
    "chat.use",
    "files.attach",
    "agents.view",
    "agents.edit",
    "agents.create",
    "channels.view",
    "channels.manage",
    "routines.view",
    "routines.manage",
    "credits.view_own",
    "credits.manage",
    "integrations.view",
    "integrations.manage",
    "people.manage",
    "audit.view",
    "workspace.manage",
)
_BY_ROLE: dict[Role, frozenset[Capability]] = {
    "owner": frozenset(_ALL),
    "admin": frozenset(_ALL),
    "member": frozenset(
        {
            "chat.use",
            "files.attach",
            "agents.view",
            "agents.edit",
            "agents.create",
            "channels.view",
            "routines.view",
            "routines.manage",
            "credits.view_own",
        }
    ),
    "visitor": frozenset({"chat.use", "credits.view_own"}),
}
# Each capability lives in one module; turning the module off removes it.
_MODULE_OF: dict[Capability, WebModuleKey] = {
    "chat.use": "chat",
    "files.attach": "chat",
    "agents.view": "agents",
    "agents.edit": "agents",
    "agents.create": "agents",
    "channels.view": "channels",
    "channels.manage": "channels",
    "routines.view": "routines",
    "routines.manage": "routines",
    "credits.view_own": "credits",
    "credits.manage": "credits",
    "integrations.view": "integrations",
    "integrations.manage": "integrations",
    "people.manage": "admin",
    "audit.view": "admin",
    "workspace.manage": "admin",
}
_MODULES_BY_ROLE: dict[Role, frozenset[WebModuleKey]] = {
    "owner": frozenset(
        {"chat", "agents", "channels", "routines", "credits", "integrations", "admin"}
    ),
    "admin": frozenset(
        {"chat", "agents", "channels", "routines", "credits", "integrations", "admin"}
    ),
    "member": frozenset({"chat", "agents", "channels", "routines", "credits"}),
    "visitor": frozenset({"chat"}),
}
_MODULE_ORDER: tuple[WebModuleKey, ...] = (
    "chat",
    "agents",
    "channels",
    "routines",
    "credits",
    "integrations",
    "admin",
)


def role_for(
    *,
    kind: WebPersonKind,
    workspace_grant: WebRoleGrant | None,
    platform_admin: bool,
    chat_admin: bool = False,
) -> Role:
    """The highest role the person holds. A visitor never rises above visitor.

    A visitor reaches only the agents they were invited to, so no grant or platform admin
    lifts them: promoting one is an admin turning them into a member.
    `chat_admin` is an admin of the chat workspace itself, as its member list says.
    """
    if kind == "visitor":
        return "visitor"
    if workspace_grant == "owner":
        return "owner"
    if workspace_grant == "admin" or platform_admin or chat_admin:
        return "admin"
    return "member"


def is_admin(role: Role) -> bool:
    """Whether the role is the workspace's owner or an admin."""
    return role in ("owner", "admin")


_RANK: dict[Role, int] = {"visitor": 0, "member": 1, "admin": 2, "owner": 3}


def at_or_above(role: Role, other: Role) -> bool:
    """Whether `role` ranks the same as `other` or higher."""
    return _RANK[role] >= _RANK[other]


def modules_for(role: Role, enabled: Iterable[WebModuleKey]) -> list[WebModuleKey]:
    """The sections this role sees, among those the deployment turned on."""
    allowed = _MODULES_BY_ROLE[role] & set(enabled)
    return [m for m in _MODULE_ORDER if m in allowed]


def capabilities_for(
    role: Role, modules: Collection[WebModuleKey], *, files: bool = False
) -> frozenset[Capability]:
    """What the role may do, limited to the modules that are on.

    A visitor's role never carries files; `files` adds it when their invite allows it.
    """
    caps = set(_BY_ROLE[role])
    if files:
        caps.add("files.attach")
    return frozenset(c for c in caps if _MODULE_OF[c] in modules)


ROLE_WORDS: dict[Role, str] = {
    "owner": "Owner",
    "admin": "Admin",
    "member": "Member",
    "visitor": "Visitor",
}


async def chat_standings(
    session: AsyncSession, people: Sequence[WebPersonRow]
) -> dict[uuid.UUID, ChatStandingRow]:
    """Each chat-backed person's account against their workspace's member list, in one read.

    Visitors never run as a chat account, so they have no standing to check.
    """
    members = [p for p in people if p.kind != "visitor"]
    found = await members_store.list_chat_standings(
        session, people={(p.tenant_id, p.account_id) for p in members}
    )
    return {
        p.id: standing
        for p in members
        if (standing := found.get((p.tenant_id, p.account_id))) is not None
    }


def has_left(standings: Mapping[uuid.UUID, ChatStandingRow], person: WebPersonRow) -> bool:
    """Whether the person's chat account is gone from their workspace's member list."""
    standing = standings.get(person.id)
    return standing is not None and not standing.admitted


async def roles_of(
    session: AsyncSession,
    people: Sequence[WebPersonRow],
    standings: Mapping[uuid.UUID, ChatStandingRow] | None = None,
) -> dict[uuid.UUID, Role]:
    """The role each person holds in their workspace, from grants held now rather than at
    sign-in. A few reads however many people, so a listing never costs a query per person."""
    if standings is None:
        standings = await chat_standings(session, people)
    accounts = await accounts_store.list_roles(session, {p.account_id for p in people})
    by_tenant: dict[uuid.UUID, list[WebPersonRow]] = {}
    for person in people:
        by_tenant.setdefault(person.tenant_id, []).append(person)
    roles: dict[uuid.UUID, Role] = {}
    for tenant_id, members in by_tenant.items():
        grants = await roles_store.list_workspace_roles(session, tenant_id=tenant_id)
        for person in members:
            roles[person.id] = role_for(
                kind=person.kind,
                workspace_grant=grants.get(person.account_id),
                platform_admin=accounts.get(person.account_id) == AccountRole.ADMIN,
                chat_admin=(standing := standings.get(person.id)) is not None
                and standing.admitted
                and standing.is_admin,
            )
    return roles


async def server_admin(session: AsyncSession, person: WebPersonRow) -> bool:
    """Whether the person is a platform admin or an admin of the chat workspace itself, as
    chat's own admin check is; a workspace grant alone is not enough."""
    if (await accounts_store.list_roles(session, {person.account_id})).get(
        person.account_id
    ) == AccountRole.ADMIN:
        return True
    standing = (await chat_standings(session, [person])).get(person.id)
    return standing is not None and standing.admitted and standing.is_admin


async def role_of(session: AsyncSession, person: WebPersonRow) -> Role:
    """The person's role in their workspace."""
    return (await roles_of(session, [person]))[person.id]


async def role_of_account(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID, chat_admin: bool
) -> Role | None:
    """The web role an account holds in a workspace, as its web person does when they have
    one; None for a suspended person, who can open nothing. `chat_admin` is the caller's
    admin standing in chat, used when the account has no web person yet."""
    person = await people_store.get_person_for_account(
        session, tenant_id=tenant_id, account_id=account_id
    )
    if person is not None:
        return None if person.status == "suspended" else await role_of(session, person)
    return role_for(
        kind="member",
        workspace_grant=await roles_store.get_workspace_role(
            session, tenant_id=tenant_id, account_id=account_id
        ),
        platform_admin=(await accounts_store.list_roles(session, {account_id})).get(account_id)
        == AccountRole.ADMIN,
        chat_admin=chat_admin,
    )
