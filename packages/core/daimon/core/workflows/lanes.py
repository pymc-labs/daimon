"""Lanes: which agent session an agent step runs in (`session:` in the spec).

Steps of one agent share `main`; a named lane keeps its own thread of context; `fresh`
is a new session for each visit. A different agent is always a different lane, since a
session freezes its agent at creation. Parallel steps that find their lane busy get one of
their own (`runs.lane_for`).
"""

from __future__ import annotations

from typing import Final

from daimon.core.workflows.spec import AgentNode, Spec

MAIN: Final = "main"
FRESH: Final = "fresh"
OWNER_DM: Final = "owner_dm"


def agent_of(spec: Spec, node: AgentNode) -> str | None:
    """The agent's name, or None for the workspace's default agent."""
    return node.agent or (spec.defaults.agent if spec.defaults else None)


def lane_key(spec: Spec, node_id: str, node: AgentNode, visit: int) -> str:
    """`main:<agent>`, `<lane>:<agent>`, or `fresh:<node>#<visit>` for a fresh session."""
    lane = node.session or MAIN
    if lane == FRESH:
        return f"{FRESH}:{node_id}#{visit}"
    return f"{lane}:{agent_of(spec, node) or ''}"
