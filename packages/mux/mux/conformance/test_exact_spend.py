"""Offline exact spend, runtime and append-only signed reconciliation regressions."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal
from unittest.mock import patch

import pytest
from pydantic import JsonValue, ValidationError

from mux.conformance.budget import (
    ActualSpend,
    BudgetGuard,
    BudgetLedgerError,
    BudgetRefused,
    ContainerAllowance,
    ContainerUsage,
    MeasuredRequest,
    ModelPrice,
    ProbePlan,
    Reconciliation,
    SpendReceipt,
    TokenUsage,
)
from mux.conformance.test_budget import receipts, setup_guard
from mux.conformance.usage_settlement import settle_anthropic_events
from mux.contracts.ids import ResourceRef

AT = datetime(2026, 10, 10, tzinfo=UTC)
SOURCE = "https://developers.openai.com/api/docs/pricing"


@pytest.fixture(autouse=True)
def guard_clock() -> Iterator[None]:
    with patch("mux.conformance.budget.datetime", wraps=datetime) as clock:
        clock.now.return_value = AT
        yield


def dated_guard(tmp_path: Path, provider: str = "openai") -> BudgetGuard:
    guard = setup_guard(tmp_path)
    data = json.loads(guard.config_path.read_text())
    model = "gpt-6-luna" if provider == "openai" else "claude-haiku-5-5"
    data["providers"][provider]["models"][model] = {
        "input": ".1",
        "cached_input": ".01",
        "cache_write_input": ".125",
        "output": ".5",
        "effective_from": "2026-10-10",
        "source": SOURCE,
    }
    data["providers"][provider]["session_prices"] = [
        {
            "memory_gb": 1,
            "unit_seconds": 1200,
            "minimum_seconds": 1200,
            "usd_per_unit": ".03",
            "effective_from": "2026-10-10",
            "source": SOURCE,
        }
    ]
    guard.config_path.write_text(json.dumps(data))
    return guard


def smoke_plan(provider: str = "openai", *, hosted: bool = True) -> ProbePlan:
    return ProbePlan(
        provider=provider,
        model="gpt-6-luna" if provider == "openai" else "claude-haiku-5-5",
        fixture_id="C06",
        container_allowance=ContainerAllowance() if hosted else None,
    )


def measured(*, seconds: int = 3, hosted: bool = True) -> ActualSpend:
    return ActualSpend(
        usage_complete=True,
        requests=(
            MeasuredRequest(
                id="request-1",
                observed_at=AT,
                pricing_basis="standard-global",
                tokens=TokenUsage(
                    input_tokens=1000,
                    output_tokens=100,
                    input_cached_tokens=200,
                    input_cache_write_tokens=0,
                ),
            ),
        ),
        containers=(
            ContainerUsage(id="container-1", memory_gb=1, seconds=Decimal(seconds), started_at=AT),
        )
        if hosted
        else (),
    )


def test_defaults_are_probe_sized_and_create_only_is_zero_tokens(tmp_path: Path) -> None:
    guard = dated_guard(tmp_path)
    plan = smoke_plan()
    assert (plan.limits.input_tokens, plan.limits.output_tokens) == (20_000, 2000)
    reservation = guard.reserve(plan)
    assert reservation.receipt.held_usd == Decimal(".0335")
    assert reservation.receipt.actual_usd is None
    assert reservation.receipt.accounting_status == "pending"
    create = ProbePlan.create_only(provider="openai", model="gpt-6-luna", fixture_id="C06")
    assert create.limits.input_tokens == create.limits.output_tokens == 0
    assert guard.reserve(create).receipt.held_usd == 0


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_actual_usage_and_runtime_release_hold_immediately(
    tmp_path: Path, status: Literal["completed", "failed", "cancelled"]
) -> None:
    guard = dated_guard(tmp_path)
    plan = smoke_plan()
    held = guard.reserve(plan)
    before = guard.spend_path.read_bytes()
    receipt = guard.settle(held, status=status, limits=plan.limits, actual=measured())
    assert receipt.actual_usd == Decimal(".030132")
    assert receipt.held_usd == 0 and receipt.accounting_status == "actual"
    assert receipt.token_usd == Decimal(".000132") and receipt.container_usd == Decimal(".03")
    assert guard.spend_path.read_bytes().startswith(before)
    assert len(receipts(guard)) == 2
    assert (
        Decimal(json.loads(guard.checkpoint_path.read_text())["provider_totals"]["openai"])
        == receipt.actual_usd
    )
    assert guard.report() == (receipt,)
    with pytest.raises(BudgetLedgerError):
        guard.settle(held, status=status, limits=plan.limits, actual=measured())


@pytest.mark.parametrize("seconds,expected", [(1, ".03"), (1200, ".03"), (1201, ".06")])
def test_published_container_quantum_is_separate_from_tokens(
    tmp_path: Path, seconds: int, expected: str
) -> None:
    guard = dated_guard(tmp_path)
    plan = smoke_plan()
    held = guard.reserve(plan)
    receipt = guard.settle(
        held, status="completed", limits=plan.limits, actual=measured(seconds=seconds)
    )
    assert receipt.container_usd == Decimal(expected) and receipt.held_usd == 0
    if seconds > 1200:
        assert receipt.status == "overrun"
        with pytest.raises(BudgetRefused):
            guard.reserve(
                ProbePlan.create_only(provider="openai", model="gpt-6-luna", fixture_id="C06")
            )


@pytest.mark.parametrize(
    "damage",
    [
        "partial_walk",
        "unknown_runtime",
        "missing_input",
        "missing_cached",
        "undated",
        "before_effective",
        "expired",
        "unknown_write_duration",
    ],
)
def test_incomplete_evidence_retains_a_separate_unverified_hold(
    tmp_path: Path, damage: str
) -> None:
    guard = dated_guard(tmp_path)
    data = json.loads(guard.config_path.read_text())
    price = data["providers"]["openai"]["models"]["gpt-6-luna"]
    if damage == "undated":
        price.pop("effective_from")
    elif damage == "expired":
        price["effective_until"] = "2026-10-11"
    elif damage == "unknown_write_duration":
        price["cache_write_5m_input"] = ".1"
    guard.config_path.write_text(json.dumps(data))
    plan = smoke_plan()
    held = guard.reserve(plan)
    evidence = measured().model_dump()
    if damage == "partial_walk":
        evidence["usage_complete"] = False
    elif damage == "unknown_runtime":
        evidence["containers"] = None
    elif damage in ("missing_input", "missing_cached", "unknown_write_duration"):
        field = {
            "missing_input": "input_tokens",
            "missing_cached": "input_cached_tokens",
            "unknown_write_duration": "input_cache_write_tokens",
        }[damage]
        evidence["requests"][0]["tokens"][field] = 1 if damage == "unknown_write_duration" else None
    elif damage in ("before_effective", "expired"):
        evidence["requests"][0]["observed_at"] = datetime(
            2026, 10, 9 if damage == "before_effective" else 11, tzinfo=UTC
        )
    receipt = guard.settle(
        held, status="completed", limits=plan.limits, actual=ActualSpend.model_validate(evidence)
    )
    assert receipt.actual_usd is None and receipt.held_usd == held.receipt.reserved_usd
    assert receipt.accounting_status == "estimated_unverified"


def test_price_snapshot_cannot_be_lowered_after_admission(tmp_path: Path) -> None:
    guard = dated_guard(tmp_path)
    plan = smoke_plan()
    held = guard.reserve(plan)
    data = json.loads(guard.config_path.read_text())
    data["providers"]["openai"]["models"]["gpt-6-luna"]["input"] = "0"
    guard.config_path.write_text(json.dumps(data))
    forged = held.model_copy(update={"price": held.price.model_copy(update={"input": Decimal(0)})})
    with pytest.raises(BudgetLedgerError, match="changed"):
        guard.settle(forged, status="completed", limits=plan.limits, actual=measured())
    receipt = guard.settle(held, status="completed", limits=plan.limits, actual=measured())
    assert receipt.actual_usd == Decimal(".030132")


def haiku_price() -> ModelPrice:
    return ModelPrice.model_validate(
        {
            "input": ".5",
            "cached_input": ".05",
            "cache_write_input": "1",
            "cache_write_5m_input": ".625",
            "output": "2.5",
            "effective_from": "2026-10-10",
            "source": "https://platform.claude.com/docs/en/about-claude/pricing",
            "short_prompt": {
                "through_input_tokens": 100_000,
                "input": ".1",
                "cached_input": ".01",
                "cache_write_input": ".2",
                "cache_write_5m_input": ".125",
                "output": ".5",
            },
        }
    )


@pytest.mark.parametrize("incoming,rate", [(100_000, ".1"), (100_001, ".5")])
def test_haiku_price_tier_is_chosen_per_request(incoming: int, rate: str) -> None:
    tokens = TokenUsage(
        input_tokens=incoming, output_tokens=0, input_cached_tokens=0, input_cache_write_tokens=0
    )
    assert haiku_price().actual(tokens, AT) == Decimal(incoming) * Decimal(rate) / 1_000_000


def test_anthropic_normalizer_settles_cache_duration_and_inclusive_input(tmp_path: Path) -> None:
    guard = dated_guard(tmp_path, "anthropic")
    config = json.loads(guard.config_path.read_text())
    config["providers"]["anthropic"]["models"]["claude-haiku-5-5"] = json.loads(
        haiku_price().model_dump_json()
    )
    guard.config_path.write_text(json.dumps(config))
    plan = smoke_plan("anthropic", hosted=False)
    held = guard.reserve(plan)
    events: list[dict[str, JsonValue]] = [
        {
            "id": "span-1",
            "type": "span.model_request_end",
            "model_usage": {
                "input_tokens": 800,
                "cache_read_input_tokens": 200,
                "cache_creation_input_tokens": 50,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 40,
                    "ephemeral_1h_input_tokens": 10,
                },
                "output_tokens": 100,
            },
        }
    ]
    session = ResourceRef(
        provider="anthropic", account_scope_id="qa", kind="session", id="session-1"
    )
    receipt = settle_anthropic_events(
        guard,
        held,
        events,
        session,
        observed_at=AT,
        containers=(),
        usage_complete=True,
        pricing_basis="standard-global",
    )
    assert receipt.tokens is not None and receipt.tokens.input_tokens == 1050
    assert receipt.actual_usd == Decimal(".000139") and receipt.held_usd == 0
    assert "native_meter" not in guard.spend_path.read_text()


def test_duplicate_and_inconsistent_cache_evidence_refuses() -> None:
    row = measured().requests[0]
    with pytest.raises(ValidationError, match="duplicate"):
        ActualSpend(requests=(row, row), containers=(), usage_complete=True)
    with pytest.raises(ValidationError, match="durations"):
        TokenUsage(
            input_tokens=100,
            input_cache_write_tokens=20,
            input_cache_write_5m_tokens=20,
            input_cache_write_1h_tokens=1,
        )


def sign(guard: BudgetGuard, proposal: Reconciliation) -> None:
    config = json.loads(guard.config_path.read_text())
    config["approved_reconciliations"] = [proposal.digest]
    guard.config_path.write_text(json.dumps(config))


def proposal(guard: BudgetGuard, run_id: str) -> Reconciliation:
    return guard.propose_reconciliation(
        run_id,
        evidence_sha256="e" * 64,
        basis="provider_usage",
        token_usd=Decimal(".001"),
        container_usd=Decimal(".03"),
        price_effective_from=date(2026, 10, 10),
    )


def test_reconcile_requires_exact_lead_approval_and_never_rewrites_rows(tmp_path: Path) -> None:
    guard = dated_guard(tmp_path)
    plan = smoke_plan()
    held = guard.reserve(plan)
    guard.settle(held, status="failed", limits=plan.limits)
    before = guard.spend_path.read_bytes()
    approval = proposal(guard, held.receipt.run_id)
    assert guard.spend_path.read_bytes() == before
    with pytest.raises(BudgetRefused, match="lead-signed"):
        guard.reconcile(approval)
    assert guard.spend_path.read_bytes() == before
    sign(guard, approval)
    receipt = guard.reconcile(approval)
    assert receipt.status == "reconciled" and receipt.reason == "reconciled"
    assert receipt.actual_usd == Decimal(".031") and receipt.held_usd == 0
    assert guard.spend_path.read_bytes().startswith(before) and len(receipts(guard)) == 3
    assert guard.report() == (receipt,)
    with pytest.raises(BudgetLedgerError):
        guard.reconcile(approval)


@pytest.mark.parametrize("damage", ["amount", "evidence", "ledger", "previous", "run"])
def test_reconcile_tampering_cannot_release_a_hold(tmp_path: Path, damage: str) -> None:
    guard = dated_guard(tmp_path)
    held = guard.reserve(smoke_plan())
    approval = proposal(guard, held.receipt.run_id)
    sign(guard, approval)
    before = guard.spend_path.read_bytes()
    changed = approval.model_dump()
    if damage == "amount":
        changed.update(actual_usd=Decimal(0), token_usd=Decimal(0), container_usd=Decimal(0))
    else:
        field = {
            "evidence": "evidence_sha256",
            "ledger": "ledger_id",
            "previous": "previous_sha256",
            "run": "run_id",
        }[damage]
        changed[field] = "0" * (64 if "sha256" in field else 32)
    mutant = Reconciliation.model_validate(changed)
    with pytest.raises(BudgetRefused):
        guard.reconcile(mutant)
    assert guard.spend_path.read_bytes() == before
    if damage in ("ledger", "previous", "run"):
        sign(guard, mutant)
        with pytest.raises(BudgetLedgerError):
            guard.reconcile(mutant)
        assert guard.spend_path.read_bytes() == before


def test_signed_pending_proposal_is_stale_after_terminal_receipt(tmp_path: Path) -> None:
    guard = dated_guard(tmp_path)
    plan = smoke_plan()
    held = guard.reserve(plan)
    approval = proposal(guard, held.receipt.run_id)
    sign(guard, approval)
    guard.settle(held, status="failed", limits=plan.limits)
    with pytest.raises(BudgetLedgerError, match="stale"):
        guard.reconcile(approval)


def test_legacy_hold_is_not_relabelled_as_actual() -> None:
    receipt = SpendReceipt.model_validate(
        {
            "version": 1,
            "run_id": "a" * 32,
            "provider": "openai",
            "model": "gpt-6-luna",
            "fixture_id": "C06",
            "timestamp": AT,
            "status": "completed",
            "tokens": {"input_tokens": 0, "output_tokens": 0},
            "reserved_usd": "1",
            "cost_estimate_usd": ".1",
            "reason": "settled",
        }
    )
    assert receipt.actual_usd is None and receipt.held_usd == Decimal(".1")
    assert receipt.accounting_status == "estimated_unverified"


def test_optimized_interpreter_cannot_bypass_lead_approval(tmp_path: Path) -> None:
    guard = dated_guard(tmp_path)
    held = guard.reserve(smoke_plan())
    approval = proposal(guard, held.receipt.run_id)
    script = """
