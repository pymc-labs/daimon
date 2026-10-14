"""N3-only explicit regeneration under two-worker load; excluded by testpaths."""

import importlib.util
import os
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "oracle_runner", Path(__file__).with_name("runner.py")
)
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


@pytest.mark.skipif(
    os.environ.get("DAIMON_ORACLE_REGEN_STRESS") != "1",
    reason="only N3 may explicitly regenerate unchanged integration goldens",
)
@pytest.mark.parametrize("scenario", tuple(RUNNER.SCENARIOS))
def test_regenerate_under_load(scenario: str) -> None:
    RUNNER.check(scenario, regen=True)
