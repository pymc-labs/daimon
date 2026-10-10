"""Lead-signed admission recovery preserves unknown actuals and proven spend."""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from mux.conformance.budget import (
    BudgetGuard,
    BudgetLedgerError,
    BudgetRefused,
    LatchRecovery,
    ProbePlan,
    SpendReceipt,
    TokenUsage,
)
from mux.conformance.test_budget import receipts
from mux.conformance.test_exact_spend import guard_clock as guard_clock
from mux.conformance.test_overrun_latch import guard_and_plan, legacy_guard, snapshot


def sign(guard: BudgetGuard, proposal: LatchRecovery) -> None:
    data = json.loads(guard.config_path.read_text())
    data["approved_latch_recoveries"] = [proposal.digest]
    guard.config_path.write_text(json.dumps(data))


def latched(root: Path) -> tuple[BudgetGuard, ProbePlan, SpendReceipt]:
    guard, plan = guard_and_plan(root, "openai")
    reservation = guard.reserve(plan)
    row = guard.settle_with_overrun(
        reservation,
        status="failed",
        limits=plan.limits,
        actual=snapshot(1_100_000, complete=False),
        overrun_evidence=snapshot(1_000_000, complete=False),
    )
    return guard, plan, row


def proposal(guard: BudgetGuard, row: SpendReceipt, hold: str = "2") -> LatchRecovery:
    return guard.propose_latch_recovery(
        row.run_id, held_usd=Decimal(hold), evidence_sha256="e" * 64
    )


def test_signed_recovery_appends_estimate_preserves_all_counters_and_unblocks(
    tmp_path: Path,
) -> None:
    guard, plan, original = latched(tmp_path)
    before = guard.spend_path.read_bytes()
    recovery = proposal(guard, original)
    assert guard.spend_path.read_bytes() == before  # Proposal is read only.
    sign(guard, recovery)
    row = guard.recover_latch(recovery)
    assert row.status == "latch_recovered" and row.reason == "latch_recovery"
    assert row.actual_usd is None and row.accounting_status == "estimated_unverified"
    assert row.held_usd == row.cost_estimate_usd == Decimal("2")
    assert not row.admission_blocked and row.latch_recovery == recovery
    for name in (
        "tokens",
        "actual_evidence",
        "overrun_evidence",
        "price",
        "limits",
        "reserved_usd",
        "container_allowance",
        "session_prices",
        "token_usd",
        "container_usd",
    ):
        assert getattr(row, name) == getattr(original, name)
    assert guard.spend_path.read_bytes().startswith(before)
    assert len(receipts(guard)) == 3
    reopened = BudgetGuard(guard.config_path, guard.spend_path)
    assert reopened.report() == (row,)
    assert Decimal(json.loads(guard.checkpoint_path.read_text())["provider_totals"]["openai"]) == 2
    reopened.reserve(
        ProbePlan.create_only(provider=plan.provider, model=plan.model, fixture_id="C07")
    )
    with pytest.raises(BudgetLedgerError):
        reopened.recover_latch(recovery)


@pytest.mark.parametrize(
    "damage", ["unsigned", "held_usd", "run_id", "ledger_id", "previous_sha256", "evidence_sha256"]
)
def test_unsigned_changed_or_foreign_proposals_cannot_append(tmp_path: Path, damage: str) -> None:
    guard, _, original = latched(tmp_path)
    recovery = proposal(guard, original)
    if damage != "unsigned":
        sign(guard, recovery)
        recovery = recovery.model_copy(
            update={
                damage: Decimal("3")
                if damage == "held_usd"
                else "0" * (32 if damage in ("run_id", "ledger_id") else 64)
            }
        )
    before = (
        guard.spend_path.read_bytes(),
        guard.checkpoint_path.read_bytes(),
        guard.lock_path.read_bytes(),
    )
    with pytest.raises(BudgetRefused, match="lead-signed"):
        guard.recover_latch(recovery)
    assert before == (
        guard.spend_path.read_bytes(),
        guard.checkpoint_path.read_bytes(),
        guard.lock_path.read_bytes(),
    )
    # Even pinning a foreign/stale digest cannot authorize a different ledger row.
    if damage in ("run_id", "ledger_id", "previous_sha256"):
        sign(guard, recovery)
        with pytest.raises(BudgetLedgerError):
            guard.recover_latch(recovery)


