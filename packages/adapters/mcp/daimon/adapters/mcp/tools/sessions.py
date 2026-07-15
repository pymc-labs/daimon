"""Sessions tools: list / get / events / send_message.

``register_sessions_tools(mcp, runtime)`` wires the ``@mcp.tool`` closures for
this group; each closure delegates to a module-private ``_*_impl`` function
that can be unit-tested without a FastMCP Context.

Tenant scope: a session belongs to the caller's tenant iff its ``agent.id`` is
in ``{a.id for a in list_agents_by_tenant(...)}``. Cross-tenant ``get`` /
``events`` / ``send_message`` raise ``ToolError("session not found")`` — terse
and identical for unknown vs. forbidden so existence isn't leaked.
"""

from __future__ import annotations

import datetime
from typing import Any, Literal

from anthropic.types.beta import BetaManagedAgentsSession
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._pagination import Page
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag, list_agents_by_tenant
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict


class SessionInfo(BaseModel):
    id: str
    agent_id: str
    agent_name: str
    title: str | None
    # Plain str, NOT Literal[...] — MA is free to ship a status value the
    # pinned SDK does not model (#214 class; upstream-controlled value set).
    status: str
    created_at: datetime.datetime
    updated_at: datetime.datetime
    archived_at: datetime.datetime | None

    @classmethod
    def from_ma(cls, s: BetaManagedAgentsSession) -> SessionInfo:
        return cls(
            id=s.id,
            agent_id=s.agent.id,
            agent_name=s.agent.name,
            title=s.title,
            status=s.status,
            created_at=s.created_at,
            updated_at=s.updated_at,
            archived_at=s.archived_at,
        )


class SessionEventOut(BaseModel):
    """Permissive projection of an MA session event for ``list_session_events``
    and ``send_message`` output.

    Deliberately NOT the SDK's ``BetaManagedAgentsSessionEvent`` discriminated
    union: the MA API emits event types a pinned SDK version does not model
    (e.g. ``session.thread_status_running`` / ``session.thread_status_idle``,
    present on every completed turn). Pinning the tool's OUTPUT schema to that
    union made FastMCP reject the whole transcript with an output-validation
    error. ``type`` is a plain ``str`` and unknown fields are preserved
    (``extra="allow"``) so new upstream event types pass through untouched
    instead of failing the read.

    Lives here (not in ``agent_chat.py``) because ``agent_chat.py`` already
    imports ``SessionInfo`` FROM this module — importing the other direction
    would create a cycle.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    # Heterogeneous per event kind (text/tool_use/thinking/image blocks on
    # agent.message; None on status events). Kept as raw JSON blocks rather than
    # the SDK's typed content union — the caller folds agent.message text itself.
    content: list[dict[str, Any]] | None = None


class SendMessageOut(BaseModel):
    """Permissive projection of ``BetaManagedAgentsSendSessionEvents`` output.

    Same rationale as ``SessionEventOut`` above — the SDK's send-response
    union can carry event types a pinned SDK version does not model.
    """

    model_config = ConfigDict(extra="allow")

    data: list[SessionEventOut] | None = None


async def _verify_tenant_owns_session(
    runtime: McpRuntime, auth: AuthIdentity, session_id: str
) -> BetaManagedAgentsSession:
    """Retrieve a session and assert the caller's tenant owns its agent.

    Raises ``ToolError("session not found")`` for cross-tenant access — the
    same message used for genuinely missing sessions, so existence isn't
    leaked across tenants.
    """
    s = await runtime.client.beta.sessions.retrieve(session_id)
    tenant_agent_ids = {
        a.id for a in await list_agents_by_tenant(runtime.client, tenant_id=auth.tenant_id)
    }
    if s.agent.id not in tenant_agent_ids:
        raise ToolError("session not found")
    return s


async def _list_sessions_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    page: str | None,
    agent_name: str | None,
) -> list[SessionInfo]:
    if agent_name is not None:
        agent = await find_agent_by_daimon_tag(
            runtime.client, tenant_id=auth.tenant_id, name=agent_name
        )
        if agent is None:
            raise ToolError(f"agent {agent_name!r} not found")
        list_kwargs: dict[str, Any] = {"agent_id": agent.id}
        if page is not None:
            list_kwargs["page"] = page
        results: list[SessionInfo] = []
        async for s in runtime.client.beta.sessions.list(**list_kwargs):
            results.append(SessionInfo.from_ma(s))
        return results

    # Unfiltered: drain across every tenant agent. Cross-agent cursors are
    # not coherent, so we ignore the caller's `page` arg in this branch.
    del page
    agents = await list_agents_by_tenant(runtime.client, tenant_id=auth.tenant_id)
    drained: list[SessionInfo] = []
    for agent in agents:
        async for s in runtime.client.beta.sessions.list(agent_id=agent.id):
            drained.append(SessionInfo.from_ma(s))
    drained.sort(key=lambda r: r.created_at, reverse=True)
    return drained


async def _get_session_impl(
    runtime: McpRuntime, auth: AuthIdentity, session_id: str
) -> SessionInfo:
    s = await _verify_tenant_owns_session(runtime, auth, session_id)
    return SessionInfo.from_ma(s)


async def _list_session_events_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    session_id: str,
    page: str | None,
    limit: int | None,
    order: Literal["asc", "desc"] | None,
) -> Page[SessionEventOut]:
    await _verify_tenant_owns_session(runtime, auth, session_id)
    list_kwargs: dict[str, Any] = {}
    if page is not None:
        list_kwargs["page"] = page
    if limit is not None:
        list_kwargs["limit"] = limit
    if order is not None:
        list_kwargs["order"] = order
    cursor = await runtime.client.beta.sessions.events.list(session_id, **list_kwargs)
    return Page[SessionEventOut](
        items=[
            SessionEventOut.model_validate(event.model_dump(mode="json")) for event in cursor.data
        ],
        next_page=cursor.next_page,
    )


async def _send_message_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    session_id: str,
    text: str,
) -> SendMessageOut:
    await _verify_tenant_owns_session(runtime, auth, session_id)
    resp = await runtime.client.beta.sessions.events.send(
        session_id,
        events=[
            {
                "type": "user.message",
                "content": [{"type": "text", "text": text}],
            }
        ],
    )
    return SendMessageOut.model_validate(resp.model_dump(mode="json"))


def register_sessions_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def list_sessions(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        page: str | None = None,
        agent_name: str | None = None,
    ) -> list[SessionInfo]:
        """List sessions in the tenant pool. ``agent_name`` narrows to one agent."""
        return await _list_sessions_impl(runtime, await _auth(ctx), page, agent_name)

    @mcp.tool
    async def get_session(ctx: Context, session_id: str) -> SessionInfo:  # pyright: ignore[reportUnusedFunction]
        """Look up a session by id (tenant-scoped)."""
        return await _get_session_impl(runtime, await _auth(ctx), session_id)

    @mcp.tool
    async def list_session_events(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        session_id: str,
        page: str | None = None,
        limit: int | None = None,
        order: Literal["asc", "desc"] | None = None,
    ) -> Page[SessionEventOut]:
        """List events for a session (SDK pass-through, single page)."""
        return await _list_session_events_impl(
            runtime, await _auth(ctx), session_id, page, limit, order
        )

    @mcp.tool
    async def send_message(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, session_id: str, text: str
    ) -> SendMessageOut:
        """Post a single ``user.message`` text event to a session."""
        return await _send_message_impl(runtime, await _auth(ctx), session_id, text)
