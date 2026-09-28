import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

from daimon.adapters.cli.commands import audit
from daimon.adapters.cli.main import app
from daimon.core.stores.security_audit import append_event
from daimon.testing.factories import make_account, make_security_audit_event, make_tenant
from typer.testing import CliRunner


def test_audit_rejects_naive_timestamp_before_runtime():
    result = CliRunner().invoke(app, ["audit", "list", str(uuid.uuid4()), "--since", "2026-01-01"])
    assert result.exit_code != 0
    assert "timezone" in result.output


async def test_operator_json_export_filters_tenant_and_account(
    db_nullpool_engine, db_clean, monkeypatch
):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    tenant, other, account = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with sm() as session, session.begin():
        t = await make_tenant(session, id=tenant)
        await make_tenant(session, id=other)
        await make_account(session, id=account, tenant=t)
        for tid in (tenant, other):
            await append_event(
                session,
                tenant_id=tid,
                account_id=account,
                agent_id=None,
                platform="slack",
                platform_user_id="U1",
                tool_name="read",
                operation=None,
                outcome="allowed",
                reason="completed",
            )

    @asynccontextmanager
    async def runtime(_settings):
        yield SimpleNamespace(sessionmaker=sm)

    monkeypatch.setattr(audit, "build_runtime", runtime)
    monkeypatch.setattr(audit, "load_settings", lambda: None)
    # The CLI owns its own event loop; its database engine is NullPool.
    import asyncio

    result = await asyncio.to_thread(
        CliRunner().invoke,
        app,
        [
            "audit",
            "list",
            str(tenant),
            "--account",
            str(account),
            "--since",
            "2026-01-01T00:00:00Z",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert len(rows) == 1 and rows[0]["tenant_id"] == str(tenant)
    assert rows[0]["account_id"] == str(account)


async def test_prune_command_uses_configured_age_and_tenant(
    db_nullpool_engine, db_clean, monkeypatch
):
    import asyncio
    from datetime import UTC, datetime, timedelta

    from daimon.core.stores.security_audit import list_events
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    tenant, other = uuid.uuid4(), uuid.uuid4()
    async with sm() as session, session.begin():
        for tid, age in [(tenant, 8), (tenant, 6), (other, 8)]:
            await make_security_audit_event(
                session, tenant_id=tid, occurred_at=datetime.now(UTC) - timedelta(days=age)
            )

    @asynccontextmanager
    async def runtime(_settings):
        yield SimpleNamespace(sessionmaker=sm)

    monkeypatch.setattr(audit, "build_runtime", runtime)
    monkeypatch.setattr(
        audit, "load_settings", lambda: SimpleNamespace(security_audit_retention_days=7)
    )
    result = await asyncio.to_thread(CliRunner().invoke, app, ["audit", "prune", str(tenant)])
    assert result.exit_code == 0, result.output
    assert "Removed 1" in result.output
    async with sm() as session:
        assert len(await list_events(session, tenant_id=tenant)) == 1
        assert len(await list_events(session, tenant_id=other)) == 1


def test_prune_indefinite_retention_skips_runtime(monkeypatch):
    monkeypatch.setattr(
        audit, "load_settings", lambda: SimpleNamespace(security_audit_retention_days=0)
    )

    def forbidden(_settings):
        raise AssertionError("must not open database")

    monkeypatch.setattr(audit, "build_runtime", forbidden)
    result = CliRunner().invoke(app, ["audit", "prune", str(uuid.uuid4())])
    assert result.exit_code == 0 and "no events removed" in result.output
