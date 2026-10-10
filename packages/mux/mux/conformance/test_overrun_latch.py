"""Billable final measurements and the overrun admission latch are independent."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from mux.conformance.budget import (
    ActualSpend,
    BudgetGuard,
    BudgetLedgerError,
    BudgetRefused,
    MeasuredRequest,
    ProbePlan,
    SpendReceipt,
    TokenLimits,
    TokenUsage,
)
from mux.conformance.test_budget import receipts
from mux.conformance.test_exact_spend import AT, dated_guard, measured, proposal, sign, smoke_plan
from mux.conformance.test_exact_spend import guard_clock as guard_clock

MODELS = {
    "openai": "gpt-6-luna",
    "anthropic": "claude-haiku-5-5",
    "gemini": "gemini-3.8-flash",
}


def guard_and_plan(root: Path, provider: str = "gemini") -> tuple[BudgetGuard, ProbePlan]:
    config, ledger = root / "budget.json", root / "spend.md"
    config.write_text(
        json.dumps(
            {
                "ledger_path": str(ledger),
                "providers": {
                    provider: {
                        "cap_usd": "30",
                        "models": {
                            MODELS[provider]: {
                                "input": "1",
                                "cached_input": "1",
                                "cache_write_input": "1",
                                "output": "4",
                                "effective_from": "2026-10-10",
                                "source": "https://ai.google.dev/gemini-api/docs/pricing",
                            }
                        },
                    }
                },
            }
        )
    )
    BudgetGuard.initialize(config, ledger)
    guard = BudgetGuard(config, ledger)
    return guard, ProbePlan(
        provider=provider,
        model=MODELS[provider],
        fixture_id="C07",
        limits=TokenLimits(input_tokens=32768, output_tokens=32768),
    )


def snapshot(incoming: int, *, complete: bool, at: datetime = AT) -> ActualSpend:
    return ActualSpend(
        requests=(
            MeasuredRequest(
                id="same-accepted-root",
                observed_at=at,
                pricing_basis="standard-global",
                tokens=TokenUsage(
                    input_tokens=incoming,
                    output_tokens=8,
                    input_cached_tokens=0,
                    input_cache_write_tokens=0,
                ),
            ),
        ),
        containers=(),
        usage_complete=complete,
    )


def assert_blocked(guard: BudgetGuard, plan: ProbePlan) -> None:
    # Reopen the persisted ledger: refusal is independent of the worker object.
    guard = BudgetGuard(guard.config_path, guard.spend_path)
    with pytest.raises(BudgetRefused):
        guard.reserve(
            ProbePlan.create_only(provider=plan.provider, model=plan.model, fixture_id="C07")
        )
    blocked = receipts(guard)[-1]
    assert blocked.status == "blocked" and blocked.reason == "budget"
    assert blocked.actual_usd == blocked.held_usd == 0


@pytest.mark.parametrize("provider", list(MODELS))
@pytest.mark.parametrize("final_input", [1_100_000, 64])
def test_fresh_complete_revision_settles_actual_without_clearing_overrun(
    tmp_path: Path, provider: str, final_input: int
) -> None:
    # Codex's POST in_progress=1M@00:00, GET completed=1.1M@01:00;
    # also a fresh downwards correction to 64, below the original plan limit.
    guard, plan = guard_and_plan(tmp_path, provider)
    held = guard.reserve(plan)
    earlier = snapshot(1_000_000, complete=False)
    newest = snapshot(final_input, complete=True, at=AT + timedelta(hours=1))
    before = guard.spend_path.read_bytes()
    receipt = guard.settle_with_overrun(
        held, status="failed", limits=plan.limits, actual=newest, overrun_evidence=earlier
    )
    assert receipt.status == receipt.reason == "overrun"
    assert receipt.admission_blocked
    assert receipt.accounting_status == "actual" and receipt.held_usd == 0
    assert receipt.actual_usd == Decimal(final_input) / 1_000_000 + Decimal(".000032")
    assert receipt.cost_estimate_usd == receipt.actual_usd
    assert receipt.tokens == newest.requests[0].tokens
    assert receipt.actual_evidence == newest and receipt.overrun_evidence == earlier
    assert guard.report() == (receipt,)
    assert guard.spend_path.read_bytes().startswith(before)
    assert len(receipts(guard)) == 2
    assert Decimal(json.loads(guard.checkpoint_path.read_text())["provider_totals"][provider]) == (
        receipt.actual_usd
    )
    assert_blocked(guard, plan)


@pytest.mark.parametrize("partial_input", [None, 64, 1_100_000])
def test_unknown_final_usage_retains_maximum_snapshot_bound_without_double_counting(
    tmp_path: Path, partial_input: int | None
) -> None:
    guard, plan = guard_and_plan(tmp_path)
    held = guard.reserve(plan)
    earlier = snapshot(1_000_000, complete=False, at=AT + timedelta(hours=1))
    # None is also the adapter seam when a stale terminal/cancel was rejected.
    # Native freshness validation belongs to the driver, not this cost ledger.
    latest = snapshot(partial_input, complete=False) if partial_input is not None else None
    receipt = guard.settle_with_overrun(
        held, status="failed", limits=plan.limits, actual=latest, overrun_evidence=earlier
    )
    assert receipt.status == "overrun" and receipt.admission_blocked
    assert receipt.actual_usd is None and receipt.accounting_status == "estimated_unverified"
    assert (
        receipt.held_usd
        == Decimal(max(1_000_000, partial_input or 0)) / 1_000_000 + Decimal(32768 * 4) / 1_000_000
    )
    assert receipt.actual_evidence == latest and receipt.overrun_evidence == earlier
    assert_blocked(guard, plan)


def test_rejected_stale_measurement_does_not_replace_already_verified_actual(
    tmp_path: Path,
) -> None:
    guard, plan = guard_and_plan(tmp_path)
    held = guard.reserve(plan)
    proof = snapshot(1_000_000, complete=False)
    verified = snapshot(1_100_000, complete=True, at=AT + timedelta(hours=1))
    # The driver's rejected 64-token stale cleanup must not overwrite verified.
    receipt = guard.settle_with_overrun(
        held, status="cancelled", limits=plan.limits, actual=verified, overrun_evidence=proof
    )
    assert receipt.actual_usd == Decimal("1.100032") and receipt.held_usd == 0
    assert receipt.actual_evidence == verified and receipt.admission_blocked
    assert_blocked(guard, plan)


def test_latch_requires_proven_overrun_not_a_caller_boolean(tmp_path: Path) -> None:
    guard, plan = guard_and_plan(tmp_path)
    held = guard.reserve(plan)
    before = guard.spend_path.read_bytes()
    with pytest.raises(BudgetLedgerError, match="does not prove"):
        guard.settle_with_overrun(
            held,
            status="completed",
            limits=plan.limits,
            actual=snapshot(64, complete=True),
            overrun_evidence=snapshot(64, complete=False),
        )
    assert guard.spend_path.read_bytes() == before
    assert guard.report() == (held.receipt,)


def test_reconciliation_releases_hold_without_clearing_admission_latch(tmp_path: Path) -> None:
    guard, plan = guard_and_plan(tmp_path)
    held = guard.reserve(plan)
    guard.settle_with_overrun(
        held,
        status="failed",
        limits=plan.limits,
        overrun_evidence=snapshot(1_000_000, complete=False),
    )
    approval = proposal(guard, held.receipt.run_id)
    sign(guard, approval)
    receipt = guard.reconcile(approval)
    assert receipt.status == "reconciled" and receipt.admission_blocked
    assert receipt.actual_usd == approval.actual_usd and receipt.held_usd == 0
    assert receipt.overrun_evidence is not None
    assert_blocked(guard, plan)


def test_runtime_overrun_proof_does_not_replace_fresh_actual(tmp_path: Path) -> None:
    guard = dated_guard(tmp_path)
    plan = smoke_plan()
    held = guard.reserve(plan)
    prior = measured(seconds=1201).model_copy(update={"usage_complete": False})
    fresh = measured(seconds=3)
    row = guard.settle_with_overrun(
        held, status="completed", limits=plan.limits, actual=fresh, overrun_evidence=prior
    )
    assert row.status == "overrun" and row.admission_blocked
    assert row.actual_evidence == fresh and row.overrun_evidence == prior
    assert row.actual_usd == Decimal(".030132") and row.held_usd == 0
    assert_blocked(guard, plan)


def test_replay_refuses_a_reconciliation_that_clears_the_latch(tmp_path: Path) -> None:
    guard, plan = guard_and_plan(tmp_path)
    held = guard.reserve(plan)
    guard.settle_with_overrun(
        held,
        status="failed",
        limits=plan.limits,
        overrun_evidence=snapshot(1_000_000, complete=False),
    )
    approval = proposal(guard, held.receipt.run_id)
    sign(guard, approval)
    row = guard.reconcile(approval)
    # Only this disposable test ledger is corrupted; production files are untouched.
    original = row.model_dump_json()
    corrupted = row.model_copy(update={"admission_blocked": False, "overrun_evidence": None})
    guard.spend_path.write_text(
        guard.spend_path.read_text().replace(original, corrupted.model_dump_json())
    )
    with pytest.raises(BudgetLedgerError, match="admission latch cannot be cleared"):
        guard.report()


def test_legacy_overrun_retains_latch_after_reconciliation(tmp_path: Path) -> None:
    guard, plan = guard_and_plan(tmp_path)
    held = guard.reserve(plan)
    row = guard.settle(
        held,
        status="completed",
        limits=plan.limits,
        usage=TokenUsage(input_tokens=1_000_000, output_tokens=8),
    )
    legacy = row.model_dump()
    legacy.pop("admission_blocked")
    legacy.pop("overrun_evidence")
    assert SpendReceipt.model_validate(legacy).admission_blocked
    approval = proposal(guard, row.run_id)
    sign(guard, approval)
    assert guard.reconcile(approval).admission_blocked
    assert_blocked(guard, plan)


def test_receipt_refuses_unlatched_evidence_and_non_boolean_latch(tmp_path: Path) -> None:
    guard, plan = guard_and_plan(tmp_path)
    original = guard.reserve(plan).receipt.model_dump()
    with pytest.raises(ValidationError, match="admission latch"):
        SpendReceipt.model_validate(
            {**original, "overrun_evidence": snapshot(1_000_000, complete=False)}
        )
    with pytest.raises(ValidationError):
        SpendReceipt.model_validate({**original, "admission_blocked": 1})


@pytest.mark.parametrize("final_input", [None, 64, 1_100_000])
def test_overrun_latch_and_exact_settlement_survive_optimized_python(
    tmp_path: Path, final_input: int | None
) -> None:
    guard, plan = guard_and_plan(tmp_path)
    held = guard.reserve(plan)
    script = """
