"""Adapter independence for the Teams package, checked here as well as by import-linter."""

from __future__ import annotations

import ast
import importlib.util
import tomllib
from pathlib import Path

import yaml

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "daimon" / "adapters" / "teams"
REPO_ROOT = Path(__file__).resolve().parents[4]


def test_teams_is_in_adapter_independence_contract() -> None:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    contracts = config["tool"]["importlinter"]["contracts"]
    matches = [c for c in contracts if c["name"] == "Adapters must not import each other"]
    assert len(matches) == 1, "the adapter independence contract must exist exactly once"
    assert matches[0]["type"] == "independence"
    assert "daimon.adapters.teams" in matches[0]["modules"]


def test_ci_runs_teams_tests() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    spec = importlib.util.spec_from_file_location("ci_plan", REPO_ROOT / "scripts/ci/plan.py")
    assert spec and spec.loader
    plan = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plan)
    legs = plan.matrix_legs(workflow["jobs"]["pytest-db"])
    teams = [leg for leg in legs if leg["suite"] == "teams"]
    assert len(teams) == 1, "CI must run the Teams adapter tests"
    assert teams[0]["paths"] == "packages/adapters/teams", (
        "the Teams leg must run the adapter suite"
    )
    assert teams[0]["shards"] == 1, "the Teams leg must not skip any of its tests"


def _module_sources() -> dict[str, str]:
    return {path.name: path.read_text() for path in sorted(PACKAGE_DIR.glob("*.py"))}


def test_no_module_imports_another_adapter() -> None:
    """No module imports another adapter's package."""
    for name, source in _module_sources().items():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
            elif isinstance(node, ast.Import):
                module = ",".join(a.name for a in node.names)
            else:
                continue
            if "daimon.adapters." in module:
                assert module.startswith("daimon.adapters.teams"), (
                    f"{name} imports {module} — adapters must not import each other"
                )


def test_no_other_adapter_modules_load_at_package_init() -> None:
    """Importing the package must not transitively load another adapter.

    Runs in a clean interpreter: in-process checks can't distinguish what this
    import loaded from what sibling suites already left in sys.modules.
    """
    import subprocess
    import sys

    code = (
        "import sys; import daimon.adapters.teams; "
        "leaked = [m for m in sys.modules "
        "if m.startswith('daimon.adapters.') "
        "and not m.startswith('daimon.adapters.teams')]; "
        "print(leaked); sys.exit(1 if leaked else 0)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, (
        f"importing daimon.adapters.teams loaded other adapters: {result.stdout}"
    )
