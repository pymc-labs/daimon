"""Web invites and visitor sign-in links, from chat. The link goes to the admin's direct
messages; the result never carries it."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._web_admin import (
    agent_names,
    now,
    refused,
    send_privately,
    web_admin_of,
)
from daimon.adapters.mcp.tools._web_url import WebUrl, tenant_links
from daimon.core import web_admin
from daimon.core.stores import web_invites
from daimon.core.stores.domain import WebInviteRow
from daimon.core.web_admin import WebAdminError, link_url
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

InviteRole = Literal["visitor", "member"]
_LIMIT = 100


@dataclass(frozen=True)
class WebInvite:
    invite_id: str
    email: str
    role: str
    agent_ids: list[str]
    credit_usd: float
    allow_files: bool
    expires_at: datetime
    status: Literal["pending", "accepted", "expired"]


@dataclass(frozen=True)
class WebInvites:
    invites: list[WebInvite]
    web_url: WebUrl = None


@dataclass(frozen=True)
class WebInviteSent:
    invite: WebInvite
    delivered: Literal["direct_message"]
    """Where the link went; it is never in this result."""
    web_url: WebUrl = None


@dataclass(frozen=True)
class WebInviteChanged:
    invite: WebInvite
    web_url: WebUrl = None


@dataclass(frozen=True)
class WebInviteRevoked:
    invite_id: str


@dataclass(frozen=True)
class VisitorLinkSent:
    person_id: str
    delivered: Literal["direct_message"]
    web_url: WebUrl = None


@dataclass(frozen=True)
class VisitorLinkRevoked:
    person_id: str


def _shown(invite: WebInviteRow, at: datetime) -> WebInvite:
    status: Literal["pending", "accepted", "expired"] = (
        "accepted"
        if invite.accepted_at is not None
        else "expired"
        if invite.expires_at <= at
        else "pending"
    )
    return WebInvite(
        invite_id=str(invite.id),
        email=invite.email,
        role=invite.role,
        agent_ids=list(invite.agent_ids),
        credit_usd=float(invite.credit_usd),
        allow_files=invite.allow_files,
        expires_at=invite.expires_at,
        status=status,
    )


def _link_of(runtime: McpRuntime, token: str) -> str:
    web = runtime.settings.web
    if web is None:
        raise ToolError("The web app is not set up for this deployment.")
    return link_url(web, token)


def _uuid(value: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as err:
        raise ToolError(f"{what} is not valid.") from err


async def _list_impl(runtime: McpRuntime, auth: AuthIdentity) -> WebInvites:
    actor = await web_admin_of(runtime, auth)
    async with runtime.session_factory() as db:
        found = await web_invites.list_invites(db, tenant_id=actor.tenant_id, limit=_LIMIT)
    at = now()
    links = tenant_links(runtime, auth)
    return WebInvites([_shown(i, at) for i in found], links.invites() if links else None)


async def _create_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    email: str,
    role: InviteRole,
    agent_ids: list[str],
    credit_usd: Decimal,
    days: int,
    allow_files: bool,
) -> WebInviteSent:
    actor = await web_admin_of(runtime, auth)
    if "@" not in email or not 1 <= days <= 90 or not 0 <= credit_usd <= 100_000:
        raise ToolError("Give an email, 1 to 90 days and a credit from 0 to 100000 dollars.")
    agents = await agent_names(runtime, actor.tenant_id)
    at = now()
    try:
        async with runtime.session_factory.begin() as db:
            invite, token = await web_admin.create_invite(
                db,
                actor,
                at,
                email=email,
                role=role,
                agent_ids=agent_ids,
                agents=agents,
                credit_usd=credit_usd.quantize(Decimal("0.01")),
                days=days,
                allow_files=allow_files,
            )
    except WebAdminError as err:
        raise refused(err) from err
    try:
        await send_privately(runtime, auth, f"Invite link for {email}: {_link_of(runtime, token)}")
    except ToolError as err:
        async with runtime.session_factory.begin() as db:
            await web_admin.revoke_invite(db, actor, now(), invite.id)
        raise ToolError(f"{err} The invite was revoked.") from err
    links = tenant_links(runtime, auth)
    url = links.invites(str(invite.id)) if links else None
    return WebInviteSent(_shown(invite, at), "direct_message", url)


async def _files_impl(
    runtime: McpRuntime, auth: AuthIdentity, invite_id: str, allow: bool
) -> WebInviteChanged:
    actor = await web_admin_of(runtime, auth)
    at = now()
    try:
        async with runtime.session_factory.begin() as db:
            invite = await web_admin.set_invite_files(
                db, actor, at, _uuid(invite_id, "invite_id"), allow=allow
            )
    except WebAdminError as err:
        raise refused(err) from err
    links = tenant_links(runtime, auth)
    url = links.invites(str(invite.id)) if links else None
    return WebInviteChanged(_shown(invite, at), url)


async def _revoke_impl(runtime: McpRuntime, auth: AuthIdentity, invite_id: str) -> WebInviteRevoked:
    actor = await web_admin_of(runtime, auth)
    try:
        async with runtime.session_factory.begin() as db:
            await web_admin.revoke_invite(db, actor, now(), _uuid(invite_id, "invite_id"))
    except WebAdminError as err:
        raise refused(err) from err
    return WebInviteRevoked(invite_id)


async def _issue_link_impl(
    runtime: McpRuntime, auth: AuthIdentity, person_id: str
) -> VisitorLinkSent:
    actor = await web_admin_of(runtime, auth)
    person = _uuid(person_id, "person_id")
    try:
        async with runtime.session_factory.begin() as db:
            _, token = await web_admin.issue_sign_in_link(db, actor, now(), person)
    except WebAdminError as err:
        raise refused(err) from err
    try:
        await send_privately(runtime, auth, f"Sign-in link: {_link_of(runtime, token)}")
    except ToolError as err:
        async with runtime.session_factory.begin() as db:
            await web_admin.revoke_sign_in_link(db, actor, now(), person)
        raise ToolError(f"{err} The link was revoked.") from err
    links = tenant_links(runtime, auth)
    return VisitorLinkSent(person_id, "direct_message", links.person(person_id) if links else None)


async def _revoke_link_impl(
    runtime: McpRuntime, auth: AuthIdentity, person_id: str
) -> VisitorLinkRevoked:
    actor = await web_admin_of(runtime, auth)
    try:
        async with runtime.session_factory.begin() as db:
            await web_admin.revoke_sign_in_link(db, actor, now(), _uuid(person_id, "person_id"))
    except WebAdminError as err:
        raise refused(err) from err
    return VisitorLinkRevoked(person_id)


def register_web_invite_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(name="list_web_invites", tags={"admin"})
    async def list_invites(ctx: Context) -> WebInvites:  # pyright: ignore[reportUnusedFunction]
        """List the web app's invites: open, accepted or expired, newest first. Admin-only."""
        return await _list_impl(runtime, await _auth(ctx))

    @mcp.tool(name="create_web_invite", tags={"admin"})
    async def create_invite(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        email: str,
        role: InviteRole,
        agent_ids: list[str] | None = None,
        credit_usd: Decimal = Decimal(0),
        days: int = 7,
        allow_files: bool = False,
    ) -> WebInviteSent:
        """Invite someone to the web app by email as a member, or as a visitor of the given
        agents. The link is sent to the caller by direct message, never shown here. Admin-only."""
        return await _create_impl(
            runtime,
            await _auth(ctx),
            email=email,
            role=role,
            agent_ids=agent_ids or [],
            credit_usd=credit_usd,
            days=days,
            allow_files=allow_files,
        )

    @mcp.tool(name="set_web_invite_files", tags={"admin"})
    async def set_files(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, invite_id: str, allow: bool
    ) -> WebInviteChanged:
        """Turn file attachments on or off for a visitor invite, accepted or not. Admin-only."""
        return await _files_impl(runtime, await _auth(ctx), invite_id, allow)

    @mcp.tool(name="revoke_web_invite", tags={"admin"})
    async def revoke_invite(ctx: Context, invite_id: str) -> WebInviteRevoked:  # pyright: ignore[reportUnusedFunction]
        """Revoke a web invite so its link stops working. Admin-only."""
        return await _revoke_impl(runtime, await _auth(ctx), invite_id)

    @mcp.tool(name="issue_visitor_link", tags={"admin"})
    async def issue_link(ctx: Context, person_id: str) -> VisitorLinkSent:  # pyright: ignore[reportUnusedFunction]
        """Issue a one-time sign-in link for a web visitor. The link is sent to the caller by
        direct message, never shown here. Admin-only; refused for a person at or above the
        caller's role."""
        return await _issue_link_impl(runtime, await _auth(ctx), person_id)

    @mcp.tool(name="revoke_visitor_link", tags={"admin"})
    async def revoke_link(ctx: Context, person_id: str) -> VisitorLinkRevoked:  # pyright: ignore[reportUnusedFunction]
        """Revoke a person's open sign-in links, as when one went to the wrong place. Admin-only."""
        return await _revoke_link_impl(runtime, await _auth(ctx), person_id)


__all__ = ["register_web_invite_tools"]
