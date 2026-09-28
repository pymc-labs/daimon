import asyncio
import uuid

import pytest
from daimon.adapters.mcp.middleware.mcp_identity import IdentityMiddleware
from daimon.core.operation_policy import TargetFacts, decide_operation
from daimon.core.stores.security_audit import list_events
from daimon.testing.factories import make_account, make_tenant
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError


async def seed_identity(sm, tenant, account):
    async with sm() as session, session.begin():
        t = await make_tenant(session, id=tenant)
        await make_account(session, id=account, tenant=t)


async def drain(server):
    for middleware in server.middleware:
        if isinstance(middleware, IdentityMiddleware):
            await middleware.drain_audit()


def make_audited_server(sessionmaker, tenant, account):
    async def subject(_ctx):
        return str(account)

    async def tenant_claim(_ctx):
        return str(tenant)

    async def absent(_ctx):
        return None

    async def role(_ctx):
        return "user"

    mcp = FastMCP("audit-test")
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=subject,
            tenant_resolver=tenant_claim,
            role_resolver=role,
            agent_id_resolver=absent,
            is_admin_resolver=absent,
            internal_resolver=absent,
            sessionmaker=sessionmaker,
        )
    )

    @mcp.tool
    async def attempt(deny: bool, secret: str) -> str:
        outcome = decide_operation(
            "key_remove",
            is_admin=not deny,
            target=TargetFacts(is_daimon_managed=False, is_reachable_in_tenant=True),
        )
        if outcome != "allow":
            raise ToolError(f"sensitive error {secret}")
        return secret

    return mcp


async def test_allowed_and_denied_calls_write_exactly_one_metadata_event(committing_sessionmaker):
    sessionmaker = committing_sessionmaker
    tenant, account = uuid.uuid4(), uuid.uuid4()
    await seed_identity(sessionmaker, tenant, account)
    server = make_audited_server(sessionmaker, tenant, account)
    async with Client(server) as client:
        await client.call_tool("attempt", {"deny": False, "secret": "NEVER_SAVE_THIS"})
        with pytest.raises(Exception, match="sensitive error"):
            await client.call_tool("attempt", {"deny": True, "secret": "NEVER_SAVE_THIS"})
    await drain(server)
    async with sessionmaker() as session:
        rows = await list_events(session, tenant_id=tenant)
        listings = [row for row in rows if row.tool_name == "tools/list"]
        assert listings and all(row.outcome == "allowed" for row in listings)
        rows = [row for row in rows if row.tool_name == "attempt"]
        assert len(rows) == 2
        assert [row.outcome for row in rows] == ["allowed", "denied"]
        assert [row.reason for row in rows] == ["policy_allow", "needs_admin"]
        assert all(row.tool_name == "attempt" and row.operation == "key_remove" for row in rows)
        assert all(row.account_id == account for row in rows)
        assert "NEVER_SAVE_THIS" not in str(rows)
        assert await list_events(session, tenant_id=uuid.uuid4()) == []


async def test_concurrent_tenants_do_not_share_policy_state(committing_sessionmaker):
    sessionmaker = committing_sessionmaker
    tenants = [uuid.uuid4(), uuid.uuid4()]

    async def call(tenant, denied):
        account = uuid.uuid4()
        await seed_identity(sessionmaker, tenant, account)
        server = make_audited_server(sessionmaker, tenant, account)
        async with Client(server) as client:
            await client.call_tool(
                "attempt", {"deny": denied, "secret": "private"}, raise_on_error=False
            )

        await drain(server)

    await asyncio.gather(call(tenants[0], True), call(tenants[1], False))
    async with sessionmaker() as session:
        first = await list_events(session, tenant_id=tenants[0])
        second = await list_events(session, tenant_id=tenants[1])
    first = [row for row in first if row.tool_name == "attempt"]
    second = [row for row in second if row.tool_name == "attempt"]
    assert len(first) == len(second) == 1
    assert first[0].outcome == "denied" and second[0].outcome == "allowed"


