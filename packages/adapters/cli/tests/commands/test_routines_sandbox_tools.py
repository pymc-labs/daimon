"""`daimon routines sandbox-tools`: the operator writes `routine_sandbox_tools` values."""

import uuid
from datetime import UTC, datetime
from io import StringIO
from typing import Any
from unittest.mock import AsyncMock

import daimon.adapters.cli.commands.routines as routines_cli
import daimon.core.routine_sandbox as routine_sandbox
import pytest
from daimon.adapters.cli.commands.routines import run_agent_sandbox_tools, run_sandbox_tools
from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.errors import UserFacingError
from daimon.core.features.registry import DEFAULT
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.routine_sandbox import SandboxPinUnavailable, own_opt_in, routine_place
from daimon.core.stores.channel_changes import WORKSPACE_ID, list_changes
from daimon.core.stores.domain import AgentSandboxPin, RoutineRow, SandboxPin
from daimon.core.stores.routines import create_routine
from daimon.core.stores.sandbox_pins import list_sandbox_pins
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_agent
from pydantic import PostgresDsn, SecretStr
from rich.console import Console

from ..harness import build_cli_runtime

PIN = SandboxPin("ag", 3, "env_1", datetime(2026, 10, 1, tzinfo=UTC))
SETTINGS = Settings.model_validate(
    {
        "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
        "anthropic": AnthropicSettings(api_key=SecretStr("test")),
        "discord": DiscordSettings(bot_token=SecretStr("test")),
    }
)


@pytest.fixture(autouse=True)
def current_pin_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """The routine's agent and environment, as Managed Agents would report them."""
    monkeypatch.setattr(routine_sandbox, "current_pin", AsyncMock(return_value=PIN))


def _rt(db_session_factory: Any) -> Any:
    return build_cli_runtime(db_session_factory, settings=SETTINGS)


async def _routines(db_session_factory: Any, count: int, **kw: Any) -> list[RoutineRow]:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, workspace_id=f"guild-{uuid.uuid4()}")
        return [
            await create_routine(
                session,
                tenant_id=tenant.id,
                created_by_user_id="1001",
                agent_id="ag",
                agent_name="daimon",
                cron_expr="0 9 * * *",
                timezone_="UTC",
                trigger_message=f"routine {n}",
                **kw,
            )  # fmt: skip
            for n in range(count)
        ]


async def _values(db_session_factory: Any, rows: list[RoutineRow]) -> list[object]:
    return [
        await own_opt_in(db_session_factory, routine_place(row, "discord"), "routine")
        for row in rows
    ]


async def _routine_run(db_session_factory: Any, rows: list[RoutineRow], **kw: Any) -> str:
    output = StringIO()
    args: dict[str, Any] = {"value": "on", "dry_run": False, **kw}
    await run_sandbox_tools(
        rt=_rt(db_session_factory), console=Console(file=output, width=300),
        tenant_id=rows[0].tenant_id, routine_ids=[row.id for row in rows], **args,
    )  # fmt: skip
    return output.getvalue()


async def _agent_run(db_session_factory: Any, tenant_id: uuid.UUID, **kw: Any) -> str:
    output = StringIO()
    args: dict[str, Any] = {"agent_name": "daimon", "value": "on", "dry_run": False, **kw}
    await run_agent_sandbox_tools(
        rt=_rt(db_session_factory), console=Console(file=output, width=300),
        tenant_id=tenant_id, environment_name=None, **args,
    )  # fmt: skip
    return output.getvalue()


async def test_dry_run_reports_and_writes_nothing(db_session_factory: Any) -> None:
    """A dry run prints what would change and its pin, and writes nothing."""
    rows = await _routines(db_session_factory, 2)
    text = await _routine_run(db_session_factory, rows[:1], dry_run=True)
    assert f"{rows[0].id}: set on" in text, "the change is shown"
    assert "agent ag version 3, environment env_1 as of 2026-10-01" in text, "with its pin"
    assert "Dry run: 1 routine(s) would change" in text, "and the count"
    assert await _values(db_session_factory, rows) == [(None, None)] * 2, "nothing written"


