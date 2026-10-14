"""Explicit oracle stress check; run with -n 2 before recording a READY."""

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "oracle_runner", Path(__file__).with_name("runner.py")
)
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


@pytest.mark.parametrize("iteration", range(20))
@pytest.mark.parametrize("scenario", ("ceiling", "cancel_mid_stream"))
def test_twenty_replays_under_load(iteration: int, scenario: str) -> None:
    RUNNER.check(scenario)
