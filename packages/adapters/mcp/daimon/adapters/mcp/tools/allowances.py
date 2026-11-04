"""Spend allowances and caps, from chat: who may spend how much, per person, group or invite."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._card import require_card
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._web_admin import agent_names, now, refused, web_admin_of
from daimon.adapters.mcp.tools._web_url import WebUrl, tenant_links
from daimon.core import credit_views, web_allowances
from daimon.core.allowances import EVERYONE
from daimon.core.stores import spend_allowances
from daimon.core.stores.domain import AllowanceSubjectKind, AllowanceWindow, SpendAllowanceRow
from daimon.core.web_admin import WebAdminError
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

_MAX_USD = Decimal(1_000_000)


@dataclass(frozen=True)
class AllowanceLine:
    allowance_id: str
    subject_kind: AllowanceSubjectKind
    subject: str
    """A person's web id, a group's name or an invite's id."""
    subject_name: str
    agent_id: str | None
    amount_usd: float
    window: AllowanceWindow
    expires_at: datetime | None
    cap: bool
    """A monthly ceiling on top of the allowance that governs, not an allowance."""


@dataclass(frozen=True)
class Allowances:
    allowances: list[AllowanceLine]
    web_url: WebUrl = None


@dataclass(frozen=True)
class AllowanceChanged:
    allowance: AllowanceLine
    web_url: WebUrl = None


@dataclass(frozen=True)
class AllowanceDeleted:
    allowance_id: str


def _id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as err:
        raise ToolError("allowance_id is not valid.") from err


def _amount(value: Decimal) -> Decimal:
    if not 0 < value <= _MAX_USD:
        raise ToolError("amount_usd must be above 0 and at most 1000000 dollars.")
    return value.quantize(Decimal("0.01"))


async def _lines(
    runtime: McpRuntime, tenant_id: uuid.UUID, rows: list[SpendAllowanceRow]
) -> list[AllowanceLine]:
    async with runtime.session_factory() as db:
        people = await credit_views.label_people(
            db,
            tenant_id=tenant_id,
            user_ids=[r.subject_id for r in rows if r.subject_kind == "person"],
        )
    lines: list[AllowanceLine] = []
    for r in rows:
        subject, name = r.subject_id, r.subject_id
        if r.subject_kind == "person":
            label = people.get(r.subject_id)
            subject = str(label.person_id) if label and label.person_id else r.subject_id
            name = label.name if label else "Unknown person"
        elif r.subject_kind == "group":
            name = "Everyone" if r.subject_id == EVERYONE else r.subject_id
        lines.append(
            AllowanceLine(
                allowance_id=str(r.id),
                subject_kind=r.subject_kind,
                subject=subject,
                subject_name=name,
                agent_id=r.agent_id,
                amount_usd=float(r.amount_usd),
                window=r.window,
                expires_at=r.expires_at,
                cap=r.is_cap,
            )
        )
    return lines


async def _list_impl(runtime: McpRuntime, auth: AuthIdentity) -> Allowances:
    actor = await web_admin_of(runtime, auth)
    async with runtime.session_factory() as db:
        rows = await spend_allowances.list_for_tenant(db, tenant_id=actor.tenant_id)
    links = await tenant_links(runtime, auth)
    return Allowances(
        list(reversed(await _lines(runtime, actor.tenant_id, rows))),
        links.allowance() if links else None,
    )


async def _create_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    subject_kind: AllowanceSubjectKind,
    subject: str,
    amount_usd: Decimal,
    window: AllowanceWindow,
    agent_id: str | None,
    expires_at: datetime | None,
    cap: bool,
) -> AllowanceChanged:
    actor = await web_admin_of(runtime, auth)
    names = await agent_names(runtime, actor.tenant_id)
    if agent_id is not None and agent_id not in names:
        raise ToolError("Pick one of this team's agents.")
    try:
        async with runtime.session_factory.begin() as db:
            row = await web_allowances.create_allowance(
                db,
                actor,
                now(),
                subject_kind=subject_kind,
                subject_id=subject,
                agent_id=agent_id,
                agent_names=names,
                amount_usd=_amount(amount_usd),
                window=window,
                expires_at=expires_at,
                cap=cap,
            )
    except WebAdminError as err:
        raise refused(err) from err
    [line] = await _lines(runtime, actor.tenant_id, [row])
    links = await tenant_links(runtime, auth)
    return AllowanceChanged(line, links.allowance(line.allowance_id) if links else None)


