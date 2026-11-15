"""`ALLOWED_MODEL_IDS` is the price table's key list; nothing else reads it for a catalog."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGES = Path(__file__).parents[2]
OWNERS = {"pricing.py", "constants.py"}


def _importers(source: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(
            alias.name == "ALLOWED_MODEL_IDS" for alias in node.names
        ):
            return True
        if isinstance(node, ast.Attribute) and node.attr == "ALLOWED_MODEL_IDS":
            return True
    return False


def test_only_the_price_table_modules_use_allowed_model_ids() -> None:
    """A second list of models drifts from the catalog; read `models_catalog` instead."""
    offenders = [
        str(path.relative_to(PACKAGES))
        for path in PACKAGES.glob("*/**/daimon/**/*.py")
        if path.name not in OWNERS
        and "node_modules" not in str(path)
        and _importers(path.read_text())
    ]
    assert offenders == [], f"modules outside pricing.py and constants.py use it: {offenders}"


def test_the_scan_catches_an_import() -> None:
    assert _importers("from daimon.core.constants import ALLOWED_MODEL_IDS\n")
    assert _importers("from daimon.core import constants\nconstants.ALLOWED_MODEL_IDS\n")
    assert not _importers("from daimon.core.constants import DEFAULT_AGENT_MODEL\n")
