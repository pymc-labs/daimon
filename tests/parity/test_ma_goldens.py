"""Current-path integration oracle; golden changes are restricted to lane N3."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("oracle_runner", ROOT / "tests/golden/runner.py")
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


@pytest.mark.parametrize("scenario", tuple(RUNNER.SCENARIOS))
def test_current_path_matches_golden(scenario: str) -> None:
    RUNNER.check(scenario)


@pytest.mark.parametrize(
    ("scenario", "mutation"),
    (
        ("plain_slack", "slack_eyes"),
        ("cold_discord", "discord_eyes"),
        ("cold_discord", "ledger_dating"),
        ("dm_delivery", "dm_delivery"),
    ),
)
def test_golden_detects_production_mutation(scenario: str, mutation: str) -> None:
    # The source scenario must still pass; sensitivity comes from its transcript.
    with pytest.raises(AssertionError, match="Golden changed"):
        RUNNER.check(scenario, mutation=mutation)
