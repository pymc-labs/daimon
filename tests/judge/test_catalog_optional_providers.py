"""Anthropic catalog collection/execution must not require later-slice hosts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("module", ("judge.headless_executor", "headless_executor"))
@pytest.mark.parametrize("optimized", (False, True))
def test_anthropic_catalog_without_optional_host_modules(module: str, optimized: bool) -> None:
    script = """
import importlib
import importlib.abc
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path[:0] = [str(root / "tests"), str(root / "tests/judge")]

class NoOptionalHosts(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("daimon.core.turn.openai", "daimon.core.turn.gemini")) or (
            fullname.rsplit(".", 1)[-1] in {
                "openai_catalog_replay", "gemini_catalog_replay",
                "openai_catalog_host", "gemini_catalog_host",
            }
        ):
            raise ModuleNotFoundError("optional provider host absent", name=fullname)

sys.meta_path.insert(0, NoOptionalHosts())
importlib.import_module(sys.argv[2])
import pytest
raise SystemExit(pytest.main([
    "-q", "-n", "0", "--tb=short",
    "tests/judge/test_anthropic_catalog.py",
    "tests/judge/test_headless_executor.py::test_real_turn_captures_authoritative_roots_and_reuses_persisted_session",
    "tests/judge/test_catalog_clock_binding.py",
    "-k", "anthropic or test_real_turn_captures_authoritative_roots_and_reuses_persisted_session",
]))
"""
    command = [sys.executable, *(["-O"] if optimized else []), "-c", script]
    result = subprocess.run(
        [*command, str(Path(__file__).resolve().parents[2]), module],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
