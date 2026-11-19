"""`daimon monitoring list`: an agent's kept conversations, counts only, and the audit row."""

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from daimon.adapters.cli.commands import monitoring
from daimon.adapters.cli.main import app
from daimon.core import monitoring_view as view
from daimon.core.stores import monitoring as store
from daimon.core.stores.web_audit import list_recent
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from typer.testing import CliRunner

_AT = datetime(2030, 1, 15, 12, tzinfo=UTC)


def _plain(output: str) -> str:
    """Rich colours and wraps usage errors on CI; compare the words only."""
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", output).split())


def test_monitoring_list_rejects_a_bad_day_before_runtime() -> None:
    """A malformed date is refused before any database is opened."""
    result = CliRunner().invoke(
        app, ["monitoring", "list", str(uuid.uuid4()), "--agent", "agt_1", "--first", "soon"]
    )

    assert result.exit_code != 0, result.output
    assert "--first" in _plain(result.output), result.output


async def test_monitoring_list_gives_counts_without_content_and_audits(
    db_nullpool_engine: AsyncEngine, db_clean: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The list shows counts and times for the agent only, never what was said, and one
    audit row records the read."""
    sm = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    tenant, account = uuid.uuid4(), uuid.uuid4()
    async with sm.begin() as session:
        t = await make_tenant(session, id=tenant)
        await make_account(session, id=account, tenant=t)
        subject = store.MonitoredSubject(
            tenant_id=tenant,
            agent_id=view.agent_uuid(tenant, "agt_1"),
            platform="web",
            account_id=account,
            conversation="w1",
        )
        await store.record_events(
            session,
            subject,
            [
                store.MonitoredEntry(
                    kind="question",
                    content="SECRET QUESTION",
                    tool_name=None,
                    outcome=None,
                    turn_ref="t1",
                    created_at=_AT,
                )
            ],
        )

    @asynccontextmanager
    async def runtime(_settings: object) -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(sessionmaker=sm)

    monkeypatch.setattr(monitoring, "build_runtime", runtime)
    monkeypatch.setattr(monitoring, "load_settings", lambda: None)

    result = await asyncio.to_thread(
        CliRunner().invoke,
        app,
        ["monitoring", "list", str(tenant), "--agent", "agt_1", "--json"],
    )

    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert [(r["where"], r["questions"], r["tool_calls"]) for r in rows] == [("web", 1, 0)], rows
    assert "SECRET QUESTION" not in result.output, "what was said stays out of the command line"
    async with sm() as session:
        audited = await list_recent(session, tenant_id=tenant)
    assert [(e.action, e.target) for e in audited] == [(view.AUDIT_LIST, "agt_1")], audited
