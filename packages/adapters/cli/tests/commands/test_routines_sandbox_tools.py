"""`daimon routines sandbox-tools`: the operator's cutover for a confirmed list of routines."""

import uuid
from datetime import UTC, datetime
from io import StringIO
from unittest.mock import AsyncMock

import daimon.adapters.cli.commands.routines as routines_cli
import pytest
from daimon.adapters.cli.commands.routines import run_agent_sandbox_tools, run_sandbox_tools
from daimon.core.authz import Subject
from daimon.core.errors import UserFacingError
from daimon.core.routine_sandbox import SandboxPinUnavailable
from daimon.core.stores.agent_sandbox import (
    agent_sandbox_history,
    agent_sandbox_pin,
    agent_sandbox_pins,
    set_agent_sandbox_tools,
)
from daimon.core.stores.domain import AgentSandboxPin, SandboxPin
from daimon.core.stores.routines import create_routine, get_routine, sandbox_tools_history
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_agent
from rich.console import Console

from ..harness import build_cli_runtime

PIN = SandboxPin(
    agent_id="ag",
    agent_version=3,
    environment_id="env_1",
    environment_updated_at=datetime(2026, 10, 1, tzinfo=UTC),
)


@pytest.fixture(autouse=True)
def _current_pin(monkeypatch):
    """The routine's agent and environment, as Managed Agents would report them."""
    monkeypatch.setattr(routines_cli, "current_pin", AsyncMock(return_value=PIN))


async def _routines(db_session_factory, count: int):
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, workspace_id="cutover-guild")
        rows = [
            await create_routine(
                session,
                tenant_id=tenant.id,
                created_by_user_id="owner",
                agent_id="ag",
                agent_name="daimon",
                cron_expr="0 9 * * *",
                timezone_="UTC",
                trigger_message=f"routine {n}",
            )
            for n in range(count)
        ]
    return tenant.id, rows


async def _enabled(db_session_factory, tenant_id, rows) -> list[bool]:
    async with db_session_factory() as session:
        return [
            (await get_routine(session, row.id, tenant_id=tenant_id)).sandbox_tools_enabled
            for row in rows
        ]


async def test_dry_run_reports_and_writes_nothing(db_session_factory) -> None:
    tenant_id, rows = await _routines(db_session_factory, 2)
    output = StringIO()

    await run_sandbox_tools(
        rt=build_cli_runtime(db_session_factory),
        console=Console(file=output, width=200),
        tenant_id=tenant_id,
        routine_ids=[rows[0].id],
        enabled=True,
        dry_run=True,
    )

    assert f"{rows[0].id}: turn on" in output.getvalue()
    assert (
        "(agent ag version 3, environment env_1 as of 2026-10-01T00:00:00+00:00)"
        in output.getvalue()
    )
    assert "Dry run: 1 routine(s) would change" in output.getvalue()
    assert await _enabled(db_session_factory, tenant_id, rows) == [False, False]


async def test_bulk_opt_in_changes_only_listed_routines_and_is_idempotent(
    db_session_factory,
) -> None:
    tenant_id, rows = await _routines(db_session_factory, 3)
    rt = build_cli_runtime(db_session_factory)
    listed = [rows[0].id, rows[1].id]

    for _ in range(2):
        output = StringIO()
        await run_sandbox_tools(
            rt=rt,
            console=Console(file=output, width=200),
            tenant_id=tenant_id,
            routine_ids=listed,
            enabled=True,
            dry_run=False,
        )

    assert "already on" in output.getvalue()
    assert "0 routine(s); 2 unchanged" in output.getvalue()
    assert await _enabled(db_session_factory, tenant_id, rows) == [True, True, False]
    async with db_session_factory() as session:
        [entry] = await sandbox_tools_history(session, rows[0].id, tenant_id=tenant_id)
    assert entry.via == "cli" and entry.after_enabled is True and entry.set_by


async def test_an_unknown_id_changes_nothing(db_session_factory) -> None:
    tenant_id, rows = await _routines(db_session_factory, 1)

    with pytest.raises(UserFacingError, match="Nothing was changed"):
        await run_sandbox_tools(
            rt=build_cli_runtime(db_session_factory),
            console=Console(file=StringIO()),
            tenant_id=tenant_id,
            routine_ids=[rows[0].id, uuid.uuid4()],
            enabled=True,
            dry_run=False,
        )

    assert await _enabled(db_session_factory, tenant_id, rows) == [False]


async def test_off_reverts_an_opt_in(db_session_factory) -> None:
    tenant_id, rows = await _routines(db_session_factory, 1)
    rt = build_cli_runtime(db_session_factory)
    for enabled in (True, False):
        await run_sandbox_tools(
            rt=rt,
            console=Console(file=StringIO()),
            tenant_id=tenant_id,
            routine_ids=[rows[0].id],
            enabled=enabled,
            dry_run=False,
        )

    assert await _enabled(db_session_factory, tenant_id, rows) == [False]
    async with db_session_factory() as session:
        history = await sandbox_tools_history(session, rows[0].id, tenant_id=tenant_id)
    assert [entry.after_enabled for entry in history] == [True, False]