@pytest.mark.parametrize("source", ["reservation", "proof", "current_hold", "direct_counts"])
def test_signed_hold_cannot_lower_any_conservative_floor(tmp_path: Path, source: str) -> None:
    guard, plan = guard_and_plan(tmp_path, "openai")
    held = guard.reserve(plan)
    if source == "direct_counts":
        original = guard.settle(
            held, status="failed", limits=plan.limits, usage=TokenUsage(input_tokens=2_000_000)
        )
        floor = Decimal("2")
    else:
        bound = 100_000 if source == "reservation" else 1_000_000
        original = guard.settle_with_overrun(
            held,
            status="failed",
            limits=plan.limits,
            overrun_evidence=snapshot(bound, complete=False),
        )
        floor = max(original.reserved_usd, Decimal(bound) / 1_000_000 + Decimal(".000032"))
        if source == "current_hold":
            # Conservative limit-sized hold exceeds the known request price.
            # Use a second disposable ledger, produced entirely through the API.
            (tmp_path / "larger").mkdir()
            guard, plan = guard_and_plan(tmp_path / "larger", "openai")
            held = guard.reserve(plan)
            original = guard.settle(
                held,
                status="failed",
                limits=plan.limits,
                actual=snapshot(1_000_000, complete=False),
            )
            floor = original.held_usd
            assert floor is not None and floor > Decimal("1.000032")
    recovery = LatchRecovery(
        ledger_id=json.loads(guard.checkpoint_path.read_text())["binding"]["ledger_id"],
        run_id=original.run_id,
        previous_sha256=guard.receipt_digest(original),
        evidence_sha256="e" * 64,
        held_usd=floor - Decimal(".000001"),
    )
    sign(guard, recovery)
    before = guard.spend_path.read_bytes()
    with pytest.raises(BudgetLedgerError, match="below reservation or proven spend"):
        guard.recover_latch(recovery)
    assert guard.spend_path.read_bytes() == before


def test_union_floor_keeps_disjoint_known_proof_and_actual_requests(tmp_path: Path) -> None:
    guard, plan = guard_and_plan(tmp_path, "openai")
    held = guard.reserve(plan)
    proof = snapshot(1_000_000, complete=False)
    other = snapshot(1_100_000, complete=False)
    other = other.model_copy(
        update={"requests": (other.requests[0].model_copy(update={"id": "other-request"}),)}
    )
    row = guard.settle_with_overrun(
        held, status="failed", limits=plan.limits, actual=other, overrun_evidence=proof
    )
    with pytest.raises(BudgetLedgerError, match="below reservation"):
        proposal(guard, row, "2.1")  # Union is 2.100064, not max(1M,1.1M).
    recovery = proposal(guard, row, "3")
    sign(guard, recovery)
    assert guard.recover_latch(recovery).held_usd == 3


def test_nonlatched_or_complete_actual_runs_cannot_recover(tmp_path: Path) -> None:
    guard, plan = guard_and_plan(tmp_path, "openai")
    held = guard.reserve(plan)
    with pytest.raises(BudgetLedgerError, match="incomplete actuals"):
        proposal(guard, held.receipt)
    complete = guard.settle_with_overrun(
        held,
        status="completed",
        limits=plan.limits,
        actual=snapshot(1_100_000, complete=True),
        overrun_evidence=snapshot(1_000_000, complete=False),
    )
    assert complete.actual_usd is not None
    with pytest.raises(BudgetLedgerError, match="incomplete actuals"):
        proposal(guard, complete)


