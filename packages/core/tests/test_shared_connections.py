"""Shared MCP connections: who sees which tool, what an admin approved, and the verdicts."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from daimon.core import shared_connections as shared
from daimon.core.stores import mcp_connections as store
from daimon.core.stores.domain import DiscoveredTool, McpConnectionToolRow
from daimon.core.stores.web_people import update_person
from daimon.core.tool_safety import ANY_KEY, ResolvedAgentToolRule, ToolSafetyPolicy
from daimon.testing.factories import make_tenant, make_web_person
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession


def _tool(name: str, *, description: str = "Does it.", writes: bool = False) -> DiscoveredTool:
    return DiscoveredTool(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": {"id": {"type": "string"}}},
        writes=writes,
    )


@pytest.fixture(autouse=True)
async def _reset_scope_check() -> AsyncIterator[None]:  # pyright: ignore[reportUnusedFunction]
    yield
    shared.register_scope_check(None)


async def _connection(db: AsyncSession, tenant_id: uuid.UUID, *tools: DiscoveredTool) -> uuid.UUID:
    row = await store.create_connection(
        db,
        tenant_id=tenant_id,
        name="crm",
        url="https://mcp.example.com/mcp",
        encrypted_auth=None,
        scope=None,
    )
    await store.sync_tools(db, tenant_id=tenant_id, connection_id=row.id, discovered=tools)
    return row.id


async def _stored(db: AsyncSession, tenant_id: uuid.UUID, name: str) -> McpConnectionToolRow:
    return next(t for t in await store.list_tools(db, tenant_id=tenant_id) if t.name == name)


async def _approve(
    db: AsyncSession, tenant_id: uuid.UUID, connection: uuid.UUID, name: str
) -> None:
    await shared.approve_tool(
        db,
        tenant_id=tenant_id,
        connection_id=connection,
        name=name,
        seen=shared.seen_digest(await _stored(db, tenant_id, name)),
    )


async def test_listing_follows_approval_status_and_scope(db_session: AsyncSession) -> None:
    """Only approved tools of active connections list; a scoped tool needs its scope held."""
    tenant = await make_tenant(db_session)
    connection = await _connection(
        db_session, tenant.id, _tool("list_contacts"), _tool("export"), _tool("unapproved")
    )
    for name in ("list_contacts", "export"):
        await _approve(db_session, tenant.id, connection, name)
    await store.update_tool(
        db_session,
        tenant_id=tenant.id,
        connection_id=connection,
        name="export",
        values={"scope": "sales"},
    )
    caller = shared.SharedCaller(tenant_id=tenant.id, account_id=uuid.uuid4())

    async def names() -> list[str]:
        return [t.name for t in await shared.served_tools(db_session, caller)]

    assert await names() == ["crm_list_contacts"], "a scoped tool is refused until a check fills in"

    async def holds_sales(caller: shared.SharedCaller, scope: str) -> bool:
        return scope == "sales"

    shared.register_scope_check(holds_sales)
    assert await names() == ["crm_export", "crm_list_contacts"], "the held scope lists its tool"

    await store.update_connection(
        db_session, tenant_id=tenant.id, connection_id=connection, values={"status": "paused"}
    )
    assert await names() == [], "a paused connection serves nothing"


async def test_a_changed_tool_waits_for_approval(db_session: AsyncSession) -> None:
    """Changed upstream words keep the approved ones and serve nothing until re-approved."""
    tenant = await make_tenant(db_session)
    connection = await _connection(db_session, tenant.id, _tool("lookup"))
    await _approve(db_session, tenant.id, connection, "lookup")
    shown = shared.seen_digest(await _stored(db_session, tenant.id, "lookup"))
    await store.sync_tools(
        db_session,
        tenant_id=tenant.id,
        connection_id=connection,
        discovered=[_tool("lookup", description="Now also mails everyone.")],
    )
    caller = shared.SharedCaller(tenant_id=tenant.id, account_id=uuid.uuid4())

    changed = await _stored(db_session, tenant.id, "lookup")

    assert changed.description == "Does it.", "the approved description is kept"
    assert changed.changed, "the admin is shown that the upstream words moved"
    assert await shared.served_tools(db_session, caller) == [], "a changed tool is not served"
    with pytest.raises(shared.SharedConnectionError):
        await shared.approve_tool(
            db_session, tenant_id=tenant.id, connection_id=connection, name="lookup", seen=shown
        )


async def test_refresh_never_lowers_writes(db_session: AsyncSession) -> None:
    """A tool newly marked as writing is a change to approve; a later refresh keeps the mark."""
    tenant = await make_tenant(db_session)
    connection = await _connection(db_session, tenant.id, _tool("lookup"))
    await _approve(db_session, tenant.id, connection, "lookup")

    for writes in (True, False):
        await store.sync_tools(
            db_session,
            tenant_id=tenant.id,
            connection_id=connection,
            discovered=[_tool("lookup", writes=writes)],
        )
        tool = await _stored(db_session, tenant.id, "lookup")
        assert tool.writes, f"still writes after a refresh saying writes={writes}"
        assert tool.changed, "the new write mark waits for approval"


def test_daimon_tool_names_are_left_out() -> None:
    """An upstream tool whose served name is a Daimon tool's is skipped and named."""
    clash = next(n for n in sorted(shared.DAIMON_TOOL_NAMES) if n.startswith("add_"))
    listed: tuple[dict[str, JsonValue], ...] = (
        {"name": clash.removeprefix("add_")},
        {"name": "lookup"},
    )

    found, left_out = shared._discovered(ToolSafetyPolicy(), "add", listed)  # pyright: ignore[reportPrivateUsage]

    assert [t.name for t in found] == ["lookup"], f"found: {found}"
    assert left_out == [f"{clash} (a Daimon tool's name)"], f"left out: {left_out}"


