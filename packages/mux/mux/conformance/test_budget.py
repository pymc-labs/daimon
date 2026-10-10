"""Offline budget admission/receipts and ledger rollback regressions."""

from __future__ import annotations

import base64
import json
import multiprocessing
import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from mux.conformance.budget import (
    HEADER,
    BudgetConfig,
    BudgetGuard,
    BudgetLedgerError,
    BudgetRefused,
    ModelPrice,
    ProbePlan,
    SpendReceipt,
    TokenLimits,
    TokenUsage,
)


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_settlement_without_usage_keeps_worst_case_reservation(
    tmp_path: Path, status: Literal["completed", "failed", "cancelled"]
) -> None:
    guard = setup_guard(tmp_path)
    held = guard.reserve(plan())
    receipt = guard.settle(held, status=status, limits=plan().limits)
    assert receipt.cost_estimate_usd == 1
    assert receipt.status == ("uncertain" if status == "completed" else status)


def test_direct_measured_settlement_releases_only_known_savings(tmp_path: Path) -> None:
    guard = setup_guard(tmp_path, "2")
    held = guard.reserve(plan())
    receipt = guard.settle(
        held,
        status="completed",
        limits=plan().limits,
        usage=TokenUsage(
            input_tokens=250_000, output_tokens=0, input_cached_tokens=0, input_cache_write_tokens=0
        ),
    )
    assert receipt.cost_estimate_usd == Decimal("0.25")
    guard.reserve(plan())
    with pytest.raises(BudgetRefused):
        guard.reserve(plan())


@pytest.mark.parametrize("output", [0, None])
@pytest.mark.parametrize(
    "bucket", ["input_tokens", "input_cached_tokens", "input_cache_write_tokens", "cache_subsets"]
)
def test_direct_partial_usage_overrun_locks_out_zero_cost_admission(
    tmp_path: Path, output: int | None, bucket: str
) -> None:
    guard = setup_guard(tmp_path)
    held = guard.reserve(plan())
    counts = (
        {"input_cached_tokens": 1_000_000, "input_cache_write_tokens": 1_000_000}
        if bucket == "cache_subsets"
        else {bucket: 2_000_000}
    )
    receipt = guard.settle(
        held,
        status="completed",
        limits=plan().limits,
        usage=TokenUsage.model_validate({**counts, "output_tokens": output}),
    )
    assert receipt.status == "overrun" and receipt.cost_estimate_usd == 2
    with pytest.raises(BudgetRefused):
        guard.reserve(plan(0))


@pytest.mark.parametrize(
    "damage", ["missing", "empty", "header_only", "headerless", "pristine_rollback"]
)
def test_reset_refuses_without_reinitializing_state(tmp_path: Path, damage: str) -> None:
    guard = setup_guard(tmp_path)
    pristine = guard.spend_path.read_bytes()
    guard.reserve(plan(8_000_000))
    if damage == "missing":
        guard.spend_path.rename(tmp_path / "spend.bak")
    else:
        guard.spend_path.write_bytes(
            pristine
            if damage == "pristine_rollback"
            else {"empty": b"", "header_only": (HEADER + "\n").encode(), "headerless": b"{}\n"}[
                damage
            ]
        )
    before = guard.spend_path.read_bytes() if guard.spend_path.exists() else None
    markers = guard.lock_path.read_bytes(), guard.checkpoint_path.read_bytes()
    with pytest.raises(BudgetLedgerError):
        BudgetGuard(guard.config_path, guard.spend_path).reserve(plan(1))
    with pytest.raises(BudgetLedgerError, match="already exists"):
        BudgetGuard.initialize(guard.config_path, guard.spend_path)
    assert (guard.spend_path.read_bytes() if guard.spend_path.exists() else None) == before
    assert (guard.lock_path.read_bytes(), guard.checkpoint_path.read_bytes()) == markers


