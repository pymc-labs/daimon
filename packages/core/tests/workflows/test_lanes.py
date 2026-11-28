"""Which steps share an agent session (`lanes.lane_key`)."""

from __future__ import annotations

from daimon.core.workflows.lanes import lane_key
from daimon.core.workflows.spec import AgentNode, parse_spec


def test_steps_with_another_place_or_personal_memory_never_share_a_session() -> None:
    agent = {"kind": "agent", "agent": "helper", "instructions": "go"}
    spec = parse_spec(
        {"schema": "daimon.workflow/v1", "name": "w", "start": "a", "nodes": {
            "a": agent | {"next": "b"}, "b": agent | {"next": "c"},
            "c": agent | {"at": "#team", "next": "d"}, "d": agent | {"memory": "personal", "next": "e"},
            "e": agent | {"at": "#team", "next": "f"}, "f": {"kind": "finish"},
        }}
    )  # fmt: skip
    lanes = {}
    for key, node in spec.nodes.items():
        if isinstance(node, AgentNode):
            lanes[key] = lane_key(spec, key, node, 1)
    assert lanes["a"] == lanes["b"] and lanes["c"] == lanes["e"], "same place, one session"
    assert len({lanes["a"], lanes["c"], lanes["d"]}) == 3, f"a place or memory apart: {lanes}"
