"""Selection, pending-bridge gates and offline fixture authorization."""

from __future__ import annotations

import importlib
import importlib.util
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from daimon.core.turn import driver
from mux.contracts.ids import Scope

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "oracle_paths_runner", ROOT / "tests/golden/runner.py"
)
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


async def bridged_turn(
    *,
    path: str | None = None,
    scope: Scope | None = None,
    backend: object = None,
    session_ref: object = None,
    **kwargs: Any,
) -> dict[str, Any]:
    return dict(path=path, scope=scope, backend=backend, session_ref=session_ref, **kwargs)


def test_unwired_flag_and_partial_seam_cannot_advertise_mux_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def partial(*, path: str, scope: Scope) -> None:
        pass

    monkeypatch.setattr(driver, "run_turn", partial)
    assert not RUNNER.mux_turn_bridge_available()
    monkeypatch.setattr(driver, "run_turn", bridged_turn)
    assert RUNNER.mux_turn_bridge_available()


def test_pending_mux_fails_before_starting_a_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(RUNNER, "mux_turn_bridge_available", lambda: False)

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("pending mux must not replay the legacy path")

    monkeypatch.setattr(subprocess, "run", unexpected)
    with pytest.raises(RuntimeError, match="public turn bridge has not landed"):
        RUNNER.replay("plain_turn", turn_path="mux")


def test_selected_modes_override_ambient_flags_and_compare_one_shared_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(driver, "run_turn", bridged_turn)
    monkeypatch.setattr(RUNNER, "GOLDENS", tmp_path)
    monkeypatch.setenv("DAIMON_TURN__PATH", "mux")
    baseline = json.dumps({"price": "0.0000001234", "caller": "literal"}) + "\n"
    golden = tmp_path / "plain_turn.json"
    golden.write_text(baseline)
    seen: list[str] = []
    changed = False

    def replay_child(
        command: list[str], *, env: dict[str, str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        requested = env["DAIMON_ORACLE_TURN_PATH"]
        assert env["DAIMON_TURN__PATH"] == requested
        assert "oracle_plugin" in command and "turn_path_plugin" in command
        seen.append(requested)
        Path(env["DAIMON_ORACLE_OUTPUT"]).write_text(
            baseline.replace("0.0000001234", "0.0000001235") if changed else baseline
        )
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(subprocess, "run", replay_child)
    RUNNER.check("plain_turn", turn_path="legacy")
    RUNNER.check("plain_turn", turn_path="mux")
    assert seen == ["legacy", "mux"]
    changed = True
    with pytest.raises(AssertionError, match="Golden changed"):
        RUNNER.check("plain_turn", turn_path="mux")
    assert list(tmp_path.glob("*.json")) == [golden]
    assert golden.read_text() == baseline


@pytest.mark.parametrize(("turn_path", "mutation"), (("mux", None), ("legacy", "slack_eyes")))
def test_regeneration_guard_runs_before_replay(
    monkeypatch: pytest.MonkeyPatch, turn_path: str, mutation: str | None
) -> None:
    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("forbidden regeneration must not start a replay")

    monkeypatch.setattr(RUNNER, "replay", unexpected)
    with pytest.raises(ValueError, match="unmutated legacy path"):
        RUNNER.check("plain_turn", turn_path=turn_path, mutation=mutation, regen=True)


@pytest.fixture
def path_plugin(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setattr(sys, "path", [str(ROOT / "tests/golden"), *sys.path])
    return importlib.import_module("turn_path_plugin")


@pytest.mark.parametrize("turn_path", ("legacy", "mux"))
async def test_fixture_injects_selected_path_and_explicit_scope_into_real_aliases(
    monkeypatch: pytest.MonkeyPatch, path_plugin: ModuleType, turn_path: str
) -> None:
    monkeypatch.setattr(driver, "run_turn", bridged_turn)
    source = ModuleType("offline_source")
    vars(source)["run_turn"] = bridged_turn
    host = ModuleType("daimon.offline_fixture_host")
    vars(host)["imported_alias"] = bridged_turn
    monkeypatch.setitem(sys.modules, host.__name__, host)
    anthropic_turn = ModuleType("mux.drivers.anthropic.turn")
    vars(anthropic_turn)["datetime"] = None
    monkeypatch.setitem(sys.modules, anthropic_turn.__name__, anthropic_turn)
    path_plugin.install_turn_path(turn_path, source=source, patch=monkeypatch, clock=datetime)
    result = await source.run_turn(session_id="sess_literal")
    assert result["path"] == turn_path
    assert result["session_id"] == "sess_literal"
    assert host.imported_alias is source.run_turn is driver.run_turn
    if turn_path == "mux":
        assert result["scope"] == Scope(
            tenant_id="offline-oracle-tenant",
            account_id="offline-oracle-account",
            principal_id="offline-oracle-host",
            authorization_id="offline-scripted-turn",
        )
        assert anthropic_turn.datetime is datetime
    else:
        assert result["scope"] is None
    caller_scope = Scope(
        tenant_id="actual-tenant",
        account_id="actual-account",
        principal_id="actual-caller",
        authorization_id="actual-authorization",
    )
    backend, session_ref = object(), object()
    actual = await host.imported_alias(scope=caller_scope, backend=backend, session_ref=session_ref)
    assert actual["scope"] is caller_scope
    assert actual["backend"] is backend
    assert actual["session_ref"] is session_ref
    other_path = "mux" if turn_path == "legacy" else "legacy"
    with pytest.raises(AssertionError, match="different path"):
        await source.run_turn(path=other_path)


def test_actual_mock_recorder_omits_only_control_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        RUNNER.SCENARIOS,
        "controls_probe",
        "tests/golden/test_turn_path_instrumentation.py::test_controls_are_forwarded_without_erasing_semantic_kwargs",
    )
    data = json.loads(RUNNER.replay("controls_probe"))
    effects = [effect for effect in data["effects"] if effect["operation"] == "run_turn"]
    assert len(effects) == 1
    kwargs = effects[0]["payload"]["kwargs"]
    assert kwargs == {
        "session_id": "<id:1>",
        "user_message": "Literal /notes/file_alpha",
        "model_id": "caller-model",
        "price": "0.0000001234",
        "continuity": "history",
    }


@pytest.mark.parametrize("available", (False, True))
def test_cli_both_covers_every_scenario_and_labels_pending_mux(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], available: bool
) -> None:
    monkeypatch.setattr(sys, "argv", ["runner.py", "--turn-path", "both"])
    monkeypatch.setattr(RUNNER, "mux_turn_bridge_available", lambda: available)
    seen: list[tuple[str, str]] = []

    def check(name: str, *, turn_path: str, **kwargs: object) -> None:
        seen.append((name, turn_path))

    monkeypatch.setattr(RUNNER, "check", check)
    assert RUNNER.main() == 0
    assert seen == [
        (scenario, path)
        for scenario in RUNNER.SCENARIOS
        for path in (("legacy", "mux") if available else ("legacy",))
    ]
    output = capsys.readouterr().out
    assert output.count("(legacy): matched") == 33
    assert output.count("(mux): matched") == (33 if available else 0)
    assert output.count("(mux): skipped") == (0 if available else 33)
