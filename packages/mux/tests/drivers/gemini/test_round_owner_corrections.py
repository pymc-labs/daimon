"""Exact owner reproductions: all replay identities and coherent billing evidence."""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

import httpx
import pytest
from mux.contracts.ids import ResourceRef
from mux.drivers.gemini.core import GeminiEvents, GeminiSessions
from mux.drivers.gemini.transport import Object, SDKTransport
from mux.testing.conformance.budget import BudgetGuard, ProbePlan
from mux.testing.conformance.runner import ConformanceFailure
from mux.testing.drivers.gemini import round_cases
from mux.testing.drivers.gemini.live_cert import EvidenceAliases
from mux.testing.drivers.gemini.round_cases import case_ceiling, run_c04, run_c06
from mux.testing.drivers.gemini.round_runner import execute
from mux.testing.drivers.gemini.round_usage import UsageBook
from mux.testing.isolated_sdk import build_gemini_client

from .test_round_cases import SCOPE, NativeWire, admission


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["c04:crash:1", "c04:crash:2"])
async def test_c04_sent_as_queued_only_on_public_replay_fails(
    key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = GeminiEvents.send

    async def fabricated(self, scope, session, events, *, key: str, expected_turn=None):
        receipt = await original(self, scope, session, events, key=key, expected_turn=expected_turn)
        if key == target and receipt.status == "outcome_unknown":
            return receipt.model_copy(
                update={"status": "queued", "input_ids": ("fabricated",), "turn_id": "fabricated"}
            )
        return receipt

    target = key
    monkeypatch.setattr(GeminiEvents, "send", fabricated)
    native = NativeWire(initial="completed")
    with build_gemini_client(
        transport=httpx.MockTransport(native.handle), api_key="offline-round"
    ) as sdk:
        native.sdk = SDKTransport(sdk)
        try:
            with pytest.raises(ConformanceFailure, match="ambiguous intent"):
                await run_c04(native, SCOPE)
        finally:
            await sdk.aio.aclose()
    assert len(native.posts) <= 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["operation", "session", "input", "root", "status"])