@pytest.mark.parametrize("agent_claim", ["agent_id", "chat_agent_id"])
async def test_http_claims_and_hidden_tool_denial_are_audited(committing_sessionmaker, agent_claim):
    from daimon.adapters.mcp.middleware import mcp_identity as identity
    from daimon.testing.asgi import call_mcp_tool
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    tenant, account, agent = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await seed_identity(committing_sessionmaker, tenant, account)
    token = "audit-test-credential"
    mcp = FastMCP(
        "claim-audit",
        auth=StaticTokenVerifier(
            tokens={
                token: {
                    "sub": str(account),
                    "tenant_id": str(tenant),
                    "role": "user",
                    "client_id": "test",
                    agent_claim: str(agent),
                    "platform": "discord",
                    "platform_user_id": "12345",
                }
            }
        ),
    )
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=identity.production_subject_resolver,
            tenant_resolver=identity.production_tenant_resolver,
            role_resolver=identity.production_role_resolver,
            agent_id_resolver=identity.production_agent_id_resolver,
            is_admin_resolver=identity.production_is_admin_resolver,
            internal_resolver=identity.production_internal_resolver,
            sessionmaker=committing_sessionmaker,
        )
    )

    @mcp.tool(tags={"agent-chat"})
    async def visible() -> str:
        return "ok"

    @mcp.tool
    async def hidden() -> str:
        raise AssertionError("must not execute")

    app = mcp.http_app()
    await call_mcp_tool(app, token=token, name="visible", arguments={})
    if agent_claim == "agent_id":
        await call_mcp_tool(app, token=token, name="hidden", arguments={})
    await drain(mcp)
    async with committing_sessionmaker() as session:
        rows = await list_events(session, tenant_id=tenant)
    calls = [row for row in rows if row.tool_name in {"visible", "hidden"}]
    assert len(calls) == (2 if agent_claim == "agent_id" else 1)
    assert [row.outcome for row in calls] == (
        ["allowed", "denied"] if agent_claim == "agent_id" else ["allowed"]
    )
    assert all(row.account_id == account and row.agent_id == agent for row in calls)
    assert all(row.platform == "discord" and row.platform_user_id == "12345" for row in calls)
    assert token not in str(rows)


async def test_audit_outage_preserves_call_and_logs_only_error_type(
    committing_sessionmaker, monkeypatch
):
    from daimon.adapters.mcp.middleware import mcp_identity
    from structlog.testing import capture_logs

    async def unavailable(*args, **kwargs):
        raise OSError("do not log this sensitive diagnostic")

    monkeypatch.setattr(mcp_identity, "append_event", unavailable)
    with capture_logs() as logs:
        server = make_audited_server(committing_sessionmaker, uuid.uuid4(), uuid.uuid4())
        async with Client(server) as client:
            result = await client.call_tool("attempt", {"deny": False, "secret": "ok"})
        await drain(server)
    assert not result.is_error
    failures = [entry for entry in logs if entry.get("event") == "security_audit.write_failed"]
    assert failures and all(entry["error_type"] == "OSError" for entry in failures)
    assert "sensitive diagnostic" not in str(failures)


