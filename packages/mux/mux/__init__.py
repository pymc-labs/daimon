"""Mux: provider-agnostic managed-agent interface.

One interface with interchangeable backends (Anthropic, OpenAI, Google). The
top-level import is deliberately `mux`, not `daimon.mux`: outside teams are
meant to implement against this, so the name must survive a spin-out to its
own repo with zero renames.

Dependency direction is one-way: `daimon` may consume `mux`, `mux` never
imports `daimon` (enforced by an import-linter contract). That keeps a future
extraction to a `git subtree split` with no code motion.
"""

from __future__ import annotations

from mux.backends import BACKENDS
from mux.capabilities import BACKEND_IDS, BackendId, Capabilities
from mux.core_profile import FLOOR, missing_capabilities
from mux.errors import CapabilityUnavailableError, MuxError
from mux.events import MuxEvent, MuxEventKind

__all__ = [
    "BACKENDS",
    "BACKEND_IDS",
    "BackendId",
    "Capabilities",
    "CapabilityUnavailableError",
    "FLOOR",
    "MuxError",
    "MuxEvent",
    "MuxEventKind",
    "missing_capabilities",
]