async def test_refresh_keeps_a_pause(db_session: AsyncSession) -> None:
    """A good read clears an error, and no read, good or failed, resumes a pause."""
    tenant = await make_tenant(db_session)
    connection = await _connection(db_session, tenant.id)
    for before, after in (("paused", "paused"), ("error", "active")):
        await store.update_connection(
            db_session, tenant_id=tenant.id, connection_id=connection, values={"status": before}
        )
        await store.record_refresh(
            db_session, tenant_id=tenant.id, connection_id=connection, note=None
        )
        row = await store.get_connection(db_session, tenant_id=tenant.id, connection_id=connection)
        assert row is not None and row.status == after, f"{before} became {row}"
    await store.update_connection(
        db_session, tenant_id=tenant.id, connection_id=connection, values={"status": "paused"}
    )
    await store.mark_connection(
        db_session, tenant_id=tenant.id, connection_id=connection, status="error", last_error="x"
    )
    row = await store.get_connection(db_session, tenant_id=tenant.id, connection_id=connection)
    assert row is not None and row.status == "paused", f"a failed read resumed it: {row}"


async def test_visitors_get_no_shared_tools(db_session: AsyncSession) -> None:
    """A web visitor sees no shared tool, even an approved, unscoped one."""
    tenant = await make_tenant(db_session)
    connection = await _connection(db_session, tenant.id, _tool("lookup"))
    await _approve(db_session, tenant.id, connection, "lookup")
    person = await make_web_person(db_session, tenant=tenant)
    caller = shared.SharedCaller(tenant_id=tenant.id, account_id=person.account_id)
    assert len(await shared.served_tools(db_session, caller)) == 1, "a member sees it"

    await update_person(db_session, person_id=person.id, kind="visitor")

    assert await shared.served_tools(db_session, caller) == [], "a visitor sees none"


def test_outside_clients_get_reads_only() -> None:
    """`unattended_writes` lets a routine write, never an outside client."""
    policy = ToolSafetyPolicy(unattended_writes=(ANY_KEY,))

    def outcome(*, outside: bool) -> str:
        return shared.decide_shared_call(
            policy, "crm_send", {}, writes=True, attended=False, agent_rule=None, outside=outside
        ).outcome

    assert outcome(outside=False) == "allow", "a routine may write when the operator allows"
    assert outcome(outside=True) == "deny", "an outside client may not"


@pytest.mark.parametrize(
    ("writes", "attended", "rule", "expected"),
    [
        (False, False, None, ("allow", "read")),
        (True, True, None, ("ask", "write_needs_confirmation")),
        (True, False, None, ("deny", "unattended_write")),
        (False, True, ResolvedAgentToolRule(rule="deny"), ("deny", "agent_rule_deny")),
    ],
    ids=["read runs", "write asks", "unattended write refused", "agent rule blocks"],
)
def test_verdicts(
    writes: bool, attended: bool, rule: ResolvedAgentToolRule | None, expected: tuple[str, str]
) -> None:
    """A shared tool is third-party: a write asks, and an agent's deny rule wins."""
    verdict = shared.decide_shared_call(
        ToolSafetyPolicy(agent_rules=True),
        "crm_send",
        {},
        writes=writes,
        attended=attended,
        agent_rule=rule,
    )

    assert (verdict.outcome, verdict.reason) == expected, f"verdict: {verdict}"
