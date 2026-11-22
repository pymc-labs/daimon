"""`/api/admin/overview` and `/api/admin/issues`: how the workspace is doing, and what went wrong.

Reads need `audit.view`; resolving, muting and reopening an issue need `workspace.manage` and
are written to the audit trail. The workspace is always the signed-in person's.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from daimon.adapters.web.admin_common import AuditViewer, WorkspaceAdmin, listed_agents
from daimon.adapters.web.paging import PageRequest, cut, page_of
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import ops_overview as ops
from daimon.core.ops_catalog import OpsArea, OpsSeverity
from daimon.core.ops_overview import (
    Issue,
    IssueAction,
    IssueDetail,
    IssueEvent,
    IssueSort,
    IssueStatus,
    OpsOverview,
    OpsPeriod,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

log = structlog.get_logger(__name__)

_NO_ISSUE = "This issue isn't in the workspace."


class StateChange(BaseModel):
    action: IssueAction


def ops_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/admin")

    async def agent_names(tenant_id: uuid.UUID) -> dict[str, str]:
        try:
            return {a.id: a.name for a in await listed_agents(runtime, tenant_id)}
        except Exception as exc:
            # Boundary: this page exists to show an outage, so a failing agent listing must not
            # take it down; issues then name agents from the routines and watches seen.
            log.warning("web.ops_agents_unlisted", error_class=type(exc).__name__)
            return {}

    @router.get("/overview")
    async def overview(  # pyright: ignore[reportUnusedFunction]
        caller: AuditViewer, period: OpsPeriod = "24h"
    ) -> OpsOverview:
        tenant_id = caller.person.tenant_id
        names = await agent_names(tenant_id)
        async with runtime.sessionmaker() as db:
            return await ops.load_ops_overview(
                db,
                tenant_id=tenant_id,
                now=runtime.now(),
                period=period,
                markup=runtime.settings.billing.markup,
                agents=len(names) if names else None,
                agent_names=names,
            )

    @router.get("/issues")
    async def issues(  # pyright: ignore[reportUnusedFunction]
        caller: AuditViewer,
        response: Response,
        page: Annotated[PageRequest, Depends(page_of(50))],
        period: OpsPeriod = "24h",
        status: IssueStatus = "active",
        area: OpsArea | None = None,
        severity: OpsSeverity | None = None,
        agent: Annotated[str | None, Query(max_length=200)] = None,
        q: Annotated[str | None, Query(max_length=200)] = None,
        sort: IssueSort = "last_seen",
    ) -> list[Issue]:
        tenant_id = caller.person.tenant_id
        names = await agent_names(tenant_id)
        async with runtime.sessionmaker() as db:
            found = await ops.load_issues(
                db,
                tenant_id=tenant_id,
                now=runtime.now(),
                period=period,
                status=status,
                area=area,
                severity=severity,
                agent_id=agent,
                query=q,
                sort=sort,
                agent_names=names,
            )
        rest = ops.after_cursor(found, sort, page.cursor)[: page.limit + 1]
        return cut(rest, page, response, ops.issue_cursor)

    @router.get("/issues/{fingerprint}")
    async def issue(  # pyright: ignore[reportUnusedFunction]
        caller: AuditViewer, fingerprint: str, period: OpsPeriod = "24h"
    ) -> IssueDetail:
        tenant_id = caller.person.tenant_id
        names = await agent_names(tenant_id)
        async with runtime.sessionmaker() as db:
            found = await ops.load_issue(
                db,
                tenant_id=tenant_id,
                fingerprint=fingerprint,
                now=runtime.now(),
                period=period,
                agent_names=names,
            )
        if found is None:
            raise HTTPException(status_code=404, detail=_NO_ISSUE)
        return found

    @router.get("/issues/{fingerprint}/events")
    async def issue_events(  # pyright: ignore[reportUnusedFunction]
        caller: AuditViewer,
        response: Response,
        fingerprint: str,
        page: Annotated[PageRequest, Depends(page_of(20))],
    ) -> list[IssueEvent]:
        async with runtime.sessionmaker() as db:
            rows = await ops.load_issue_events(
                db,
                tenant_id=caller.person.tenant_id,
                fingerprint=fingerprint,
                before=page.cursor,
                limit=page.limit + 1,
            )
        if not rows and page.cursor is None:
            async with runtime.sessionmaker() as db:
                known = await ops.load_issue_summary(
                    db,
                    tenant_id=caller.person.tenant_id,
                    fingerprint=fingerprint,
                    now=runtime.now(),
                )
            if known is None:
                raise HTTPException(status_code=404, detail=_NO_ISSUE)
        return [event for _, event in cut(rows, page, response, lambda row: row[0])]

    @router.post("/issues/{fingerprint}/state")
    async def change_state(  # pyright: ignore[reportUnusedFunction]
        caller: WorkspaceAdmin, fingerprint: str, body: StateChange
    ) -> Issue:
        async with runtime.sessionmaker.begin() as db:
            current = await ops.change_issue_state(
                db,
                tenant_id=caller.person.tenant_id,
                fingerprint=fingerprint,
                action=body.action,
                by_account_id=caller.person.account_id,
                now=runtime.now(),
            )
        if current is None:
            raise HTTPException(status_code=404, detail=_NO_ISSUE)
        return current

    return router
