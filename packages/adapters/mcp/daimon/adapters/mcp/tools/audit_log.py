"""The workspace's audit log, from chat: what admins changed in the web app and the
chat tool calls and panel writes, newest first."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._web_admin import web_admin_of
from daimon.adapters.mcp.tools._web_url import WebUrl, tenant_links
from daimon.core import audit_trail
from daimon.core.audit_trail import AuditFilter
from daimon.core.stores import web_people as people_store
from daimon.core.stores.domain import WebAuditRow
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

_LIMIT = 100


@dataclass(frozen=True)
class AuditLine:
    source: Literal["web", "security"]
    at: datetime
    who: str | None
    action: str
    target: str | None
    detail: str | None
    outcome: str | None


@dataclass(frozen=True)
class AuditLog:
    entries: list[AuditLine]
    web_url: WebUrl = None


@dataclass(frozen=True)
class AuditKindLine:
    source: Literal["web", "security"]
    kind: str


@dataclass(frozen=True)
class AuditKinds:
    kinds: list[AuditKindLine]


def _day(value: str | None, name: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as err:
        raise ToolError(f"{name} must be a date like 2026-01-31.") from err


def _audit_url(runtime: McpRuntime, auth: AuthIdentity) -> str | None:
    links = tenant_links(runtime, auth)
    return links.audit() if links else None


async def _log_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    source: Literal["web", "security"] | None,
    person_id: str | None,
    kind: str | None,
    first: str | None,
    last: str | None,
    limit: int,
) -> AuditLog:
    actor = await web_admin_of(runtime, auth)
    since, until = audit_trail.day_bounds(_day(first, "first"), _day(last, "last"))
    async with runtime.session_factory() as db:
        account: uuid.UUID | None = None
        if person_id:
            try:
                wanted = uuid.UUID(person_id)
            except ValueError as err:
                raise ToolError("person_id is not valid.") from err
            chosen = next(
                (
                    p
                    for p in await people_store.get_people(db, person_ids=[wanted])
                    if p.tenant_id == actor.tenant_id
                ),
                None,
            )
            if chosen is None:
                return AuditLog([], _audit_url(runtime, auth))
            account = chosen.account_id
        found = await audit_trail.list_entries(
            db,
            tenant_id=actor.tenant_id,
            query=AuditFilter(
                source=source,
                actor_account_id=account,
                kind=kind or None,
                since=since,
                until=until,
            ),
            limit=max(1, min(limit, _LIMIT)),
        )
        names = {
            p.account_id: p.display_name
            for p in await people_store.list_people(db, tenant_id=actor.tenant_id)
        }
    lines: list[AuditLine] = []
    for e in found:
        who = names.get(e.actor_account_id) if e.actor_account_id else None
        if isinstance(e.row, WebAuditRow):
            lines.append(
                AuditLine(e.source, e.at, who, e.row.action, e.row.target, e.row.detail, None)
            )
        else:
            lines.append(
                AuditLine(
                    e.source,
                    e.at,
                    who or e.row.agent_name,
                    e.kind,
                    e.row.operation or None,
                    e.row.reason or None,
                    e.row.outcome,
                )
            )
    return AuditLog(lines, _audit_url(runtime, auth))


async def _kinds_impl(runtime: McpRuntime, auth: AuthIdentity) -> AuditKinds:
    actor = await web_admin_of(runtime, auth)
    async with runtime.session_factory() as db:
        found = await audit_trail.list_kinds(db, tenant_id=actor.tenant_id)
    return AuditKinds([AuditKindLine(k.source, k.kind) for k in found])


def register_audit_log_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(name="list_audit_log", tags={"admin"})
    async def audit_log(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        source: Literal["web", "security"] | None = None,
        person_id: str | None = None,
        kind: str | None = None,
        first: str | None = None,
        last: str | None = None,
        limit: int = 50,
    ) -> AuditLog:
        """List the workspace's audit log, newest first: ``web`` for admin changes in the
        web app, ``security`` for chat tool calls and panel writes. Filter by acting web
        person, ``kind`` (see list_audit_kinds) and UTC days ``first`` to ``last``
        (YYYY-MM-DD). Admin-only."""
        return await _log_impl(
            runtime,
            await _auth(ctx),
            source=source,
            person_id=person_id,
            kind=kind,
            first=first,
            last=last,
            limit=limit,
        )

    @mcp.tool(name="list_audit_kinds", tags={"admin"})
    async def audit_kinds(ctx: Context) -> AuditKinds:  # pyright: ignore[reportUnusedFunction]
        """List the kinds of audit entry the workspace has recorded, for the log's ``kind``
        filter. Admin-only."""
        return await _kinds_impl(runtime, await _auth(ctx))


__all__ = ["register_audit_log_tools"]
