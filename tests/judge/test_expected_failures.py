"""Expected baseline defects and fixture gaps never earn spurious QA credit."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from daimon.testing.outcome_oracle import CheckResult, OutcomeReport
from pydantic import ValidationError

from .catalog_runner import ScenarioPlan, load_catalog, plan_scenario
from .headless_executor import (
    TABLE_SCENARIO,
    TABLE_SOURCE_SHA256,
    CellResult,
    ScenarioReplay,
    execute_cell,
)

REPO = Path(__file__).resolve().parents[2]
CATALOG = REPO / "packages/testing/fixtures/target53/catalog"
REPLAYS = Path(__file__).with_name("fixtures") / "catalog_anthropic.json"
TABLE = (
    "| Fruit | Colour | Price |\n|---|---|---|\n"
    "| Apple | Red | $1 |\n| Banana | Yellow | $2 |\n"
    "| Orange | Orange | $3 |\n| Pear | Green | $4 |"
)

BAD_TABLE = (
    "Fruit | Color | Taste\nApple | Red | Sweet\nPear | Green | Sweet\n"
    "Lemon | Yellow | Sour\nGrape | Purple | Sweet"
)


def selected() -> tuple[ScenarioPlan, ScenarioReplay]:
    entry = next(e for e in load_catalog(CATALOG) if e.id == TABLE_SCENARIO)
    entry = entry.model_copy(update={"sha256": TABLE_SOURCE_SHA256})
    plan = plan_scenario(entry, CATALOG, "anthropic", run_id="unscored-control")
    raw = next(r for r in json.loads(REPLAYS.read_text()) if r["scenario_id"] == TABLE_SCENARIO)
    return plan, ScenarioReplay.model_validate(raw)


def control(replay: ScenarioReplay, text: str) -> ScenarioReplay:
    data = replay.model_dump(mode="json")
    message = next(e for e in data["turns"][0]["events"] if e["type"] == "agent.message")
    message["content"] = [{"type": "text", "text": text}]
    return ScenarioReplay.model_validate(data)


async def test_original_nonequivalent_anthropic_fixture_has_no_scored_pass() -> None:
    plan, replay = selected()
    result = await execute_cell(plan, control(replay, BAD_TABLE))
    assert result.status == result.reported_status == "PENDING"
    assert result.request_count == 0 and not result.evidence.turns
    assert any(g.code == "REPLAY_TABLE_CONTROL_NOT_EQUIVALENT" for g in result.gaps)
    assert all(c.status == "PENDING" for c in result.catalog_outcome.checks)


@pytest.mark.parametrize("damage", ["outer_pipes", "separator", "wrong_column", "missing_row"])
async def test_nonequivalent_controls_are_pending_before_dispatch(damage: str) -> None:
    plan, replay = selected()
    text = TABLE
    if damage == "outer_pipes":
        text = "\n".join(line.strip("|") for line in TABLE.splitlines())
    elif damage == "separator":
        text = TABLE.replace("|---|---|---|\n", "")
    elif damage == "wrong_column":
        text = TABLE.replace("Price", "Taste")
    else:
        text = TABLE.rsplit("\n", 1)[0]
    result = await execute_cell(plan, control(replay, text))
    assert result.reported_status == "PENDING" and result.request_count == 0
    assert not result.evidence.turns


async def test_matched_anthropic_control_is_unscored_expected_failure() -> None:
    plan, replay = selected()
    result = await execute_cell(plan, control(replay, TABLE))
    assert not result.scored  # A separate synthetic control, no frozen tape edit.
    assert result.status == "FAIL" and result.reported_status == "EXPECTED-FAIL"
    assert result.catalog_outcome.checks[0].code == "FORBIDDEN_VISIBLE_TEXT"
    assert result.host_outcome.status == "PASS" and result.request_count == 3
    assert result.discord_surfaces[0].text_capture_complete
    assert TABLE in result.evidence.turns[0].texts[0].text


async def test_unexpected_global_or_host_failures_are_never_relabelled() -> None:
    plan, replay = selected()
    result = await execute_cell(plan, control(replay, TABLE))
    bad_check = CheckResult(
        kind="text_absent", turn=1, status="FAIL", code="FORBIDDEN_VISIBLE_TEXT"
    )
    catalog = result.catalog_outcome.model_copy(
        update={"checks": (*result.catalog_outcome.checks, bad_check)}
    )
    changed = result.model_copy(update={"catalog_outcome": catalog})
    assert changed.reported_status == "FAIL"
    host = OutcomeReport(
        scenario_id=TABLE_SCENARIO,
        backend="anthropic",
        checks=(
            CheckResult(kind="turn_completed", turn=1, status="FAIL", code="TURN_NOT_COMPLETED"),
        ),
    )
    assert result.model_copy(update={"host_outcome": host}).reported_status == "FAIL"
    assert result.model_copy(update={"discord_surfaces": ()}).reported_status == "FAIL"
    data = result.model_dump(exclude={"status", "reported_status"})
    data["source_sha256"] = "a" * 64
    with pytest.raises(ValidationError, match="reviewed catalog source"):
        CellResult.model_validate(data)


def test_expected_failure_and_fixture_gaps_survive_optimized_python() -> None:
    script = """
import asyncio, sys
sys.path.insert(0,sys.argv[1])
from judge.test_expected_failures import selected,control,TABLE,BAD_TABLE
from judge.headless_executor import execute_cell
async def check():
 p,r=selected()
 old=await execute_cell(p,control(r,BAD_TABLE))
 if old.status!='PENDING' or old.reported_status!='PENDING' or old.request_count or old.evidence.turns:
  raise RuntimeError('spurious table fixture earned credit')
 x=await execute_cell(p,control(r,TABLE))
 if x.scored or x.status!='FAIL' or x.reported_status!='EXPECTED-FAIL':
  raise RuntimeError('matched unscored table baseline misclassified')
 if x.model_copy(update={'discord_surfaces':()}).reported_status!='FAIL':
  raise RuntimeError('missing surface observation relabelled')
asyncio.run(check())
"""
    result = subprocess.run(
        [sys.executable, "-O", "-c", script, str(Path(__file__).parents[1])],
        capture_output=True,
        check=False,
        timeout=60,
    )
    if result.returncode:
        raise AssertionError(result.stderr.decode())
