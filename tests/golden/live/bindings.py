"""Offline recipes for N9's live host harness; importing this never runs a probe."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

HERE = Path(__file__).resolve().parent

type BlockCode = Literal[
    "PROVIDER_FAULT_INJECTION",
    "HOST_ADMISSION_ONLY",
    "HOST_CONTROL_ONLY",
    "HOST_DECISION_ONLY",
    "NON_TURN_ENTRY_POINT",
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Blocker(Strict):
    code: BlockCode
    reason: str = Field(min_length=1)
    unblock: str = Field(min_length=1)


class Setup(Strict):
    resource: str = Field(min_length=1)
    instructions: str = Field(min_length=1)


class Turn(Strict):
    number: int = Field(ge=1)
    actor: Literal["user", "host"]
    text: str = Field(min_length=1)
    entry_point: str = Field(min_length=1)


class Trigger(Strict):
    after: str = Field(min_length=1)
    action: str = Field(min_length=1)


class Assertion(Strict):
    id: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)


class JudgeTask(Strict):
    # Same fields as tests/judge/harness.py:Task. Registry remains N9-owned.
    id: str = Field(min_length=1)
    core: bool
    goal: str = Field(min_length=1)
    critical: list[Literal["continuity", "tool_fidelity", "artifact_integrity", "safety"]]
    prompts: list[str] = Field(min_length=1)


class Binding(Strict):
    """Declarative scenario record, distinct from N9's executable Binding protocol."""

    name: str = Field(pattern=r"^[a-z][a-z_]*$")
    status: Literal["BOUND", "BLOCKED"]
    source_node: str = Field(min_length=1)
    golden_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    host_entry_point: str = Field(min_length=1)
    platform: Literal["headless", "discord", "slack", "mcp", "cli"]
    coverage: str = Field(min_length=1)
    setup: list[Setup] = Field(min_length=1)
    turns: list[Turn] = Field(min_length=1)
    triggers: list[Trigger]
    assertions: list[Assertion] = Field(min_length=1)
    requirements: list[str] = Field(min_length=1)
    max_sessions: int = Field(ge=0)
    max_model_turns: int = Field(ge=0)
    blocked: Blocker | None
    judge_task: JudgeTask

    @model_validator(mode="after")
    def coherent_recipe(self) -> Self:
        if (self.status == "BLOCKED") != (self.blocked is not None):
            raise ValueError("BLOCKED requires a typed reason; BOUND cannot carry one")
        if self.status == "BOUND" and (self.max_sessions < 1 or self.max_model_turns < 1):
            raise ValueError("BOUND requires a real provider session and model turn")
        if self.judge_task.id != "golden_" + self.name:
            raise ValueError("golden judge task IDs must preserve scenario identity")
        if [turn.number for turn in self.turns] != list(range(1, len(self.turns) + 1)):
            raise ValueError("turn numbers must be consecutive")
        ids = [check.id for check in self.assertions]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate assertion IDs")
        if self.judge_task.prompts != [turn.text for turn in self.turns]:
            raise ValueError("judge prompts must match the declared user/host turns")
        return self


class Manifest(Strict):
    schema_version: Literal[1]
    preparation: Literal["OFFLINE_RECIPES_ONLY"]
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    provider: Literal["anthropic"]
    profile: Literal["anthropic.managed_agents"]
    turn_path: Literal["legacy"]
    model: Literal["claude-haiku-4-5-20251001"]
    run_admission: list[str] = Field(min_length=1)
    fixtures: dict[str, dict[str, str]]
    common_assertions: list[Assertion] = Field(min_length=1)
    normalization: list[str] = Field(min_length=1)
    scenarios: list[Binding] = Field(min_length=33, max_length=33)


def source_scenarios() -> dict[str, str]:
    """Read the offline source registry without importing any runtime or SDK."""
    module = ast.parse((HERE.parent / "runner.py").read_text())
    for statement in module.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "SCENARIOS"
            for target in statement.targets
        ):
            return TypeAdapter(dict[str, str], config=ConfigDict(strict=True)).validate_python(
                ast.literal_eval(statement.value)
            )
    raise ValueError("missing golden scenario registry")


def load_manifest(path: Path = HERE / "scenarios.json") -> Manifest:
    manifest = Manifest.model_validate_json(path.read_text())
    source = source_scenarios()
    names = [scenario.name for scenario in manifest.scenarios]
    if len(set(names)) != 33 or set(names) != set(source):
        raise ValueError("manifest must cover all 33 source scenarios exactly once")
    for scenario in manifest.scenarios:
        if scenario.source_node != source[scenario.name]:
            raise ValueError("scenario source node changed: " + scenario.name)
        digest = hashlib.sha256((HERE.parent / (scenario.name + ".json")).read_bytes()).hexdigest()
        if digest != scenario.golden_sha256:
            raise ValueError("canonical golden changed: " + scenario.name)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=HERE / "scenarios.json")
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    counts = {
        status: sum(s.status == status for s in manifest.scenarios)
        for status in ("BOUND", "BLOCKED")
    }
    print(json.dumps({"preparation": manifest.preparation, "counts": counts, "live_executed": 0}))


if __name__ == "__main__":
    main()
