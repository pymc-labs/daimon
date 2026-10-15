"""OpenAI host cases; shared helpers belong to test_platform_catalog_surface."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
import yaml
from daimon.testing.provider_fixtures import load_provider_fixtures
from daimon.testing.provider_replay import SourcePin

from .catalog_runner import load_catalog, plan_scenario, sha
from .headless_executor import execute_cell, execute_gemini_cell, execute_openai_cell
from .test_catalog_runner import planned, source
from .test_platform_catalog_surface import (
    CATALOG as CATALOG,
)
from .test_platform_catalog_surface import (
    INDEX as INDEX,
)
from .test_platform_catalog_surface import (
    REPO as REPO,
)


@pytest.mark.parametrize("platform", ["slack", "teams"])
@pytest.mark.parametrize("backend", ("openai",))
async def test_real_native_hosts_bind_newly_pinned_platform_followup_microcases(
    tmp_path: Path,
    platform: str,
    backend: Literal["openai", "gemini"],
) -> None:
    # Separate source bytes + source pin: no scored catalog fixture is changed.
    pack = load_provider_fixtures(INDEX, catalog_root=CATALOG)
    template = next(
        s for s in pack.scenarios if s.scenario_id == "QA-NEW10-SPLIT-ANSWER-AND-CHUNK-REPLY"
    )
    original = next(p for p in template.providers if p.backend == backend)
    row = source(f"QA-ISOLATED-{backend.upper()}-{platform.upper()}")
    row["surface"] = platform
    row["steps"][1]["text"] = original.turns[0].request_text
    row["steps"][3]["text"] = original.turns[1].request_text
    row["assert"] = [
        {"kind": "text_absent", "turn": t, "pattern": "NEVER_FORBIDDEN"} for t in (1, 2)
    ]
    row["assert"].append({"kind": "card_finalized", "turn": 1})
    plan = planned(tmp_path, row, backend)
    invocations = [i for i in plan.invocations if i.operation == "host_turn"]
    turns = tuple(
        t.model_copy(
            update={
                "turn": i.turn,
                "location": i.location,
                "channel": i.channel,
                "thread": i.thread,
                "completion_gate": None,
                # This newly pinned microcase requests the current safety shape;
                # frozen fixture requests remain solely N3-owned.
                "request": {**t.request, "tools": []} if backend == "gemini" else t.request,
            }
        )
        for t, i in zip(original.turns, invocations, strict=True)
    )
    fixture = original.model_copy(update={"turns": turns, "gaps": ()})
    pin = SourcePin(path=f"scenarios/{plan.scenario.filename}", sha256=plan.scenario.sha256)
    raw = (tmp_path / pin.path).read_bytes()
    assert sha(raw) == pin.sha256
    scenario = template.model_copy(
        update={
            "scenario_id": plan.scenario.id,
            "source": pin,
            "projection": pin,
            "scenario": yaml.safe_load(raw),
            "values": {},
            "assets": (),
            "providers": tuple(fixture if p.backend == backend else p for p in template.providers),
        }
    )
    execute = execute_openai_cell if backend == "openai" else execute_gemini_cell
    result = await execute(plan, scenario, fixture, source_root=tmp_path, repository_root=REPO)
    assert result.status == "PENDING"  # The unbound card predicate remains in this microcase.
    assert result.host_outcome.status == "PASS"
    assert [c.status for c in result.catalog_outcome.checks[:2]] == ["PASS", "PASS"]
    assert len(result.catalog_outcome.checks) == len(plan.assertions) + len(plan.global_assertions)
    assert len(result.platform_surfaces) == 2 and not result.discord_surfaces
    assert len({t.session_id for t in result.evidence.turns}) == 1
    assert len({t.slot_id for t in result.evidence.turns}) == 1
    assert result.request_count == (12 if backend == "openai" else 9)
    for turn, capture in zip(result.evidence.turns, result.platform_surfaces, strict=True):
        assert capture.session_id == turn.session_id and capture.root_turn_id == turn.root_turn_id
        assert turn.text_capture_complete and turn.texts
        assert turn.terminals[0].authority == "record" and turn.terminal_capture_complete
        assert not turn.card_capture_complete and not turn.tool_capture_complete
        expected = [
            "\n".join((*m.body_texts, *m.card_texts, *m.control_labels)) for m in capture.messages
        ]
        assert [t.text for t in turn.texts] == [t for t in expected if t.strip()]


@pytest.mark.parametrize("scenario_id", ["QA-D1-SLACK-FOLLOWUP", "QA-D3-TEAMS-ANSWER-NOT-REPLACED"])
@pytest.mark.parametrize("backend", ("openai",))
async def test_frozen_manual_platform_cells_remain_pending_without_dispatch(
    scenario_id: str, backend: Literal["anthropic", "openai", "gemini"]
) -> None:
    pack = load_provider_fixtures(INDEX, catalog_root=CATALOG)
    scenario = next(s for s in pack.scenarios if s.scenario_id == scenario_id)
    entry = next(e for e in load_catalog(CATALOG) if e.id == scenario_id)
    entry = entry.model_copy(update={"sha256": scenario.source.sha256})
    plan = plan_scenario(entry, CATALOG, backend, run_id="manual-control")
    assert not plan.assertions and not plan.invocations
    if backend == "anthropic":
        result = await execute_cell(plan, None)
    else:
        fixture = next(p for p in scenario.providers if p.backend == backend)
        execute = execute_openai_cell if backend == "openai" else execute_gemini_cell
        result = await execute(plan, scenario, fixture, source_root=CATALOG, repository_root=REPO)
    assert result.reported_status == "PENDING" and result.status == "PENDING"
    assert result.request_count == 0 and not result.evidence.turns
    assert not result.platform_surfaces and not result.headless_outputs
