"""`/api/admin/monitoring`: the conversations kept for a monitored agent, for workspace admins.

Only those who may turn monitoring on read it, as in chat
(`monitoring_view.may_read_monitored`). Every read is written to the audit trail before
anything is returned, the first page of a list or transcript only: loading more is the same read.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.admin_common import listed_agents
from daimon.adapters.web.audit import audit
from daimon.adapters.web.caller import WebCaller, current_caller
from daimon.adapters.web.paging import PageRequest, cut, page_of
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import monitoring_view as view
from daimon.core.monitoring import MonitoringState
from daimon.core.monitoring_view import Where
from daimon.core.stores import web_people as people_store
from daimon.core.stores.domain import MonitoredConversationRow, MonitoredEventRow, WebPersonRow
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

_NO_AGENT = "This agent isn't in the workspace."
_ADMINS_ONLY = "Only workspace admins can read monitored conversations."


def _switch(state: MonitoringState) -> dict[str, Any]:
    return {"on": state.on, "since": state.since.isoformat() if state.since else None}


def _conversation(
    row: MonitoredConversationRow, people: dict[uuid.UUID, WebPersonRow]
) -> dict[str, Any]:
    person = people.get(row.account_id)
    return {
        "id": view.conversation_id(row),
        "person": {
            "id": str(person.id) if person else None,
            "name": person.display_name if person else "Someone",
        },
        "where": view.where_of(row.platform),
        "platform": row.platform,
        "questions": row.questions,
        "tools": row.tools,
        "first_at": row.first_at.isoformat(),
        "last_at": row.last_at.isoformat(),
    }


def _event(row: MonitoredEventRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "at": row.created_at.isoformat(),
        "kind": row.kind,
        "content": row.content,
        "tool": row.tool_name,
        "outcome": row.outcome,
    }


async def _monitoring_reader(
    request: Request, caller: Annotated[WebCaller, Depends(current_caller)]
) -> WebCaller:
    """A workspace owner or admin, the bar the chat tools and the agent page share. Checked
    live, before a read is audited."""
    runtime: WebRuntime = request.app.state.runtime
    async with runtime.sessionmaker() as db:
        allowed = await view.may_read_monitored(
            db, tenant_id=caller.person.tenant_id, account_id=caller.person.account_id
        )
    if not allowed:
        raise HTTPException(status_code=403, detail=_ADMINS_ONLY)
    return caller


MonitoringReader = Annotated[WebCaller, Depends(_monitoring_reader)]


def monitoring_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/admin/monitoring")

    async def agent_of(caller: WebCaller, agent_id: str) -> BetaManagedAgentsAgent:
        agents = await listed_agents(runtime, caller.person.tenant_id)
        agent = next((a for a in agents if a.id == agent_id), None)
        if agent is None:
            raise HTTPException(status_code=404, detail=_NO_AGENT)
        return agent

    async def people_of(tenant_id: uuid.UUID) -> dict[uuid.UUID, WebPersonRow]:
        async with runtime.sessionmaker() as db:
            found = await people_store.list_people(db, tenant_id=tenant_id)
        return {p.account_id: p for p in found}

    async def audited(caller: WebCaller, action: str, agent: str, detail: str | None) -> None:
        async with runtime.sessionmaker.begin() as db:
            await audit(
                db,
                tenant_id=caller.person.tenant_id,
                actor_account_id=caller.person.account_id,
                action=action,
                target=agent,
                detail=detail,
                at=runtime.now(),
            )

    @router.get("/agents/{agent_id}")
    async def agent_switches(  # pyright: ignore[reportUnusedFunction]
        caller: MonitoringReader, agent_id: str
    ) -> dict[str, Any]:
        agent = await agent_of(caller, agent_id)
        async with runtime.sessionmaker() as db:
            state = await view.agent_monitoring(
                db, runtime.settings, tenant_id=caller.person.tenant_id, ma_agent_id=agent.id
            )
        return {
            "id": agent.id,
            "name": agent.name,
            "direct_messages": _switch(state.direct_messages),
            "coding_tools": _switch(state.coding_tools),
        }

    @router.get("/agents/{agent_id}/conversations")
    async def conversations(  # pyright: ignore[reportUnusedFunction]
        caller: MonitoringReader,
        agent_id: str,
        response: Response,
        page: Annotated[PageRequest, Depends(page_of(50))],
        person: Annotated[uuid.UUID | None, Query(description="A person's id")] = None,
        where: Where | None = None,
        first: Annotated[date | None, Query(alias="from")] = None,
        last: Annotated[date | None, Query(alias="to")] = None,
    ) -> list[dict[str, Any]]:
        tenant_id = caller.person.tenant_id
        agent = await agent_of(caller, agent_id)
        people = await people_of(tenant_id)
        account = None
        if person is not None:
            account = next((p.account_id for p in people.values() if p.id == person), None)
            if account is None:
                return []
        if page.cursor is None:
            await audited(caller, view.AUDIT_LIST, agent.name, None)
        async with runtime.sessionmaker() as db:
            found = await view.list_conversations(
                db,
                tenant_id=tenant_id,
                ma_agent_id=agent.id,
                where=where,
                account_id=account,
                first=first,
                last=last,
                before=page.cursor,
                limit=page.limit + 1,
            )
        rows = cut(found, page, response, lambda r: (r.last_at, r.cursor_id))
        return [_conversation(r, people) for r in rows]

    @router.get("/agents/{agent_id}/conversations/{conversation_id}")
    async def transcript(  # pyright: ignore[reportUnusedFunction]
        caller: MonitoringReader,
        agent_id: str,
        conversation_id: str,
        response: Response,
        page: Annotated[PageRequest, Depends(page_of(200))],
    ) -> list[dict[str, Any]]:
        tenant_id = caller.person.tenant_id
        agent = await agent_of(caller, agent_id)
        parsed = view.parse_conversation_id(conversation_id)
        if parsed is None:
            raise HTTPException(status_code=404, detail="This conversation isn't kept.")
        who = (await people_of(tenant_id)).get(parsed[0])
        if page.cursor is None:
            await audited(caller, view.AUDIT_OPEN, agent.name, who.display_name if who else None)
        async with runtime.sessionmaker() as db:
            found = await view.get_transcript(
                db,
                tenant_id=tenant_id,
                ma_agent_id=agent.id,
                conversation=conversation_id,
                after=page.cursor,
                limit=page.limit + 1,
            )
        rows = cut(found or [], page, response, lambda r: (r.created_at, r.id))
        return [_event(r) for r in rows]

    return router