def test_other_latch_and_provider_caps_still_block_admission(tmp_path: Path) -> None:
    guard, plan = guard_and_plan(tmp_path, "openai")
    held1, held2 = guard.reserve(plan), guard.reserve(plan)
    rows = [
        guard.settle_with_overrun(
            h,
            status="failed",
            limits=plan.limits,
            overrun_evidence=snapshot(1_000_000, complete=False),
        )
        for h in (held1, held2)
    ]
    recovery = proposal(guard, rows[0])
    sign(guard, recovery)
    guard.recover_latch(recovery)
    with pytest.raises(BudgetRefused):
        guard.reserve(
            ProbePlan.create_only(provider=plan.provider, model=plan.model, fixture_id="C07")
        )
    recovery2 = proposal(guard, rows[1], "25")
    sign(guard, recovery2)
    guard.recover_latch(recovery2)
    with pytest.raises(BudgetRefused):  # Both latches cleared; cap still enforced.
        guard.reserve(
            ProbePlan.create_only(provider=plan.provider, model=plan.model, fixture_id="C07")
        )


def test_legacy_rows_and_signed_reconciliation_still_replay(tmp_path: Path) -> None:
    guard, runs, staged, copied = legacy_guard(tmp_path)
    before = guard.spend_path.read_bytes()
    assert {row.run_id for row in guard.report()} >= set(runs.values())
    assert guard.spend_path.read_bytes() == before
    assert all(line in before.decode().splitlines() for line in copied)
    assert guard.reconcile(staged).actual_usd == staged.actual_usd
    assert guard.report()
    assert guard.spend_path.read_bytes().startswith(before)


def test_old_worker_cannot_settle_after_operator_recovery(tmp_path: Path) -> None:
    guard, plan = guard_and_plan(tmp_path, "openai")
    held = guard.reserve(plan)
    original = guard.settle_with_overrun(
        held,
        status="failed",
        limits=plan.limits,
        overrun_evidence=snapshot(1_000_000, complete=False),
    )
    recovery = proposal(guard, original)
    sign(guard, recovery)
    guard.recover_latch(recovery)
    before = guard.spend_path.read_bytes()
    with pytest.raises(BudgetLedgerError, match="already settled"):
        guard.settle(held, status="completed", limits=plan.limits)
    assert guard.spend_path.read_bytes() == before


def test_recovery_can_later_reconcile_with_verified_actual(tmp_path: Path) -> None:
    from mux.conformance.test_exact_spend import proposal as actual_proposal
    from mux.conformance.test_exact_spend import sign as sign_actual

    guard, _, original = latched(tmp_path)
    recovery = proposal(guard, original)
    sign(guard, recovery)
    guard.recover_latch(recovery)
    actual = actual_proposal(guard, original.run_id)
    sign_actual(guard, actual)
    row = guard.reconcile(actual)
    assert row.actual_usd == actual.actual_usd and row.held_usd == 0
    assert not row.admission_blocked and row.latch_recovery == recovery
    assert guard.report() == (row,)


