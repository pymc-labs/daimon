"""The turn and fleet flags are on by default and stay usable as emergency stops."""

from __future__ import annotations

import uuid
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.core.config import FleetSettings, TurnSettings, load_turn_settings
from daimon.core.fleet_orchestration import FleetRefused, HostFleetPorts
from daimon.testing.turn_fakes import deps_for
from daimon.testing.turn_leg import MUX_LEG
from sqlalchemy.ext.asyncio import async_sessionmaker

_FLAGS = (
    "DAIMON_TURN__PATH",
    "DAIMON_TURN__CHANNEL_BACKENDS",
    "DAIMON_TURN__AGENT_BACKENDS",
    "DAIMON_FLEET__ENABLED",
    "DAIMON_TURN__PROVIDER_RUNTIME_FILE",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in _FLAGS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_a_fresh_install_runs_mux_with_backends_and_fleets_on(
    clean_env: pytest.MonkeyPatch,
) -> None:
    turn = load_turn_settings(_env_file=None)
    assert (turn.path, turn.channel_backends, turn.agent_backends) == ("mux", True, True)
    assert FleetSettings().enabled is True


@pytest.mark.parametrize(
    ("name", "value", "field", "stopped"),
    [
        ("DAIMON_TURN__PATH", "legacy", "path", "legacy"),
        ("DAIMON_TURN__CHANNEL_BACKENDS", "false", "channel_backends", False),
        ("DAIMON_TURN__AGENT_BACKENDS", "false", "agent_backends", False),
    ],
)
def test_each_turn_flag_still_stops(
    clean_env: pytest.MonkeyPatch, name: str, value: str, field: str, stopped: object
) -> None:
    clean_env.setenv(name, value)
    assert getattr(load_turn_settings(_env_file=None), field) == stopped


def test_the_default_load_needs_no_runtime_manifest(clean_env: pytest.MonkeyPatch) -> None:
    """Defaults on validate a manifest only when one is configured; none is required."""
    assert load_turn_settings(_env_file=None) == TurnSettings()


def _parent(run_id: uuid.UUID) -> Any:
    return SimpleNamespace(fleet=SimpleNamespace(run_id=run_id))


async def _never_dispatched(*_: object) -> None:
    raise AssertionError("a stopped fleet must not dispatch")


@pytest.mark.parametrize(
    "stop",
    [
        {"turn_path": "legacy"},
        {"channel_backends": False},
        {"agent_backends": False},
    ],
    ids=["turn_path", "channel_backends", "agent_backends"],
)
async def test_a_stopped_flag_refuses_a_worker_before_it_is_funded(
    stop: dict[str, object],
) -> None:
    """Admission refuses first, so no money is reserved and no slot is claimed."""
    deps = replace(deps_for(leg=MUX_LEG), **cast(dict[str, Any], stop))

    def no_session() -> None:
        raise AssertionError("refused before any database read")

    ports = HostFleetPorts(
        cast(async_sessionmaker[Any], no_session),
        prices={},
        markup=Decimal(1),
        dispatch=_never_dispatched,
        deps=deps,
    )
    run_id = uuid.uuid4()
    with pytest.raises(FleetRefused, match="prerequisites_unavailable"):
        await ports.admit(
            _parent(run_id),
            cast(Any, None),
            run_id=run_id,
            operation_key="op",
            role="worker",
            session=None,
            result_contract=None,
            reviewer_of=None,
            reviewed_result=None,
        )
