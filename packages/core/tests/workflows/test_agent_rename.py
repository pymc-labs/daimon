"""An agent's rename follows into workflow specs, so steps keep their agent."""

from __future__ import annotations

from daimon.core.stores import workflow_publish_requests, workflow_runs, workflows
from daimon.core.stores.agent_rename import count_agent_references, rename_agent_references
from daimon.core.workflows.spec import node_hash, parse_spec
from daimon.testing.workflows import NOW, make_world
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

NODES = {
    "a": {"kind": "agent", "agent": "default", "instructions": "Read", "next": "f"},
    "f": {"kind": "finish"},
}
FIELDS = {
    "defaults": {"agent": "default"},
    "triggers": [
        {"id": "p", "kind": "poll", "agent": "default", "tool": "x/y", "every": "5m", "key": ".id"}
    ],
}


async def test_rename_agent_references_when_workflows_name_it_then_specs_tools_and_grants_follow(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, NODES, "a", spec_fields=FIELDS)
    other = await make_world(db_session, NODES, "a", workspace_id="other")
    async with world.sm.begin() as s:
        version = await workflows.get_version(
            s, tenant_id=world.tenant_id, workflow_id=world.workflow_id, number=1
        )
        assert version is not None
        await s.execute(text('UPDATE workflow_versions SET agent_tools = \'{"default": ["a/b"]}\''))
        await workflow_publish_requests.add_grants(
            s,
            tenant_id=world.tenant_id,
            workflow_id=world.workflow_id,
            version=1,
            grants=[
                {
                    "kind": "builtins",
                    "node_id": "a",
                    "pins": {"node_hash": node_hash(parse_spec(version.spec).nodes["a"])},
                }
            ],
            approved_by=world.owner,
            now=NOW,
        )
        counts = await count_agent_references(
            s, tenant_id=world.tenant_id, old="Default", new="aria"
        )
        assert counts["workflows.spec"] == 2, f"the draft and the version would move: {counts}"
        await rename_agent_references(s, tenant_id=world.tenant_id, old="Default", new="aria")

    async with world.sm() as s:
        row = await workflows.get_workflow(
            s, tenant_id=world.tenant_id, workflow_id=world.workflow_id
        )
        moved = await workflows.get_version(
            s, tenant_id=world.tenant_id, workflow_id=world.workflow_id, number=1
        )
        grants = await workflow_runs.live_grants(s, workflow_id=world.workflow_id, version=1)
    assert row is not None and moved is not None
    assert row.draft_spec["nodes"]["a"]["agent"] == "aria", "the draft names the new agent"
    assert moved.spec["nodes"]["a"]["agent"] == "aria", "the live version names the new agent"
    assert moved.spec["defaults"]["agent"] == "aria", "the workflow's default agent follows"
    assert moved.spec["triggers"][0]["agent"] == "aria", "a poll trigger follows"
    assert moved.agent_tools == {"aria": ["a/b"]}, "the version's tools are keyed by the new name"
    assert moved.spec_hash != "h", "the hash covers the rewritten spec"
    pinned = node_hash(parse_spec(moved.spec).nodes["a"])
    assert [g.pins["node_hash"] for g in grants] == [pinned], "the grant follows its node"

    async with other.sm() as s:
        untouched = await workflows.get_workflow(
            s, tenant_id=other.tenant_id, workflow_id=other.workflow_id
        )
    assert untouched is not None and untouched.draft_spec["nodes"]["a"]["agent"] == "default", (
        "another workspace keeps its agent"
    )