import sys
from pathlib import Path
from decimal import Decimal
from mux.conformance.budget import BudgetGuard, BudgetRefused, ProbePlan, Reservation
from mux.conformance.test_overrun_latch import snapshot, AT
from datetime import timedelta
g = BudgetGuard(Path(sys.argv[1]), Path(sys.argv[2]))
r = Reservation.model_validate_json(sys.argv[3])
n = None if sys.argv[4] == 'None' else int(sys.argv[4])
a = snapshot(n, complete=True, at=AT + timedelta(hours=1)) if n is not None else None
row = g.settle_with_overrun(r, status='failed', limits=r.receipt.limits, actual=a,
               overrun_evidence=snapshot(1_000_000, complete=False))
if row.status != 'overrun' or not row.admission_blocked:
    raise SystemExit('overrun admission latch lost under -O')
if n is None:
    if row.actual_usd is not None or row.held_usd != Decimal('1.131072'):
        raise SystemExit('unknown final lost its proven bound under -O')
else:
    if (row.actual_usd != Decimal(n) / 1_000_000 + Decimal('.000032')
        or row.held_usd != 0 or row.accounting_status != 'actual'):
        raise SystemExit('final actual replaced by partial overrun snapshot under -O')
try:
    g.reserve(ProbePlan.create_only(provider='gemini', model='gemini-3.8-flash', fixture_id='C07'))
except BudgetRefused:
    pass
else:
    raise SystemExit('admission reopened after known actual under -O')
"""
    subprocess.run(
        [
            sys.executable,
            "-O",
            "-c",
            script,
            str(guard.config_path),
            str(guard.spend_path),
            held.model_dump_json(),
            str(final_input),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
