"""Make parent test helpers (conftest, factories) importable from hub/ tests.

The parent ``tests/`` directory has no ``__init__.py``, so ``factories`` is
only importable once that directory is on sys.path.

Because packages/core/tests also has a ``factories`` module on sys.path
(added by core's conftest), we must insert the MCP tests directory first
AND invalidate any cached ``factories`` so the MCP-local version wins.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

_parent = str(Path(__file__).resolve().parent.parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)
else:
    # Ensure it's at the front so MCP's factories shadows core's.
    sys.path.remove(_parent)
    sys.path.insert(0, _parent)

# Invalidate cached ``factories`` module from core/tests so the MCP-local
# version is found on next import.
if "factories" in sys.modules:
    _cached = sys.modules["factories"]
    if _cached.__file__ and "adapters/mcp" not in _cached.__file__:
        del sys.modules["factories"]
        importlib.invalidate_caches()
