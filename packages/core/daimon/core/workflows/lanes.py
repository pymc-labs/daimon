"""Lanes: which agent session an agent step runs in (`session:` in the spec).

Steps of one agent share `main`; a named lane keeps its own thread of context; `fresh`
is a new session for each visit. A different agent is always a different lane, since a
session freezes its agent at creation. Parallel steps that find their lane busy get one of
their own (`runs.lane_for`).
"""

from __future__ import annotations

from typing import Final

from daimon.core.workflows.spec import AgentNode, ForEachNode, Spec

MAIN: Final = "main"
FRESH: Final = "fresh"


def agent_of(spec: Spec, node: AgentNode) -> str | None:
    """The agent's name, or None for the workspace's default agent."""
    return node.agent or (spec.defaults.agent if spec.defaults else None)


def lane_key(spec: Spec, node_id: str, node: AgentNode, visit: int) -> str:
    """`main:<agent>`, `<lane>:<agent>`, or `fresh:<node>#<visit>` for a fresh session.

    A step's place (`at`) and personal memory stay in its session's context, so steps that
    differ in either never share one: each gets `|at=<place>` and `|memory=personal`.
    """
    lane = node.session or MAIN
    if lane == FRESH:
        return f"{FRESH}:{node_id}#{visit}"
    at = f"|at={node.at}" if node.at is not None else ""
    memory = f"|memory={node.memory}" if node.memory is not None else ""
    return f"{lane}:{agent_of(spec, node) or ''}{at}{memory}"


def lane_skills(spec: Spec, node_id: str, node: AgentNode) -> list[str]:
    """The skills a step's session is created with: its own, then (outside `fresh`) every
    lane-mate's, since a reused session cannot add any."""
    names = [*(node.skills or [])]
    lane = lane_key(spec, node_id, node, 0)
    if not lane.startswith(f"{FRESH}:"):
        for mate in (n.do if isinstance(n, ForEachNode) else n for n in spec.nodes.values()):
            if isinstance(mate, AgentNode) and lane_key(spec, "", mate, 0) == lane:
                names += mate.skills or []
    return list(dict.fromkeys(names))
