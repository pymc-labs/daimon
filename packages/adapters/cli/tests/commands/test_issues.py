"""`daimon issues summary | list | show | resolve | mute | reopen`, run through the CLI on a real DB."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from daimon.adapters.cli.commands import issues
from daimon.adapters.cli.main import app
from daimon.core.ops_catalog import OpsCode
from daimon.core.ops_events import OpsEvent, record_of
from daimon.core.stores import ops_events as store
from daimon.core.stores import web_audit
from daimon.core.stores.domain import TenantRow
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner

AGENT_ID = "agent-1"


@pytest.fixture
def sm(schema_sessionmaker: async_sessionmaker[AsyncSession]) -> async_sessionmaker[AsyncSession]:
    return schema_sessionmaker


@pytest.fixture(autouse=True)
def cli_host(sm: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch) -> None:
    """The command's runtime on the test DB, and the workspace's one agent named."""

    @asynccontextmanager
    async def runtime(_settings: object):
        yield SimpleNamespace(sessionmaker=sm, anthropic=object())

    async def listed(*_args: object, **_kwargs: object) -> list[SimpleNamespace]:
        return [SimpleNamespace(id=AGENT_ID, name="Research agent")]

    monkeypatch.setattr(issues, "build_runtime", runtime)
    monkeypatch.setattr(
        issues, "load_settings", lambda: SimpleNamespace(billing=SimpleNamespace(markup=Decimal(1)))
    )
    monkeypatch.setattr(issues, "list_agents_by_tenant", listed)


async def _issue(
    sm: async_sessionmaker[AsyncSession], code: OpsCode = "turn.failed"
) -> tuple[TenantRow, str]:
    """A workspace with one issue: the workspace and the issue's fingerprint."""
    async with sm() as session, session.begin():
        tenant = await make_tenant(session, platform="discord")
        record = record_of(
            OpsEvent(code, tenant.id, reason="timeout", agent_id=AGENT_ID, platform="discord"),
            at=datetime.now(UTC),
        )
        await store.record_ops_events(session, tenant_id=tenant.id, events=[record])
    return tenant, record.fingerprint


async def _run(*args: str) -> Any:
    # The command owns its event loop, so it runs off this one.
    return await asyncio.to_thread(CliRunner().invoke, app, ["issues", *args])


async def test_summary_reports_the_workspace_and_its_issue(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    tenant, fingerprint = await _issue(sm)

    text = await _run("summary", str(tenant.id))
    as_json = await _run("summary", str(tenant.id), "--json")

    assert text.exit_code == 0 and "Status" in text.output, text.output
    body = json.loads(as_json.stdout)
    assert [i["fingerprint"] for i in body["attention"]] == [fingerprint]
    assert body["attention"][0]["subject"]["name"] == "Research agent", (
        "the subject carries the agent's real name"
    )


async def test_list_filters_by_area_and_shows_real_agent_names(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    tenant, fingerprint = await _issue(sm)

    everything = json.loads((await _run("list", str(tenant.id), "--json")).stdout)
    none = json.loads((await _run("list", str(tenant.id), "--area", "dm", "--json")).stdout)
    bad = await _run("list", str(tenant.id), "--since", "soon")

    assert [(i["fingerprint"], i["subject"]["name"]) for i in everything] == [
        (fingerprint, "Research agent")
    ]
    assert none == [], "the area filter drops it"
    assert bad.exit_code != 0, "an unreadable window is refused"


async def test_show_prints_the_issue_with_events_and_refuses_unknown_ones(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    tenant, fingerprint = await _issue(sm)

    shown = await _run("show", str(tenant.id), fingerprint, "--json")
    missing = await _run("show", str(tenant.id), "nope")

    body = json.loads(shown.stdout)
    assert body["agent"]["name"] == "Research agent" and len(body["events"]) == 1
    assert missing.exit_code != 0 and "no such issue" in missing.output


async def test_resolve_mute_and_reopen_change_the_state_and_name_an_actor(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    tenant, fingerprint = await _issue(sm)
    async with sm() as session, session.begin():
        member = await make_account(session, tenant=tenant)
        await make_platform_principal(
            session, platform="discord", external_id="u-ops", tenant=tenant, account=member
        )

    async def state(*extra: str) -> str:
        found = json.loads((await _run("list", str(tenant.id), *extra, "--json")).stdout)
        return found[0]["state"]

    assert (await _run("resolve", str(tenant.id), fingerprint, "--account", "u-ops")).exit_code == 0
    assert await state() == "resolved"
    assert (await _run("mute", str(tenant.id), fingerprint)).exit_code == 0
    assert await state() == "muted"
    assert (await _run("reopen", str(tenant.id), fingerprint)).exit_code == 0
    assert await state() == "open"

    async with sm() as session:
        rows = await web_audit.list_recent(session, tenant_id=tenant.id)
    by_action = {r.action: r for r in rows}
    assert len(rows) == 3, "each change is audited"
    resolved = next(r for r in rows if r.actor_account_id is not None)
    assert resolved.actor_account_id == member.id, "the named member is the actor"
    assert all("via the CLI" in (r.detail or "") for r in by_action.values()), (
        "every row says it came from the CLI, including the two with no account"
    )


async def test_a_change_with_an_account_from_another_workspace_is_refused(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    tenant, fingerprint = await _issue(sm)
    other, _ = await _issue(sm, code="dm.failed")
    async with sm() as session, session.begin():
        stranger = await make_account(session, tenant=other)

    refused = await _run("resolve", str(tenant.id), fingerprint, "--account", str(stranger.id))

    assert refused.exit_code != 0 and "no account" in refused.output
    still = json.loads((await _run("list", str(tenant.id), "--json")).stdout)
    assert still[0]["state"] == "open", "nothing changed"


@pytest.mark.parametrize("command", ["show", "resolve", "mute", "reopen"])
async def test_another_workspaces_issue_is_not_found(
    sm: async_sessionmaker[AsyncSession], command: str
) -> None:
    mine, _ = await _issue(sm)
    _, theirs = await _issue(sm, code="dm.failed")

    result = await _run(command, str(mine.id), theirs)

    assert result.exit_code != 0 and "no such issue" in result.output
    listed = json.loads((await _run("list", str(mine.id), "--json")).stdout)
    assert theirs not in [i["fingerprint"] for i in listed], "it never shows in the list either"