async def test_hanging_audit_does_not_delay_tool_response(committing_sessionmaker, monkeypatch):
    from daimon.adapters.mcp.middleware import mcp_identity
    from structlog.testing import capture_logs

    started = asyncio.Event()

    async def hang(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(mcp_identity, "append_event", hang)
    server = make_audited_server(committing_sessionmaker, uuid.uuid4(), uuid.uuid4())
    with capture_logs() as logs:
        async with Client(server) as client:
            # Includes client discovery/listing; no 2-second DB timeout may block it.
            async with asyncio.timeout(0.5):
                result = await client.call_tool("attempt", {"deny": False, "secret": "ok"})
            assert not result.is_error
            await started.wait()
        await drain(server)
    assert any(entry.get("error_type") == "TimeoutError" for entry in logs)


async def test_tool_crash_is_error_not_authorization_denial(committing_sessionmaker):
    tenant, account = uuid.uuid4(), uuid.uuid4()
    await seed_identity(committing_sessionmaker, tenant, account)
    server = make_audited_server(committing_sessionmaker, tenant, account)

    @server.tool
    async def broken() -> str:
        raise ToolError("private failure detail")

    async with Client(server) as client:
        await client.call_tool("broken", {}, raise_on_error=False)
    await drain(server)
    async with committing_sessionmaker() as session:
        rows = await list_events(session, tenant_id=tenant)
    errors = [row for row in rows if row.tool_name == "broken"]
    assert len(errors) == 1 and errors[0].outcome == "error"
    assert errors[0].reason == "tool_error"
    assert "private failure detail" not in str(errors)


async def test_pending_audit_limit_logs_overflow_and_tracks_tasks(
    committing_sessionmaker, monkeypatch
):
    from daimon.core.stores.security_audit import SecurityAuditEntry
    from structlog.testing import capture_logs

    server = make_audited_server(committing_sessionmaker, uuid.uuid4(), uuid.uuid4())
    middleware = next(m for m in server.middleware if isinstance(m, IdentityMiddleware))
    release = asyncio.Event()

    async def write(_event):
        await release.wait()

    monkeypatch.setattr(middleware, "_write_audit", write)
    middleware._audit_max_pending = 1
    event = SecurityAuditEntry(
        tenant_id=uuid.uuid4(),
        account_id=None,
        agent_id=None,
        platform=None,
        platform_user_id=None,
        tool_name="read",
        operation=None,
        outcome="allowed",
        reason="completed",
    )
    with capture_logs() as logs:
        middleware._queue_audit(event)
        middleware._queue_audit(event)
        assert len(middleware._audit_tasks) == 1
        release.set()
        await drain(server)
    assert any(entry.get("error_type") == "QueueFull" for entry in logs)
    assert not middleware._audit_tasks


async def test_connection_bound_factory_writes_outside_tool_rollback(committing_sessionmaker):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    tenant, account = uuid.uuid4(), uuid.uuid4()
    await seed_identity(committing_sessionmaker, tenant, account)
    async with committing_sessionmaker() as owner:
        connection = await owner.connection()
        bound_factory = async_sessionmaker(connection, expire_on_commit=False)
        server = make_audited_server(bound_factory, tenant, account)
        async with Client(server) as client:
            await client.call_tool("attempt", {"deny": False, "secret": "ok"})
            # The tool transaction can keep using its connection while writes run.
            assert await owner.scalar(text("SELECT 1")) == 1
            await drain(server)
        await owner.rollback()
    async with committing_sessionmaker() as session:
        rows = await list_events(session, tenant_id=tenant)
    calls = [row for row in rows if row.tool_name == "attempt"]
    assert len(calls) == 1 and calls[0].outcome == "allowed"


async def test_request_timestamps_preserve_order_when_inserts_finish_in_reverse(
    committing_sessionmaker, monkeypatch
):
    from daimon.adapters.mcp.middleware import mcp_identity

    original_append = mcp_identity.append_event
    denied_inserted = asyncio.Event()
    inserted = []

    async def reordered(session, **values):
        if values["tool_name"] == "attempt" and values["outcome"] == "allowed":
            await denied_inserted.wait()
        row = await original_append(session, **values)
        if values["tool_name"] == "attempt":
            inserted.append(values["outcome"])
            if values["outcome"] == "denied":
                denied_inserted.set()
        return row

    monkeypatch.setattr(mcp_identity, "append_event", reordered)
    tenant, account = uuid.uuid4(), uuid.uuid4()
    await seed_identity(committing_sessionmaker, tenant, account)
    server = make_audited_server(committing_sessionmaker, tenant, account)
    async with Client(server) as client:
        await client.call_tool("attempt", {"deny": False, "secret": "ok"})
        await client.call_tool("attempt", {"deny": True, "secret": "ok"}, raise_on_error=False)
    await drain(server)
    assert inserted == ["denied", "allowed"]
    async with committing_sessionmaker() as session:
        rows = await list_events(session, tenant_id=tenant)
    calls = [row for row in rows if row.tool_name == "attempt"]
    assert [row.outcome for row in calls] == ["allowed", "denied"]
    assert calls[0].occurred_at < calls[1].occurred_at