async def test_bulk_opt_in_changes_only_listed_routines_and_is_idempotent(
    db_session_factory: Any,
) -> None:
    """Listed routines get a pinned On, recorded once; a repeat changes nothing."""
    rows = await _routines(db_session_factory, 3)
    await _routine_run(db_session_factory, rows[:2])
    text = await _routine_run(db_session_factory, rows[:2])
    assert "already on" in text and "0 routine(s); 2 unchanged" in text, "the repeat is a no-op"
    assert await _values(db_session_factory, rows) == [("on", PIN)] * 2 + [(None, None)], "listed"
    async with db_session_factory() as session:
        changes = await list_changes(session, tenant_id=rows[0].tenant_id, channel_id=WORKSPACE_ID)
    assert [(c.surface, c.actor_account_id is not None) for c in changes] == [("cli", True)] * 2, (
        "each routine's change is the operator's"
    )


async def test_an_unknown_id_changes_nothing(db_session_factory: Any) -> None:
    """One bad id refuses the whole batch."""
    rows = await _routines(db_session_factory, 1)
    with pytest.raises(UserFacingError, match="Nothing was changed"):
        await run_sandbox_tools(
            rt=_rt(db_session_factory), console=Console(file=StringIO()),
            tenant_id=rows[0].tenant_id, routine_ids=[rows[0].id, uuid.uuid4()], value="on",
            dry_run=False,
        )  # fmt: skip
    assert await _values(db_session_factory, rows) == [(None, None)], "nothing written"


async def test_off_and_inherit_are_the_routine_own_values(db_session_factory: Any) -> None:
    """--off sets the routine's own Off; --inherit clears it so the agent decides."""
    rows = await _routines(db_session_factory, 1)
    await _routine_run(db_session_factory, rows)
    await _routine_run(db_session_factory, rows, value="off")
    assert await _values(db_session_factory, rows) == [("off", None)], "its own Off, no pin"
    await _routine_run(db_session_factory, rows, value=DEFAULT)
    assert await _values(db_session_factory, rows) == [(None, None)], "cleared"


async def test_a_listed_dm_routine_refuses_the_whole_batch(db_session_factory: Any) -> None:
    """A DM routine takes no On of its own, so a batch naming one changes nothing."""
    [row] = await _routines(db_session_factory, 1)
    [dm] = await _routines(
        db_session_factory, 1, source_provenance_known=True, destination_kind="dm",
        destination_id="1001",
    )  # fmt: skip
    with pytest.raises(UserFacingError, match="DM routines can't use sandbox tools"):
        await run_sandbox_tools(
            rt=_rt(db_session_factory), console=Console(file=StringIO()),
            tenant_id=dm.tenant_id, routine_ids=[dm.id], value="on", dry_run=False,
        )  # fmt: skip
    assert await _values(db_session_factory, [row, dm]) == [(None, None)] * 2, "nothing written"


