"""OpenAI host cases; shared helpers belong to test_catalog_steps."""

from __future__ import annotations

from typing import Literal

import pytest
from daimon.testing.provider_fixtures import FixtureGap, load_provider_fixtures

from .catalog_runner import load_catalog, plan_scenario
from .headless_executor import execute_gemini_cell, execute_openai_cell
from .test_catalog_steps import (
    CATALOG as CATALOG,
)
from .test_catalog_steps import (
    FIXTURES as FIXTURES,
)
from .test_catalog_steps import (
    REPO as REPO,
)


@pytest.mark.parametrize("backend", ("openai",))
@pytest.mark.parametrize(
    "scenario_id",
    [
        "QA-D1-REPLACE-NOTICE-HIDDEN-FOR-SKILLS",
        "QA-D7-BIG-WORKSPACE-CHECKPOINT",
        "QA-NEW12-AGENT-SWITCH-HANDOFF-COPY",
    ],
)
async def test_real_native_turn_prefix_stops_at_the_original_admin(
    backend: Literal["openai", "gemini"], scenario_id: str
) -> None:
    pack = load_provider_fixtures(FIXTURES / "index.json", catalog_root=CATALOG)
    scenario, fixture = pack.select(scenario_id, backend)
    entry = next(e for e in load_catalog(CATALOG) if e.id == scenario_id).model_copy(
        update={"sha256": scenario.source.sha256}
    )
    plan = plan_scenario(entry, CATALOG, backend, run_id="prefix")
    result = await (execute_openai_cell if backend == "openai" else execute_gemini_cell)(
        plan, scenario, fixture, source_root=CATALOG, repository_root=REPO
    )
    assert result.status == "PENDING" and result.reported_status == "PENDING"
    assert result.host_outcome.status == "PASS"
    assert result.request_count == (6 if backend == "openai" else 3)
    assert [t.turn for t in result.evidence.turns] == [1]
    assert [r.location for r in result.step_receipts] == [i.location for i in plan.invocations]
    assert [r.status for r in result.step_receipts[:3]] == ["executed"] * 3
    assert result.step_receipts[3].kind == "admin"
    assert all(r.status == "pending" and r.root_turn_id is None for r in result.step_receipts[3:])
    assert result.step_receipts[2].root_turn_id == result.evidence.turns[0].root_turn_id
    assert any(g.code == "STEP_BINDING_UNAVAILABLE" for g in result.gaps)
    assert any(g.code == "CATALOG_CAPABILITY_PENDING" for g in result.gaps)
    assert len(result.catalog_outcome.checks) == len(plan.assertions) + len(plan.global_assertions)
    assert not any(
        t.tool_capture_complete or t.approval_capture_complete for t in result.evidence.turns
    )


@pytest.mark.parametrize("backend", ("openai",))
@pytest.mark.parametrize(
    "scenario_id", ("QA-NEW17-MCP-USABLE-NEXT-MESSAGE", "QA-D5-UNBOUND-AGENT-EDIT-REFUSED")
)
async def test_global_mcp_prerequisites_remain_zero_dispatch_pending(
    monkeypatch: pytest.MonkeyPatch, backend: Literal["openai", "gemini"], scenario_id: str
) -> None:
    if backend == "openai":
        from . import openai_catalog_replay as consumer
    else:
        from . import gemini_catalog_replay as consumer

    scenario, fixture = load_provider_fixtures(
        FIXTURES / "index.json", catalog_root=CATALOG
    ).select(scenario_id, backend)
    entry = next(e for e in load_catalog(CATALOG) if e.id == scenario_id).model_copy(
        update={"sha256": scenario.source.sha256}
    )
    plan = plan_scenario(entry, CATALOG, backend, run_id="mcp-refusal")

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("MCP prerequisite reached SDK allocation")

    monkeypatch.setattr(consumer, "provider_replay", forbidden)
    result = await (execute_openai_cell if backend == "openai" else execute_gemini_cell)(
        plan, scenario, fixture, source_root=CATALOG, repository_root=REPO
    )
    if result.status != "PENDING" or result.request_count or result.evidence.turns:
        raise AssertionError("MCP prerequisites earned execution or full-cell credit")
    for gap in fixture.gaps:
        if gap.location in {"mcp", "deepwiki"} and not any(
            g.location == gap.location and g.code == "PROVIDER_" + gap.code for g in result.gaps
        ):
            raise AssertionError("original MCP prerequisite disappeared")


async def test_exact_remote_effect_gap_keeps_native_prefix_pending() -> None:
    from .catalog_steps import REMOTE_EFFECT_GAP

    scenario, fixture = load_provider_fixtures(
        FIXTURES / "index.json", catalog_root=CATALOG
    ).select("QA-NEW37-MARKDOWN-TABLE-READABLE", "openai")
    gap = FixtureGap(code="RUNNER_HOOK", location="steps[1]", reason=REMOTE_EFFECT_GAP)
    fixture = fixture.model_copy(update={"gaps": (gap,)})
    scenario = scenario.model_copy(update={"providers": (fixture, scenario.providers[1])})
    entry = next(e for e in load_catalog(CATALOG) if e.id == scenario.scenario_id).model_copy(
        update={"sha256": scenario.source.sha256}
    )
    plan = plan_scenario(entry, CATALOG, "openai", run_id="remote-effect")
    result = await execute_openai_cell(
        plan, scenario, fixture, source_root=CATALOG, repository_root=REPO
    )
    if result.request_count != 6 or result.host_outcome.status != "PASS":
        raise AssertionError("exact observational gap suppressed the owned native prefix")
    if not any(
        g.code == "PROVIDER_RUNNER_HOOK" and g.location == gap.location for g in result.gaps
    ):
        raise AssertionError("native packets erased the remote-effect prerequisite")
    if result.status == "PASS":
        raise AssertionError("authored packets earned full-cell remote-effect credit")
