"""Load an Alembic migration by slug, so tests survive a rechain renumbering it."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_VERSIONS = Path(__file__).resolve().parents[4] / "packages/core/alembic/versions"


def load_migration(slug: str) -> ModuleType:
    """Import `versions/<id>_<slug>.py`; the slug is the file name without its number."""
    paths = [p for p in _VERSIONS.glob("*.py") if p.stem.partition("_")[2] == slug]
    if len(paths) != 1:
        raise LookupError(f"expected one migration with slug {slug!r}, found {len(paths)}")
    spec = importlib.util.spec_from_file_location(f"migration_{slug}", paths[0])
    assert spec is not None and spec.loader is not None, f"cannot load {paths[0]}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
