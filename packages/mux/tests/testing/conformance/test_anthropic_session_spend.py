"""Offline signed-envelope-sized admission and disjoint session spend proofs.

All ledgers and prices are local mock fixtures; no canonical spend or provider I/O.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Literal, cast
from unittest.mock import patch

import pytest
from mux.contracts.ids import ModelRef, ResourceRef
from mux.drivers.anthropic.usage import observation_from_event
from mux.testing.conformance.budget import (
    BudgetGuard,
    BudgetLedgerError,
    BudgetRefused,
    ContainerAllowance,
    ContainerUsage,
    ProbePlan,
    SpendReceipt,
    TokenLimits,
)
from mux.testing.conformance.usage_settlement import (
    AnthropicSessionSpend,
    actual_from_anthropic_sessions,
)
from pydantic import JsonValue

from packages.mux.tests.testing.conformance.test_budget import receipts
from packages.mux.tests.testing.conformance.test_exact_spend import anthropic_session_guard

AT = datetime(2026, 10, 11, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clock() -> Iterator[None]:
    with patch("mux.testing.conformance.budget.datetime", wraps=datetime) as mocked:
        mocked.now.return_value = AT
        yield


def probe(kind: Literal["A7", "F1"] = "A7") -> ProbePlan:
    return ProbePlan(
        provider="anthropic",
        model="claude-haiku-5-5",
        fixture_id=kind,
        limits=TokenLimits(
            input_tokens=200_000 if kind == "A7" else 500_000,
            output_tokens=40_000 if kind == "A7" else 32_000,
        ),
        container_allowance=ContainerAllowance(
            meter="agent_session",
            memory_gb=None,
            sessions=2 if kind == "A7" else 1,
            seconds_per_session=600 if kind == "A7" else 900,
        ),
    )


def evidence(index: int = 1, *, seconds: str = "4.007") -> AnthropicSessionSpend:
    session = ResourceRef(
        provider="anthropic", account_scope_id="qa", kind="session", id=f"session-{index}"
    )
    event: dict[str, JsonValue] = {
        "id": f"span-{index}",
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
    observation = observation_from_event(
        event, session, observed_at=AT, model_id="claude-haiku-5-5"
    )
    return AnthropicSessionSpend(
        session=session,
        observations=(observation,),
        runtime=ContainerUsage(
            id=session.id,
            meter="agent_session",
            memory_gb=None,
            seconds=Decimal(seconds),
            started_at=AT,
        ),
        usage_complete=True,
    )


@pytest.mark.parametrize("kind,floor", [("A7", ".35"), ("F1", ".65")])
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_cash_floor_persists_without_inflating_limits_and_unknown_keeps_it(
    tmp_path: Path,
    kind: Literal["A7", "F1"],
    floor: str,
    status: Literal["completed", "failed", "cancelled"],
) -> None:
    guard = anthropic_session_guard(tmp_path)
    plan = probe(kind)
    held = guard.reserve_with_hold(plan, minimum_hold_usd=Decimal(floor))
    assert held.receipt.reserved_usd == held.receipt.held_usd == Decimal(floor)
    assert held.receipt.limits == plan.limits
    assert held.receipt.container_allowance == plan.container_allowance
    assert BudgetGuard(guard.config_path, guard.spend_path).report() == (held.receipt,)
    settled = guard.settle(held, status=status, limits=plan.limits)
    assert settled.actual_usd is None and settled.held_usd == Decimal(floor)


@pytest.mark.parametrize("floor", [Decimal(0), Decimal(".001")])
def test_cash_floor_cannot_reduce_priced_upper_bound(tmp_path: Path, floor: Decimal) -> None:
    guard = anthropic_session_guard(tmp_path)
    with localcontext() as context:
        context.prec = 6
        held = guard.reserve_with_hold(probe(), minimum_hold_usd=floor)
    assert held.receipt.reserved_usd == Decimal(".326666666666666666")


@pytest.mark.parametrize(
    "floor",
    [
        Decimal("-1"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("1e-19"),
        Decimal("1e32"),
        ".35",
        0.35,
        True,
    ],
)
def test_invalid_floor_refuses_before_ledger_io(tmp_path: Path, floor: object) -> None:
    guard = anthropic_session_guard(tmp_path)
    before = (
        guard.spend_path.read_bytes(),
        guard.lock_path.read_bytes(),
        guard.checkpoint_path.read_bytes(),
    )
    with pytest.raises(BudgetRefused, match="cash floor"):
        guard.reserve_with_hold(probe(), minimum_hold_usd=cast(Decimal, floor))
    assert before == (
        guard.spend_path.read_bytes(),
        guard.lock_path.read_bytes(),
        guard.checkpoint_path.read_bytes(),
    )


def test_cash_floor_counts_against_stop_before_invocation(tmp_path: Path) -> None:
    guard = anthropic_session_guard(tmp_path)
    guard.reserve_with_hold(probe("F1"), minimum_hold_usd=Decimal("7.7"))
    with pytest.raises(BudgetRefused):
        guard.reserve_with_hold(probe(), minimum_hold_usd=Decimal(".35"))
    assert [row.status for row in receipts(guard)] == ["reserved", "blocked"]
    assert receipts(guard)[-1].held_usd == 0


def test_pair_settles_once_with_exact_ttls_and_runtime_at_low_precision(tmp_path: Path) -> None:
    guard = anthropic_session_guard(tmp_path)
    plan = probe()
    with localcontext() as context:
        context.prec = 6
        held = guard.reserve_with_hold(plan, minimum_hold_usd=Decimal(".35"))
        actual = actual_from_anthropic_sessions(
            held, (evidence(), evidence(2)), pricing_basis="standard-global"
        )
        settled = guard.settle(held, status="completed", limits=plan.limits, actual=actual)
    assert settled.token_usd == Decimal(".000278")
    assert settled.container_usd == Decimal(".000178088888888888")
    assert settled.actual_usd == Decimal(".000456088888888888")
    assert settled.held_usd == 0
    assert settled.tokens is not None and settled.tokens.input_tokens == 2100
    assert SpendReceipt.model_validate_json(settled.model_dump_json()) == settled
    assert BudgetGuard(guard.config_path, guard.spend_path).report() == (settled,)
    assert len(receipts(guard)) == 2
    before = guard.spend_path.read_bytes()
    with pytest.raises(BudgetLedgerError, match="already settled"):
        guard.settle(held, status="completed", limits=plan.limits, actual=actual)
    assert guard.spend_path.read_bytes() == before


@pytest.mark.parametrize(
    "damage", ["missing_session", "runtime", "finality", "tokens", "basis", "ttl"]
)
def test_incomplete_pair_keeps_full_cash_hold(tmp_path: Path, damage: str) -> None:
    guard = anthropic_session_guard(tmp_path)
    plan = probe()
    held = guard.reserve_with_hold(plan, minimum_hold_usd=Decimal(".35"))
    second = evidence(2)
    if damage == "runtime":
        second = replace(second, runtime=None)
    elif damage == "finality":
        second = replace(second, usage_complete=False)
    elif damage == "tokens":
        obs = second.observations[0].model_copy(update={"output_tokens": None})
        second = replace(second, observations=(obs,))
    elif damage == "ttl":
        obs = second.observations[0]
        second = replace(
            second,
            observations=(
                obs.model_copy(
                    update={
                        "native_meter": {
                            key: value
                            for key, value in obs.native_meter.items()
                            if key != "cache_creation"
                        }
                    }
                ),
            ),
        )
    actual = actual_from_anthropic_sessions(
        held,
        (evidence(),) if damage == "missing_session" else (evidence(), second),
        pricing_basis=None if damage == "basis" else "standard-global",
    )
    settled = guard.settle(held, status="completed", limits=plan.limits, actual=actual)
    assert settled.actual_usd is None and settled.held_usd == Decimal(".35")
    assert settled.accounting_status == "estimated_unverified"


@pytest.mark.parametrize(
    "damage",
    [
        "duplicate_session",
        "duplicate_request",
        "foreign_request",
        "wrong_model",
        "wrong_runtime",
        "stale_request",
        "stale_runtime",
        "aggregate",
        "scope",
    ],
)
def test_foreign_stale_overlapping_evidence_refuses_before_settlement(
    tmp_path: Path,
    damage: str,
) -> None:
    guard = anthropic_session_guard(tmp_path)
    held = guard.reserve_with_hold(probe(), minimum_hold_usd=Decimal(".35"))
    first, second = evidence(), evidence(2)
    obs = second.observations[0]
    if damage == "duplicate_session":
        second = first
    elif damage == "duplicate_request":
        second = replace(second, observations=(obs.model_copy(update={"id": "span-1"}),))
    elif damage == "foreign_request":
        second = replace(second, observations=first.observations)
    elif damage == "wrong_model":
        second = replace(
            second,
            observations=(
                obs.model_copy(
                    update={"model": ModelRef(provider="anthropic", id="claude-opus-4-6")}
                ),
            ),
        )
    elif damage == "aggregate":
        second = replace(second, observations=(obs.model_copy(update={"grain": "session"}),))
    elif damage == "scope":
        second = replace(
            second, session=second.session.model_copy(update={"account_scope_id": "foreign"})
        )
    elif damage == "stale_request":
        second = replace(
            second,
            observations=(obs.model_copy(update={"observed_at": AT - timedelta(seconds=1)}),),
        )
    else:
        assert second.runtime is not None
        second = replace(
            second,
            runtime=second.runtime.model_copy(
                update={"id": "foreign"}
                if damage == "wrong_runtime"
                else {"started_at": AT - timedelta(seconds=1)}
            ),
        )
    before = guard.spend_path.read_bytes()
    with pytest.raises(BudgetLedgerError):
        actual_from_anthropic_sessions(held, (first, second), pricing_basis="standard-global")
    assert guard.spend_path.read_bytes() == before


def test_missing_runtime_does_not_erase_known_overrun_interval(tmp_path: Path) -> None:
    guard = anthropic_session_guard(tmp_path)
    plan = probe()
    held = guard.reserve_with_hold(plan, minimum_hold_usd=Decimal(".35"))
    actual = actual_from_anthropic_sessions(
        held,
        (evidence(seconds="20000"), replace(evidence(2), runtime=None)),
        pricing_basis="standard-global",
    )
    settled = guard.settle(held, status="failed", limits=plan.limits, actual=actual)
    assert settled.status == "overrun" and settled.admission_blocked
    assert settled.actual_usd is None
    assert settled.held_usd is not None and settled.held_usd > Decimal(".35")
    with pytest.raises(BudgetRefused):
        guard.reserve_with_hold(probe("F1"))


def test_single_f2_measured_failure_still_settles_actual(tmp_path: Path) -> None:
    guard = anthropic_session_guard(tmp_path)
    plan = probe("F1")
    held = guard.reserve_with_hold(plan, minimum_hold_usd=Decimal(".65"))
    actual = actual_from_anthropic_sessions(held, (evidence(),), pricing_basis="standard-global")
    receipt = guard.settle(held, status="failed", limits=plan.limits, actual=actual)
    assert receipt.actual_usd == Decimal(".000228044444444444")
    assert receipt.held_usd == 0 and receipt.status == "failed"


def test_pinned_install_admits_only_within_signed_ceiling(tmp_path: Path) -> None:
    guard = anthropic_session_guard(tmp_path)
    config_sha = hashlib.sha256(guard.config_path.read_bytes()).hexdigest()
    ledger_id = json.loads(guard.checkpoint_path.read_text())["binding"]["ledger_id"]
    held = guard.reserve_with_hold(
        probe(),
        minimum_hold_usd=Decimal(".35"),
        maximum_hold_usd=Decimal(".35"),
        expected_config_sha256=config_sha,
        expected_ledger_id=ledger_id,
    )
    assert held.receipt.held_usd == Decimal(".35")
    with pytest.raises(BudgetRefused):
        guard.reserve_with_hold(probe(), maximum_hold_usd=Decimal(".30"))
    assert [row.status for row in receipts(guard)] == ["reserved", "blocked"]


@pytest.mark.parametrize("damage", ["config", "ledger", "malformed", "floor_above_ceiling"])
def test_installation_mismatch_refuses_before_append(tmp_path: Path, damage: str) -> None:
    guard = anthropic_session_guard(tmp_path)
    before = guard.spend_path.read_bytes()
    config_sha = hashlib.sha256(guard.config_path.read_bytes()).hexdigest()
    ledger_id = json.loads(guard.checkpoint_path.read_text())["binding"]["ledger_id"]
    if damage == "config":
        guard.config_path.write_bytes(guard.config_path.read_bytes() + b"\n")
    with pytest.raises(BudgetRefused):
        guard.reserve_with_hold(
            probe(),
            minimum_hold_usd=Decimal(".35"),
            maximum_hold_usd=Decimal(".34") if damage == "floor_above_ceiling" else Decimal(".35"),
            expected_config_sha256="bad" if damage == "malformed" else config_sha,
            expected_ledger_id="f" * 32 if damage == "ledger" else ledger_id,
        )
    assert guard.spend_path.read_bytes() == before


def test_zero_model_requests_still_require_measured_runtime(tmp_path: Path) -> None:
    guard = anthropic_session_guard(tmp_path)
    plan = probe("F1")
    held = guard.reserve_with_hold(plan, minimum_hold_usd=Decimal(".65"))
    session = replace(evidence(), observations=())
    actual = actual_from_anthropic_sessions(held, (session,), pricing_basis="standard-global")
    receipt = guard.settle(held, status="cancelled", limits=plan.limits, actual=actual)
    assert receipt.token_usd == 0
    assert receipt.actual_usd == Decimal(".000089044444444444")
    assert receipt.held_usd == 0
