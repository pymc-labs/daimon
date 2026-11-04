"""The web app's people, groups and agent access, from chat."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._card import require_card
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._web_admin import (
    PrivateDetails,
    now,
    private_details,
    refused,
    web_admin_of,
)
from daimon.adapters.mcp.tools._web_url import WebUrl, tenant_links
from daimon.core import web_admin
from daimon.core.allowances import EVERYONE
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.stores import web_identities as identities_store
from daimon.core.stores import web_invites
from daimon.core.stores import web_people as people_store
from daimon.core.stores.domain import WebAgentWho, WebIdentityRow, WebPersonRow, WebPersonStatus
from daimon.core.web_admin import AgentAccess, WebAdminError
from daimon.core.web_roles_rules import Role, role_of, roles_of
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

_LIMIT = 200
PersonRole = Literal["admin", "member"]


@dataclass(frozen=True)
class WebPerson:
    person_id: str
    name: str
    email: str | None
    role: Role
    status: WebPersonStatus
    groups: list[str]
    last_seen: datetime
    web_url: WebUrl = None
    """Set on the result of a change, not in a list."""


@dataclass(frozen=True)
class WebPeople:
    people: list[WebPerson]
    private_details: PrivateDetails
    web_url: WebUrl = None


@dataclass(frozen=True)
class WebPersonDetail:
    person: WebPerson
    identities: list[str]
    """Where they sign in, as `provider:label`; empty unless `private_details` is `shown`."""
    private_details: PrivateDetails
    invite_id: str | None
    allow_files: bool | None
    """A visitor's invite and whether it lets them attach files; null for everyone else."""
    web_url: WebUrl = None


@dataclass(frozen=True)
class WebGroup:
    group: str
    members: int


@dataclass(frozen=True)
class WebGroups:
    groups: list[WebGroup]


@dataclass(frozen=True)
class AgentWebAccess:
    agent_id: str
    name: str
    web: bool
    who: WebAgentWho
    groups: list[str]
    fixed_to: str | None
    """The channel an agent is kept to, which keeps it off the web."""


@dataclass(frozen=True)
class AgentWebAccessList:
    agents: list[AgentWebAccess]


def _person(
    person: WebPersonRow, role: Role, groups: list[str], details: PrivateDetails
) -> WebPerson:
    return WebPerson(
        person_id=str(person.id),
        name=person.display_name,
        email=(person.email or "") if details == "shown" else None,
        role=role,
        status=person.status,
        groups=groups,
        last_seen=person.last_seen_at,
    )


def _groups(identities: Sequence[WebIdentityRow]) -> list[str]:
    return sorted({g for i in identities for g in i.groups})


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as err:
        raise ToolError("person_id is not valid.") from err


def _access(row: AgentAccess) -> AgentWebAccess:
    return AgentWebAccess(
        agent_id=row.agent_id,
        name=row.name,
        web=row.web,
        who=row.who,
        groups=row.groups,
        fixed_to=row.fixed_to,
    )


async def _people_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    query: str | None,
    limit: int,
    origin_context_id: str | None = None,
) -> WebPeople:
    actor = await web_admin_of(runtime, auth)
    details = await private_details(runtime, auth, origin_context_id)
    async with runtime.session_factory() as db:
        rows = await people_store.list_people(
            db,
            tenant_id=actor.tenant_id,
            limit=max(1, min(limit, _LIMIT)),
            search=query.strip() if query else None,
        )
        identities = await identities_store.list_for_people(db, person_ids=[p.id for p in rows])
        roles = await roles_of(db, rows)
    links = await tenant_links(runtime, auth)
    return WebPeople(
        [_person(p, roles[p.id], _groups(identities[p.id]), details) for p in rows],
        details,
        links.person_list() if links else None,
    )


async def _person_impl(
    runtime: McpRuntime, auth: AuthIdentity, person_id: str, origin_context_id: str | None = None
) -> WebPersonDetail:
    actor = await web_admin_of(runtime, auth)
    details = await private_details(runtime, auth, origin_context_id)
    async with runtime.session_factory() as db:
        try:
            person = await web_admin.person_in(db, actor.tenant_id, _uuid(person_id))
        except WebAdminError as err:
            raise refused(err) from err
        identities = await identities_store.list_for_person(db, person_id=person.id)
        role = await role_of(db, person)
        invite = (
            await web_invites.get_invite(db, tenant_id=person.tenant_id, invite_id=person.invite_id)
            if role == "visitor" and person.invite_id is not None
            else None
        )
    open_invite = invite if invite is not None and invite.revoked_at is None else None
    links = await tenant_links(runtime, auth)
    return WebPersonDetail(
        person=_person(person, role, _groups(identities), details),
        identities=[f"{i.provider}:{i.label}" for i in identities] if details == "shown" else [],
        private_details=details,
        invite_id=None if open_invite is None else str(open_invite.id),
        allow_files=None if open_invite is None else open_invite.allow_files,
        web_url=links.person(str(person.id)) if links else None,
    )


async def _groups_impl(runtime: McpRuntime, auth: AuthIdentity) -> WebGroups:
    actor = await web_admin_of(runtime, auth)
    async with runtime.session_factory() as db:
        counts = await identities_store.count_people_by_group(db, tenant_id=actor.tenant_id)
        members = await people_store.count_members(db, tenant_id=actor.tenant_id)
    return WebGroups(
        [
            WebGroup(EVERYONE, members),
            *(WebGroup(name, n) for name, n in sorted(counts.items()) if name != EVERYONE),
        ]
    )


