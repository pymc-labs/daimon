"""Which Daimon tools a capability feature turns off, for session toolsets and the server."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from daimon.core import ports
from daimon.core.capability_tools import CAPABILITY_KEYS, CAPABILITY_TOOLS
from daimon.core.data_tools import DATA_READS
from daimon.core.features.resolver import ResolvedFeature
from daimon.core.repo_memory_tools import REPO_MEMORY_TOOLS

GATED_KEYS: Final[tuple[str, ...]] = (*CAPABILITY_KEYS, "repo_memory", "web_awareness")

WEB_APP_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "list_web_invites",
        "create_web_invite",
        "set_web_invite_files",
        "issue_visitor_link",
        "revoke_web_link",
        "update_web_person",
        "list_agent_web_access",
        "set_agent_web_access",
    }
)
"""Tools that exist only for the web app: `web_awareness` Off hides and refuses them. The
people and group lookups stay: allowances, owners and turn rules take their ids."""

STOP_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "compute_stop_session",
        "compute_list_sessions",
        "list_long_jobs",
        "get_long_job",
        "cancel_long_job",
    }
)
"""The server keeps these callable while their feature is Off: Off leaves running work running,
so it must stay findable and stoppable."""

SESSION_KEPT_TOOLS: Final[frozenset[str]] = frozenset(
    {"compute_stop_session", "compute_list_sessions"}
)
"""Compute Off hides the rest of its tools from a session but not these two: a running sandbox
session must stay stoppable from chat. Long jobs have no such exception; their tools all go."""

_TOOLS: Final[dict[str, frozenset[str]]] = {
    **CAPABILITY_TOOLS,
    "repo_memory": REPO_MEMORY_TOOLS,
    "web_awareness": WEB_APP_TOOLS,
}
_FEATURE_OF: Final[dict[str, str]] = {tool: key for key, tools in _TOOLS.items() for tool in tools}
_REGISTERED: Final[dict[str, frozenset[str]]] = {}
"""Tools an installed package gates with its own feature (`daimon.core.ports.tool_gate`)."""


def register_gated_tools(key: str, tools: Iterable[str]) -> None:
    """Gate `tools` behind the feature `key`: the same rule as the built-in keys, so a session
    loses them and the server refuses them while the feature is Off. A key may not repeat a
    built-in one."""
    if key in _TOOLS:
        raise ValueError(f"{key} already gates tools")
    _REGISTERED[key] = frozenset(tools)


def gated_keys() -> tuple[str, ...]:
    """Every feature key that gates tools, the registered ones last."""
    ports.load()
    return (*GATED_KEYS, *_REGISTERED)


def hides_tools(found: ResolvedFeature) -> bool:
    """Off here hides the feature's tools. Platform, backend and dependency gaps at the
    default tier hide nothing. Memory's tools are registered only once it is set up and not
    locked Off, so a gap or a lock there leaves nothing to hide; any other Off hides them."""
    if found.on:
        return False
    if found.key == DATA_READS:
        # Data is never offered by default: any Off, a missing dependency too, hides it.
        return True
    if found.key == "repo_memory":
        return found.unavailable is None and not (found.tier == "deployment" and found.locked)
    return found.tier != "default" or found.unavailable in (None, "requires")


def gated_tools(keys: Iterable[str]) -> frozenset[str]:
    """The Daimon tools a session loses when `keys` (from `GATED_KEYS`) are Off."""
    ports.load()
    tools = {**_TOOLS, **_REGISTERED}
    return frozenset[str]().union(*(tools[key] for key in keys)) - SESSION_KEPT_TOOLS


def server_gate_key(tool: str) -> str | None:
    """The feature whose Off makes the server refuse `tool`; None when nothing gates it."""
    if tool in STOP_TOOLS:
        return None
    ports.load()
    return _FEATURE_OF.get(tool) or next(
        (key for key, tools in _REGISTERED.items() if tool in tools), None
    )
