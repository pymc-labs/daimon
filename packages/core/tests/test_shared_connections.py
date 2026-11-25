"""Shared MCP connections: who sees which tool, what an admin approved, and the verdicts."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from daimon.core import shared_connections as shared
from daimon.core.stores import mcp_connections as store
from daimon.core.stores.domain import DiscoveredTool, McpConnectionToolRow
from daimon.core.tool_safety import ResolvedAgentToolRule, ToolSafetyPolicy
from daimon.testing.factories import make_tenant
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


async def test_approved_words_are_pinned(db_session: AsyncSession) -> None:
    """A changed upstream description shows as a change and serves nothing new until approved."""
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

    changed = await _stored(db_session, tenant.id, "lookup")

    assert changed.description == "Does it.", "callers still get the approved description"
    assert changed.changed, "the admin is shown that the upstream words moved"
    with pytest.raises(shared.SharedConnectionError):
        await shared.approve_tool(
            db_session, tenant_id=tenant.id, connection_id=connection, name="lookup", seen=shown
        )


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
