"""Native prefix execution, deadline cancellation and source mutants."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from daimon.testing.outcome_oracle import TurnEvidence
from daimon.testing.provider_fixtures import FixtureGap, load_provider_fixtures

from .catalog_runner import Invocation, load_catalog, plan_scenario
from .catalog_steps import StepReceipt, run_steps
from .headless_executor import execute_cell
from .test_catalog_runner import planned, source
from .test_headless_executor import scripted

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "packages/testing/fixtures/target53"
CATALOG = FIXTURES / "catalog"


async def test_measured_wait_and_explicit_deadline_through_real_anthropic_host(
    tmp_path: Path,
) -> None:
    row = source()
    row["surface"] = "headless"
    row["steps"][2]["timeout_s"] = 5.0
    row["steps"].insert(3, {"do": "wait", "s": 0.01})
    plan = planned(tmp_path, row)
    result = await execute_cell(plan, scripted(plan))
    assert result.status == "PASS" and result.request_count == 6
    assert len(result.step_receipts) == len(plan.invocations)
    wait = next(r for r in result.step_receipts if r.kind == "wait")
    assert wait.status == "executed" and wait.delay_s == 0.01
    assert wait.finished_s is not None and wait.started_s is not None
    assert wait.finished_s - wait.started_s >= 0.01
    done = result.step_receipts[2]
    assert done.kind == "wait_done" and done.timeout_s == 5.0
    assert done.root_turn_id == result.evidence.turns[0].root_turn_id


async def test_real_deadline_cancels_and_drains_the_owned_turn(tmp_path: Path) -> None:
    row = source()
    row["steps"][2]["timeout_s"] = 0.01
    plan = planned(tmp_path, row)
    cancelled = asyncio.Event()

    async def invoke(_i: Invocation, _timeout: float) -> TurnEvidence:
        try:
            await asyncio.Event().wait()
            raise RuntimeError("unreachable")
        finally:
            cancelled.set()

    async def prepare(_i: Invocation) -> None:
        return None

    with pytest.raises(TimeoutError):
        await run_steps(plan, invoke=invoke, prepare_channel=prepare)
    assert cancelled.is_set()


@pytest.mark.parametrize("mutation", ["drop", "reorder", "source", "operation", "number"])
async def test_source_operation_mutants_refuse_before_any_hook(
    tmp_path: Path, mutation: str
) -> None:
    row = source()
    row["steps"].insert(3, {"do": "admin", "tool": "cli", "args": "never execute"})
    plan = planned(tmp_path, row)
    steps = list(plan.invocations)
    if mutation == "drop":
        steps.pop(3)
    elif mutation == "reorder":
        steps[3], steps[4] = steps[4], steps[3]
    elif mutation == "source":
        steps[3] = steps[3].model_copy(update={"source": {"do": "wait", "s": 0}})
    elif mutation == "operation":
        steps[3] = steps[3].model_copy(update={"operation": "new_channel"})
    else:
        steps[1] = steps[1].model_copy(update={"turn": 7})
    plan = plan.model_copy(update={"invocations": tuple(steps)})

    async def no_invoke(_i: Invocation, _timeout: float) -> TurnEvidence:
        raise AssertionError("corrupt plan reached turn hook")

    async def no_prepare(_i: Invocation) -> None:
        raise AssertionError("corrupt plan reached channel hook")

    with pytest.raises(ValueError):
        await run_steps(plan, invoke=no_invoke, prepare_channel=no_prepare)


async def test_unknown_runner_hook_is_not_a_remote_effect_exception(tmp_path: Path) -> None:
    plan = planned(tmp_path, source())

    async def no_invoke(_i: Invocation, _timeout: float) -> TurnEvidence:
        raise AssertionError("unknown hook reached provider")

    async def prepare(_i: Invocation) -> None:
        return None

    steps = await run_steps(
        plan,
        invoke=no_invoke,
        prepare_channel=prepare,
        native_gaps=(
            FixtureGap(code="RUNNER_HOOK", location="steps[1]", reason="unknown remote work"),
        ),
    )
    assert any(g.code == "PROVIDER_RUNNER_HOOK" for g in steps.gaps)
    assert steps.receipts[0].status == "executed"
    assert all(r.status == "pending" for r in steps.receipts[1:])


def test_receipt_cannot_invent_a_measured_pending_or_admin_step() -> None:
    with pytest.raises(ValueError, match="measured execution"):
        StepReceipt(
            location="steps[1]", kind="mention", status="pending", code="UNBOUND", started_s=1.0
        )
    with pytest.raises(ValueError, match="owned execution hook"):
        StepReceipt(
            location="steps[1]",
            kind="admin",
            status="executed",
            code="BOUND",
            started_s=1.0,
            finished_s=2.0,
        )


def test_optimized_source_deadline_and_receipt_refusals(tmp_path: Path) -> None:
    row = source()
    row["steps"][2]["timeout_s"] = 0.01
    plan = planned(tmp_path, row)
    inputs = tmp_path / "steps.json"
    inputs.write_text(json.dumps(plan.model_dump(mode="json")))
    script = """
