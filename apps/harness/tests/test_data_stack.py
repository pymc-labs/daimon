"""Optional data dependencies must work together, rather than merely install."""

import importlib
import importlib.util

import pytest


@pytest.mark.skipif(
    importlib.util.find_spec("pymc_marketing") is None, reason="requires data extra"
)
def test_data_stack_imports() -> None:
    for name in ("numpy", "pandas", "pymc", "pymc_marketing"):
        assert importlib.import_module(name).__name__ == name
