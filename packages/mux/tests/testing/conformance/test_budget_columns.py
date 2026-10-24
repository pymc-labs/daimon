"""Column admission uses invented rates and temporary ledgers; no provider I/O."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Literal
from unittest.mock import patch

import pytest
from mux.testing.conformance.budget import (
    ActualSpend,
    BudgetConfig,
    BudgetGuard,
    BudgetLedgerError,
    BudgetRefused,
    ColumnCellPlan,
    ColumnHoldApproval,
    ColumnPlan,
    ContainerAllowance,
    ContainerUsage,
    MeasuredRequest,
    ModelPrice,
    ProbePlan,
    ProviderBudget,
    RecordedCellReceipt,
    SessionPrice,
    SpendReceipt,
    TokenLimits,
    TokenUsage,
)
from pydantic import ValidationError

AT = datetime(2026, 10, 10, 12, tzinfo=UTC)
MODELS = {"openai": "gpt-6-luna", "gemini": "gemini-3.8-flash", "anthropic": "claude-haiku-5-5"}
SOURCE = "https://developers.openai.com/api/docs/pricing"


@pytest.fixture(autouse=True)
def column_clock() -> Iterator[None]:
    with patch("mux.testing.conformance.budget.datetime", wraps=datetime) as clock:
        clock.now.return_value = AT
        yield


def guard_at(root: Path, *, cap: str = "10", opening: str = "0") -> BudgetGuard:
    root.mkdir(parents=True, exist_ok=True)
    price = ModelPrice(
        input=Decimal(1),
        cached_input=Decimal(".25"),
        cache_write_input=Decimal(2),
        output=Decimal(3),
        effective_from=date(2026, 10, 10),
        source=SOURCE,
    )
    runtime = SessionPrice(
        memory_gb=1,
        unit_seconds=60,
        minimum_seconds=60,
        usd_per_unit=Decimal(".01"),
        effective_from=date(2026, 10, 10),
        source=SOURCE,
    )
    config = BudgetConfig(
        ledger_path=root / "spend.md",
        total_cap_usd=Decimal(150),
        providers={
            p: ProviderBudget(
                cap_usd=Decimal(cap),
                opening_spend_usd=Decimal(opening),
                models={m: price},
                session_prices=(runtime,),
            )
            for p, m in MODELS.items()
        },
    )
    config_path = root / "config.json"
    config_path.write_text(config.model_dump_json())
    BudgetGuard.initialize(config_path, config.ledger_path)
    return BudgetGuard(config_path, config.ledger_path)


def cell(name: str, provider: str = "openai", *, hosted: bool = True) -> ColumnCellPlan:
    return ColumnCellPlan(
        recorded=RecordedCellReceipt(
            cell_id=name,
            provider=provider,
            model=MODELS[provider],
            receipt_sha256="a" * 64,
            full_cell_executable=True,
            request_count=2,
            turn_count=1,
            tokens=TokenUsage(
                input_tokens=1000,
                output_tokens=100,
                input_cached_tokens=200,
                input_cache_write_tokens=100,
            ),
            session_count=1 if hosted else 0,
            max_session_seconds=60 if hosted else None,
            tool_policy="no_separate_tool_charges",
        ),
        per_request_limits=TokenLimits(input_tokens=10_000, output_tokens=5000),
        request_limit=2,
        container_allowance=ContainerAllowance(sessions=1, seconds_per_session=120)
        if hosted
        else None,
    )


def column(provider: str = "openai", *, count: int = 3, name: str = "qa-column") -> ColumnPlan:
    return ColumnPlan(
        column_id=name,
        provider=provider,
        model=MODELS[provider],
        cells=tuple(cell(f"A{i:02}", provider) for i in range(count)),
    )


def sign(guard: BudgetGuard, *approvals: ColumnHoldApproval) -> None:
    config = json.loads(guard.config_path.read_text())
    config["approved_column_holds"] = [a.digest for a in approvals]
    guard.config_path.write_text(json.dumps(config))


def evidence(
    name: str,
    *,
    complete: bool = True,
    hosted: bool = True,
    incoming: int = 1000,
    observed: datetime = AT,
) -> ActualSpend:
    return ActualSpend(
        usage_complete=complete,
        requests=(
            MeasuredRequest(
                id=f"request-{name}",
                observed_at=observed,
                pricing_basis="standard-global",
                tokens=TokenUsage(
                    input_tokens=incoming,
                    output_tokens=100,
                    input_cached_tokens=200,
                    input_cache_write_tokens=100,
                ),
            ),
        ),
        containers=(
            ContainerUsage(id=f"container-{name}", memory_gb=1, seconds=Decimal(60), started_at=AT),
        )
        if hosted
        else (),
    )


def admit(guard: BudgetGuard, plan: ColumnPlan | None = None) -> SpendReceipt:
    approval = guard.propose_column_hold(plan or column())
    sign(guard, approval)
    return guard.reserve_column(approval)


def disk(guard: BudgetGuard) -> tuple[bytes, bytes, bytes]:
    return (
        guard.spend_path.read_bytes(),
        guard.checkpoint_path.read_bytes(),
        guard.lock_path.read_bytes(),
    )


@pytest.mark.parametrize("provider", MODELS)
def test_recorded_projection_is_read_only_and_bound_includes_all_requests_and_runtime(
    tmp_path: Path,
    provider: str,
) -> None:
    guard = guard_at(tmp_path)
    before = disk(guard)
    approval = guard.propose_column_hold(column(provider))
    assert disk(guard) == before
    assert approval.bound_usd == Decimal(".27")
    for quote in approval.cells:
        assert quote.limits == TokenLimits(input_tokens=20_000, output_tokens=10_000)
        assert quote.estimated_usd == Decimal(".01125")
        assert quote.upper_bound_usd == Decimal(".09")
    sign(guard, approval)
    reserved = guard.reserve_column(approval)
    assert reserved.actual_usd is None and reserved.held_usd == Decimal(".27")
    assert len(guard.report()) == 1
    assert guard.spend_path.read_bytes().startswith(before[0])
    assert json.loads(guard.checkpoint_path.read_text())["sequence"] == 1
    assert BudgetGuard(guard.config_path, guard.spend_path).report() == (reserved,)


@pytest.mark.parametrize(
    "damage", ["unsigned", "changed", "foreign", "expired", "future", "repriced"]
)
def test_unsigned_changed_foreign_expired_or_repriced_column_cannot_write(
    tmp_path: Path,
    damage: str,
) -> None:
    guard = guard_at(tmp_path)
    approval = guard.propose_column_hold(column())
    if damage != "unsigned":
        sign(guard, approval)
    if damage == "changed":
        approval = approval.model_copy(update={"column_id": "changed"})
    elif damage == "foreign":
        approval = approval.model_copy(update={"ledger_id": "0" * 32})
        sign(guard, approval)
    elif damage in ("expired", "future"):
        shift = timedelta(hours=-2 if damage == "expired" else 2)
        approval = approval.model_copy(
            update={"created_at": AT + shift, "expires_at": AT + shift + timedelta(hours=1)}
        )
        sign(guard, approval)
    elif damage == "repriced":
        config = json.loads(guard.config_path.read_text())
        config["providers"]["openai"]["models"][MODELS["openai"]]["input"] = "1.1"
        guard.config_path.write_text(json.dumps(config))
    before = disk(guard)
    with pytest.raises((BudgetRefused, BudgetLedgerError)):
        guard.reserve_column(approval)
    assert disk(guard) == before and not guard.report()


@pytest.mark.parametrize("opening,admitted", [(".02", True), (".020000000000000001", False)])
def test_entire_batch_bound_is_compared_with_remaining_provider_stop_without_partial_hold(
    tmp_path: Path,
    opening: str,
    admitted: bool,
) -> None:
    guard = guard_at(tmp_path, cap=".25", opening=opening)
    approval = guard.propose_column_hold(column(count=2))
    sign(guard, approval)
    before = disk(guard)
    if admitted:
        assert guard.reserve_column(approval).held_usd == Decimal(".18")
    else:
        with pytest.raises(BudgetRefused, match="remaining"):
            guard.reserve_column(approval)
        assert disk(guard) == before and not guard.report()


def test_total_stop_and_existing_single_run_holds_are_included(tmp_path: Path) -> None:
    guard = guard_at(tmp_path, cap="50")
    config = json.loads(guard.config_path.read_text())
    config["total_cap_usd"] = "150"
    config["providers"]["openai"]["cap_usd"] = "100"
    config["providers"]["gemini"]["cap_usd"] = "25"
    config["providers"]["anthropic"]["cap_usd"] = "25"
    config["providers"]["anthropic"]["opening_spend_usd"] = "40"
    config["providers"]["gemini"]["opening_spend_usd"] = "40"
    config["providers"]["openai"]["opening_spend_usd"] = "39.9"
    guard.config_path.write_text(json.dumps(config))
    approval = guard.propose_column_hold(column(count=2))
    sign(guard, approval)
    before = disk(guard)
    with pytest.raises(BudgetRefused):
        guard.reserve_column(approval)
    assert disk(guard) == before
    config["providers"]["openai"]["opening_spend_usd"] = "0"
    guard.config_path.write_text(json.dumps(config))
    sign(guard, approval)
    single = guard.reserve(
        ProbePlan(
            provider="openai",
            model=MODELS["openai"],
            fixture_id="F1",
            limits=TokenLimits(input_tokens=19_900_000, output_tokens=0),
        )
    )
    assert single.receipt.held_usd == Decimal("39.8")
    guard.reserve_column(approval)
    more = guard.propose_column_hold(column(count=1, name="another-column"))
    sign(guard, more)
    before = disk(guard)
    with pytest.raises(BudgetRefused):
        guard.reserve_column(more)
    assert disk(guard) == before


def test_one_hold_settles_disjoint_cells_monotonically_and_releases_only_unused_remainder(
    tmp_path: Path,
) -> None:
    guard = guard_at(tmp_path)
    held = admit(guard)
    guard.start_column_cell(held.run_id, "A00")
    first = guard.settle_column_cell(held.run_id, "A00", actual=evidence("A00"))
    assert first.actual_usd == Decimal(".01125") and first.held_usd == Decimal(".18")
    guard.start_column_cell(held.run_id, "A01")
    second = guard.settle_column_cell(held.run_id, "A01", actual=evidence("A01"))
    assert second.actual_usd == Decimal(".02250") and second.held_usd == Decimal(".09")
    final = guard.finish_column(held.run_id, status="completed")
    assert final.actual_usd == Decimal(".02250") and final.held_usd == 0
    assert final.token_usd is not None and final.container_usd is not None
    assert final.token_usd + final.container_usd == final.actual_usd
    assert final.accounting_status == "actual" and final.column is not None
    assert final.column.cells[-1].state == "not_started"
    assert guard.report() == (final,)
    assert (
        Decimal(json.loads(guard.checkpoint_path.read_text())["provider_totals"]["openai"])
        == final.actual_usd
    )


@pytest.mark.parametrize(
    "unknown", ["missing", "partial", "unknown_runtime", "missing_output", "foreign_basis"]
)
def test_terminal_keeps_unknown_cell_hold_until_complete_covering_evidence(
    tmp_path: Path,
    unknown: str,
) -> None:
    guard = guard_at(tmp_path)
    held = admit(guard)
    guard.start_column_cell(held.run_id, "A00")
    proof = evidence("A00")
    if unknown == "missing":
        proof = None
    elif unknown == "partial":
        proof = proof.model_copy(update={"usage_complete": False})
    elif unknown == "unknown_runtime":
        proof = proof.model_copy(update={"containers": None})
    elif unknown == "missing_output":
        request = proof.requests[0]
        proof = proof.model_copy(
            update={
                "requests": (
                    request.model_copy(
                        update={"tokens": request.tokens.model_copy(update={"output_tokens": None})}
                    ),
                )
            }
        )
    else:
        proof = proof.model_copy(
            update={
                "requests": (proof.requests[0].model_copy(update={"pricing_basis": "unknown"}),)
            }
        )
    row = guard.settle_column_cell(held.run_id, "A00", actual=proof)
    assert row.actual_usd is None and row.held_usd == Decimal(".27")
    terminal = guard.finish_column(held.run_id, status="failed")
    assert terminal.actual_usd == 0 and terminal.held_usd == Decimal(".09")
    assert terminal.accounting_status == "estimated_unverified"
    exact = guard.settle_column_cell(held.run_id, "A00", actual=evidence("A00"))
    assert exact.actual_usd == Decimal(".01125") and exact.held_usd == 0
    assert exact.column is not None and exact.column.terminal_status == "failed"
    assert exact.accounting_status == "actual"


def test_finishing_with_inflight_cell_retains_its_full_bound_and_fences_pending_claims(
    tmp_path: Path,
) -> None:
    guard = guard_at(tmp_path)
    held = admit(guard)
    guard.start_column_cell(held.run_id, "A00")
    terminal = guard.finish_column(held.run_id, status="cancelled")
    assert terminal.held_usd == Decimal(".09")
    with pytest.raises(BudgetRefused):
        guard.start_column_cell(held.run_id, "A01")
    with pytest.raises(BudgetLedgerError):
        guard.settle_column_cell(held.run_id, "A01", actual=evidence("A01"))
    exact = guard.settle_column_cell(held.run_id, "A00", actual=evidence("A00"))
    assert exact.held_usd == 0


@pytest.mark.parametrize("overlap", ["request", "container"])
def test_overlapping_cell_evidence_refuses_before_append(tmp_path: Path, overlap: str) -> None:
    guard = guard_at(tmp_path)
    held = admit(guard)
    guard.start_column_cell(held.run_id, "A00")
    guard.settle_column_cell(held.run_id, "A00", actual=evidence("A00"))
    guard.start_column_cell(held.run_id, "A01")
    second = evidence("A01")
    first = evidence("A00")
    second = second.model_copy(
        update={"requests": first.requests}
        if overlap == "request"
        else {"containers": first.containers}
    )
    before = disk(guard)
    with pytest.raises(BudgetLedgerError, match="overlapping"):
        guard.settle_column_cell(held.run_id, "A01", actual=second)
    assert disk(guard) == before


@pytest.mark.parametrize("damage", ["drop", "stale", "lower_tokens"])
def test_partial_proof_cannot_be_erased_by_a_forged_complete_snapshot(
    tmp_path: Path, damage: str
) -> None:
    guard = guard_at(tmp_path)
    held = admit(guard)
    guard.start_column_cell(held.run_id, "A00")
    guard.settle_column_cell(held.run_id, "A00", actual=evidence("A00", complete=False))
    proposed = evidence("A00")
    if damage == "drop":
        proposed = ActualSpend(usage_complete=True, containers=())
    elif damage == "stale":
        proposed = evidence("A00", observed=AT - timedelta(seconds=1))
    else:
        proposed = evidence("A00", incoming=900)
    before = disk(guard)
    with pytest.raises(BudgetLedgerError, match="drop charges|stale"):
        guard.settle_column_cell(held.run_id, "A00", actual=proposed)
    assert disk(guard) == before


def test_actual_overrun_latches_all_new_claims_and_other_runs_but_preserves_known_actual(
    tmp_path: Path,
) -> None:
    guard = guard_at(tmp_path)
    held = admit(guard)
    guard.start_column_cell(held.run_id, "A00")
    row = guard.settle_column_cell(held.run_id, "A00", actual=evidence("A00", incoming=250_000))
    assert row.admission_blocked and row.actual_usd == Decimal(".26025")
    assert row.held_usd == Decimal(".18")
    with pytest.raises(BudgetRefused):
        guard.start_column_cell(held.run_id, "A01")
    with pytest.raises(BudgetRefused):
        guard.reserve(
            ProbePlan.create_only(provider="openai", model=MODELS["openai"], fixture_id="F1")
        )
    final = guard.finish_column(held.run_id, status="failed")
    assert final.status == "overrun" and final.admission_blocked
    assert final.held_usd == 0 and final.actual_usd == row.actual_usd


def test_approval_and_cell_settlement_replays_are_idempotent_but_claims_are_single_use(
    tmp_path: Path,
) -> None:
    guard = guard_at(tmp_path)
    approval = guard.propose_column_hold(column())
    sign(guard, approval)
    held = guard.reserve_column(approval)
    before = disk(guard)
    assert guard.reserve_column(approval) == held and disk(guard) == before
    guard.start_column_cell(held.run_id, "A00")
    with pytest.raises(BudgetLedgerError, match="claimed"):
        guard.start_column_cell(held.run_id, "A00")
    exact = guard.settle_column_cell(held.run_id, "A00", actual=evidence("A00"))
    before = disk(guard)
    assert guard.settle_column_cell(held.run_id, "A00", actual=evidence("A00")) == exact
    assert disk(guard) == before
    with pytest.raises(BudgetLedgerError):
        guard.settle_column_cell(held.run_id, "A00", actual=evidence("A00", incoming=1100))
    final = guard.finish_column(held.run_id, status="completed")
    before = disk(guard)
    assert guard.finish_column(held.run_id, status="completed") == final and disk(guard) == before
    with pytest.raises(BudgetRefused):
        guard.reserve_column(approval)


def test_concurrent_columns_cannot_both_take_the_same_remaining_line(tmp_path: Path) -> None:
    guard = guard_at(tmp_path, cap=".25")
    first = guard.propose_column_hold(column(count=2, name="one"))
    second = guard.propose_column_hold(column(count=2, name="two"))
    sign(guard, first, second)

    def attempt(approval: ColumnHoldApproval) -> bool:
        try:
            BudgetGuard(guard.config_path, guard.spend_path).reserve_column(approval)
            return True
        except BudgetRefused:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (first, second)))
    assert sorted(results) == [False, True]
    assert len(guard.report()) == 1
    assert Decimal(
        json.loads(guard.checkpoint_path.read_text())["provider_totals"]["openai"]
    ) == Decimal(".18")


def test_single_probe_bytes_have_no_column_field_and_ordinary_settlement_cannot_touch_column(
    tmp_path: Path,
) -> None:
    guard = guard_at(tmp_path)
    single = guard.reserve(
        ProbePlan.create_only(provider="openai", model=MODELS["openai"], fixture_id="F1")
    )
    assert '"column"' not in single.receipt.model_dump_json()
    assert SpendReceipt.model_validate_json(single.receipt.model_dump_json()) == single.receipt
    held = admit(guard)
    with pytest.raises(BudgetLedgerError, match="per-cell"):
        guard.propose_reconciliation(
            held.run_id,
            evidence_sha256="b" * 64,
            basis="billed_export",
            token_usd=Decimal(0),
            container_usd=Decimal(0),
        )


@pytest.mark.parametrize(
    "damage",
    [
        "not_executable",
        "unknown_tools",
        "requests",
        "input",
        "output",
        "sessions",
        "runtime",
        "no_allowance",
        "undated",
        "expired_price",
        "missing_price",
        "missing_runtime",
        "unallowlisted",
    ],
)
def test_unknown_or_understated_cell_ceiling_refuses_before_any_hold(
    tmp_path: Path, damage: str
) -> None:
    guard = guard_at(tmp_path)
    candidate = cell("A00")
    if damage in ("not_executable", "unknown_tools"):
        candidate = candidate.model_copy(
            update={
                "recorded": candidate.recorded.model_copy(
                    update={"full_cell_executable": False}
                    if damage == "not_executable"
                    else {"tool_policy": "unknown"}
                )
            }
        )
    elif damage == "requests":
        candidate = candidate.model_copy(update={"request_limit": 1})
    elif damage in ("input", "output"):
        candidate = candidate.model_copy(
            update={
                "per_request_limits": TokenLimits(
                    input_tokens=400 if damage == "input" else 10_000,
                    output_tokens=40 if damage == "output" else 5000,
                )
            }
        )
    elif damage in ("sessions", "runtime"):
        candidate = candidate.model_copy(
            update={
                "container_allowance": ContainerAllowance(
                    sessions=0 if damage == "sessions" else 1,
                    seconds_per_session=30 if damage == "runtime" else 120,
                )
            }
        )
    elif damage == "no_allowance":
        candidate = candidate.model_copy(update={"container_allowance": None})
    else:
        config = json.loads(guard.config_path.read_text())
        prices = config["providers"]["openai"]["models"]
        price = prices[MODELS["openai"]]
        if damage == "undated":
            price["effective_from"] = None
        elif damage == "expired_price":
            price["effective_from"] = "2026-10-08"
            price["effective_until"] = "2026-10-09"
        elif damage == "missing_price":
            prices.clear()
        elif damage == "missing_runtime":
            config["providers"]["openai"]["session_prices"] = []
        elif damage == "unallowlisted":
            candidate = candidate.model_copy(
                update={
                    "recorded": candidate.recorded.model_copy(update={"model": "gpt-unreviewed"})
                }
            )
        guard.config_path.write_text(json.dumps(config))
    plan = ColumnPlan(
        column_id="column", provider="openai", model=candidate.recorded.model, cells=(candidate,)
    )
    before = disk(guard)
    with pytest.raises(BudgetRefused):
        guard.propose_column_hold(plan)
    assert disk(guard) == before and not guard.report()


def test_missing_offline_tokens_uses_full_bound_and_gemini_without_tools_needs_no_infra(
    tmp_path: Path,
) -> None:
    guard = guard_at(tmp_path)
    candidate = cell("A00", "gemini", hosted=False)
    candidate = candidate.model_copy(
        update={"recorded": candidate.recorded.model_copy(update={"tokens": TokenUsage()})}
    )
    quote = guard.propose_column_hold(
        ColumnPlan(
            column_id="gemini", provider="gemini", model=MODELS["gemini"], cells=(candidate,)
        )
    )
    assert quote.cells[0].upper_bound_usd == quote.cells[0].estimated_usd == Decimal(".07")


@pytest.mark.parametrize("bad", ["empty", "duplicate", "mixed_provider", "too_many"])
def test_column_shape_requires_unique_one_provider_cells(tmp_path: Path, bad: str) -> None:
    guard = guard_at(tmp_path)
    if bad == "empty":
        with pytest.raises(ValidationError):
            ColumnPlan(column_id="empty", provider="openai", model=MODELS["openai"], cells=())
        return
    if bad == "too_many":
        with pytest.raises(ValidationError):
            column(count=54)
        return
    cells = (cell("A00"), cell("A00") if bad == "duplicate" else cell("A01", "gemini"))
    before = disk(guard)
    with pytest.raises(ValidationError):
        guard.propose_column_hold(
            ColumnPlan(column_id="mixed", provider="openai", model=MODELS["openai"], cells=cells)
        )
    assert disk(guard) == before


@pytest.mark.parametrize(
    "boundary", ["not_claimed", "unknown_cell", "foreign_run", "expiry", "lowered_stop"]
)
def test_cell_claims_require_a_live_signed_hold_with_remaining_capacity(
    tmp_path: Path, boundary: str
) -> None:
    guard = guard_at(tmp_path)
    held = admit(guard)
    before = disk(guard)
    if boundary == "expiry":
        with patch("mux.testing.conformance.budget.datetime", wraps=datetime) as clock:
            clock.now.return_value = AT + timedelta(hours=2)
            with pytest.raises(BudgetRefused):
                guard.start_column_cell(held.run_id, "A00")
    elif boundary == "lowered_stop":
        config = json.loads(guard.config_path.read_text())
        config["providers"]["openai"]["cap_usd"] = ".1"
        guard.config_path.write_text(json.dumps(config))
        with pytest.raises(BudgetRefused):
            guard.start_column_cell(held.run_id, "A00")
    else:
        with pytest.raises(BudgetLedgerError):
            if boundary == "not_claimed":
                guard.settle_column_cell(held.run_id, "A00", actual=evidence("A00"))
            else:
                guard.start_column_cell(
                    "0" * 32 if boundary == "foreign_run" else held.run_id,
                    "foreign" if boundary == "unknown_cell" else "A00",
                )
    assert disk(guard) == before


@pytest.mark.parametrize(
    "exceeded",
    ["request_count", "infra_lifetime", "infra_count", "token_count", "per_request", "cost"],
)
def test_every_cell_bound_overrun_latches_admission_even_when_actual_is_unknown(
    tmp_path: Path, exceeded: str
) -> None:
    guard = guard_at(tmp_path)
    held = admit(guard)
    guard.start_column_cell(held.run_id, "A00")
    data = evidence("A00", complete=False)
    assert data.containers is not None
    if exceeded == "request_count":
        request = data.requests[0]
        data = data.model_copy(
            update={
                "requests": tuple(request.model_copy(update={"id": f"extra-{i}"}) for i in range(3))
            }
        )
    elif exceeded == "infra_lifetime":
        data = data.model_copy(
            update={
                "containers": (data.containers[0].model_copy(update={"seconds": Decimal(121)}),)
            }
        )
    elif exceeded == "infra_count":
        data = data.model_copy(
            update={
                "containers": tuple(
                    data.containers[0].model_copy(update={"id": f"infra-{i}"}) for i in range(2)
                )
            }
        )
    else:
        data = evidence(
            "A00",
            complete=False,
            incoming=250_000
            if exceeded == "cost"
            else 11_000
            if exceeded == "per_request"
            else 21_000,
        )
    row = guard.settle_column_cell(held.run_id, "A00", actual=data)
    assert row.held_usd is not None and held.held_usd is not None
    assert row.actual_usd is None and row.held_usd >= held.held_usd and row.admission_blocked
    with pytest.raises(BudgetRefused):
        guard.start_column_cell(held.run_id, "A01")
    final = guard.finish_column(held.run_id, status="cancelled")
    assert final.held_usd is not None
    assert (
        final.status == "overrun" and final.admission_blocked and final.held_usd >= Decimal(".09")
    )


@pytest.mark.parametrize(
    "damage", ["runtime_shrink", "cache_reprice", "lost_completeness", "price_basis"]
)
def test_known_proof_floors_and_completeness_cannot_regress(tmp_path: Path, damage: str) -> None:
    guard = guard_at(tmp_path)
    held = admit(guard)
    guard.start_column_cell(held.run_id, "A00")
    old = evidence("A00", complete=damage == "lost_completeness")
    if damage == "lost_completeness":
        old = old.model_copy(update={"containers": None})
    guard.settle_column_cell(held.run_id, "A00", actual=old)
    new = evidence("A00")
    assert new.containers is not None
    if damage == "runtime_shrink":
        new = new.model_copy(
            update={"containers": (new.containers[0].model_copy(update={"seconds": Decimal(30)}),)}
        )
    elif damage == "cache_reprice":
        request = new.requests[0]
        new = new.model_copy(
            update={
                "requests": (
                    request.model_copy(
                        update={
                            "tokens": request.tokens.model_copy(update={"input_cached_tokens": 300})
                        }
                    ),
                )
            }
        )
    elif damage == "lost_completeness":
        new = new.model_copy(update={"usage_complete": False})
    else:
        new = new.model_copy(
            update={"requests": (new.requests[0].model_copy(update={"pricing_basis": "unknown"}),)}
        )
    before = disk(guard)
    with pytest.raises(BudgetLedgerError):
        guard.settle_column_cell(held.run_id, "A00", actual=new)
    assert disk(guard) == before


def test_full_53_cell_column_is_one_hold_and_cancelled_unused_cells_release_it(
    tmp_path: Path,
) -> None:
    guard = guard_at(tmp_path)
    held = admit(guard, column(count=53))
    assert held.held_usd == Decimal("4.77") and len(guard.report()) == 1
    final = guard.finish_column(held.run_id, status="cancelled")
    assert final.actual_usd == 0 and final.held_usd == 0 and final.accounting_status == "actual"
    assert final.column is not None and len(final.column.cells) == 53


def test_partial_components_never_double_count_a_separate_complete_cell(tmp_path: Path) -> None:
    guard = guard_at(tmp_path)
    held = admit(guard)
    guard.start_column_cell(held.run_id, "A00")
    guard.settle_column_cell(held.run_id, "A00", actual=evidence("A00"))
    guard.start_column_cell(held.run_id, "A01")
    partial = evidence("A01").model_copy(update={"containers": None})
    guard.settle_column_cell(held.run_id, "A01", actual=partial)
    final = guard.finish_column(held.run_id, status="completed")
    assert final.actual_usd == Decimal(".01125") and final.held_usd == Decimal(".09")
    assert final.token_usd == Decimal(".00125") and final.container_usd == Decimal(".01")
    assert final.column is not None
    assert final.column.cells[1].token_usd == Decimal(".00125")
    assert final.column.cells[1].actual_usd is None


def test_claim_stop_comparison_is_exact_under_low_decimal_precision(tmp_path: Path) -> None:
    guard = guard_at(tmp_path, cap=".25", opening=".11")
    held = admit(guard, column(count=1))
    config = json.loads(guard.config_path.read_text())
    config["providers"]["openai"]["opening_spend_usd"] = ".110000000000000001"
    guard.config_path.write_text(json.dumps(config))
    before = disk(guard)
    with localcontext() as context:
        context.prec = 6
        with pytest.raises(BudgetRefused):
            guard.start_column_cell(held.run_id, "A00")
    assert disk(guard) == before


@pytest.mark.parametrize("provider", MODELS)
def test_fractional_settlement_is_exact_under_low_precision_and_reopens(
    tmp_path: Path, provider: str
) -> None:
    guard = guard_at(tmp_path)
    config = json.loads(guard.config_path.read_text())
    config["providers"][provider]["models"][MODELS[provider]]["input"] = "0.33333333"
    guard.config_path.write_text(json.dumps(config))
    held = admit(guard, column(provider, count=1))
    guard.start_column_cell(held.run_id, "A00")
    with localcontext() as context:
        context.prec = 6
        settled = guard.settle_column_cell(
            held.run_id, "A00", actual=evidence("A00", incoming=1234)
        )
        assert settled.actual_usd == Decimal(".01086133333022")
        assert settled.token_usd == Decimal(".00086133333022")
        assert settled.container_usd == Decimal(".01")
        assert settled.held_usd == 0
        final = guard.finish_column(held.run_id, status="completed")
    assert final.actual_usd == Decimal(".01086133333022")
    assert SpendReceipt.model_validate_json(final.model_dump_json()) == final
    assert BudgetGuard(guard.config_path, guard.spend_path).report() == (final,)
    checkpoint = json.loads(guard.checkpoint_path.read_text())
    assert Decimal(checkpoint["provider_totals"][provider]) == Decimal(".01086133333022")


def test_claim_persists_increased_opening_spend_even_if_config_is_later_lowered(
    tmp_path: Path,
) -> None:
    guard = guard_at(tmp_path)
    held = admit(guard)
    config = json.loads(guard.config_path.read_text())
    config["providers"]["openai"]["opening_spend_usd"] = ".01"
    guard.config_path.write_text(json.dumps(config))
    guard.start_column_cell(held.run_id, "A00")
    config["providers"]["openai"]["opening_spend_usd"] = "0"
    guard.config_path.write_text(json.dumps(config))
    checkpoint = json.loads(guard.checkpoint_path.read_text())
    assert Decimal(checkpoint["opening_spend"]["openai"]) == Decimal(".01")
    assert Decimal(checkpoint["provider_totals"]["openai"]) == Decimal(".28")
    guard.report()


def test_column_id_cannot_be_reused_with_another_signed_approval(tmp_path: Path) -> None:
    guard = guard_at(tmp_path)
    first = guard.propose_column_hold(column())
    second = guard.propose_column_hold(column())
    sign(guard, first, second)
    guard.reserve_column(first)
    before = disk(guard)
    with pytest.raises(BudgetRefused, match="already admitted"):
        guard.reserve_column(second)
    assert disk(guard) == before


@pytest.mark.parametrize(
    ("version", "digest"),
    [
        (1, "04759030f41b95f40adfe2ec9f92af66f027709515af9f6934f174713ecf9887"),
        (2, "c30a134aaa960d5ef167d702c07b5145d6fb4d73bc1b36a3bc8e0488e032ceff"),
    ],
)
def test_ordinary_receipt_digests_match_unchanged_integration(
    version: Literal[1, 2], digest: str
) -> None:
    # Byte-checked against 4f8bae6b5d420f0c50d01b42fc2dc5de228fe2fe before pinning.
    row = SpendReceipt(
        version=version,
        run_id="0" * 32,
        provider="anthropic",
        model=MODELS["anthropic"],
        fixture_id="F1",
        timestamp=AT,
        status="reserved",
        reason="admitted",
        reserved_usd=Decimal(".01"),
        cost_estimate_usd=Decimal(".01"),
    )
    assert BudgetGuard.receipt_digest(row) == digest


def test_optimized_python_keeps_admission_and_accounting_checks(tmp_path: Path) -> None:
    # Explicit RuntimeError checks survive -O; this exercises the public API in
    # a clean child process without assertions, provider settings or network I/O.
    script = """