async def test_a_listed_dm_routine_refuses_the_whole_batch(db_session_factory) -> None:
    tenant_id, rows = await _routines(db_session_factory, 1)
    async with db_session_factory.begin() as session:
        dm = await create_routine(
            session,
            tenant_id=tenant_id,
            created_by_user_id="1001",
            agent_id="ag",
            agent_name="daimon",
            cron_expr="0 9 * * *",
            timezone_="UTC",
            trigger_message="private",
            source_provenance_known=True,
            destination_kind="dm",
            destination_id="1001",
        )

    with pytest.raises(UserFacingError, match="DM routines can't use sandbox tools"):
        await run_sandbox_tools(
            rt=build_cli_runtime(db_session_factory),
            console=Console(file=StringIO()),
            tenant_id=tenant_id,
            routine_ids=[rows[0].id, dm.id],
            enabled=True,
            dry_run=True,
        )

    assert await _enabled(db_session_factory, tenant_id, [*rows, dm]) == [False, False]


async def test_an_unreadable_agent_or_environment_refuses_the_whole_batch(
    db_session_factory, monkeypatch
) -> None:
    tenant_id, rows = await _routines(db_session_factory, 2)
    monkeypatch.setattr(
        routines_cli,
        "current_pin",
        AsyncMock(side_effect=[PIN, SandboxPinUnavailable("archived")]),
    )

    with pytest.raises(UserFacingError, match="Can't read the routine's agent version"):
        await run_sandbox_tools(
            rt=build_cli_runtime(db_session_factory),
            console=Console(file=StringIO()),
            tenant_id=tenant_id,
            routine_ids=[row.id for row in rows],
            enabled=True,
            dry_run=False,
        )

    assert await _enabled(db_session_factory, tenant_id, rows) == [False, False]


async def test_re_running_after_an_agent_edit_re_approves_it(
    db_session_factory, monkeypatch
) -> None:
    tenant_id, rows = await _routines(db_session_factory, 1)
    rt = build_cli_runtime(db_session_factory)
    for version in (3, 4):
        monkeypatch.setattr(
            routines_cli,
            "current_pin",
            AsyncMock(return_value=SandboxPin("ag", version, "env_1", PIN.environment_updated_at)),
        )
        output = StringIO()
        await run_sandbox_tools(
            rt=rt,
            console=Console(file=output, width=200),
            tenant_id=tenant_id,
            routine_ids=[rows[0].id],
            enabled=True,
            dry_run=False,
        )

    assert "re-approve current agent and environment" in output.getvalue()
    async with db_session_factory() as session:
        row = await get_routine(session, rows[0].id, tenant_id=tenant_id)
    assert row is not None and row.sandbox_agent_version == 4


async def test_agent_opt_in_records_the_agent_and_environment_not_a_version(
    db_session_factory, monkeypatch
) -> None:
    """`--agent` pins the agent id and environment name, then `--off` forgets them."""
    tenant_id, _ = await _routines(db_session_factory, 0)
    pin = AgentSandboxPin("ag", "default")
    monkeypatch.setattr(routines_cli, "current_agent_pin", AsyncMock(return_value=pin))
    monkeypatch.setattr(
        routines_cli, "find_agent_by_daimon_tag", AsyncMock(return_value=ma_agent(id="ag"))
    )
    output = StringIO()
    kwargs = {
        "rt": build_cli_runtime(db_session_factory),
        "console": Console(file=output, width=200),
        "tenant_id": tenant_id,
        "agent_name": "daimon",
        "environment_name": None,
        "dry_run": False,
    }

    await run_agent_sandbox_tools(enabled=True, **kwargs)
    async with db_session_factory() as session:
        assert await agent_sandbox_pin(session, tenant_id=tenant_id, agent_id="ag") == pin
    await run_agent_sandbox_tools(enabled=True, **kwargs)
    assert "already on" in output.getvalue(), "a repeat writes nothing"
    await run_agent_sandbox_tools(enabled=False, **kwargs)

    async with db_session_factory() as session:
        assert await agent_sandbox_pin(session, tenant_id=tenant_id, agent_id="ag") is None
        history = await agent_sandbox_history(session, tenant_id=tenant_id, agent_id="ag")
    assert [h.after_enabled for h in history] == [True, False]


async def test_agent_dry_run_writes_nothing(db_session_factory, monkeypatch) -> None:
    tenant_id, _ = await _routines(db_session_factory, 0)
    monkeypatch.setattr(
        routines_cli,
        "current_agent_pin",
        AsyncMock(return_value=AgentSandboxPin("ag", "default")),
    )
    output = StringIO()

    await run_agent_sandbox_tools(
        rt=build_cli_runtime(db_session_factory),
        console=Console(file=output, width=200),
        tenant_id=tenant_id,
        agent_name="daimon",
        environment_name=None,
        enabled=True,
        dry_run=True,
    )

    async with db_session_factory() as session:
        assert await agent_sandbox_pin(session, tenant_id=tenant_id, agent_id="ag") is None
    assert "Dry run: nothing written." in output.getvalue()


