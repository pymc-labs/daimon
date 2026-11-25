"""What the store registers with core at startup (the `daimon.ports` entry point).

M2 fills `ERASURE` (its tables) and the audit source; M5 fills `GATED_TOOLS`. Everything
here is read only where the package is installed and does nothing for a deployment that
never set the store up: erasure statements skip a schema that does not exist.
"""

from __future__ import annotations

from typing import Final

from daimon.core.ports.erasure import ErasureStatement, register_erasure
from daimon.core.ports.tool_gate import register_gated_tools

ERASURE: Final[tuple[ErasureStatement, ...]] = ()
GATED_TOOLS: Final[dict[str, frozenset[str]]] = {}


def register() -> None:
    register_erasure(ERASURE)
    for key, tools in GATED_TOOLS.items():
        register_gated_tools(key, tools)
