"""The validator against the fixtures the web canvas also checks (`validation.test.ts`), and
the publish it blocks."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from daimon.core.tool_safety import OPEN_TOOL_SAFETY, ToolSafetyPolicy
from daimon.core.workflows import service
from daimon.core.workflows.access import Who
from daimon.core.workflows.catalog import spec_agent_tools, tool_catalog
from daimon.core.workflows.service import Env
from daimon.core.workflows.spec import parse_spec
from daimon.core.workflows.taint import Catalog, catalog_of, tool_facts
from daimon.core.workflows.unattended import exposed_at_run
from daimon.core.workflows.validate import validate_document, validate_spec
from daimon.testing.ma_models import ma_agent
from daimon.testing.workflows import NOW, agents_with, make_world
from sqlalchemy.ext.asyncio import AsyncSession

FIXTURES = Path(__file__).parents[4] / "web/src/modules/workflows/fixtures/validation.json"
CASES: list[dict[str, Any]] = json.loads(FIXTURES.read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_validator_matches_shared_fixture(case: dict[str, Any]) -> None:
    catalog = Catalog.model_validate(case["catalog"]) if "catalog" in case else None
    result = validate_document(case["spec"], catalog)

    errors = sorted({f"{p.code}:{p.node or ''}" for p in result.errors})
    warnings = sorted({f"{p.code}:{p.node or ''}" for p in result.warnings})
    assert errors == case["errors"], f"errors differ: {[p.message for p in result.errors]}"
    assert warnings == case["warnings"], "warnings differ from the fixture"
    if "grants" in case:
        grants = sorted({f"{g.kind}:{g.node or ''}" for g in result.grants})
        assert grants == case["grants"], "grants differ from the fixture"
    if "classes" in case:
        assert result.classes == case["classes"], "Rule of Two classes differ from the fixture"


def test_an_action_is_classified_as_tool_safety_classifies_it() -> None:
    """A write the old name guess missed needs its grant; an operator effect overrides it."""

    def grants(tool: str) -> list[str]:
        spec: dict[str, Any] = {
            "schema": "daimon.workflow/v1",
            "name": "merge",
            "start": "act",
            "nodes": {
                "act": {"kind": "action", "tool": tool, "args": {}, "next": "done"},
                "done": {"kind": "finish"},
            },
        }
        return [g.kind for g in validate_document(spec, None).grants]

    assert grants("github/merge_pull_request") == ["unattended_write"], "unknown names write"
    assert grants("github/get_pull_request") == [], "a get_ name reads"
    marked = ToolSafetyPolicy(effects={"github/get_pull_request": "write"})
    assert tool_facts(marked, "github/get_pull_request").writes, "the operator's effect wins"


def test_a_one_time_schedule_must_be_in_the_future() -> None:
    spec: dict[str, Any] = {
        "schema": "daimon.workflow/v1",
        "name": "once",
        "triggers": [{"id": "once", "kind": "schedule", "at": "2026-11-21T09:00:00Z"}],
        "start": "done",
        "nodes": {"done": {"kind": "finish"}},
    }

    def codes(now: datetime) -> list[str]:
        return [p.code for p in validate_document(spec, None, now=now).errors]

    assert codes(datetime(2026, 11, 21, 10, tzinfo=UTC)) == ["at_in_past"]
    assert codes(datetime(2026, 11, 21, 8, tzinfo=UTC)) == []


async def test_a_draft_with_errors_cannot_be_published(db_session: AsyncSession) -> None:
    world = await make_world(db_session, {"f": {"kind": "finish"}}, "f")
    env = Env(world.sm, world.settings, Who(world.tenant_id, world.owner, admin=True), NOW)
    draft = await service.get_draft(env, world.workflow_id)
    broken = draft.spec | {"start": "missing"}
    await service.save_draft(env, world.workflow_id, base_revision=draft.revision, spec=broken)
    with pytest.raises(service.Invalid, match="before publishing"):
        await service.publish(env, world.workflow_id, "")


async def test_every_validation_classes_the_agents_tools(db_session: AsyncSession) -> None:
    """An agent reading mail and sending it breaks the Rule of Two on any path, and its run
    sees the same exposure from the tools kept with the version."""
    tools = {"triage": ["gmail/list_messages", "gmail/send_message"]}
    nodes = {
        "read": {
            "kind": "agent",
            "agent": "triage",
            "instructions": "x",
            "writes": "allow",
            "next": "done",
        },
        "done": {"kind": "finish"},
    }
    world = await make_world(db_session, nodes, "read")
    who = Who(world.tenant_id, world.owner, admin=True)
    blind = Env(world.sm, world.settings, who, NOW)
    env = Env(world.sm, world.settings, who, NOW, agents_with(tools))
    draft = await service.get_draft(env, world.workflow_id)
    codes = [p.code for p in (await service.validate(env, world.workflow_id, draft.spec)).errors]
    assert "rule_of_two" in codes, f"the agent's own mail reads taint its sends: {codes}"
    with pytest.raises(service.Invalid, match="before publishing"):
        await service.publish(env, world.workflow_id, "")
    unknown = [
        p.code for p in (await service.validate(blind, world.workflow_id, draft.spec)).errors
    ]
    assert "unknown_agent" in unknown, "a path that lists no agents fails closed"
    spec = parse_spec(draft.spec)
    run_catalog = catalog_of(OPEN_TOOL_SAFETY, tools)
    assert exposed_at_run(spec, "read", untrusted_inputs=False, catalog=run_catalog), "at run"


async def test_a_toolset_on_by_default_offers_its_unlisted_tools() -> None:
    """A server attached with only `default_config` (as `mcp_attach` does) enables every tool:
    its reads taint the agent's writes, and a spec may name its tools."""
    on = {"enabled": True, "permission_policy": {"type": "always_allow"}}
    gmail = {"type": "mcp_toolset", "mcp_server_name": "gmail", "default_config": on, "configs": []}

    async def agents() -> list[Any]:
        return [ma_agent(id="agent_triage", name="triage", tools=[gmail])]

    catalog = await tool_catalog(OPEN_TOOL_SAFETY, agents)
    spec = parse_spec(
        {
            "schema": "daimon.workflow/v1",
            "name": "triage",
            "start": "read",
            "nodes": {
                "read": {
                    "kind": "agent",
                    "agent": "triage",
                    "instructions": "x",
                    "writes": "allow",
                    "next": "send",
                },
                "send": {"kind": "action", "tool": "gmail/send_message", "args": {}, "next": "f"},
                "f": {"kind": "finish"},
            },
        }
    )
    codes = [p.code for p in validate_spec(spec, catalog).errors]
    assert "rule_of_two" in codes and "unknown_tool" not in codes, codes
    stored = catalog_of(OPEN_TOOL_SAFETY, spec_agent_tools(spec, catalog))
    assert exposed_at_run(spec, "read", untrusted_inputs=False, catalog=stored), "at run"