@pytest.mark.parametrize("snapshot_runs", [0, 1])
def test_paired_snapshot_restore_refuses_with_retained_anchor(
    tmp_path: Path, snapshot_runs: int
) -> None:
    guard = setup_guard(tmp_path)
    for _ in range(snapshot_runs):
        guard.reserve(plan())
    snapshot = guard.spend_path.read_bytes(), guard.checkpoint_path.read_bytes()
    for _ in range(8 - snapshot_runs):
        guard.reserve(plan())
    anchor = guard.lock_path.read_bytes()
    guard.spend_path.write_bytes(snapshot[0])
    guard.checkpoint_path.write_bytes(snapshot[1])
    with pytest.raises(BudgetLedgerError, match="sequence"):
        BudgetGuard(guard.config_path, guard.spend_path).reserve(plan())
    assert (guard.spend_path.read_bytes(), guard.checkpoint_path.read_bytes()) == snapshot
    assert guard.lock_path.read_bytes() == anchor


@pytest.mark.parametrize("marker", ["anchor", "checkpoint"])
def test_interrupted_durable_update_refuses_future_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    guard = setup_guard(tmp_path)
    before = guard.spend_path.read_bytes(), guard.checkpoint_path.read_bytes()
    fsync = os.fsync
    inode = guard.lock_path.stat().st_ino

    def fail_sync(fd: int) -> None:
        fsync(fd)
        if os.fstat(fd).st_ino == inode:
            raise OSError("injected durable-anchor fault")

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("injected checkpoint fault")

    with monkeypatch.context() as patch:
        patch.setattr(
            "mux.conformance.budget.os." + ("fsync" if marker == "anchor" else "replace"),
            fail_sync if marker == "anchor" else fail_replace,
        )
        with pytest.raises(OSError):
            guard.reserve(plan())
    if marker == "anchor":
        assert (guard.spend_path.read_bytes(), guard.checkpoint_path.read_bytes()) == before
    else:
        assert receipts(guard)[-1].status == "reserved"
    with pytest.raises(BudgetLedgerError):
        guard.reserve(plan())


@pytest.mark.parametrize(
    "encoding", ["raw", "base64", "base64url", "utf16", "double", "encoded_json"]
)
def test_credential_shaped_metadata_refuses_before_ledger_write(
    tmp_path: Path, encoding: str
) -> None:
    guard = setup_guard(tmp_path)
    credential = "sk-" + "f" * 32
    value = "9" + credential
    if encoding != "raw":
        binary = value.encode("utf-16-le" if encoding == "utf16" else "utf-8")
        if encoding == "encoded_json":
            binary = json.dumps({"session_token": "opaque-dummy-value"}).encode()
        if encoding == "double":
            binary = base64.b64encode(binary)
        value = base64.urlsafe_b64encode(binary).decode().rstrip("=")
    before = guard.spend_path.read_bytes()
    with pytest.raises(BudgetRefused, match="metadata"):
        guard.reserve(plan().model_copy(update={"model": value}))
    assert guard.spend_path.read_bytes() == before
    config = json.loads(guard.config_path.read_text())
    config["providers"]["openai"]["models"][value] = config["providers"]["openai"]["models"].pop(
        "gpt-6-luna"
    )
    with pytest.raises(ValidationError, match="metadata"):
        BudgetConfig.model_validate(config)


def test_budget_import_and_optimized_admission_need_no_recorder(tmp_path: Path) -> None:
    guard = setup_guard(tmp_path)
    script = """
import sys
from pathlib import Path
from mux.conformance.budget import BudgetGuard, BudgetRefused, ProbePlan, TokenLimits
if 'mux.conformance.recording' in sys.modules:
    raise SystemExit('budget imported recorder')
guard = BudgetGuard(Path(sys.argv[1]), Path(sys.argv[2]))
plan = ProbePlan(provider='openai', model='gpt-6-luna', fixture_id='C16',
    limits=TokenLimits(input_tokens=8_000_000, output_tokens=0))
guard.reserve(plan)
try:
    guard.reserve(plan)
except BudgetRefused:
    pass
else:
    raise SystemExit('optimized cap check vanished')
"""
    subprocess.run(
        [sys.executable, "-O", "-c", script, str(guard.config_path), str(guard.spend_path)],
        check=True,
        capture_output=True,
        timeout=30,
    )