import asyncio,json,sys
sys.path.insert(0,sys.argv[2])
from judge.catalog_runner import ScenarioPlan
from judge.catalog_steps import run_steps, StepReceipt
p=ScenarioPlan.model_validate(json.load(open(sys.argv[1])))
async def check():
    cancelled=asyncio.Event()
    async def invoke(i,timeout):
        try: await asyncio.Event().wait()
        finally: cancelled.set()
    async def prepare(i): pass
    try: await run_steps(p,invoke=invoke,prepare_channel=prepare)
    except TimeoutError: pass
    else: raise RuntimeError("deadline ignored under -O")
    if not cancelled.is_set(): raise RuntimeError("owned task leaked under -O")
    for index in range(len(p.invocations)):
        mutated=p.model_copy(update={"invocations":p.invocations[:index]+p.invocations[index+1:]})
        async def refuse(*args): raise RuntimeError("dropped step reached a hook")
        try: await run_steps(mutated,invoke=refuse,prepare_channel=refuse)
        except ValueError: pass
        else: raise RuntimeError("dropped original operation accepted under -O")
    try: StepReceipt(location="steps[1]",kind="admin",status="executed",code="BOUND",started_s=1,finished_s=2)
    except ValueError: pass
    else: raise RuntimeError("admin execution invented under -O")
