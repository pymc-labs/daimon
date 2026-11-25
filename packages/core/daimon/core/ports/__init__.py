"""Seams an optional package fills at startup, so core never imports it.

`daimon-data` declares an entry point in the `daimon.ports` group. `load()` runs each
one once per process; the entry point calls the `register_*` functions of this package's
modules. Readers call `load()` before they look at a registry, so every process that
installs the package is wired, with no per-adapter startup code.
"""

from __future__ import annotations

from functools import cache
from importlib.metadata import entry_points

GROUP = "daimon.ports"


@cache
def load() -> None:
    """Run every installed package's registration once."""
    for entry in entry_points(group=GROUP):
        entry.load()()