def setup_guard(tmp_path: Path, cap: str = "10") -> BudgetGuard:
    config = tmp_path / "budget.json"
    config.write_text(
        json.dumps(
            {
                "ledger_path": str(tmp_path / "spend.md"),
                "providers": {
                    provider: {
                        "cap_usd": cap,
                        "models": {
                            ("gpt-6-luna" if provider == "openai" else "claude-haiku-5-5"): {
                                "input": "1",
                                "cached_input": "1",
                                "cache_write_input": "1",
                                "output": "1",
                            }
                        },
                    }
                    for provider in ("openai", "anthropic")
                },
            }
        )
    )
    BudgetGuard.initialize(config, tmp_path / "spend.md")
    return BudgetGuard(config, tmp_path / "spend.md")


def plan(tokens: int = 1_000_000, provider: str = "openai") -> ProbePlan:
    return ProbePlan(
        provider=provider,
        model="gpt-6-luna" if provider == "openai" else "claude-haiku-5-5",
        fixture_id="C16",
        limits=TokenLimits(input_tokens=tokens, output_tokens=0),
    )


def receipts(guard: BudgetGuard) -> list[SpendReceipt]:
    return [
        SpendReceipt.model_validate_json(line[len("<!-- mux-probe ") : -4])
        for line in guard.spend_path.read_text().splitlines()
        if line.startswith("<!-- mux-probe ")
    ]


def test_80_percent_boundary_persists_before_io_and_survives_restarts(tmp_path: Path) -> None:
    guard = setup_guard(tmp_path)
    reservation = guard.reserve(plan(8_000_000))
    assert reservation.receipt.cost_estimate_usd == 8
    restored = BudgetGuard(guard.config_path, guard.spend_path)
    with pytest.raises(BudgetRefused):
        restored.reserve(plan(1))
    rows = receipts(guard)
    assert [r.status for r in rows] == ["reserved", "blocked"]
    assert rows[-1].reason == "budget"


def test_settlement_cannot_be_replayed(tmp_path: Path) -> None:
    guard = setup_guard(tmp_path)
    held = guard.reserve(plan())
    guard.settle(held, status="failed", limits=plan().limits)
    with pytest.raises(BudgetLedgerError):
        guard.settle(
            held,
            status="completed",
            limits=plan().limits,
            usage=TokenUsage(input_tokens=0, output_tokens=0),
        )


def test_inclusive_input_and_cached_subsets_are_not_double_counted() -> None:
    price = ModelPrice(
        input=Decimal(2), cached_input=Decimal(1), cache_write_input=Decimal(3), output=Decimal(4)
    )
    usage = TokenUsage(
        input_tokens=100, output_tokens=20, input_cached_tokens=30, input_cache_write_tokens=10
    )
    assert price.estimate(usage) == Decimal("0.00026")
    assert price.reserve(TokenLimits(input_tokens=100, output_tokens=20)) == Decimal("0.00038")
    assert price.estimate(TokenUsage(input_tokens=100, output_tokens=20)) == Decimal("0.00038")
    with pytest.raises(ValidationError):
        TokenUsage(input_tokens=1, input_cached_tokens=2)


@pytest.mark.parametrize("value", [True, 1.5, -1])
def test_token_counts_are_strict_nonnegative_integers(value: object) -> None:
    with pytest.raises(ValidationError):
        TokenLimits.model_validate({"input_tokens": value, "output_tokens": 0})


def test_unknown_pricing_and_malformed_ledger_fail_closed(tmp_path: Path) -> None:
    guard = setup_guard(tmp_path)
    unknown = plan().model_copy(update={"model": "not-priced"})
    with pytest.raises(BudgetRefused):
        guard.reserve(unknown)
    assert receipts(guard)[-1].reason == "unconfigured"
    guard.spend_path.write_text("an unaccounted charge\n")
    with pytest.raises(BudgetLedgerError):
        guard.reserve(plan())


