"""Offline guard checks: blocked/stale recipes cannot masquerade as live coverage."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

HERE = Path(__file__).resolve().parent


def module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("missing local module")
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


BINDINGS = module("nc_live_golden_bindings", HERE / "bindings.py")
JUDGE = module("nc_live_golden_judge_schema", HERE.parents[1] / "judge/harness.py")


def data() -> dict[str, Any]:
    return json.loads((HERE / "scenarios.json").read_text())


def test_all_sources_and_judge_tasks_are_covered_without_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden() -> None:
        raise AssertionError("offline recipe validation cannot invoke judge/auth")

    monkeypatch.setattr(JUDGE, "require_subscription_auth", forbidden)
    manifest = BINDINGS.load_manifest()
    assert len(manifest.scenarios) == 33
    assert sum(s.status == "BOUND" for s in manifest.scenarios) == 20
    assert sum(s.status == "BLOCKED" for s in manifest.scenarios) == 13
    current_tasks = JUDGE.load_tasks()
    for scenario in manifest.scenarios:
        task = JUDGE.Task.model_validate(scenario.judge_task.model_dump())
        assert task.id not in current_tasks
        assert task.id == "golden_" + scenario.name


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "source", "hash"])
def test_missing_duplicate_and_stale_scenarios_refuse(tmp_path: Path, corruption: str) -> None:
    content = data()
    if corruption == "missing":
        content["scenarios"].pop()
    elif corruption == "duplicate":
        content["scenarios"][-1] = content["scenarios"][0]
    elif corruption == "source":
        content["scenarios"][0]["source_node"] = "unknown.py::test_fake"
    else:
        content["scenarios"][0]["golden_sha256"] = "0" * 64
    path = tmp_path / "mutated-manifest.json"
    path.write_text(json.dumps(content))
    with pytest.raises((ValueError, ValidationError)):
        BINDINGS.load_manifest(path)


@pytest.mark.parametrize(
    ("key", "value"),
    [("status", "PASS"), ("turn_path", "mux"), ("model", "claude-sonnet-4-6")],
)
def test_a_recipe_cannot_claim_execution_or_change_path_model(key: str, value: str) -> None:
    content = data()
    if key == "status":
        content["scenarios"][0][key] = value
    else:
        content[key] = value
    with pytest.raises(ValidationError):
        BINDINGS.Manifest.model_validate(content)


def test_a_blocked_recipe_requires_a_typed_reason() -> None:
    content = data()
    blocked = next(s for s in content["scenarios"] if s["status"] == "BLOCKED")
    blocked["blocked"] = None
    with pytest.raises(ValidationError, match="typed reason"):
        BINDINGS.Manifest.model_validate(content)


def test_a_blocked_recipe_cannot_be_promoted_without_provider_work() -> None:
    content = data()
    blocked = next(s for s in content["scenarios"] if s["status"] == "BLOCKED")
    blocked["status"] = "BOUND"
    blocked["blocked"] = None
    with pytest.raises(ValidationError, match="real provider session"):
        BINDINGS.Manifest.model_validate(content)


def test_judge_prompts_cannot_diverge_from_the_recipe() -> None:
    content = data()
    content["scenarios"][0]["judge_task"]["prompts"] = ["A different task"]
    with pytest.raises(ValidationError, match="prompts must match"):
        BINDINGS.Manifest.model_validate(content)