from pathlib import Path
import sys
from mux.conformance.budget import BudgetGuard, BudgetRefused, Reconciliation
g = BudgetGuard(Path(sys.argv[1]), Path(sys.argv[2]))
p = Reconciliation.model_validate_json(sys.argv[3])
try:
    g.reconcile(p)
except BudgetRefused:
    pass
else:
    raise SystemExit('unsigned reconciliation released a hold under -O')
"""
    subprocess.run(
        [
            sys.executable,
            "-O",
            "-c",
            script,
            str(guard.config_path),
            str(guard.spend_path),
            approval.model_dump_json(),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )


def test_anthropic_running_milliseconds_are_prorated_not_container_hours(tmp_path: Path) -> None:
    guard = dated_guard(tmp_path, "anthropic")
    data = json.loads(guard.config_path.read_text())
    data["providers"]["anthropic"]["session_prices"] = [
        {
            "meter": "agent_session",
            "memory_gb": None,
            "unit_seconds": 3600,
            "billing": "proportional",
            "usd_per_unit": ".08",
            "effective_from": "2026-10-10",
            "source": "https://platform.claude.com/docs/en/about-claude/pricing",
        }
    ]
    guard.config_path.write_text(json.dumps(data))
    plan = smoke_plan("anthropic", hosted=False).model_copy(
        update={
            "container_allowance": ContainerAllowance(
                meter="agent_session", memory_gb=None, seconds_per_session=90
            ),
        }
    )
    held = guard.reserve(plan)
    row = measured(hosted=False).model_copy(
        update={
            "containers": (
                ContainerUsage(
                    id="session-1",
                    meter="agent_session",
                    memory_gb=None,
                    seconds=Decimal("4.005"),
                    started_at=AT,
                ),
            )
        }
    )
    receipt = guard.settle(held, status="completed", limits=plan.limits, actual=row)
    assert receipt.container_usd == Decimal(".000089")  # 4.005 * .08 / 3600
    assert receipt.actual_usd == Decimal(".000221") and receipt.held_usd == 0


@pytest.mark.parametrize("basis", [None, "fast-us"])
def test_unknown_or_foreign_processing_basis_does_not_claim_actual(
    tmp_path: Path, basis: str | None
) -> None:
    guard = dated_guard(tmp_path)
    plan = smoke_plan()
    held = guard.reserve(plan)
    evidence = measured().model_dump()
    evidence["requests"][0]["pricing_basis"] = basis
    receipt = guard.settle(
        held, status="completed", limits=plan.limits, actual=ActualSpend.model_validate(evidence)
    )
    assert receipt.actual_usd is None and receipt.held_usd == held.receipt.reserved_usd


def test_undeclared_tier_retains_hold_and_locks_overrun(tmp_path: Path) -> None:
    guard = dated_guard(tmp_path)
    data = json.loads(guard.config_path.read_text())
    data["providers"]["openai"]["models"]["gpt-6-luna"]["actual_input_limit"] = 20_000
    guard.config_path.write_text(json.dumps(data))
    plan = smoke_plan()
    held = guard.reserve(plan)
    evidence = measured().model_dump()
    evidence["requests"][0]["tokens"]["input_tokens"] = 100_000
    receipt = guard.settle(
        held, status="completed", limits=plan.limits, actual=ActualSpend.model_validate(evidence)
    )
    assert receipt.actual_usd is None and receipt.status == "overrun"
    assert receipt.held_usd is not None and receipt.held_usd >= held.receipt.reserved_usd
    with pytest.raises(BudgetRefused):
        guard.reserve(
            ProbePlan.create_only(provider="openai", model="gpt-6-luna", fixture_id="C06")
        )


async def test_probe_outcome_wires_actual_evidence_to_settlement(tmp_path: Path) -> None:
    from mux.conformance.live_probe import ProbeOutcome, run_probe
    from mux.conformance.recording import Recorder
    from mux.conformance.runner import Result

    guard = dated_guard(tmp_path)

    async def invoke(recorder: Recorder) -> ProbeOutcome:
        return ProbeOutcome(actual=measured(), result=Result("C06", "pass", ("offline",)))

    result = await run_probe(guard, smoke_plan(), tmp_path / "probe.json", invoke)
    assert result.receipt.actual_usd == Decimal(".030132") and result.receipt.held_usd == 0


@pytest.mark.parametrize(
    "damage", ["provider", "model", "grain", "basis", "overlap", "partial", "duplicate"]
)
def test_normalized_usage_walk_cannot_claim_foreign_or_overlapping_actual(damage: str) -> None:
    from mux.conformance.usage_settlement import actual_from_observations
    from mux.contracts.ids import ModelRef
    from mux.contracts.usage import UsageObservation

    observation = UsageObservation(
        id="request-1",
        revision=1,
        session=ResourceRef(
            provider="openai", account_scope_id="qa", kind="session", id="session-1"
        ),
        model=ModelRef(provider="openai", id="gpt-6-luna"),
        grain="model_request",
        basis="increment",
        input_tokens=1000,
        output_tokens=100,
        input_cached_tokens=0,
        input_cache_write_tokens=0,
        completeness="measured",
        observed_at=AT,
    )
    data = observation.model_dump(mode="json")
    if damage in ("provider", "model"):
        data["model"]["provider" if damage == "provider" else "id"] = (
            "anthropic" if damage == "provider" else "foreign"
        )
    elif damage == "grain":
        data["grain"] = "turn"
    elif damage == "basis":
        data["basis"] = "cumulative"
    elif damage == "overlap":
        data["covers"] = ["request-2"]
    elif damage == "partial":
        data["completeness"] = "partial"
    row = UsageObservation.model_validate(data)
    if damage == "partial":
        result = actual_from_observations(
            (row,),
            provider="openai",
            model="gpt-6-luna",
            containers=(),
            usage_complete=True,
            pricing_basis="standard-global",
        )
        assert result.usage_complete is False
    else:
        with pytest.raises((BudgetLedgerError, ValidationError)):
            actual_from_observations(
                (row, row) if damage == "duplicate" else (row,),
                provider="openai",
                model="gpt-6-luna",
                containers=(),
                usage_complete=True,
                pricing_basis="standard-global",
            )


def test_multiple_haiku_spans_use_request_tiers_not_aggregate_tier(tmp_path: Path) -> None:
    guard = dated_guard(tmp_path, "anthropic")
    config = json.loads(guard.config_path.read_text())
    config["providers"]["anthropic"]["models"]["claude-haiku-5-5"] = json.loads(
        haiku_price().model_dump_json()
    )
    guard.config_path.write_text(json.dumps(config))
    plan = smoke_plan("anthropic", hosted=False).model_copy(
        update={
            "limits": smoke_plan().limits.model_copy(
                update={"input_tokens": 140_000, "output_tokens": 0}
            )
        }
    )
    held = guard.reserve(plan)
    tokens = TokenUsage(
        input_tokens=70_000, output_tokens=0, input_cached_tokens=0, input_cache_write_tokens=0
    )
    evidence = ActualSpend(
        requests=tuple(
            MeasuredRequest(
                id=f"request-{i}", observed_at=AT, tokens=tokens, pricing_basis="standard-global"
            )
            for i in (1, 2)
        ),
        containers=(),
        usage_complete=True,
    )
    receipt = guard.settle(held, status="completed", limits=plan.limits, actual=evidence)
    assert receipt.token_usd == Decimal(".014") and receipt.held_usd == 0


def test_usage_metadata_credentials_never_reach_receipts(tmp_path: Path) -> None:
    guard = dated_guard(tmp_path)
    plan = smoke_plan()
    held = guard.reserve(plan)
    before = guard.spend_path.read_bytes()
    evidence = measured().model_dump()
    evidence["requests"][0]["pricing_basis"] = "sk-" + "f" * 32
    with pytest.raises(BudgetRefused, match="metadata"):
        guard.settle(
            held,
            status="completed",
            limits=plan.limits,
            actual=ActualSpend.model_validate(evidence),
        )
    assert guard.spend_path.read_bytes() == before
