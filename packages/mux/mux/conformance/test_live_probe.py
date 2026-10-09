"""Budgeted orchestration with local callbacks and normalized evidence only."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest

from mux.conformance.budget import BudgetRefused, TokenUsage
from mux.conformance.live_probe import ProbeOutcome, ProbeRunError, run_probe
from mux.conformance.recording import Recorder, RecordingError, RequestMetadata
from mux.conformance.runner import Result
from mux.conformance.test_budget import plan, receipts, setup_guard


async def outcome(recorder: Recorder, tokens: int = 250_000) -> ProbeOutcome:
    recorder.record(RequestMetadata(method="GET", path="/events"), ())
    return ProbeOutcome(
        usage=TokenUsage(
            input_tokens=tokens, output_tokens=0, input_cached_tokens=0, input_cache_write_tokens=0
        ),
        result=Result("C16", "pass", ("fake probe",)),
    )


async def test_completed_usage_settles_once_and_releases_only_known_savings(tmp_path: Path) -> None:
    guard = setup_guard(tmp_path, "2")
    result = await run_probe(guard, plan(), tmp_path / "one.json", outcome)
    assert result.receipt.cost_estimate_usd == Decimal("0.25")
    assert [r.status for r in receipts(guard)] == ["reserved", "completed"]
    guard.reserve(plan())  # 0.25 + 1 <= 1.6
    with pytest.raises(BudgetRefused):
        guard.reserve(plan())
    other = guard.reserve(plan(provider="anthropic"))
    assert other.receipt.provider == "anthropic"


async def test_refused_run_never_invokes_callback_or_creates_tape(tmp_path: Path) -> None:
    guard = setup_guard(tmp_path, "1")
    calls = 0

    async def forbidden(recorder: Recorder) -> ProbeOutcome:
        nonlocal calls
        calls += 1
        return await outcome(recorder)

    with pytest.raises(BudgetRefused):
        await run_probe(guard, plan(), tmp_path / "no.json", forbidden)
    assert calls == 0 and not (tmp_path / "no.json").exists()
    assert receipts(guard)[0].status == "blocked"


async def test_failure_retains_reservation_and_never_serializes_exception_text(
    tmp_path: Path,
) -> None:
    guard = setup_guard(tmp_path)

    async def fail(recorder: Recorder) -> ProbeOutcome:
        raise RuntimeError("super-secret-key")

    with pytest.raises(ProbeRunError, match="conservative estimate retained"):
        await run_probe(guard, plan(), tmp_path / "failed.json", fail)
    assert receipts(guard)[-1].status == "failed"
    assert receipts(guard)[-1].cost_estimate_usd == 1
    assert (
        "super-secret" not in guard.spend_path.read_text() + (tmp_path / "failed.json").read_text()
    )


async def test_cancellation_has_receipt_and_incomplete_tape(tmp_path: Path) -> None:
    guard = setup_guard(tmp_path)

    async def cancel(recorder: Recorder) -> ProbeOutcome:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_probe(guard, plan(), tmp_path / "cancelled.json", cancel)
    assert receipts(guard)[-1].status == "cancelled"
    assert not json.loads((tmp_path / "cancelled.json").read_text())["complete"]


async def test_null_usage_is_not_a_zero_cost_refund(tmp_path: Path) -> None:
    guard = setup_guard(tmp_path)

    async def unknown(recorder: Recorder) -> ProbeOutcome:
        return ProbeOutcome(usage=TokenUsage(), result=Result("C16", "pending", ("unknown",)))

    result = await run_probe(guard, plan(), tmp_path / "unknown.json", unknown)
    assert result.receipt.status == "uncertain" and result.receipt.cost_estimate_usd == 1
    assert result.receipt.tokens is not None and result.receipt.tokens.input_tokens is None


@pytest.mark.parametrize("output", [0, None])
@pytest.mark.parametrize(
    "bucket", ["input_tokens", "input_cached_tokens", "input_cache_write_tokens", "cache_subsets"]
)
async def test_overrun_is_recorded_and_blocks_even_zero_cost_future_probe(
    tmp_path: Path, output: int | None, bucket: str
) -> None:
    guard = setup_guard(tmp_path)

    async def too_much(recorder: Recorder) -> ProbeOutcome:
        counts = (
            {"input_cached_tokens": 1_000_000, "input_cache_write_tokens": 1_000_000}
            if bucket == "cache_subsets"
            else {bucket: 2_000_000}
        )
        return ProbeOutcome(
            usage=TokenUsage.model_validate({**counts, "output_tokens": output}),
            result=Result("C16", "pass", ("fake",)),
        )

    with pytest.raises(ProbeRunError, match="exceeded its reservation"):
        await run_probe(guard, plan(), tmp_path / "overrun.json", too_much)
    assert receipts(guard)[-1].status == "overrun"
    assert receipts(guard)[-1].cost_estimate_usd == 2
    with pytest.raises(BudgetRefused):
        guard.reserve(plan(0))


async def test_existing_fixture_refuses_before_callback(tmp_path: Path) -> None:
    guard = setup_guard(tmp_path)
    path = tmp_path / "existing.json"
    path.write_text("immutable artifact")
    calls = 0

    async def forbidden(recorder: Recorder) -> ProbeOutcome:
        nonlocal calls
        calls += 1
        return await outcome(recorder)

    with pytest.raises(BudgetRefused):
        await run_probe(guard, plan(), path, forbidden)
    assert calls == 0 and path.read_text() == "immutable artifact"
    assert receipts(guard)[-1].reason == "fixture_exists"


async def test_recording_failure_cannot_erase_spend_receipt(tmp_path: Path) -> None:
    guard = setup_guard(tmp_path)
    directory = tmp_path / "recordings"
    directory.mkdir()

    async def lose_directory(recorder: Recorder) -> ProbeOutcome:
        result = await outcome(recorder)
        directory.rmdir()
        return result

    with pytest.raises(FileNotFoundError):
        await run_probe(guard, plan(), directory / "tape.json", lose_directory)
    assert receipts(guard)[-1].status == "completed"
    assert receipts(guard)[-1].cost_estimate_usd == Decimal("0.25")


async def test_missing_output_directory_refuses_before_callback(tmp_path: Path) -> None:
    guard = setup_guard(tmp_path)
    calls = 0

    async def forbidden(recorder: Recorder) -> ProbeOutcome:
        nonlocal calls
        calls += 1
        return await outcome(recorder)

    with pytest.raises(ProbeRunError, match="directory must already exist"):
        await run_probe(guard, plan(), tmp_path / "missing" / "tape.json", forbidden)
    assert calls == 0 and receipts(guard) == []


async def test_unsafe_normalized_event_refuses_export_but_keeps_receipt(tmp_path: Path) -> None:
    from mux.conformance.test_recording import delta

    guard = setup_guard(tmp_path)
    path = tmp_path / "unsafe.json"

    async def unsafe(recorder: Recorder) -> ProbeOutcome:
        recorder.record(RequestMetadata(method="GET", path="/events"), (delta("sk-" + "a" * 32),))
        return await outcome(recorder)

    with pytest.raises(RecordingError):
        await run_probe(guard, plan(), path, unsafe)
    assert receipts(guard)[-1].status == "failed"
    assert receipts(guard)[-1].cost_estimate_usd == 1
    assert not path.exists()
