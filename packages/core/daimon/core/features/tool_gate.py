"""Which Daimon tools a capability feature turns off, for session toolsets and the server."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from daimon.core.capability_tools import CAPABILITY_KEYS, CAPABILITY_TOOLS
from daimon.core.features.resolver import ResolvedFeature
from daimon.core.repo_memory_tools import REPO_MEMORY_TOOLS

GATED_KEYS: Final[tuple[str, ...]] = (*CAPABILITY_KEYS, "repo_memory")

STOP_TOOLS: Final[frozenset[str]] = frozenset(
    {"fly_stop_session", "fly_list_sessions", "list_long_jobs", "get_long_job", "cancel_long_job"}
)
"""The server keeps these callable while their feature is Off: Off leaves running work running,
so it must stay findable and stoppable."""

SESSION_KEPT_TOOLS: Final[frozenset[str]] = frozenset({"fly_stop_session", "fly_list_sessions"})
"""Compute Off hides the rest of its tools from a session but not these two: a running sandbox
session must stay stoppable from chat. Long jobs have no such exception; their tools all go."""

_TOOLS: Final[dict[str, frozenset[str]]] = {**CAPABILITY_TOOLS, "repo_memory": REPO_MEMORY_TOOLS}
_FEATURE_OF: Final[dict[str, str]] = {tool: key for key, tools in _TOOLS.items() for tool in tools}


def hides_tools(found: ResolvedFeature) -> bool:
    """Off here hides the feature's tools. Platform, backend and dependency gaps at the
    default tier hide nothing. Memory's tools are registered only once it is set up and not
    locked Off, so a gap or a lock there leaves nothing to hide; any other Off hides them."""
    if found.on:
        return False
    if found.key == "repo_memory":
        return found.unavailable is None and not (found.tier == "deployment" and found.locked)
    return found.tier != "default" or found.unavailable in (None, "requires")


def gated_tools(keys: Iterable[str]) -> frozenset[str]:
    """The Daimon tools a session loses when `keys` (from `GATED_KEYS`) are Off."""
    return frozenset[str]().union(*(_TOOLS[key] for key in keys)) - SESSION_KEPT_TOOLS


def server_gate_key(tool: str) -> str | None:
    """The feature whose Off makes the server refuse `tool`; None when nothing gates it."""
    return None if tool in STOP_TOOLS else _FEATURE_OF.get(tool)
