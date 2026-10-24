"""Budgeted orchestration with local callbacks and normalized evidence only."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import get_args

import httpx
import pytest
from mux.testing.conformance.budget import (
    H6_MODELS,
    ActualSpend,
    BudgetLedgerError,
    BudgetRefused,
    MeasuredRequest,
    ProbePlan,
    TokenUsage,
)
from mux.testing.conformance.live_probe import (
    H6EvidenceKind,
    H6Receipt,
    H6TurnReceipt,
    ProbeOutcome,
    ProbeRunError,
    run_probe,
)
from mux.testing.conformance.recording import Recorder, RecordingError, RequestMetadata
from mux.testing.conformance.runner import Result
from pydantic import ValidationError

from packages.mux.tests.testing.conformance.test_budget import (
    plan,
    receipts,
    setup_guard,
    staging_plan,
)


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
    from packages.mux.tests.testing.conformance.test_recording import delta

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


def known_staging_spend(admitted: ProbePlan) -> ActualSpend:
    return ActualSpend(
        usage_complete=True,
        containers=(),
        requests=(
            MeasuredRequest(
                id="offline-request",
                observed_at=datetime.now(UTC),
                pricing_basis="standard-global",
                tokens=TokenUsage(
                    input_tokens=10,
                    output_tokens=1,
                    input_cached_tokens=0,
                    input_cache_write_tokens=0,
                ),
            ),
        ),
    )


def cell_proof(fixture_id: str = "H6-OPENAI") -> H6Receipt:
    target = fixture_id.removeprefix("H6-").lower()
    predecessor = {"anthropic": "gemini", "openai": "anthropic", "gemini": "openai"}[target]
    turns = []
    for index, provider, slot, suffix in zip(
        range(5),
        (predecessor, target, predecessor, predecessor, target),
        (0, 1, 0, 2, 1),
        ("baseline", "target", "retained-before-revert", "reverted-new", "retained-after-revert"),
        strict=True,
    ):
        digest = hashlib.sha256(f"H6-offline-attempt-{suffix}".encode()).hexdigest()
        turns.append(
            H6TurnReceipt(
                provider=provider,
                model=H6_MODELS[provider],
                thread_id=f"thread-{slot}",
                session_id=f"session-{slot}",
                binding_id=f"binding-{slot}",
                root_id=f"root-{index}",
                input_id=f"input-{index}",
                message_ids=(f"message-{index}",),
                footer_provider=provider,
                footer_model=H6_MODELS[provider],
                native_reply_sha256=digest,
                visible_reply_sha256=digest,
            )
        )
    return H6Receipt(
        fixture_id=fixture_id,
        attempt_id="offline-attempt",
        scope_sha256="f" * 64,
        turns=tuple(turns),
        evidence=tuple((kind, "a" * 64) for kind in get_args(H6EvidenceKind)),
    )


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
async def test_staging_real_transport_edge_keeps_partial_cell_pending(
    tmp_path: Path, provider: str
) -> None:
    guard = setup_guard(tmp_path, providers=tuple(H6_MODELS))
    admitted = staging_plan(guard, provider=provider, fixture_id=f"H6-{provider.upper()}")
    calls = []

    def wire(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"id": "upstream", "status": "completed"})

    async def invoke(recorder: Recorder) -> ProbeOutcome:
        async with httpx.AsyncClient(transport=recorder, trust_env=False) as client:
            response = await client.post(
                "https://offline.invalid/v1/messages",
                json={"model": admitted.model},
                headers={"authorization": "Bearer offline-canary"},
            )
            assert response.status_code == 200
        return ProbeOutcome(
            result=Result(admitted.fixture_id, "pass", ("partial turn",)),
            actual=known_staging_spend(admitted),
        )

    result = await run_probe(
        guard, admitted, tmp_path / "edge.json", invoke, transport=httpx.MockTransport(wire)
    )
    assert calls == ["/v1/messages"] and result.result.status == "pending"
    assert result.receipt.accounting_status == "actual" and result.receipt.held_usd == 0
    tape = json.loads(result.recording.read_text())
    assert tape["fixture_id"] == admitted.fixture_id and tape["provider"] == provider
    assert len(tape["request_edges"]) == 1 and "offline-canary" not in result.recording.read_text()


@pytest.mark.parametrize(
    "damage",
    [
        "missing_transport",
        "bypass_transport",
        "deadline",
        "config",
        "released",
        "foreign",
        "origin",
        "route",
        "duplicate_write",
        "count",
        "userinfo",
        "fragment",
    ],
)
async def test_staging_guard_refuses_before_next_underlying_io(tmp_path: Path, damage: str) -> None:
    guard = setup_guard(tmp_path)
    admitted = staging_plan(guard)
    calls = []

    def wire(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"id": "upstream"})

    async def invoke(recorder: Recorder) -> ProbeOutcome:
        if damage == "bypass_transport":
            recorder.record(RequestMetadata(method="POST", path="/v1/messages"), ())
            return ProbeOutcome(result=Result(admitted.fixture_id, "pass", ("unbound",)))
        if damage == "config":
            data = json.loads(guard.config_path.read_text())
            data["providers"]["openai"]["cap_usd"] = "9"
            guard.config_path.write_text(json.dumps(data))
        if damage == "released":
            row = guard.report()[-1]
            from mux.testing.conformance.budget import Reservation

            guard.settle(
                Reservation(receipt=row, price=row.price),
                status="completed",
                limits=admitted.limits,
            )
        async with httpx.AsyncClient(transport=recorder, trust_env=False) as client:
            if damage in ("duplicate_write", "count"):
                await client.post("https://offline.invalid/v1/messages")
            url = {
                "origin": "https://foreign.invalid/v1/messages",
                "route": "https://offline.invalid/foreign",
                "userinfo": "https://user:secret@offline.invalid/v1/messages",
                "fragment": "https://offline.invalid/v1/messages#secret",
            }.get(damage, "https://offline.invalid/v1/messages")
            await client.post(url)
        return ProbeOutcome(result=Result(admitted.fixture_id, "pass", ()))

    if damage == "deadline":
        admitted = ProbePlan.model_validate(
            {
                **admitted.model_dump(),
                "h6": {
                    **admitted.h6.model_dump(),
                    "deadline": datetime.now(UTC) - timedelta(seconds=1),
                },
            }
        )
    if damage == "foreign":
        admitted = ProbePlan.model_validate(
            {**admitted.model_dump(), "h6": {**admitted.h6.model_dump(), "ledger_id": "0" * 32}}
        )
    with pytest.raises((ProbeRunError, BudgetRefused, BudgetLedgerError)):
        await run_probe(
            guard,
            admitted,
            tmp_path / "refused.json",
            invoke,
            transport=None if damage == "missing_transport" else httpx.MockTransport(wire),
        )
    assert len(calls) == (1 if damage in ("duplicate_write", "count") else 0)
    assert all(row.actual_usd is None for row in receipts(guard) if row.status != "blocked")


@pytest.mark.parametrize("fixture_id", ["H6-ANTHROPIC", "H6-OPENAI", "H6-GEMINI"])
def test_staging_whole_cell_shape_json_roundtrip(fixture_id: str) -> None:
    proof = cell_proof(fixture_id)
    assert H6Receipt.model_validate_json(proof.model_dump_json()) == proof


@pytest.mark.parametrize(
    "damage",
    [
        "missing_turn",
        "scope",
        "missing_reference",
        "duplicate_reference",
        "footer",
        "model",
        "native_reply",
        "visible_reply",
        "retained_thread",
        "retained_session",
        "retained_binding",
        "fresh_thread",
        "fresh_session",
        "root",
        "input",
        "message",
    ],
)
def test_staging_shape_rejects_incomplete_or_foreign_native_visible_join(damage: str) -> None:
    raw = cell_proof().model_dump()
    if damage == "missing_turn":
        raw["turns"] = raw["turns"][:-1]
    elif damage == "scope":
        raw["scope_sha256"] = "unbound"
    elif damage == "missing_reference":
        raw["evidence"] = raw["evidence"][:-1]
    elif damage == "duplicate_reference":
        raw["evidence"] = (*raw["evidence"], raw["evidence"][0])
    else:
        field = {
            "footer": "footer_provider",
            "model": "model",
            "native_reply": "native_reply_sha256",
            "visible_reply": "visible_reply_sha256",
            "retained_thread": "thread_id",
            "retained_session": "session_id",
            "retained_binding": "binding_id",
            "fresh_thread": "thread_id",
            "fresh_session": "session_id",
            "root": "root_id",
            "input": "input_id",
            "message": "message_ids",
        }[damage]
        index = 3 if damage.startswith("fresh_") else 2 if damage.startswith("retained_") else 1
        raw["turns"][index][field] = (
            raw["turns"][0][field]
            if damage in ("fresh_thread", "fresh_session", "root", "input", "message")
            else ("0" * 64 if "reply" in damage else "foreign")
        )
    with pytest.raises(ValidationError):
        H6Receipt.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize(
    "binding", ["absent", "reader", "false", "foreign", "unknown", "fail", "cancelled"]
)
async def test_staging_complete_schema_needs_bound_artifact_reader_and_actual_spend(
    tmp_path: Path, binding: str
) -> None:
    # This drives a mock request and proves the harness contract, never staging.
    guard = setup_guard(tmp_path)
    admitted = staging_plan(guard)
    proof = cell_proof()
    seen = []

    def verifier(value: H6Receipt) -> None:
        seen.append(value)
        if binding == "false":
            return False
        assert value == H6Receipt.model_validate_json(proof.model_dump_json())

    async def invoke(recorder: Recorder) -> ProbeOutcome:
        async with httpx.AsyncClient(transport=recorder, trust_env=False) as client:
            await client.post("https://offline.invalid/v1/messages")
        if binding == "cancelled":
            raise asyncio.CancelledError
        raw = proof.model_dump()
        if binding == "foreign":
            raw["scope_sha256"] = "0" * 64
        return ProbeOutcome(
            result=Result(
                admitted.fixture_id, "fail" if binding == "fail" else "pass", ("offline contract",)
            ),
            actual=None if binding == "unknown" else known_staging_spend(admitted),
            h6=H6Receipt.model_validate(raw),
        )

    command = run_probe(
        guard,
        admitted,
        tmp_path / "proof.json",
        invoke,
        transport=httpx.MockTransport(lambda _: httpx.Response(200)),
        verify_h6=None if binding == "absent" else verifier,
    )
    if binding in ("false", "foreign", "cancelled"):
        with pytest.raises(asyncio.CancelledError if binding == "cancelled" else ProbeRunError):
            await command
        assert receipts(guard)[-1].status in ("failed", "cancelled")
    else:
        result = await command
        assert result.result.status == (
            "pass" if binding == "reader" else "fail" if binding == "fail" else "pending"
        )
        assert bool(seen) is (binding != "absent")
        if binding == "fail":
            assert result.receipt.status == "failed"
        if binding == "unknown":
            assert (
                result.receipt.actual_usd is None
                and result.receipt.held_usd == admitted.h6.maximum_hold_usd
            )
    assert len(receipts(guard)) == 2


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
@pytest.mark.parametrize("tokens", [0, 1_000])
@pytest.mark.parametrize("spend", ["absent", "partial", "complete"])
async def test_staging_runner_releases_cash_only_on_complete_actual(
    tmp_path: Path, provider: str, tokens: int, spend: str
) -> None:
    guard = setup_guard(tmp_path, "2", providers=("anthropic", "openai", "gemini"))
    admitted = staging_plan(guard, provider=provider, fixture_id=f"H6-{provider.upper()}")
    proof = cell_proof(admitted.fixture_id)

    def verifier(value: H6Receipt) -> None:
        assert value == H6Receipt.model_validate_json(proof.model_dump_json())

    async def invoke(recorder: Recorder) -> ProbeOutcome:
        async with httpx.AsyncClient(transport=recorder, trust_env=False) as client:
            await client.post("https://offline.invalid/v1/messages")
        actual = None if spend == "absent" else known_staging_spend(admitted)
        if spend == "partial":
            actual = actual.model_copy(update={"usage_complete": False})
        return ProbeOutcome(
            usage=TokenUsage(
                input_tokens=tokens,
                output_tokens=0,
                input_cached_tokens=0,
                input_cache_write_tokens=0,
            ),
            actual=actual,
            result=Result(admitted.fixture_id, "pass", ("offline contract",)),
            h6=proof,
        )

    result = await run_probe(
        guard,
        admitted,
        tmp_path / "settlement.json",
        invoke,
        transport=httpx.MockTransport(lambda _: httpx.Response(200)),
        verify_h6=verifier,
    )
    receipt = result.receipt
    if spend == "complete":
        assert result.result.status == "pass"
        assert receipt.accounting_status == "actual" and receipt.status == "completed"
        assert receipt.actual_usd == Decimal("0.000011") and receipt.held_usd == 0
        guard.reserve(plan(provider=provider))
    else:
        assert result.result.status == "pending"
        assert receipt.status == "uncertain" and receipt.reason == "unknown_usage"
        assert receipt.actual_usd is None and receipt.accounting_status == "estimated_unverified"
        assert receipt.held_usd == receipt.cost_estimate_usd == receipt.reserved_usd == 1
        with pytest.raises(BudgetRefused):
            guard.reserve(plan(provider=provider))
