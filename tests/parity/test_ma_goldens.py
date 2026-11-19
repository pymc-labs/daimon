"""The turn path matches the immutable integration oracle."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("oracle_runner", ROOT / "tests/golden/runner.py")
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


@pytest.mark.parametrize("scenario", tuple(RUNNER.SCENARIOS))
def test_current_path_matches_golden(golden_replays: Any, scenario: str) -> None:
    RUNNER.compare(scenario, golden_replays.transcript(scenario))


@pytest.mark.parametrize(("scenario", "mutation"), RUNNER.MUTATIONS)
def test_golden_detects_production_mutation(
    golden_replays: Any, scenario: str, mutation: str
) -> None:
    # The mutated scenario must still pass; sensitivity comes from its transcript.
    transcript = golden_replays.transcript(scenario, mutation)
    with pytest.raises(AssertionError, match="Golden changed"):
        RUNNER.compare(scenario, transcript)
