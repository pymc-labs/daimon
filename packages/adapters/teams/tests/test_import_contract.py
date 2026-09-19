"""Adapter-boundary contract for the Teams package.

The upstream boundary is enforced two ways — this static check (so a miss
fails in this repo's own suite, not only in CI's import-linter job) and the
import-linter independence contract in the root pyproject.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "daimon" / "adapters" / "teams"


def _module_sources() -> dict[str, str]:
    return {path.name: path.read_text() for path in sorted(PACKAGE_DIR.glob("*.py"))}


def test_no_module_imports_another_adapter() -> None:
    """Adapter independence (import-linter contract): no daimon.adapters.X
    import where X is a different adapter."""
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
