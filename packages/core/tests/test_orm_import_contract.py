"""The ORM import contract must name every ``daimon.core`` sibling.

import-linter has no "all of ``daimon.core`` except ``_models``" expression: a
wildcard that matches ``_models`` itself makes grimp reject the self-pair. The
contract therefore enumerates the modules, and this test keeps the list in step
with the directory so a new core module cannot silently escape the ORM check.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import daimon.core

_CORE_DIR = Path(daimon.core.__file__).parent
_PYPROJECT = Path(__file__).resolve().parents[3] / "pyproject.toml"
_CONTRACT_NAME = "ORM module is private to stores and defaults"
# _models is the forbidden module; stores and defaults are the sanctioned importers
# (covered by the contract's ignore_imports).
_EXEMPT = {"_models", "stores", "defaults", "__init__"}


def _core_siblings() -> set[str]:
    return {
        path.stem if path.is_file() else path.name
        for path in _CORE_DIR.iterdir()
        if (path.suffix == ".py" or (path.is_dir() and (path / "__init__.py").exists()))
        and path.stem not in _EXEMPT
    }


def test_orm_contract_source_modules_match_core_directory() -> None:
    contracts = tomllib.loads(_PYPROJECT.read_text())["tool"]["importlinter"]["contracts"]
    (contract,) = [c for c in contracts if c["name"] == _CONTRACT_NAME]
    listed = {
        module.removeprefix("daimon.core.")
        for module in contract["source_modules"]
        if module.startswith("daimon.core.")
    }
    siblings = _core_siblings()
    missing = sorted(siblings - listed)
    stale = sorted(listed - siblings)
    assert not missing, f"add to the ORM contract's source_modules: {missing}"
    assert not stale, f"remove from the ORM contract's source_modules: {stale}"
