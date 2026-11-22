"""The workspace's failures and warnings, read from chat or by an operator token.

The shapes are `daimon.core.ops_overview`'s, the same the web app and `daimon issues` return.
Reading needs the `issues:read` scope for an operator token and a workspace admin behind it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools._scopes import require_scope, scope_tags
from daimon.adapters.mcp.tools._web_admin import agent_names
from daimon.core import ops_overview as ops
from daimon.core.ops_catalog import OpsArea, OpsSeverity
from daimon.core.ops_overview import Issue, IssueDetail, IssueEvent, OpsOverview, OpsPeriod
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

log = structlog.get_logger(__name__)

_MAX_ISSUES = 200
_MAX_EVENTS = 100


class IssueList(BaseModel):
    issues: list[Issue]


class IssueView(BaseModel):
    issue: IssueDetail
    events: list[IssueEvent]
    """Newest first."""


def _read(auth: AuthIdentity) -> None:
    require_scope(auth, "issues:read")
    _require_admin(auth)


async def _names(runtime: McpRuntime, auth: AuthIdentity) -> dict[str, str]:
    try:
        return await agent_names(runtime, auth.tenant_id)
    except Exception as exc:
        # Boundary: the read exists to show an outage, so a failing agent listing must not stop
        # it; issues then name agents from the routines and watches seen.
        log.warning("mcp.ops_agents_unlisted", error_class=type(exc).__name__)
        return {}


def register_ops_issues_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin", *scope_tags("issues:read")})
    async def get_ops_overview(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, period: OpsPeriod = "24h"
    ) -> OpsOverview:
        """Read how the workspace is doing: answers and the share that failed, direct message and
        routine failures, status (healthy, degraded or failing, from the last hour), failures per
        area, an answers chart and the issues that need attention. ``period`` is 1h, 24h, 7d or
        30d. Admin-only."""
        auth = await _auth(ctx)
        _read(auth)
        names = await _names(runtime, auth)
        async with runtime.session_factory() as session:
            return await ops.load_ops_overview(
                session,
                tenant_id=auth.tenant_id,
                now=datetime.now(UTC),
                period=period,
                markup=runtime.settings.billing.markup,
                agents=len(names) if names else None,
                agent_names=names,
            )

    @mcp.tool(tags={"admin", *scope_tags("issues:read")})
    async def list_issues(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        since: Annotated[str, Field(max_length=8)] = "24h",
        area: OpsArea | None = None,
        severity: OpsSeverity | None = None,
        active_only: bool = False,
        limit: Annotated[int, Field(ge=1, le=_MAX_ISSUES)] = 50,
    ) -> IssueList:
        """List the workspace's failures and warnings, grouped into issues, newest first. ``since``
        is how far back to look, like 15m, 24h or 7d. ``active_only`` keeps open issues seen in
        the last day; resolved and muted ones are otherwise included. Each issue has a
        ``fingerprint`` for get_issue. Admin-only."""
        auth = await _auth(ctx)
        _read(auth)
        try:
            window = ops.parse_window(since)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        names = await _names(runtime, auth)
        async with runtime.session_factory() as session:
            found = await ops.load_issues(
                session,
                tenant_id=auth.tenant_id,
                now=datetime.now(UTC),
                status="active" if active_only else "all",
                area=area,
                severity=severity,
                window=window,
                agent_names=names,
            )
        return IssueList(issues=found[:limit])

    @mcp.tool(tags={"admin", *scope_tags("issues:read")})
    async def get_issue(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        fingerprint: Annotated[str, Field(max_length=64)],
        events_limit: Annotated[int, Field(ge=0, le=_MAX_EVENTS)] = 20,
    ) -> IssueView:
        """Read one issue by its ``fingerprint`` (from list_issues): what it is about, when it
        started, who it reached and its latest events. Admin-only."""
        auth = await _auth(ctx)
        _read(auth)
        names = await _names(runtime, auth)
        async with runtime.session_factory() as session:
            detail = await ops.load_issue(
                session,
                tenant_id=auth.tenant_id,
                fingerprint=fingerprint,
                now=datetime.now(UTC),
                agent_names=names,
            )
            if detail is None:
                raise ToolError("No such issue here.")
            rows = await ops.load_issue_events(
                session,
                tenant_id=auth.tenant_id,
                fingerprint=fingerprint,
                before=None,
                limit=events_limit,
            )
        return IssueView(issue=detail, events=[event for _, event in rows])