async def test_c04_final_takeover_receipt_corruption_is_checked(
    change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = round_cases.recover_send
    calls = 0
    acknowledgements = 0

    async def corrupted(*args, **kwargs):
        nonlocal calls, acknowledgements
        result = await original(*args, **kwargs)
        calls += 1
        if kwargs["key"] == "c04:crash:3":
            acknowledgements += 1
        # Corrupt only the second acknowledgement for this key: final takeover,
        # not its initially validated recovery.
        if kwargs["key"] == "c04:crash:3" and acknowledgements == 2:
            updates = {
                "operation": {"operation_id": "foreign-operation"},
                "session": {"session": args[3].model_copy(update={"tenant_id": "foreign"})},
                "input": {"input_ids": ("foreign-input",)},
                "root": {"turn_id": "foreign-root"},
                "status": {"status": "processed"},
            }
            return dataclasses.replace(
                result, receipt=result.receipt.model_copy(update=updates[change])
            )
        return result

    monkeypatch.setattr(round_cases, "recover_send", corrupted)
    native = NativeWire(initial="completed")
    with build_gemini_client(
        transport=httpx.MockTransport(native.handle), api_key="offline-round"
    ) as sdk:
        native.sdk = SDKTransport(sdk)
        try:
            with pytest.raises(ConformanceFailure):
                await run_c04(native, SCOPE)
        finally:
            await sdk.aio.aclose()
    assert calls == 7 and acknowledgements == 2 and len(native.posts) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "accepted-session",
        "input-bytes",
        "final-session",
        "null-event-root",
        "foreign-event-root",
        "foreign-event-session",
        "preview-end",
        "child-end",
        "foreign-provenance",
    ],
)
async def test_c06_every_acceptance_occupancy_and_terminal_identity_is_joined(
    change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = NativeWire()
    if change == "accepted-session":
        original = GeminiEvents.send

        async def wrong(self, scope, session, events, *, key, expected_turn=None):
            receipt = await original(
                self, scope, session, events, key=key, expected_turn=expected_turn
            )
            return receipt.model_copy(
                update={"session": session.model_copy(update={"account_id": "foreign"})}
            )

        monkeypatch.setattr(GeminiEvents, "send", wrong)
    elif change == "input-bytes":
        original_check = native.verify_receipt

        def wrong_bytes(session, receipt, inputs):
            assert original_check(session, receipt, inputs)
            return False

        native.verify_receipt = wrong_bytes
    elif change == "final-session":
        original_list = GeminiSessions.list
        calls = 0

        async def foreign(self, scope, *, filters, page):
            nonlocal calls
            result = await original_list(self, scope, filters=filters, page=page)
            calls += 1
            if calls == 2:
                altered = result.data[0].model_copy(
                    update={"ref": result.data[0].ref.model_copy(update={"id": "foreign-session"})}
                )
                return result.model_copy(update={"data": (altered,)})
            return result

        monkeypatch.setattr(GeminiSessions, "list", foreign)
    else:
        original_events = GeminiEvents.list

        async def altered(self, scope, session, *, page):
            result = await original_events(self, scope, session, page=page)
            events = []
            for e in result.data:
                if e.type == "session.turn_ended":
                    changes = {
                        "null-event-root": {"turn_id": None},
                        "foreign-event-root": {"turn_id": "foreign-root"},
                        "foreign-event-session": {"session_id": "foreign-session"},
                        "preview-end": {"authority": "preview"},
                        "child-end": {"thread_id": "child"},
                        "foreign-provenance": {
                            "native": e.native.model_copy(update={"provider": "openai"})
                        },
                    }
                    e = e.model_copy(update=changes[change])
                events.append(e)
            return result.model_copy(update={"data": tuple(events)})

        monkeypatch.setattr(GeminiEvents, "list", altered)
    with build_gemini_client(
        transport=httpx.MockTransport(native.handle), api_key="offline-round"
    ) as sdk:
        native.sdk = SDKTransport(sdk)
        try:
            with pytest.raises(ConformanceFailure):
                await run_c06(native, SCOPE)
        finally:
            await sdk.aio.aclose()
    assert len(native.posts) == 1


def snapshot(stamp: str, **counts: int) -> Object:
    return {
        "id": "owned-input",
        "model": "gemini-3.8-flash",
        "status": "cancelled",
        "updated": stamp,
        "usageMetadata": {
            "promptTokenCount": 64,
            "cachedContentTokenCount": 0,
            "candidatesTokenCount": 8,
            "thoughtsTokenCount": 3,
            **counts,
        },
    }


@pytest.mark.parametrize(
    "category",
    ["promptTokenCount", "cachedContentTokenCount", "candidatesTokenCount", "thoughtsTokenCount"],
)
def test_stale_higher_category_stays_floor_only_and_cannot_release_hold(
    tmp_path: Path, category: str
) -> None:
    selected = admission(tmp_path, "C06")
    selected.output.mkdir()
    book = UsageBook(selected.output, EvidenceAliases(("offline-round",)))
    session = ResourceRef(
        id="local",
        kind="session",
        provider="gemini",
        account_scope_id="qa",
        tenant_id=SCOPE.tenant_id,
        account_id=SCOPE.account_id,
    )
    start = datetime.now(UTC)
    book.accept(snapshot(start.isoformat()), session, "0" * 64)
    book.observe(
        "owned-input", snapshot((start - timedelta(minutes=1)).isoformat(), **{category: 10000})
    )
    book.observe("owned-input", snapshot((start + timedelta(minutes=1)).isoformat()), final=True)
    actual, bounds = book.amounts(attempts=1)
    assert not actual.usage_complete and book.native["owned-input"].uncertain
    assert book.native["owned-input"].maxima[category] == 10000
    assert actual.requests == bounds.requests  # Floor evidence, not synthetic actual.
    guard = BudgetGuard(selected.config, tmp_path / "spend.md")
    reservation = guard.reserve(
        ProbePlan(
            provider="gemini",
            model="gemini-3.8-flash",
            fixture_id="C06",
            limits=selected.envelope.limits,
        )
    )
    receipt = guard.settle(
        reservation, status="completed", limits=selected.envelope.limits, actual=actual
    )
    assert receipt.actual_usd is None and receipt.held_usd == reservation.receipt.reserved_usd
    assert len(guard.report()) == 1
    assert receipt.actual_evidence is not None and len(receipt.actual_evidence.requests) == 1


@pytest.mark.parametrize("model", [None, "gemini-3.5-pro"])
def test_absent_or_conflicting_model_without_accepted_post_proof_is_unknown(
    tmp_path: Path, model: str | None
) -> None:
    selected = admission(tmp_path, "C06")
    selected.output.mkdir()
    book = UsageBook(selected.output, EvidenceAliases())
    session = ResourceRef(
        id="local",
        kind="session",
        provider="gemini",
        account_scope_id="qa",
        tenant_id=SCOPE.tenant_id,
        account_id=SCOPE.account_id,
    )
    start = datetime.now(UTC)
    raw = snapshot(start.isoformat())
    raw["model"] = model
    book.accept(raw, session, "0" * 64)
    raw["updated"] = (start + timedelta(minutes=1)).isoformat()
    book.observe("owned-input", raw, final=True)
    actual, _ = book.amounts(attempts=1)
    assert not actual.usage_complete
    guard = BudgetGuard(selected.config, tmp_path / "spend.md")
    reservation = guard.reserve(
        ProbePlan(
            provider="gemini",
            model="gemini-3.8-flash",
            fixture_id="C06",
            limits=selected.envelope.limits,
        )
    )
    result = guard.settle(
        reservation, status="completed", limits=selected.envelope.limits, actual=actual
    )
    assert result.actual_usd is None and result.held_usd == reservation.receipt.reserved_usd


def test_import_and_case_bound_validation_ignore_ambient_low_decimal_precision(
    tmp_path: Path,
) -> None:
    code = """from decimal import getcontext, Decimal
getcontext().prec=6
from mux.testing.drivers.gemini.round_cases import INTERACTION_CEILING_USD, case_ceiling
if INTERACTION_CEILING_USD != Decimal("1.032192") or case_ceiling(3) != Decimal("3.096576"):
    raise RuntimeError("signed bound changed")
print("exact bounds retained")
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0 and "exact bounds retained" in result.stdout
    selected = admission(tmp_path, "C04")
    with localcontext() as context:
        context.prec = 6
        assert case_ceiling(3) == Decimal("3.096576")
        from mux.testing.drivers.gemini.round_runner import Envelope

        envelope = Envelope.model_validate_json(selected.envelope.model_dump_json())
        guard = BudgetGuard(selected.config, tmp_path / "spend.md")
        reservation = guard.reserve_with_hold(
            ProbePlan(
                provider="gemini",
                model="gemini-3.8-flash",
                fixture_id="C04",
                limits=envelope.limits,
            ),
            minimum_hold_usd=envelope.maximum_usd,
            maximum_hold_usd=envelope.maximum_usd,
        )
    assert reservation.receipt.reserved_usd == Decimal("3.096576")


@pytest.mark.asyncio
async def test_last_c04_single_interaction_overrun_fails_under_aggregate_ceiling(
    tmp_path: Path,
) -> None:
    native = NativeWire(initial="completed")
    original = native.handle

    def over_single(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.method == "DELETE" or "application/json" not in response.headers.get(
            "content-type", ""
        ):
            return response
        raw = response.json()
        if raw.get("id") == "native-owned-3":
            raw["usage"]["total_input_tokens"] = 1_100_000
        return httpx.Response(200, json=raw)

    selected = admission(tmp_path, "C04")
    report = await execute(selected, mock=httpx.MockTransport(over_single))
    assert report["status"] == "fail" and len(native.posts) == 3
    receipt = BudgetGuard(selected.config, tmp_path / "spend.md").report()[0]
    assert not receipt.admission_blocked and receipt.status != "overrun"
    assert receipt.actual_usd is not None and receipt.held_usd == 0
