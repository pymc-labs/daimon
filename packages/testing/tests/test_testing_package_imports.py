"""What the pytest plugin's package import loads into every pytest process."""

from __future__ import annotations

import subprocess
import sys


def test_importing_the_pytest_plugin_does_not_load_the_orm_or_sdk_fixtures() -> None:
    """Every pytest process loads the plugin; one that builds no tenant must not pay for them."""
    program = """
import sys
import daimon.testing.pytest_plugin
import daimon.testing
assert "daimon.testing.factories" not in sys.modules
assert "daimon.testing.ma_models" not in sys.modules
make_tenant = daimon.testing.make_tenant  # resolved on first use
assert make_tenant is sys.modules["daimon.testing.factories"].make_tenant
assert daimon.testing.ma_agent is sys.modules["daimon.testing.ma_models"].ma_agent
"""
    subprocess.run([sys.executable, "-c", program], check=True, capture_output=True, timeout=60)