def test_optimized_signature_and_floor_guards(tmp_path: Path) -> None:
    guard, _, row = latched(tmp_path)
    recovery = proposal(guard, row)
    script = """
import sys,json
from pathlib import Path
from decimal import Decimal
from mux.conformance.budget import BudgetGuard,LatchRecovery,BudgetRefused,BudgetLedgerError
p=Path(sys.argv[1]);g=BudgetGuard(p,Path(sys.argv[2]));r=LatchRecovery.model_validate_json(sys.argv[3])
before=g.spend_path.read_bytes()
try: g.recover_latch(r)
except BudgetRefused: pass
else: raise RuntimeError('unsigned recovery accepted')
x=json.loads(p.read_text());bad=r.model_copy(update={'held_usd':Decimal('.01')});x['approved_latch_recoveries']=[bad.digest];p.write_text(json.dumps(x))
try: g.recover_latch(bad)
except BudgetLedgerError: pass
else: raise RuntimeError('hold floor bypassed')
if g.spend_path.read_bytes()!=before: raise RuntimeError('rejection changed ledger')
x['approved_latch_recoveries']=[r.digest];p.write_text(json.dumps(x));row=g.recover_latch(r)
if (row.actual_usd is not None or row.admission_blocked
    or row.accounting_status!='estimated_unverified'):
 raise RuntimeError('recovery changed accounting')
if g.report()!=(row,): raise RuntimeError('recovery did not replay')
"""
    subprocess.run(
        [
            sys.executable,
            "-O",
            "-c",
            script,
            str(guard.config_path),
            str(guard.spend_path),
            recovery.model_dump_json(),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )


@pytest.mark.parametrize("damage", ["counters", "proof", "hold"])
def test_replay_refuses_changed_recovery_evidence(tmp_path: Path, damage: str) -> None:
    guard, _, original = latched(tmp_path)
    recovery = proposal(guard, original)
    sign(guard, recovery)
    row = guard.recover_latch(recovery)
    data = row.model_dump()
    if damage == "counters":
        data["tokens"] = TokenUsage(input_tokens=0, output_tokens=0)
    elif damage == "proof":
        data["overrun_evidence"] = None
    else:
        data.update(
            held_usd=Decimal("1"),
            cost_estimate_usd=Decimal("1"),
            latch_recovery=recovery.model_copy(update={"held_usd": Decimal("1")}),
        )
    changed = SpendReceipt.model_validate(data)
    guard.spend_path.write_text(
        guard.spend_path.read_text().replace(row.model_dump_json(), changed.model_dump_json())
    )
    with pytest.raises(BudgetLedgerError, match="cannot change known|below reservation"):
        guard.report()


def recovered(root: Path) -> tuple[BudgetGuard, LatchRecovery, SpendReceipt]:
    guard, _, original = latched(root)
    recovery = proposal(guard, original)
    sign(guard, recovery)
    return guard, recovery, guard.recover_latch(recovery)


@pytest.mark.parametrize("variant", ["actual_with_hold", "actual_full", "accounting_actual"])
def test_forged_actual_rejected_by_model(tmp_path: Path, variant: str) -> None:
    _, _, row = recovered(tmp_path)
    data = row.model_dump()
    if variant == "actual_with_hold":
        data.update(actual_usd=Decimal("1"), cost_estimate_usd=Decimal("3"))
    elif variant == "actual_full":
        data.update(actual_usd=Decimal("2"), held_usd=Decimal("0"))
    else:
        data.update(accounting_status="actual")
    with pytest.raises(ValidationError):
        SpendReceipt.model_validate(data)


def test_forged_actual_in_ledger_refused(tmp_path: Path) -> None:
    guard, _, row = recovered(tmp_path)
    line = row.model_dump_json()
    data = row.model_dump(mode="json")
    data.update(actual_usd="2", held_usd="0", accounting_status="actual")
    forged = json.dumps(data, separators=(",", ":"))
    guard.spend_path.write_text(guard.spend_path.read_text().replace(line, forged))
    with pytest.raises(BudgetLedgerError):
        guard.report()


def test_missing_approval_in_ledger_refused(tmp_path: Path) -> None:
    guard, _, row = recovered(tmp_path)
    data = row.model_dump(mode="json")
    data["latch_recovery"] = None
    guard.spend_path.write_text(
        guard.spend_path.read_text().replace(
            row.model_dump_json(), json.dumps(data, separators=(",", ":"))
        )
    )
    with pytest.raises(BudgetLedgerError):
        guard.report()


def test_status_latch_recovered_requires_hold_ge_reserved_model(tmp_path: Path) -> None:
    _, recovery, row = recovered(tmp_path)
    data = row.model_dump()
    low = row.reserved_usd - Decimal("0.01")
    data.update(
        held_usd=low,
        cost_estimate_usd=low,
        latch_recovery=recovery.model_copy(update={"held_usd": low}),
    )
    with pytest.raises(ValidationError):
        SpendReceipt.model_validate(data)


def test_unsigned_proposal_after_config_unpin_still_replays(tmp_path: Path) -> None:
    # Replay trusts appended rows protected by the checkpoint, like RECONCILE.
    guard, _, row = recovered(tmp_path)
    data = json.loads(guard.config_path.read_text())
    data["approved_latch_recoveries"] = []
    guard.config_path.write_text(json.dumps(data))
    assert guard.report() == (row,)
