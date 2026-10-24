"""Offline budget admission/receipts and ledger rollback regressions."""

from __future__ import annotations

import base64
import hashlib
import json
import multiprocessing
import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from mux.testing.conformance.budget import (
    HEADER,
    BudgetConfig,
    BudgetGuard,
    BudgetLedgerError,
    BudgetRefused,
    H6Binding,
    ModelPrice,
    ProbePlan,
    SpendReceipt,
    TokenLimits,
    TokenUsage,
)
from pydantic import ValidationError

TEST_MODELS = {
    "openai": "gpt-6-luna",
    "anthropic": "claude-haiku-5-5",
    "gemini": "gemini-3.8-flash",
}


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
            "mux.testing.conformance.budget.os." + ("fsync" if marker == "anchor" else "replace"),
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
from mux.testing.conformance.budget import BudgetGuard, BudgetRefused, ProbePlan, TokenLimits
if 'mux.testing.conformance.recording' in sys.modules:
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


def setup_guard(
    tmp_path: Path, cap: str = "10", *, providers: tuple[str, ...] = ("openai", "anthropic")
) -> BudgetGuard:
    config = tmp_path / "budget.json"
    config.write_text(
        json.dumps(
            {
                "ledger_path": str(tmp_path / "spend.md"),
                "providers": {
                    provider: {
                        "cap_usd": cap,
                        "models": {
                            TEST_MODELS[provider]: {
                                "input": "1",
                                "cached_input": "1",
                                "cache_write_input": "1",
                                "output": "1",
                            }
                        },
                    }
                    for provider in providers
                },
            }
        )
    )
    BudgetGuard.initialize(config, tmp_path / "spend.md")
    return BudgetGuard(config, tmp_path / "spend.md")


def plan(tokens: int = 1_000_000, provider: str = "openai") -> ProbePlan:
    return ProbePlan(
        provider=provider,
        model=TEST_MODELS[provider],
        fixture_id="C16",
        limits=TokenLimits(input_tokens=tokens, output_tokens=0),
    )


def receipts(guard: BudgetGuard) -> list[SpendReceipt]:
    return [
        SpendReceipt.model_validate_json(line[len("<!-- mux-probe ") : -4])
        for line in guard.spend_path.read_text().splitlines()
        if line.startswith("<!-- mux-probe ")
    ]