async def _set_impl(
    runtime: McpRuntime, auth: AuthIdentity, allowance_id: str, amount_usd: Decimal
) -> AllowanceChanged:
    actor = await web_admin_of(runtime, auth)
    names = await agent_names(runtime, actor.tenant_id)
    try:
        async with runtime.session_factory.begin() as db:
            row = await web_allowances.set_allowance_amount(
                db,
                actor,
                now(),
                _id(allowance_id),
                amount_usd=_amount(amount_usd),
                agent_names=names,
            )
    except WebAdminError as err:
        raise refused(err) from err
    [line] = await _lines(runtime, actor.tenant_id, [row])
    links = await tenant_links(runtime, auth)
    return AllowanceChanged(line, links.allowance(line.allowance_id) if links else None)


async def _delete_impl(
    runtime: McpRuntime, auth: AuthIdentity, allowance_id: str, origin_context_id: str | None = None
) -> AllowanceDeleted:
    actor = await web_admin_of(runtime, auth)
    await require_card(
        runtime, auth, tool_name="delete_allowance", origin_context_id=origin_context_id
    )
    names = await agent_names(runtime, actor.tenant_id)
    try:
        async with runtime.session_factory.begin() as db:
            await web_allowances.delete_allowance(
                db, actor, now(), _id(allowance_id), agent_names=names
            )
    except WebAdminError as err:
        raise refused(err) from err
    return AllowanceDeleted(allowance_id)


def register_allowance_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(name="list_allowances", tags={"admin"})
    async def list_allowances(ctx: Context) -> Allowances:  # pyright: ignore[reportUnusedFunction]
        """List the workspace's spend allowances and caps, newest first. Admin-only."""
        return await _list_impl(runtime, await _auth(ctx))

    @mcp.tool(name="create_allowance", tags={"admin"})
    async def create_allowance(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        subject_kind: AllowanceSubjectKind,
        subject: str,
        amount_usd: Decimal,
        window: AllowanceWindow = "month",
        agent_id: str | None = None,
        expires_at: datetime | None = None,
        cap: bool = False,
    ) -> AllowanceChanged:
        """Give a person (web person id), a group (name, or ``everyone``) or an invite (id) a
        spend allowance in billed dollars, monthly or in total, for every agent or one. A cap is
        monthly, for everyone or one person, with no agent or end date. Admin-only."""
        return await _create_impl(
            runtime,
            await _auth(ctx),
            subject_kind=subject_kind,
            subject=subject,
            amount_usd=amount_usd,
            window=window,
            agent_id=agent_id,
            expires_at=expires_at,
            cap=cap,
        )

    @mcp.tool(name="set_allowance_amount", tags={"admin"})
    async def set_amount(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, allowance_id: str, amount_usd: Decimal
    ) -> AllowanceChanged:
        """Change the dollar amount of an existing allowance or cap. Admin-only."""
        return await _set_impl(runtime, await _auth(ctx), allowance_id, amount_usd)

    @mcp.tool(name="delete_allowance", tags={"admin"})
    async def delete_allowance(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, allowance_id: str, origin_context_id: str | None = None
    ) -> AllowanceDeleted:
        """Remove an allowance or cap. Admin-only.
        Runs on the person's Confirm card in chat; pass this active turn's origin_context_id."""
        return await _delete_impl(runtime, await _auth(ctx), allowance_id, origin_context_id)


__all__ = ["register_allowance_tools"]
