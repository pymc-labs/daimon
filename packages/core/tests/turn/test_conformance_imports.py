"""Host conformance remains runnable without optional provider installation."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("optimized", [False, True])
def test_anthropic_host_conformance_without_optional_providers(optimized):
    code = r"""
import asyncio
import importlib.abc
import sys
import pytest

blocked = (
    "daimon.core.turn.gemini", "daimon.core.turn.openai",
    "mux.drivers.gemini", "mux.drivers.openai", "google.genai", "openai",
)
attempts = []
class NoOptionalProviders(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(blocked):
            attempts.append(fullname)
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, NoOptionalProviders())

from packages.core.tests.turn import conformance_support
from packages.core.tests.turn import test_conformance as conformance
from packages.core.tests.turn import test_termination_conformance as termination
from daimon.core.turn.termination import TerminationReason
if (conformance.run_host_fixture_preflight_probe is not
        conformance_support.run_host_fixture_preflight_probe or
        termination.run_host_fixture_preflight_probe is not
        conformance_support.run_host_fixture_preflight_probe):
    raise RuntimeError("conformance consumers do not share the preflight helper")
async def run():
    with pytest.MonkeyPatch.context() as patch:
        state = await conformance.run_host(
            patch, conformance.terminal("anthropic", "completed"))
    if state.termination != TerminationReason.COMPLETED or state.error is not None:
        raise RuntimeError("Anthropic host conformance changed its outcome")
asyncio.run(run())
result = pytest.main([
    "packages/core/tests/turn/test_conformance.py",
    "packages/core/tests/turn/test_termination_conformance.py",
    "-k", "fixture_skips_contributor", "-n", "0", "-q", "--tb=short",
])
if result != 0 or attempts:
    raise RuntimeError(f"optional-provider-free conformance failed: {result}, {attempts}")
print("anthropic host and both conformance modules passed without optional providers")
"""
    child = subprocess.run(
        [sys.executable, *(["-O"] if optimized else []), "-c", code],
        cwd=Path(__file__).resolve().parents[4],
        env={key: os.environ[key] for key in ("PATH", "LANG", "TZ") if key in os.environ},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert child.returncode == 0, child.stdout + child.stderr
    assert "28 passed" in child.stdout
    assert (
        "anthropic host and both conformance modules passed without optional providers"
        in child.stdout
    )