async def test_agent_opt_in_refuses_an_unreadable_agent(db_session_factory, monkeypatch) -> None:
    tenant_id, _ = await _routines(db_session_factory, 0)
    monkeypatch.setattr(
        routines_cli,
        "current_agent_pin",
        AsyncMock(side_effect=SandboxPinUnavailable("archived")),
    )

    with pytest.raises(UserFacingError, match="Nothing was changed"):
        await run_agent_sandbox_tools(
            rt=build_cli_runtime(db_session_factory),
            console=Console(file=StringIO()),
            tenant_id=tenant_id,
            agent_name="daimon",
            environment_name=None,
            enabled=True,
            dry_run=False,
        )


async def test_agent_opt_in_prints_the_id_and_the_scope_warning(
    db_session_factory, monkeypatch
) -> None:
    """Turning on names the agent id and says what the pin follows and covers."""
    tenant_id, _ = await _routines(db_session_factory, 0)
    monkeypatch.setattr(
        routines_cli,
        "current_agent_pin",
        AsyncMock(return_value=AgentSandboxPin("ag-77", "default")),
    )
    output = StringIO()

    await run_agent_sandbox_tools(
        rt=build_cli_runtime(db_session_factory),
        console=Console(file=output, width=300),
        tenant_id=tenant_id,
        agent_name="daimon",
        environment_name=None,
        enabled=True,
        dry_run=False,
    )

    text = output.getvalue()
    assert "ag-77" in text, "the agent id is printed so --off can name it later"
    assert "recreated under the same name keeps built-ins" in text, "the pin follows the name"
    assert "DM routines included" in text, "the opt-in covers every routine of the agent"


async def test_off_by_agent_id_reaches_a_renamed_agent(db_session_factory, monkeypatch) -> None:
    """`--agent-id` turns the opt-in off without resolving any name."""
    tenant_id, _ = await _routines(db_session_factory, 0)
    monkeypatch.setattr(
        routines_cli,
        "current_agent_pin",
        AsyncMock(return_value=AgentSandboxPin("ag-77", "default")),
    )
    lookup = AsyncMock(return_value=ma_agent(id="a-different-agent"))
    monkeypatch.setattr(routines_cli, "find_agent_by_daimon_tag", lookup)
    rt = build_cli_runtime(db_session_factory)
    await run_agent_sandbox_tools(
        rt=rt,
        console=Console(file=StringIO()),
        tenant_id=tenant_id,
        agent_name="daimon",
        environment_name=None,
        enabled=True,
        dry_run=False,
    )

    await run_agent_sandbox_tools(
        rt=rt,
        console=Console(file=StringIO()),
        tenant_id=tenant_id,
        agent_name=None,
        agent_id="ag-77",
        environment_name=None,
        enabled=False,
        dry_run=False,
    )

    async with db_session_factory() as session:
        assert await agent_sandbox_pin(session, tenant_id=tenant_id, agent_id="ag-77") is None
    lookup.assert_not_awaited()


async def test_off_by_a_reused_name_points_at_the_list(db_session_factory, monkeypatch) -> None:
    """A name that now means another agent changes nothing and says where to look."""
    tenant_id, _ = await _routines(db_session_factory, 0)
    monkeypatch.setattr(
        routines_cli, "find_agent_by_daimon_tag", AsyncMock(return_value=ma_agent(id="other"))
    )
    output = StringIO()

    await run_agent_sandbox_tools(
        rt=build_cli_runtime(db_session_factory),
        console=Console(file=output, width=300),
        tenant_id=tenant_id,
        agent_name="daimon",
        environment_name=None,
        enabled=False,
        dry_run=False,
    )

    assert "sandbox-tools-list" in output.getvalue(), "the operator is told how to find the holder"


async def test_agent_pins_lists_every_opted_in_agent(db_session_factory) -> None:
    """The listing returns each opted-in agent id with its environment, in id order."""
    tenant_id, _ = await _routines(db_session_factory, 0)
    async with db_session_factory.begin() as session:
        for agent_id in ("ag-b", "ag-a"):
            await set_agent_sandbox_tools(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                pin=AgentSandboxPin(agent_id, "default"),
                set_by="op",
                actor=Subject(is_admin=True),
            )
    async with db_session_factory() as session:
        pins = await agent_sandbox_pins(session, tenant_id=tenant_id)

    assert [(p.agent_id, p.environment_name) for p in pins] == [
        ("ag-a", "default"),
        ("ag-b", "default"),
    ], "pins come back ordered by agent id"
