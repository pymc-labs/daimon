"""The guaranteed floor every backend must clear.

The draft spec marks remote MCP, bash/file/web tools, skills, and automatic
compaction as core, and all three backends hold those today, so they need no
flag. The one discriminating floor requirement is a durable filesystem view —
native or emulated, per the state-contract decision. `missing_capabilities`
is pure: it reports the gap as data, and the caller decides what to do.
"""

from __future__ import annotations

from mux.capabilities import Capabilities

FLOOR: tuple[str, ...] = ("durable_fs",)


def missing_capabilities(caps: Capabilities) -> tuple[str, ...]:
    """Return the floor flags `caps` lacks; empty means the backend clears it."""
    return tuple(flag for flag in FLOOR if not getattr(caps, flag))