async def test_an_unreadable_agent_or_environment_refuses_the_whole_batch(
    db_session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every pin is read before any write."""
    rows = await _routines(db_session_factory, 2)
    unavailable = AsyncMock(side_effect=[PIN, SandboxPinUnavailable("archived")])
    monkeypatch.setattr(routine_sandbox, "current_pin", unavailable)
    with pytest.raises(UserFacingError, match="Can't read the routine's agent version"):
        await _routine_run(db_session_factory, rows)
    assert await _values(db_session_factory, rows) == [(None, None)] * 2, "nothing written"


async def test_re_running_after_an_agent_edit_re_approves_it(
    db_session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running again on a new agent version moves the pin to it."""
    rows = await _routines(db_session_factory, 1)
    newer = SandboxPin("ag", 4, "env_1", PIN.environment_updated_at)
    await _routine_run(db_session_factory, rows)
    monkeypatch.setattr(routine_sandbox, "current_pin", AsyncMock(return_value=newer))
    text = await _routine_run(db_session_factory, rows)
    assert "re-approve current agent and environment" in text, "the operator is told"
    assert await _values(db_session_factory, rows) == [("on", newer)], "the pin moved"


async def test_agent_opt_in_pins_the_agent_and_environment_then_off_drops_it(
    db_session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--agent` pins the agent id and environment name; a repeat writes nothing."""
    [row] = await _routines(db_session_factory, 1)
    pin = AgentSandboxPin("ag", "default")
    monkeypatch.setattr(routines_cli, "current_agent_pin", AsyncMock(return_value=pin))
    monkeypatch.setattr(
        routines_cli, "find_agent_by_daimon_tag", AsyncMock(return_value=ma_agent(id="ag"))
    )
    place = FeaturePlace.of(row.tenant_id, "discord", None, agent_id="ag")
    assert "Dry run: nothing written." in await _agent_run(
        db_session_factory, row.tenant_id, dry_run=True
    ), "a dry run writes nothing"
    assert await own_opt_in(db_session_factory, place, "agent") == (None, None), "unchanged"
    await _agent_run(db_session_factory, row.tenant_id)
    assert await own_opt_in(db_session_factory, place, "agent") == ("on", pin), "pinned"
    assert "already on" in await _agent_run(db_session_factory, row.tenant_id), "a repeat"
    await _agent_run(db_session_factory, row.tenant_id, value="off")
    assert await own_opt_in(db_session_factory, place, "agent") == ("off", None), "Off, no pin"


async def test_agent_opt_in_refuses_an_unreadable_agent(
    db_session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No live agent or environment, no On."""
    [row] = await _routines(db_session_factory, 1)
    unavailable = AsyncMock(side_effect=SandboxPinUnavailable("archived"))
    monkeypatch.setattr(routines_cli, "current_agent_pin", unavailable)
    with pytest.raises(UserFacingError, match="Nothing was changed"):
        await _agent_run(db_session_factory, row.tenant_id)


async def test_off_by_agent_id_reaches_a_renamed_agent(
    db_session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--agent-id` turns the opt-in off without resolving any name."""
    [row] = await _routines(db_session_factory, 1)
    pin = AgentSandboxPin("ag-77", "default")
    monkeypatch.setattr(routines_cli, "current_agent_pin", AsyncMock(return_value=pin))
    lookup = AsyncMock(return_value=ma_agent(id="a-different-agent"))
    monkeypatch.setattr(routines_cli, "find_agent_by_daimon_tag", lookup)
    text = await _agent_run(db_session_factory, row.tenant_id)
    assert "ag-77" in text, "the agent id is printed so --agent-id can name it later"
    await _agent_run(db_session_factory, row.tenant_id, agent_name=None, agent_id="ag-77",
                     value=DEFAULT)  # fmt: skip
    place = FeaturePlace.of(row.tenant_id, "discord", None, agent_id="ag-77")
    assert await own_opt_in(db_session_factory, place, "agent") == (None, None), "cleared"
    lookup.assert_not_awaited()


async def test_the_list_shows_agents_then_routines(
    db_session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every pin is listed, agents first."""
    [row] = await _routines(db_session_factory, 1)
    await _routine_run(db_session_factory, [row])
    agent_pin = AgentSandboxPin("ag", "default")
    monkeypatch.setattr(routines_cli, "current_agent_pin", AsyncMock(return_value=agent_pin))
    await _agent_run(db_session_factory, row.tenant_id)
    async with db_session_factory() as session:
        entries = await list_sandbox_pins(session, tenant_id=row.tenant_id)
    assert [(e.tier, e.key, e.pin) for e in entries] == [
        ("agent", "ag", agent_pin),
        ("routine", str(row.id), PIN),
    ], "agents first"
