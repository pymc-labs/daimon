"""The provider-neutral contract: types, ports, errors and admission.

Nothing here imports a provider SDK (an import-linter contract holds this),
and no SDK type, exception, URL or token crosses it.

The base types live in a private module and are public here: `Contract` is
the frozen, closed base of every contract type and `FrozenMap` its immutable
mapping. Every other public name is imported from the module that defines it
(`mux.contracts.ports`, `mux.contracts.resources`, ...).
"""

from mux.contracts._base import Contract, FrozenMap

__all__ = ["Contract", "FrozenMap"]