import json
import sys
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import patch
from mux.testing.conformance.budget import BudgetLedgerError, BudgetRefused
from packages.mux.tests.testing.conformance.test_budget_columns import AT, admit, column, evidence, guard_at, sign

with patch('mux.testing.conformance.budget.datetime', wraps=datetime) as clock:
    clock.now.return_value = AT
    guard = guard_at(Path(sys.argv[1]), cap='.25')
    held = admit(guard, column(count=2))
    too_much = guard.propose_column_hold(column(count=1, name='extra'))
    sign(guard, too_much)
    try:
        guard.reserve_column(too_much)
    except BudgetRefused:
        pass
    else:
        raise RuntimeError('remaining line admission lost under -O')
    guard.start_column_cell(held.run_id, 'A00')
    guard.settle_column_cell(held.run_id, 'A00', actual=evidence('A00', complete=False))
    guard.start_column_cell(held.run_id, 'A01')
    try:
        guard.settle_column_cell(held.run_id, 'A01', actual=evidence('A00'))
    except BudgetLedgerError:
        pass
    else:
        raise RuntimeError('overlapping charge identity accepted under -O')
    guard.settle_column_cell(held.run_id, 'A01', actual=evidence('A01'))
    final = guard.finish_column(held.run_id, status='completed')
    if final.held_usd != Decimal('.09') or final.actual_usd != Decimal('.01125'):
        raise RuntimeError('unknown hold or known actual lost under -O')
    final = guard.settle_column_cell(held.run_id, 'A00', actual=evidence('A00'))
    if final.held_usd != 0 or final.actual_usd != Decimal('.02250'):
        raise RuntimeError('complete proof did not settle exactly once under -O')
    if guard.report() != (final,):
        raise RuntimeError('reopened ledger lost final identity under -O')
    fractional = guard_at(Path(sys.argv[1]) / 'fractional')
    config = json.loads(fractional.config_path.read_text())
    config['providers']['openai']['models']['gpt-6-luna']['input'] = '0.33333333'
    fractional.config_path.write_text(json.dumps(config))
    held = admit(fractional, column(count=1))
    fractional.start_column_cell(held.run_id, 'A00')
    with localcontext() as context:
        context.prec = 6
        settled = fractional.settle_column_cell(
            held.run_id, 'A00', actual=evidence('A00', incoming=1234)
        )
        if settled.actual_usd != Decimal('.01086133333022'):
            raise RuntimeError('low-precision settlement rounded actual under -O')
        final = fractional.finish_column(held.run_id, status='completed')
    if fractional.report() != (final,) or final.actual_usd != Decimal('.01086133333022'):
        raise RuntimeError('fractional persisted actual failed normal-context validation under -O')
print('optimized column checks PASS')
"""
    result = subprocess.run(
        [sys.executable, "-O", "-c", script, str(tmp_path / "optimized")],
        env={"PATH": os.defpath},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "optimized column checks PASS"


@pytest.mark.parametrize(
    "field,value",
    [("version", 3), ("held_usd", Decimal(-1)), ("fixture_id", "invalid"), ("run_id", "invalid")],
)
def test_generated_column_receipts_require_schema_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    guard = guard_at(tmp_path)
    receipt = admit(guard, column(count=1))
    state = receipt.column
    assert state is not None
    paths = (guard.spend_path, guard.lock_path, guard.checkpoint_path)
    before = tuple(p.read_bytes() for p in paths)
    projection = BudgetGuard._column_receipt_fields(state, AT)
    projection[field] = value
    monkeypatch.setattr(BudgetGuard, "_column_receipt_fields", staticmethod(lambda *_: projection))
    # A matching totals projection must not bypass the receipt's wire schema.
    with pytest.raises(ValidationError):
        BudgetGuard._column_receipt(state, AT)
    assert tuple(p.read_bytes() for p in paths) == before
