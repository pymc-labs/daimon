"""Neutrality contract: the generic package carries no private/fork surface.

The upstream boundary is enforced two ways — this static check (so a miss
fails in this repo's own suite, not only in the contribution checker) and
the import-linter independence contract in the root pyproject.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "daimon" / "adapters" / "teams"

# Names that belong to the downstream deployment, never to this package.
FORBIDDEN_SUBSTRINGS = (
    "daimon.core.facade",
    "facade_client",
    "durable_delivery",
    "teams_deliveries",
    "TeamsFacadeSettings",
    "allowed_user_ids",
    "shipped_probe",
    "fastmcp",
)


def _module_sources() -> dict[str, str]:
    return {path.name: path.read_text() for path in sorted(PACKAGE_DIR.glob("*.py"))}


def test_no_module_references_private_surface() -> None:
    for name, source in _module_sources().items():
        for forbidden in FORBIDDEN_SUBSTRINGS:
            assert forbidden not in source, (
                f"{name} references {forbidden} — that is downstream-only surface"
            )


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


def test_no_private_modules_are_imported_at_package_init() -> None:
    """Importing the package must not transitively load private modules."""
    import sys

    import daimon.adapters.teams  # noqa: F401

    leaked = [
        m
        for m in sys.modules
        if "durable_delivery" in m or "teams_deliveries" in m or "core.facade" in m
    ]
    assert leaked == []