@pytest.mark.parametrize("fixture_id", ["H6-ANTHROPIC", "H6-OPENAI", "H6-GEMINI"])
@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
def test_h6_identity_preserves_the_actual_provider_budget_line(
    tmp_path: Path, fixture_id: str, provider: str
) -> None:
    guard = setup_guard(tmp_path, providers=tuple(TEST_MODELS))
    admitted = staging_plan(guard, provider=provider, fixture_id=fixture_id)
    reservation = guard.reserve(admitted)
    assert reservation.receipt.fixture_id == fixture_id
    assert reservation.receipt.provider == provider
    assert reservation.receipt.model == TEST_MODELS[provider]
    assert reservation.receipt.reserved_usd == 1
    settled = guard.settle(reservation, status="completed", limits=admitted.limits)
    assert settled.fixture_id == fixture_id and settled.provider == provider
    assert settled.status == "uncertain" and settled.actual_usd is None
    assert settled.held_usd == reservation.receipt.reserved_usd
    assert BudgetGuard(guard.config_path, guard.spend_path).report()[-1] == settled


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
    # The worker must be importable by spawn after tests leave the wheel.
    from packages.mux.tests.testing.conformance.test_budget import reserve_worker as spawn_worker

    guard = setup_guard(tmp_path, "2")
    with ProcessPoolExecutor(
        max_workers=2, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        attempts = [
            pool.submit(spawn_worker, str(guard.config_path), str(guard.spend_path))
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


def staging_plan(
    guard: BudgetGuard,
    *,
    provider: str = "openai",
    fixture_id: str = "H6-OPENAI",
    **changes: object,
) -> ProbePlan:
    data = json.loads(guard.config_path.read_text())
    now = datetime.now(UTC)
    for budget in data["providers"].values():
        for price in budget["models"].values():
            price.update(
                effective_from=(now - timedelta(days=1)).date().isoformat(),
                effective_until=(now + timedelta(days=1)).date().isoformat(),
                source="https://developers.openai.com/api/docs/pricing",
            )
    guard.config_path.write_text(json.dumps(data))
    binding = H6Binding(
        attempt_id="offline-attempt",
        leg_id="baseline",
        phase="baseline",
        source_sha256="a" * 64,
        deployment_sha256="b" * 64,
        go_sha256="c" * 64,
        envelope_sha256="d" * 64,
        quote_sha256="e" * 64,
        scope_sha256="f" * 64,
        config_sha256=hashlib.sha256(guard.config_path.read_bytes()).hexdigest(),
        ledger_id=json.loads(guard.checkpoint_path.read_text())["binding"]["ledger_id"],
        deadline=now + timedelta(minutes=5),
        maximum_hold_usd=Decimal(1),
        origin="https://offline.invalid",
        requests=(("POST", "/v1/messages"),),
        request_limit=1,
    )
    return ProbePlan.model_validate(
        {
            **plan(provider=provider).model_dump(),
            "fixture_id": fixture_id,
            "limits": {"input_tokens": 20_000, "output_tokens": 2_000},
            "h6": {**binding.model_dump(), **changes},
        }
    )


@pytest.mark.parametrize(
    "fixture_id",
    ["H6", "H6-UNKNOWN", "H6-openai", "H6-OPENAI-extra", "H6-OPENAI\n", "C00", "C19", "F2"],
)
def test_staging_fixture_identity_is_exact(fixture_id: str) -> None:
    with pytest.raises(ValidationError):
        ProbePlan(provider="openai", model=TEST_MODELS["openai"], fixture_id=fixture_id)


@pytest.mark.parametrize(
    "damage", ["missing", "deadline", "ledger", "config", "routes", "origin", "scope", "limit"]
)
def test_unbound_staging_admission_refuses(tmp_path: Path, damage: str) -> None:
    guard = setup_guard(tmp_path)
    updates: dict[str, object] = {
        "deadline": datetime.now(UTC) - timedelta(seconds=1),
        "ledger_id": "0" * 32,
        "config_sha256": "0" * 64,
        "requests": (),
        "origin": "https://user:secret@offline.invalid",
        "scope_sha256": "unbound",
        "request_limit": 0,
    }
    with pytest.raises((ValidationError, BudgetRefused)):
        if damage == "missing":
            ProbePlan(provider="openai", model=TEST_MODELS["openai"], fixture_id="H6-OPENAI")
        else:
            field = {
                "ledger": "ledger_id",
                "config": "config_sha256",
                "routes": "requests",
                "scope": "scope_sha256",
                "limit": "request_limit",
            }.get(damage, damage)
            guard.reserve(staging_plan(guard, **{field: updates[field]}))
    assert not receipts(guard)


@pytest.mark.parametrize("damage", ["undated", "expired", "fallback", "room"])
def test_staging_dated_model_and_room_admission(tmp_path: Path, damage: str) -> None:
    guard = setup_guard(tmp_path, providers=tuple(TEST_MODELS))
    admitted = staging_plan(guard, provider="gemini")
    data = json.loads(guard.config_path.read_text())
    price = data["providers"]["gemini"]["models"][TEST_MODELS["gemini"]]
    if damage == "undated":
        price.pop("effective_until")
    elif damage == "expired":
        price["effective_until"] = datetime.now(UTC).date().isoformat()
    elif damage == "room":
        data["providers"]["gemini"]["cap_usd"] = ".5"
    else:
        data["providers"]["gemini"]["models"]["gemini-flash-latest"] = price
    guard.config_path.write_text(json.dumps(data))
    raw = admitted.model_dump()
    raw["h6"]["config_sha256"] = hashlib.sha256(guard.config_path.read_bytes()).hexdigest()
    if damage == "fallback":
        raw["model"] = "gemini-flash-latest"
    admitted = ProbePlan.model_validate(raw)
    with pytest.raises(BudgetRefused):
        guard.reserve(admitted)
    assert receipts(guard)[-1].status == "blocked"


@pytest.mark.parametrize("terminal", ["active", "unknown", "failed", "cancelled"])
def test_staging_no_duplicate_or_followup_after_unresolved_leg(
    tmp_path: Path, terminal: str
) -> None:
    guard = setup_guard(tmp_path)
    first = staging_plan(guard)
    reservation = guard.reserve(first)
    if terminal != "active":
        guard.settle(
            reservation,
            status="completed" if terminal == "unknown" else terminal,
            limits=first.limits,
        )
    with pytest.raises(BudgetRefused, match="already consumed"):
        guard.reserve(first)
    second = ProbePlan.model_validate(
        {
            **first.model_dump(),
            "h6": {**first.h6.model_dump(), "leg_id": "target", "phase": "switched_new"},
        }
    )
    with pytest.raises(BudgetRefused, match="active, failed or unresolved"):
        guard.reserve(second)
    assert len(receipts(guard)) == (1 if terminal == "active" else 2)


@pytest.mark.parametrize("provider", tuple(TEST_MODELS))
@pytest.mark.parametrize("tokens", [0, 1_000])
def test_staging_token_estimate_cannot_release_cash_hold(
    tmp_path: Path, provider: str, tokens: int
) -> None:
    guard = setup_guard(tmp_path, "2", providers=tuple(TEST_MODELS))
    admitted = staging_plan(guard, provider=provider, fixture_id=f"H6-{provider.upper()}")
    held = guard.reserve(admitted)
    receipt = guard.settle(
        held,
        status="completed",
        limits=admitted.limits,
        usage=TokenUsage(
            input_tokens=tokens, output_tokens=0, input_cached_tokens=0, input_cache_write_tokens=0
        ),
    )
    assert receipt.status == "uncertain" and receipt.reason == "unknown_usage"
    assert receipt.actual_usd is None and receipt.actual_evidence is None
    assert receipt.accounting_status == "estimated_unverified"
    assert receipt.held_usd == receipt.cost_estimate_usd == held.receipt.reserved_usd == 1
    assert BudgetGuard(guard.config_path, guard.spend_path).report()[-1] == receipt
    with pytest.raises(BudgetRefused):
        guard.reserve(plan(provider=provider))


def test_staging_token_overrun_retains_larger_proven_hold(tmp_path: Path) -> None:
    guard = setup_guard(tmp_path)
    admitted = staging_plan(guard)
    held = guard.reserve(admitted)
    usage = TokenUsage(
        input_tokens=2_000_000, output_tokens=0, input_cached_tokens=0, input_cache_write_tokens=0
    )
    receipt = guard.settle(held, status="completed", limits=admitted.limits, usage=usage)
    assert receipt.status == "overrun" and receipt.admission_blocked
    assert receipt.actual_usd is None and receipt.accounting_status == "estimated_unverified"
    assert (
        receipt.held_usd
        == receipt.cost_estimate_usd
        == held.price.reserve(admitted.limits, usage=usage)
    )
    assert receipt.held_usd > held.receipt.reserved_usd


def test_ordinary_zero_token_estimate_keeps_legacy_settlement(tmp_path: Path) -> None:
    guard = setup_guard(tmp_path)
    admitted = plan()
    held = guard.reserve(admitted)
    receipt = guard.settle(
        held,
        status="completed",
        limits=admitted.limits,
        usage=TokenUsage(
            input_tokens=0, output_tokens=0, input_cached_tokens=0, input_cache_write_tokens=0
        ),
    )
    assert receipt.status == "completed" and receipt.reason == "settled"
    assert receipt.actual_usd is None and receipt.accounting_status == "estimated_unverified"
    assert receipt.held_usd == receipt.cost_estimate_usd == 0


def fleet_plan(guard: BudgetGuard, role: str = "W3", **binding_updates: object) -> ProbePlan:
    base = staging_plan(guard, provider="gemini")
    data = json.loads(guard.config_path.read_text())
    data["providers"]["gemini"]["models"]["gemini-3.5-flash-lite"] = data["providers"]["gemini"][
        "models"
    ]["gemini-3.8-flash"]
    guard.config_path.write_text(json.dumps(data))
    provider, model, hold = {
        "ROOT": ("anthropic", "claude-haiku-5-5", ".06"),
        "W1": ("anthropic", "claude-haiku-5-5", ".04"),
        "W2": ("openai", "gpt-6-luna", ".055"),
        "W3": ("gemini", "gemini-3.8-flash", ".49"),
        "W4": ("openai", "gpt-6-luna", ".055"),
        "RESTORE-SMOKE": ("anthropic", "claude-haiku-5-5", ".03"),
        "CANDIDATE-SMOKE": ("anthropic", "claude-haiku-5-5", ".03"),
    }[role]
    assert base.h6 is not None
    return ProbePlan.model_validate(
        {
            "provider": provider,
            "model": model,
            "fixture_id": "I7-" + role,
            "limits": {"input_tokens": 1_000_000, "output_tokens": 4_000},
            "staging": {
                **base.h6.model_dump(),
                "leg_id": role,
                "maximum_hold_usd": Decimal(hold),
                "config_sha256": hashlib.sha256(guard.config_path.read_bytes()).hexdigest(),
                **binding_updates,
            },
        }
    )


@pytest.mark.parametrize(
    "minimum,maximum",
    [(".60", None), (".50", None), (".60", ".60"), (".50", ".50"), (".50", ".60")],
)
def test_fleet_binding_refuses_cash_floor_above_signed_hold_without_writes(
    tmp_path: Path, minimum: str, maximum: str | None
) -> None:
    guard = setup_guard(tmp_path, providers=tuple(TEST_MODELS))
    admitted = fleet_plan(guard)
    paths = (guard.config_path, guard.spend_path, guard.lock_path, guard.checkpoint_path)
    before = tuple(path.read_bytes() for path in paths)
    with pytest.raises(BudgetRefused, match="cash floor exceeds its ceiling"):
        guard.reserve_with_hold(
            admitted,
            minimum_hold_usd=Decimal(minimum),
            maximum_hold_usd=None if maximum is None else Decimal(maximum),
        )
    assert tuple(path.read_bytes() for path in paths) == before
    assert not guard.report()


@pytest.mark.parametrize("kind", ["ordinary", "staging", "fleet"])
def test_exact_cash_floor_and_ceiling_admit_without_changing_signed_hold(
    tmp_path: Path, kind: str
) -> None:
    guard = setup_guard(tmp_path, providers=tuple(TEST_MODELS))
    admitted = (
        fleet_plan(guard)
        if kind == "fleet"
        else staging_plan(guard)
        if kind == "staging"
        else plan()
    )
    cash = Decimal(".49") if kind == "fleet" else Decimal(1)
    held = guard.reserve_with_hold(admitted, minimum_hold_usd=cash, maximum_hold_usd=cash)
    assert held.receipt.held_usd == cash
    assert guard.report() == (held.receipt,)


@pytest.mark.parametrize("kind", ["ordinary", "staging"])
def test_cash_floor_above_effective_ceiling_refuses_without_writes(
    tmp_path: Path, kind: str
) -> None:
    guard = setup_guard(tmp_path)
    admitted = staging_plan(guard) if kind == "staging" else plan()
    paths = (guard.config_path, guard.spend_path, guard.lock_path, guard.checkpoint_path)
    before = tuple(path.read_bytes() for path in paths)
    with pytest.raises(BudgetRefused, match="cash floor exceeds its ceiling"):
        guard.reserve_with_hold(
            admitted,
            minimum_hold_usd=Decimal(2),
            maximum_hold_usd=Decimal(2) if kind == "staging" else Decimal(1),
        )
    assert tuple(path.read_bytes() for path in paths) == before
    assert not guard.report()


@pytest.mark.parametrize(
    "role,expected",
    [
        ("ROOT", ".06"),
        ("W1", ".04"),
        ("W2", ".055"),
        ("W3", ".49"),
        ("W4", ".055"),
        ("RESTORE-SMOKE", ".03"),
        ("CANDIDATE-SMOKE", ".03"),
    ],
)
def test_fleet_conditional_cash_uses_true_identity_and_original_ledger(
    tmp_path: Path, role: str, expected: str
) -> None:
    guard = setup_guard(tmp_path, providers=tuple(TEST_MODELS))
    admitted = fleet_plan(guard, role)
    held = guard.reserve(admitted)
    assert held.receipt.fixture_id == "I7-" + role
    assert held.receipt.held_usd == Decimal(expected)
    assert held.receipt.h6 is None and held.receipt.staging == admitted.staging
    assert held.receipt.price == held.price
    assert held.receipt.limits == admitted.limits
    guard.require_reservation(held, admitted)
    terminal = guard.settle(held, status="completed", limits=admitted.limits)
    assert terminal.actual_usd is None and terminal.held_usd == Decimal(expected)
    assert terminal.status == "uncertain"


def test_fleet_upfront_roles_can_share_attempt_but_duplicate_refuses(tmp_path: Path) -> None:
    guard = setup_guard(tmp_path, providers=tuple(TEST_MODELS))
    plans = [
        fleet_plan(guard, role)
        for role in ["ROOT", "W1", "W2", "W3", "W4", "RESTORE-SMOKE", "CANDIDATE-SMOKE"]
    ]
    holds = [guard.reserve(p) for p in plans]
    assert sum((r.held_usd for r in guard.report()), Decimal(0)) == Decimal(".76")
    for held, admitted in zip(holds, plans, strict=True):
        guard.require_reservation(held, admitted)
    with pytest.raises(BudgetRefused, match="consumed"):
        guard.reserve(plans[3])
    assert len(guard.report()) == 7


def test_fleet_w3_initial_dispatch_consumes_funding_once_and_survives_restart(
    tmp_path: Path,
) -> None:
    guard = setup_guard(tmp_path, providers=tuple(TEST_MODELS))
    admitted = fleet_plan(guard)
    held = guard.reserve(admitted)
    claimed = guard.require_reservation(held, admitted, claim_dispatch=True)
    assert claimed is not None
    assert claimed.receipt.dispatches_used == 1
    assert claimed.receipt.held_usd == held.receipt.held_usd == Decimal(".49")
    reopened = BudgetGuard(guard.config_path, guard.spend_path)
    assert reopened.report()[0] == claimed.receipt
    with pytest.raises(BudgetRefused, match="consumed"):
        reopened.require_reservation(claimed, admitted, claim_dispatch=True)
    with pytest.raises(BudgetRefused, match="changed"):
        reopened.require_reservation(held, admitted, claim_dispatch=True)
    result = reopened.settle(claimed, status="cancelled", limits=admitted.limits)
    assert result.dispatches_used == 1
    assert result.actual_usd is None and result.held_usd == Decimal(".49")
    assert len(reopened.report()) == 1


@pytest.mark.parametrize(
    "damage", ["missing", "legacy", "cash", "leg", "model", "expired", "config"]
)
def test_fleet_missing_or_mismatched_binding_refuses_without_spend(
    tmp_path: Path, damage: str
) -> None:
    guard = setup_guard(tmp_path, providers=tuple(TEST_MODELS))
    admitted = fleet_plan(guard)
    raw = admitted.model_dump()
    assert raw["staging"] is not None
    if damage == "missing":
        raw.pop("staging")
    elif damage == "legacy":
        raw["h6"] = raw.pop("staging")
    elif damage == "model":
        raw["model"] = "gemini-3.5-flash-lite"
    else:
        field, value = {
            "cash": ("maximum_hold_usd", Decimal(".48")),
            "leg": ("leg_id", "ROOT"),
            "expired": ("deadline", datetime.now(UTC) - timedelta(seconds=1)),
            "config": ("config_sha256", "0" * 64),
        }[damage]
        raw["staging"][field] = value
    with pytest.raises((ValidationError, BudgetRefused)):
        guard.reserve(ProbePlan.model_validate(raw))
    assert not guard.report()


def test_fleet_root_requires_latest_claim_receipt_and_stops_at_three_inputs(tmp_path: Path) -> None:
    guard = setup_guard(tmp_path, providers=tuple(TEST_MODELS))
    admitted = fleet_plan(guard, "ROOT")
    held = guard.reserve(admitted)
    for index in range(3):
        claimed = guard.require_reservation(held, admitted, claim_dispatch=True)
        assert claimed is not None
        held = claimed
        assert held.receipt.dispatches_used == index + 1
    with pytest.raises(BudgetRefused, match="consumed"):
        guard.require_reservation(held, admitted, claim_dispatch=True)
    assert guard.report()[0].held_usd == Decimal(".06")


def test_fleet_concurrent_initial_dispatch_has_one_durable_claim(tmp_path: Path) -> None:
    guard = setup_guard(tmp_path, providers=tuple(TEST_MODELS))
    admitted = fleet_plan(guard)
    held = guard.reserve(admitted)

    def claim() -> bool:
        try:
            guard.require_reservation(held, admitted, claim_dispatch=True)
        except BudgetRefused:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(lambda _: claim(), range(2)))
    assert sorted(result) == [False, True]
    assert guard.report()[0].dispatches_used == 1
    assert guard.report()[0].held_usd == Decimal(".49")


def test_fleet_receipt_cannot_rewind_dispatch_counter(tmp_path: Path) -> None:
    guard = setup_guard(tmp_path, providers=tuple(TEST_MODELS))
    admitted = fleet_plan(guard)
    held = guard.reserve(admitted)
    claimed = guard.require_reservation(held, admitted, claim_dispatch=True)
    assert claimed is not None
    with (
        pytest.raises(BudgetLedgerError, match="dispatch claim"),
        guard._locked() as (file, lock, checkpoint, _, _),
    ):
        guard._append(file, held.receipt, checkpoint, checkpoint.opening_spend, lock)
    with pytest.raises(BudgetLedgerError, match="sequence"):
        guard.report()


@pytest.mark.parametrize("damage", ["source", "date", "room"])
def test_fleet_conditional_cash_still_requires_tariff_and_outer_room(
    tmp_path: Path, damage: str
) -> None:
    guard = setup_guard(tmp_path, providers=tuple(TEST_MODELS))
    admitted = fleet_plan(guard)
    data = json.loads(guard.config_path.read_text())
    price = data["providers"]["gemini"]["models"]["gemini-3.8-flash"]
    if damage == "source":
        price.pop("source")
    elif damage == "date":
        price.pop("effective_until")
    else:
        data["providers"]["gemini"]["cap_usd"] = ".5"
    guard.config_path.write_text(json.dumps(data))
    raw = admitted.model_dump()
    raw["staging"]["config_sha256"] = hashlib.sha256(guard.config_path.read_bytes()).hexdigest()
    with pytest.raises(BudgetRefused):
        guard.reserve(ProbePlan.model_validate(raw))
    assert not guard.report()


def test_fleet_flash_caps_quote_worst_case_and_keep_all_cash_terms(tmp_path):
    from mux.testing.conformance.budget import I7_TERMS, ModelPrice, TokenLimits, TokenUsage

    guard = setup_guard(tmp_path, providers=tuple(TEST_MODELS))
    admitted = fleet_plan(guard)
    assert admitted.model == "gemini-3.8-flash"
    assert {k: (v[0], v[2], v[3]) for k, v in I7_TERMS.items()} == {
        "ROOT": ("anthropic", Decimal(".06"), 3),
        "W1": ("anthropic", Decimal(".04"), 1),
        "W2": ("openai", Decimal(".055"), 1),
        "W3": ("gemini", Decimal(".49"), 1),
        "W4": ("openai", Decimal(".055"), 1),
        "RESTORE-SMOKE": ("anthropic", Decimal(".03"), 1),
        "CANDIDATE-SMOKE": ("anthropic", Decimal(".03"), 1),
    }
    price = ModelPrice(
        input=Decimal(".75"),
        output=Decimal("3.75"),
        cached_input=Decimal(".075"),
        cache_write_input=Decimal(".75"),
        effective_from=date(2026, 10, 15),
        effective_until=date(2026, 10, 16),
        source="https://ai.google.dev/gemini-api/docs/pricing",
    )
    limits = TokenLimits(input_tokens=262144, output_tokens=65536)
    assert price.reserve(limits) == Decimal(".442368") < I7_TERMS["W3"][2]
    usage = TokenUsage(
        input_tokens=262144,
        input_cached_tokens=200000,
        input_cache_write_tokens=0,
        output_tokens=65536,
    )
    assert price.actual(usage, datetime(2026, 10, 15, tzinfo=UTC)) == Decimal(".307368")