asyncio.run(check())
"""
    run = subprocess.run(
        [sys.executable, "-O", "-c", script, str(inputs), str(REPO / "tests")],
        capture_output=True,
        timeout=180,
    )
    if run.returncode != 0:
        raise AssertionError(run.stderr.decode())


@pytest.mark.parametrize("timeout", [5.0, 180.0])
async def test_valid_catalog_deadline_binds_and_does_not_add_a_pending_gap(
    tmp_path: Path, timeout: float
) -> None:
    row = source()
    row["surface"] = "headless"
    row["steps"][2]["timeout_s"] = timeout
    plan = planned(tmp_path, row)
    result = await execute_cell(plan, scripted(plan))
    assert result.status == "PASS" and result.request_count == 6
    assert result.step_receipts[2].timeout_s == timeout
    assert result.step_receipts[1].timeout_s == min(timeout, 10.0)
    assert not any(g.code == "WAIT_DONE_TIMEOUT_UNBOUND" for g in result.gaps)


@pytest.mark.parametrize("mutation", ["foreign_root", "foreign_session", "tentative", "missing"])
async def test_wait_done_refuses_any_foreign_or_unverified_terminal(
    tmp_path: Path, mutation: str
) -> None:
    plan = planned(tmp_path, source())
    baseline = await execute_cell(plan, scripted(plan))
    evidence = baseline.evidence.turns[0]
    terminal = evidence.terminals[0]
    if mutation == "missing":
        evidence = evidence.model_copy(update={"terminals": ()})
    else:
        changes = {
            "foreign_root": {"root_turn_id": "other"},
            "foreign_session": {"session_id": "other"},
            "tentative": {"authority": "tentative"},
        }
        evidence = evidence.model_copy(
            update={
                "terminals": (*evidence.terminals, terminal.model_copy(update=changes[mutation]))
            }
        )

    async def invoke(_i: Invocation, _timeout: float) -> TurnEvidence:
        return evidence

    async def prepare(_i: Invocation) -> None:
        return None

    steps = await run_steps(plan, invoke=invoke, prepare_channel=prepare)
    assert steps.receipts[2].status == "pending"
    assert steps.receipts[2].code == "WAIT_DONE_TERMINAL_UNVERIFIED"
    assert all(r.status == "pending" for r in steps.receipts[3:])
    assert any(g.code == "WAIT_DONE_TERMINAL_UNVERIFIED" for g in steps.gaps)


@pytest.mark.parametrize(
    "mutation",
    ("unknown", "foreign", "missing_intent", "foreign_call", "foreign_source", "foreign_backend"),
)
async def test_global_mcp_gap_requires_its_selected_source_and_call(mutation: str) -> None:
    scenario, fixture = load_provider_fixtures(
        FIXTURES / "index.json", catalog_root=CATALOG
    ).select("QA-D5-UNBOUND-AGENT-EDIT-REFUSED", "openai")
    entry = next(e for e in load_catalog(CATALOG) if e.id == scenario.scenario_id).model_copy(
        update={"sha256": scenario.source.sha256}
    )
    plan = plan_scenario(entry, CATALOG, "openai", run_id="mcp-mutant")
    gap = next(g for g in fixture.gaps if g.location == "mcp")
    if mutation in {"unknown", "foreign"}:
        gap = gap.model_copy(
            update={"location": "unknown-global" if mutation == "unknown" else "deepwiki"}
        )
        fixture = fixture.model_copy(update={"gaps": (gap,)})
    elif mutation == "missing_intent":
        fixture = fixture.model_copy(update={"mcp_binding": None})
    elif mutation in {"foreign_call", "foreign_source"}:
        turn = fixture.turns[0]
        turn = turn.model_copy(
            update={"mcp_calls": ()} if mutation == "foreign_call" else {"location": "steps[0]"}
        )
        fixture = fixture.model_copy(update={"turns": (turn,)})
    else:
        plan = plan.model_copy(update={"backend": "gemini"})

    async def forbidden(*args: object) -> TurnEvidence:
        raise AssertionError("foreign MCP binding reached provider")

    async def no_channel(_i: Invocation) -> None:
        raise AssertionError("foreign MCP binding reached channel preparation")

    try:
        await run_steps(
            plan,
            invoke=forbidden,
            prepare_channel=no_channel,
            native_gaps=(gap,),
            native_fixture=fixture,
        )
    except ValueError:
        return
    raise AssertionError("foreign global prerequisite was accepted")


@pytest.mark.parametrize(
    "reason",
    (
        "Remote tool/workspace effects remain uncaptured.",
        "Runner must bind the preserved operation or asset.",
    ),
)
async def test_global_effect_scope_cannot_borrow_a_bound_step_exception(reason: str) -> None:
    scenario, fixture = load_provider_fixtures(
        FIXTURES / "index.json", catalog_root=CATALOG
    ).select("QA-D5-UNBOUND-AGENT-EDIT-REFUSED", "openai")
    entry = next(e for e in load_catalog(CATALOG) if e.id == scenario.scenario_id).model_copy(
        update={"sha256": scenario.source.sha256}
    )
    plan = plan_scenario(entry, CATALOG, "openai", run_id="global-effects")
    gap = next(g for g in fixture.gaps if g.location == "mcp").model_copy(update={"reason": reason})
    fixture = fixture.model_copy(update={"gaps": (gap,)})

    async def forbidden(_i: Invocation, _timeout: float) -> TurnEvidence:
        raise AssertionError("global prerequisite borrowed a step exception")

    async def prepare(_i: Invocation) -> None:
        return None

    result = await run_steps(
        plan, invoke=forbidden, prepare_channel=prepare, native_gaps=(gap,), native_fixture=fixture
    )
    if not any(g.code == "PROVIDER_RUNNER_HOOK" and g.location == "mcp" for g in result.gaps):
        raise AssertionError("global effect prerequisite disappeared")
    if any(r.status == "executed" and r.kind == "mention" for r in result.receipts):
        raise AssertionError("global effect prerequisite earned turn dispatch")
