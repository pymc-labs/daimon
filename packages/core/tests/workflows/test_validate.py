"""The validator against the fixtures the web canvas also checks (`validation.test.ts`), and
the publish it blocks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.core.workflows import service
from daimon.core.workflows.access import Who
from daimon.core.workflows.service import Env
from daimon.core.workflows.taint import Catalog, tool_facts
from daimon.core.workflows.validate import validate_document
from daimon.testing.workflows import NOW, make_world
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
        spec = {
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


async def test_a_draft_with_errors_cannot_be_published(db_session: AsyncSession) -> None:
    world = await make_world(db_session, {"f": {"kind": "finish"}}, "f")
    env = Env(world.sm, world.settings, Who(world.tenant_id, world.owner, admin=True), NOW)
    draft = await service.get_draft(env, world.workflow_id)
    broken = draft.spec | {"start": "missing"}
    await service.save_draft(env, world.workflow_id, base_revision=draft.revision, spec=broken)
    with pytest.raises(service.Invalid, match="before publishing"):
        await service.publish(env, world.workflow_id, "")
