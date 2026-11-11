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
so it must stay findable and stoppable. Session toolsets still hide them."""

_TOOLS: Final[dict[str, frozenset[str]]] = {**CAPABILITY_TOOLS, "repo_memory": REPO_MEMORY_TOOLS}
_FEATURE_OF: Final[dict[str, str]] = {tool: key for key, tools in _TOOLS.items() for tool in tools}


def hides_tools(found: ResolvedFeature) -> bool:
    """Off here hides the feature's tools. Platform, backend and dependency gaps at the
    default tier hide nothing; memory Off at the deployment means its tools never registered."""
    if found.on:
        return False
    if found.key == "repo_memory":
        return found.tier not in ("default", "deployment")
    return found.tier != "default" or found.unavailable in (None, "requires")


def gated_tools(keys: Iterable[str]) -> frozenset[str]:
    """The Daimon tools of `keys` (from `GATED_KEYS`)."""
    return frozenset[str]().union(*(_TOOLS[key] for key in keys))


def server_gate_key(tool: str) -> str | None:
    """The feature whose Off makes the server refuse `tool`; None when nothing gates it."""
    return None if tool in STOP_TOOLS else _FEATURE_OF.get(tool)