async def _update_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    person_id: str,
    role: PersonRole | None,
    status: WebPersonStatus | None,
    origin_context_id: str | None = None,
) -> WebPerson:
    actor = await web_admin_of(runtime, auth)
    await require_card(
        runtime, auth, tool_name="update_web_person", origin_context_id=origin_context_id
    )
    details = await private_details(runtime, auth, origin_context_id)
    try:
        async with runtime.session_factory.begin() as db:
            changed = await web_admin.change_person(
                db, actor, now(), _uuid(person_id), role=role, status=status
            )
        async with runtime.session_factory() as db:
            identities = await identities_store.list_for_person(db, person_id=changed.id)
            shown = await role_of(db, changed)
    except WebAdminError as err:
        raise refused(err) from err
    links = await tenant_links(runtime, auth)
    return replace(
        _person(changed, shown, _groups(identities), details),
        web_url=links.person(str(changed.id)) if links else None,
    )


async def _access_list_impl(runtime: McpRuntime, auth: AuthIdentity) -> AgentWebAccessList:
    actor = await web_admin_of(runtime, auth)
    agents = await list_agents_by_tenant(runtime.client, tenant_id=actor.tenant_id)
    async with runtime.session_factory() as db:
        rows = await web_admin.agent_access_rows(db, actor.tenant_id, agents)
    return AgentWebAccessList([_access(r) for r in rows])


async def _access_set_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    agent_id: str,
    web: bool | None,
    who: WebAgentWho | None,
    groups: list[str] | None,
    origin_context_id: str | None = None,
) -> AgentWebAccess:
    actor = await web_admin_of(runtime, auth)
    await require_card(
        runtime, auth, tool_name="set_agent_web_access", origin_context_id=origin_context_id
    )
    agents = [
        a
        for a in await list_agents_by_tenant(runtime.client, tenant_id=actor.tenant_id)
        if a.id == agent_id
    ]
    if not agents:
        raise ToolError("This agent isn't on this team.")
    try:
        async with runtime.session_factory.begin() as db:
            [current] = await web_admin.agent_access_rows(db, actor.tenant_id, agents)
            changed = await web_admin.set_agent_access(
                db, actor, now(), current, web=web, who=who, groups=groups
            )
    except WebAdminError as err:
        raise refused(err) from err
    return _access(changed)


def register_web_people_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(name="list_web_people", tags={"admin"})
    async def list_people(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        query: str | None = None,
        limit: int = 50,
        origin_context_id: str | None = None,
    ) -> WebPeople:
        """List the web app's people, oldest first, with role, status and groups. ``query``
        keeps those whose name or email contains it. Emails show only in a direct message with
        the admin; elsewhere ``private_details`` is ``direct_message`` and they are null. Pass
        this active turn's origin_context_id. Admin-only."""
        return await _people_impl(runtime, await _auth(ctx), query, limit, origin_context_id)

    @mcp.tool(name="get_web_person", tags={"admin"})
    async def get_person(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, person_id: str, origin_context_id: str | None = None
    ) -> WebPersonDetail:
        """Show one web person: role, status, groups, sign-ins and, for a visitor, their
        invite. Email and sign-ins show only in a direct message with the admin; elsewhere
        ``private_details`` is ``direct_message``. Pass this active turn's origin_context_id.
        Admin-only."""
        return await _person_impl(runtime, await _auth(ctx), person_id, origin_context_id)

    @mcp.tool(name="list_web_groups", tags={"admin"})
    async def list_groups(ctx: Context) -> WebGroups:  # pyright: ignore[reportUnusedFunction]
        """List everyone and each group the web sign-ins carry, with head counts. Admin-only."""
        return await _groups_impl(runtime, await _auth(ctx))

    @mcp.tool(name="update_web_person", tags={"admin"})
    async def update_person(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        person_id: str,
        role: PersonRole | None = None,
        status: WebPersonStatus | None = None,
        origin_context_id: str | None = None,
    ) -> WebPerson:
        """Change a web person's role (admin or member) or suspend or restore them. Only an
        owner changes an owner. Admin-only.
        Runs on the person's Confirm card in chat; pass this active turn's origin_context_id."""
        return await _update_impl(
            runtime, await _auth(ctx), person_id, role, status, origin_context_id
        )

    @mcp.tool(name="list_agent_web_access", tags={"admin"})
    async def list_access(ctx: Context) -> AgentWebAccessList:  # pyright: ignore[reportUnusedFunction]
        """List each agent's web chat access: on or off, who may use it and the channel it is
        kept to, if any. Admin-only."""
        return await _access_list_impl(runtime, await _auth(ctx))

    @mcp.tool(name="set_agent_web_access", tags={"admin"})
    async def set_access(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_id: str,
        web: bool | None = None,
        who: WebAgentWho | None = None,
        groups: list[str] | None = None,
        origin_context_id: str | None = None,
    ) -> AgentWebAccess:
        """Turn web chat on or off for an agent and choose who may use it there: everyone,
        the given groups or invitees (at most 100). Admin-only.
        Runs on the person's Confirm card in chat; pass this active turn's origin_context_id."""
        return await _access_set_impl(
            runtime, await _auth(ctx), agent_id, web, who, groups, origin_context_id
        )


__all__ = ["register_web_people_tools"]
