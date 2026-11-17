"""Monitored agents from chat: the conversations kept for an agent and one transcript.
For workspace admins; each read is written to the audit trail, as in the web app."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._web_admin import (
    PrivateDetails,
    agent_names,
    audit_to,
    private_details,
)
from daimon.core import monitoring_view as view
from daimon.core.cursor import decode_cursor, encode_cursor
from daimon.core.monitoring_view import Where
from daimon.core.stores import web_people as people_store
from daimon.core.stores.domain import MonitoredConversationRow
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

_LIMIT = 100
_EVENTS = 200


@dataclass(frozen=True)
class MonitoredConversation:
    conversation_id: str
    where: Where
    person: str
    person_id: str | None
    questions: int
    tool_calls: int
    first_at: datetime
    last_at: datetime


@dataclass(frozen=True)
class MonitoredActivity:
    agent: str
    direct_messages_on_since: datetime | None
    """When monitoring of direct messages and web chat was turned on; null while off."""
    coding_tools_on_since: datetime | None
    """Same for coding tools."""
    conversations: list[MonitoredConversation]
    next_page: str | None
    private_details: PrivateDetails
    """Names show only in a direct message with the admin; elsewhere `Person 1`, `Person 2`."""


@dataclass(frozen=True)
class MonitoredEvent:
    at: datetime
    kind: str
    content: str | None
    tool: str | None
    outcome: str | None


@dataclass(frozen=True)
class MonitoredTranscript:
    agent: str
    events: list[MonitoredEvent]
    next_page: str | None


def _day(value: str | None, name: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as err:
        raise ToolError(f"{name} must be a date like 2026-01-31.") from err


def _page(after: str | None) -> tuple[datetime, uuid.UUID] | None:
    if not after:
        return None
    try:
        return decode_cursor(after)
    except ValueError as err:
        raise ToolError("next_page is not a page this tool gave.") from err


async def _admin_of(runtime: McpRuntime, auth: AuthIdentity, agent_id: str) -> str:
    """The agent's name, for a caller the core rule lets read monitored activity. An agent's
    own key, a held credential or a client is never that person."""
    if auth.is_external_agent or auth.hold is not None or auth.client:
        raise ToolError("Monitored activity is for workspace admins.")
    async with runtime.session_factory() as db:
        allowed = await view.may_read_monitored(
            db, tenant_id=auth.tenant_id, account_id=auth.account_id, chat_admin=auth.is_admin
        )
    if not allowed:
        raise ToolError("Monitored activity is for workspace admins.")
    name = (await agent_names(runtime, auth.tenant_id)).get(agent_id)
    if name is None:
        raise ToolError("That agent was not found.")
    return name


async def _audited(
    runtime: McpRuntime, auth: AuthIdentity, action: str, agent: str, detail: str | None
) -> None:
    async with runtime.session_factory.begin() as db:
        await audit_to(auth, action, detail)(db, agent)


def _line(
    row: MonitoredConversationRow,
    names: dict[uuid.UUID, tuple[uuid.UUID, str]],
    shown: bool,
    n: int,
) -> MonitoredConversation:
    person = names.get(row.account_id)
    return MonitoredConversation(
        conversation_id=view.conversation_id(row),
        where=view.where_of(row.platform),
        person=(person[1] if person else "Someone") if shown else f"Person {n}",
        person_id=str(person[0]) if person and shown else None,
        questions=row.questions,
        tool_calls=row.tools,
        first_at=row.first_at,
        last_at=row.last_at,
    )


async def _list_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_id: str,
    where: Where | None,
    person_id: str | None,
    first: str | None,
    last: str | None,
    after: str | None,
    limit: int,
    origin_context_id: str | None,
) -> MonitoredActivity:
    agent = await _admin_of(runtime, auth, agent_id)
    details = await private_details(runtime, auth, origin_context_id)
    async with runtime.session_factory() as db:
        people = await people_store.list_people(db, tenant_id=auth.tenant_id)
    by_account = {p.account_id: (p.id, p.display_name) for p in people}
    account: uuid.UUID | None = None
    if person_id:
        try:
            wanted = uuid.UUID(person_id)
        except ValueError as err:
            raise ToolError("person_id is not valid.") from err
        account = next((p.account_id for p in people if p.id == wanted), None)
        if account is None:
            raise ToolError("That person was not found.")
    cursor = _page(after)
    if cursor is None:
        await _audited(runtime, auth, view.AUDIT_LIST, agent, None)
    size = max(1, min(limit, _LIMIT))
    async with runtime.session_factory() as db:
        state = await view.agent_monitoring(
            db, runtime.settings, tenant_id=auth.tenant_id, ma_agent_id=agent_id
        )
        found = await view.list_conversations(
            db,
            tenant_id=auth.tenant_id,
            ma_agent_id=agent_id,
            where=where,
            account_id=account,
            first=_day(first, "first"),
            last=_day(last, "last"),
            before=cursor,
            limit=size + 1,
        )
    rows = found[:size]
    return MonitoredActivity(
        agent=agent,
        direct_messages_on_since=state.direct_messages.since,
        coding_tools_on_since=state.coding_tools.since,
        conversations=[
            _line(r, by_account, details == "shown", n) for n, r in enumerate(rows, start=1)
        ],
        next_page=encode_cursor((rows[-1].last_at, rows[-1].cursor_id))
        if len(found) > size
        else None,
        private_details=details,
    )


async def _transcript_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_id: str,
    conversation_id: str,
    after: str | None,
    origin_context_id: str | None,
) -> MonitoredTranscript:
    agent = await _admin_of(runtime, auth, agent_id)
    # What people said is shown only where no one else can read it.
    if await private_details(runtime, auth, origin_context_id) != "shown":
        raise ToolError(
            "Monitored conversations show only in a direct message with the admin or the web "
            "chat, never in a channel. Ask them to open one."
        )
    parsed = view.parse_conversation_id(conversation_id)
    if parsed is None:
        raise ToolError("conversation_id is not one list_monitored_activity gave.")
    cursor = _page(after)
    if cursor is None:
        async with runtime.session_factory() as db:
            people = await people_store.list_people(db, tenant_id=auth.tenant_id)
        who = next((p.display_name for p in people if p.account_id == parsed[0]), None)
        await _audited(runtime, auth, view.AUDIT_OPEN, agent, who)
    async with runtime.session_factory() as db:
        found = await view.get_transcript(
            db,
            tenant_id=auth.tenant_id,
            ma_agent_id=agent_id,
            conversation=conversation_id,
            after=cursor,
            limit=_EVENTS + 1,
        )
    rows = (found or [])[:_EVENTS]
    return MonitoredTranscript(
        agent=agent,
        events=[
            MonitoredEvent(r.created_at, r.kind, r.content, r.tool_name, r.outcome) for r in rows
        ],
        next_page=encode_cursor((rows[-1].created_at, rows[-1].id))
        if found and len(found) > _EVENTS
        else None,
    )


def register_monitored_activity_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(name="list_monitored_activity", tags={"admin"})
    async def monitored_activity(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_id: str,
        where: Where | None = None,
        person_id: str | None = None,
        first: str | None = None,
        last: str | None = None,
        origin_context_id: str | None = None,
        after: str | None = None,
        limit: int = 50,
    ) -> MonitoredActivity:
        """List the conversations kept for a monitored agent, most recent first, with
        when each monitoring switch was turned on. ``where`` is ``direct_messages``,
        ``web`` or ``coding_tools``; filter by web person and UTC days ``first`` to ``last``
        (YYYY-MM-DD). A person's coding-tool calls make one row per day. Person names show only
        in a direct message with the admin; elsewhere ``private_details`` is
        ``direct_message`` and they read ``Person 1``, ``Person 2``. Pass this active turn's
        origin_context_id. The read is audited. Workspace admins only. ``next_page`` goes
        back as ``after``."""
        return await _list_impl(
            runtime,
            await _auth(ctx),
            agent_id=agent_id,
            where=where,
            person_id=person_id,
            first=first,
            last=last,
            after=after,
            limit=limit,
            origin_context_id=origin_context_id,
        )

    @mcp.tool(name="get_monitored_conversation", tags={"admin"})
    async def monitored_conversation(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_id: str,
        conversation_id: str,
        origin_context_id: str | None = None,
        after: str | None = None,
    ) -> MonitoredTranscript:
        """Read one kept conversation (a ``conversation_id`` from list_monitored_activity)
        in order: questions, answers, coding-tool calls with their outcome, and ``notice``
        when the person was told. Works only in a direct message with the admin or the web
        chat; pass this active turn's origin_context_id. The read is audited. Workspace admins only.
        ``next_page`` goes back as ``after``."""
        return await _transcript_impl(
            runtime,
            await _auth(ctx),
            agent_id=agent_id,
            conversation_id=conversation_id,
            after=after,
            origin_context_id=origin_context_id,
        )


__all__ = ["register_monitored_activity_tools"]