def reserve_worker(config: str, spend: str) -> bool:
    try:
        BudgetGuard(Path(config), Path(spend)).reserve(plan())
    except BudgetRefused:
        return False
    return True


def test_two_processes_cannot_admit_past_the_same_cap(tmp_path: Path) -> None:
    guard = setup_guard(tmp_path, "2")
    with ProcessPoolExecutor(
        max_workers=2, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        attempts = [
            pool.submit(reserve_worker, str(guard.config_path), str(guard.spend_path))
            for _ in range(2)
        ]
        assert sorted(f.result(timeout=30) for f in attempts) == [False, True]
    assert sorted(r.status for r in receipts(guard)) == ["blocked", "reserved"]


def test_opening_spend_counts_against_provider_limit(tmp_path: Path) -> None:
    guard = setup_guard(tmp_path)
    config = json.loads(guard.config_path.read_text())
    config["providers"]["openai"]["opening_spend_usd"] = "7.5"
    guard.config_path.write_text(json.dumps(config))
    with pytest.raises(BudgetRefused):
        guard.reserve(plan())
    assert receipts(guard)[-1].reason == "budget"


def test_removed_provider_receipts_still_count_against_total_budget(tmp_path: Path) -> None:
    guard = setup_guard(tmp_path, "75")
    guard.reserve(plan(60_000_000))
    config = json.loads(guard.config_path.read_text())
    del config["providers"]["openai"]
    config["providers"]["anthropic"]["cap_usd"] = "150"
    guard.config_path.write_text(json.dumps(config))
    with pytest.raises(BudgetRefused):
        guard.reserve(plan(60_000_001, provider="anthropic"))
    assert guard.reserve(plan(60_000_000, provider="anthropic")).receipt.reserved_usd == 60


@pytest.mark.parametrize("cap", ["-1", "NaN", "76"])
def test_invalid_or_overallocated_config_never_reserves(tmp_path: Path, cap: str) -> None:
    guard = setup_guard(tmp_path)
    before = guard.spend_path.read_bytes()
    config = json.loads(guard.config_path.read_text())
    for budget in config["providers"].values():
        budget["cap_usd"] = cap
    guard.config_path.write_text(json.dumps(config))
    with pytest.raises(BudgetRefused):
        guard.reserve(plan())
    assert guard.spend_path.read_bytes() == before


@pytest.mark.parametrize("alternate", ["different", "relative", "copied_and_repinned"])
def test_other_ledger_paths_cannot_create_another_budget(tmp_path: Path, alternate: str) -> None:
    guard = setup_guard(tmp_path)
    guard.reserve(plan(8_000_000))
    other_path = tmp_path / "other.md" if alternate != "relative" else Path("spend.md")
    other = BudgetGuard(guard.config_path, other_path)
    if alternate == "copied_and_repinned":
        other_path.write_bytes(guard.spend_path.read_bytes())
        other.lock_path.write_bytes(guard.lock_path.read_bytes())
        other.checkpoint_path.write_bytes(guard.checkpoint_path.read_bytes())
        config = json.loads(guard.config_path.read_text())
        config["ledger_path"] = str(other_path)
        guard.config_path.write_text(json.dumps(config))
    with pytest.raises(BudgetLedgerError):
        other.reserve(plan(1))
    if alternate == "different":
        with pytest.raises(BudgetLedgerError):
            BudgetGuard.initialize(guard.config_path, other_path)
        assert not other_path.exists()


@pytest.mark.parametrize("damage", ["missing", "empty", "invalid", "lowered_total"])
def test_checkpoint_loss_or_forged_total_refuses_admission(tmp_path: Path, damage: str) -> None:
    guard = setup_guard(tmp_path)
    guard.reserve(plan())
    before = guard.spend_path.read_bytes()
    if damage == "missing":
        guard.checkpoint_path.unlink()
    elif damage == "lowered_total":
        checkpoint = json.loads(guard.checkpoint_path.read_text())
        checkpoint["provider_totals"]["openai"] = "0"
        guard.checkpoint_path.write_text(json.dumps(checkpoint))
    else:
        guard.checkpoint_path.write_text("" if damage == "empty" else "invalid")
    with pytest.raises(BudgetLedgerError):
        guard.reserve(plan())
    assert guard.spend_path.read_bytes() == before


def test_opening_spend_checkpoint_cannot_be_removed_from_config(tmp_path: Path) -> None:
    guard = setup_guard(tmp_path)
    config = json.loads(guard.config_path.read_text())
    config["providers"]["openai"]["opening_spend_usd"] = "8"
    guard.config_path.write_text(json.dumps(config))
    with pytest.raises(BudgetRefused):
        guard.reserve(plan(1))
    config["providers"]["openai"]["opening_spend_usd"] = "0"
    guard.config_path.write_text(json.dumps(config))
    with pytest.raises(BudgetRefused):
        guard.reserve(plan(1))
    checkpoint = json.loads(guard.checkpoint_path.read_text())
    assert Decimal(checkpoint["provider_totals"]["openai"]) == 8


def test_invalid_terminal_receipt_is_rejected_before_any_append(tmp_path: Path) -> None:
    guard = setup_guard(tmp_path)
    held = guard.reserve(plan())
    before = guard.spend_path.read_bytes(), guard.checkpoint_path.read_bytes()
    invalid = held.model_copy(
        update={
            "price": held.price.model_copy(
                update={
                    "input": Decimal(-1),
                    "cached_input": Decimal(-1),
                    "cache_write_input": Decimal(-1),
                }
            ),
        }
    )
    with pytest.raises(ValidationError):
        guard.settle(
            invalid,
            status="completed",
            limits=plan().limits,
            usage=TokenUsage(
                input_tokens=1_000_000,
                output_tokens=0,
                input_cached_tokens=0,
                input_cache_write_tokens=0,
            ),
        )
    assert (guard.spend_path.read_bytes(), guard.checkpoint_path.read_bytes()) == before
    guard.reserve(plan(0))  # The rejected mutation did not brick healthy state.


def test_fresh_guard_requires_explicit_initialization(tmp_path: Path) -> None:
    existing = setup_guard(tmp_path)
    other_dir = tmp_path / "fresh"
    other_dir.mkdir()
    config = json.loads(existing.config_path.read_text())
    config["ledger_path"] = str(other_dir / "spend.md")
    config_path = other_dir / "budget.json"
    config_path.write_text(json.dumps(config))
    fresh = BudgetGuard(config_path, other_dir / "spend.md")
    with pytest.raises(BudgetLedgerError, match="initialize explicitly"):
        fresh.reserve(plan())
    assert not fresh.spend_path.exists()
    BudgetGuard.initialize(config_path, fresh.spend_path)
    fresh.reserve(plan())
    with pytest.raises(BudgetLedgerError, match="already exists"):
        BudgetGuard.initialize(config_path, fresh.spend_path)


def test_anchor_sequence_advances_without_preventing_known_settlement_savings(
    tmp_path: Path,
) -> None:
    guard = setup_guard(tmp_path)
    inode = guard.lock_path.stat().st_ino
    reservation = guard.reserve(plan())
    guard.settle(
        reservation,
        status="completed",
        limits=plan().limits,
        usage=TokenUsage(
            input_tokens=0, output_tokens=0, input_cached_tokens=0, input_cache_write_tokens=0
        ),
    )
    guard.reserve(plan(8_000_000))
    assert guard.lock_path.stat().st_ino == inode
    assert json.loads(guard.lock_path.read_text())["sequence"] == 3
    assert json.loads(guard.checkpoint_path.read_text())["sequence"] == 3
