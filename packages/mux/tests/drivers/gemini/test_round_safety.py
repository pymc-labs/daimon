"""Financial facts, refusal and wrong-native-evidence controls for the live round."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from mux.testing.conformance.budget import (
    BudgetConfig,
    BudgetGuard,
    BudgetRefused,
    ProbePlan,
    TokenLimits,
)
from mux.testing.drivers.gemini.round_cases import INPUT_MAX, OUTPUT_MAX
from mux.testing.drivers.gemini.round_runner import admit, execute
from pydantic import ValidationError

from .test_round_cases import SCOPE, NativeWire, admission


class FaultyFinal(NativeWire):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode, self.gets = mode, 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        response = super().handle(request)
        if request.method == "GET" and request.url.params.get("stream") != "true":
            self.gets += 1
            if self.gets == 2:
                if self.mode == "get-429":
                    return httpx.Response(429, json={"error": {"message": "offline"}})
                raw = response.json()
                if self.mode == "decreasing":
                    raw["usage"]["total_output_tokens"] = 1
                elif self.mode == "missing-cache":
                    raw["usage"]["total_cached_tokens"] = None
                elif self.mode == "boolean-cache":
                    raw["usage"]["total_cached_tokens"] = False
                elif self.mode == "foreign-id":
                    raw["id"] = "foreign-input"
                elif self.mode == "foreign-model":
                    raw["model"] = "gemini-3.5-pro"
                elif self.mode == "stale":
                    raw["updated"] = (self.created - timedelta(days=1)).isoformat()
                return httpx.Response(200, json=raw)
        return response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "decreasing",
        "missing-cache",
        "boolean-cache",
        "foreign-id",
        "foreign-model",
        "stale",
        "get-429",
    ],
)
async def test_invalid_final_usage_keeps_known_counts_full_hold_one_fetch_and_cleanup(
    tmp_path: Path, mode: str
) -> None:
    native = FaultyFinal(mode)
    selected = admission(tmp_path, "C06")
    result = await execute(selected, mock=httpx.MockTransport(native.handle))
    assert result["status"] == ("fail" if mode == "foreign-model" else "pass")
    assert result["actual_usd"] is None and result["held_usd"] == "1.032192"
    assert native.gets == 2 and len(native.deletes) == 1 and len(native.posts) == 1
    receipt = BudgetGuard(selected.config, tmp_path / "spend.md").report()[0]
    assert receipt.tokens is not None and receipt.tokens.input_tokens == 64
    assert receipt.tokens.output_tokens == 11
    assert receipt.accounting_status == "estimated_unverified"
    facts = result["interactions"]
    assert isinstance(facts, list) and facts[0]["complete"] is False


class OverrunThenDecrease(FaultyFinal):
    def handle(self, request: httpx.Request) -> httpx.Response:
        response = super().handle(request)
        if (
            response.headers.get("content-type") == "application/json"
            and request.method != "DELETE"
        ):
            raw = response.json()
            raw["usage"]["total_input_tokens"] = 2_000_000 if self.gets < 2 else 64
            return httpx.Response(200, json=raw)
        return response


@pytest.mark.asyncio
async def test_overrun_cannot_be_hidden_by_newer_decreasing_usage_and_blocks_next_reserve(
    tmp_path: Path,
) -> None:
    native = OverrunThenDecrease("decreasing")
    selected = admission(tmp_path, "C06")
    result = await execute(selected, mock=httpx.MockTransport(native.handle))
    assert result["status"] == "fail" and result["actual_usd"] is None
    guard = BudgetGuard(selected.config, tmp_path / "spend.md")
    receipt = guard.report()[0]
    assert receipt.status == "overrun" and receipt.admission_blocked
    assert receipt.tokens is not None and receipt.tokens.input_tokens == 2_000_000
    assert receipt.held_usd >= receipt.reserved_usd
    with pytest.raises(BudgetRefused):
        guard.reserve(
            ProbePlan(
                provider="gemini",
                model="gemini-3.8-flash",
                fixture_id="C06",
                limits=TokenLimits(input_tokens=INPUT_MAX, output_tokens=OUTPUT_MAX),
            )
        )
    assert len(native.posts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["acceptance", "intermediate", "final"])
@pytest.mark.parametrize("fault", ["bad-cached", "bad-thoughts", "missing-candidates"])
@pytest.mark.parametrize("meter", ["usage", "usageMetadata"])
async def test_mixed_counter_overrun_retains_valid_floors_and_blocks_next_reserve(
    tmp_path: Path, phase: str, fault: str, meter: str
) -> None:
    """Reviewer reproduction, including both names and all pre-decode boundaries."""
    native = NativeWire()
    gets = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal gets
        response = native.handle(request)
        if request.method == "GET" and request.url.params.get("stream") != "true":
            gets += 1
        selected = (
            phase == "acceptance"
            and request.method == "POST"
            and request.url.path.endswith("/interactions")
            or phase == "intermediate"
            and request.method == "GET"
            and gets == 1
            or phase == "final"
            and request.method == "GET"
            and gets == 2
        )
        if not selected or response.headers.get("content-type") != "application/json":
            return response
        raw = response.json()
        counts = raw["usage"]
        counts["total_input_tokens"] = 2_000_000
        if fault == "bad-cached":
            counts["total_cached_tokens"] = False
        elif fault == "bad-thoughts":
            counts["total_thought_tokens"] = "invalid"
        else:
            del counts["total_output_tokens"]
        if meter == "usageMetadata":
            raw["usageMetadata"] = {
                public: counts[native_name]
                for public, native_name in (
                    ("promptTokenCount", "total_input_tokens"),
                    ("cachedContentTokenCount", "total_cached_tokens"),
                    ("candidatesTokenCount", "total_output_tokens"),
                    ("thoughtsTokenCount", "total_thought_tokens"),
                )
                if native_name in counts
            }
        return httpx.Response(200, json=raw)

    selected = admission(tmp_path, "C06")
    result = await execute(selected, mock=httpx.MockTransport(handle))
    assert result["status"] == "fail" and result["actual_usd"] is None
    guard = BudgetGuard(selected.config, tmp_path / "spend.md")
    receipt = guard.report()[0]
    assert receipt.status == "overrun" and receipt.admission_blocked
    assert receipt.tokens is not None and receipt.tokens.input_tokens == 2_000_000
    assert receipt.tokens.output_tokens >= (3 if fault == "missing-candidates" else 8)
    assert receipt.held_usd >= receipt.reserved_usd
    if phase == "final" and fault == "bad-cached":
        # The earlier cached zero is a floor, not an exact split for this
        # newer prompt. Token overrun latches without inventing uncached cost.
        floor = receipt.actual_evidence.requests[0]
        assert floor.tokens.input_cache_write_tokens is None
        price = BudgetConfig.load(selected.config).providers["gemini"].models["gemini-3.8-flash"]
        assert price.minimum_charge(floor.tokens, floor.observed_at) == Decimal("0.15004125")
        # BudgetGuard may independently expand the conservative hard hold;
        # that estimate is not a claim of proven uncached actual charges.
    facts = result["interactions"]
    assert facts[0]["max_seen"]["promptTokenCount"] == 2_000_000
    assert facts[0]["complete"] is False
    if fault != "missing-candidates":
        assert facts[0]["tainted_counters"] == [
            "cachedContentTokenCount" if fault == "bad-cached" else "thoughtsTokenCount"
        ]
    with pytest.raises(BudgetRefused):
        guard.reserve(
            ProbePlan(
                provider="gemini",
                model="gemini-3.8-flash",
                fixture_id="C06",
                limits=selected.envelope.limits,
            )
        )
    assert len(native.posts) == 1 and len(native.deletes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [False, -1, "invalid", {}])
async def test_valid_output_overrun_survives_malformed_thoughts_even_sdk_rejection(
    tmp_path: Path, malformed: object
) -> None:
    native = NativeWire()
    gets = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal gets
        response = native.handle(request)
        if request.method == "GET" and request.url.params.get("stream") != "true":
            gets += 1
            if gets == 2:
                raw = response.json()
                raw["usage"]["total_output_tokens"] = 70_000
                raw["usage"]["total_thought_tokens"] = malformed
                return httpx.Response(200, json=raw)
        return response

    selected = admission(tmp_path, "C06")
    result = await execute(selected, mock=httpx.MockTransport(handle))
    receipt = BudgetGuard(selected.config, tmp_path / "spend.md").report()[0]
    assert result["status"] == "fail" and result["actual_usd"] is None
    assert receipt.tokens.output_tokens == 70_000
    assert receipt.status == "overrun" and receipt.admission_blocked
    assert result["interactions"][0]["tainted_counters"] == ["thoughtsTokenCount"]
    assert gets == 2 and len(native.posts) == len(native.deletes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "valid,bad,count,public_bad",
    [
        ("total_cached_tokens", "total_input_tokens", 2_000_000, "promptTokenCount"),
        ("total_thought_tokens", "total_output_tokens", 70_000, "candidatesTokenCount"),
    ],
)
async def test_cached_and_thought_overrun_floors_survive_malformed_siblings(
    tmp_path: Path, valid: str, bad: str, count: int, public_bad: str
) -> None:
    native = NativeWire()
    gets = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal gets
        response = native.handle(request)
        if request.method == "GET" and request.url.params.get("stream") != "true":
            gets += 1
            if gets == 2:
                raw = response.json()
                raw["usage"][valid], raw["usage"][bad] = count, False
                return httpx.Response(200, json=raw)
        return response

    selected = admission(tmp_path, "C06")
    result = await execute(selected, mock=httpx.MockTransport(handle))
    guard = BudgetGuard(selected.config, tmp_path / "spend.md")
    receipt = guard.report()[0]
    assert result["status"] == "fail" and result["actual_usd"] is None
    assert receipt.status == "overrun" and receipt.admission_blocked
    assert (
        receipt.tokens.minimum_input_tokens if "cached" in valid else receipt.tokens.output_tokens
    ) == count
    assert result["interactions"][0]["tainted_counters"] == [public_bad]
    with pytest.raises(BudgetRefused):
        guard.reserve(
            ProbePlan(
                provider="gemini",
                model="gemini-3.8-flash",
                fixture_id="C06",
                limits=selected.envelope.limits,
            )
        )
    assert gets == 2 and len(native.posts) == len(native.deletes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", [64, 2_000_000])
async def test_sdk_identity_disagreement_keeps_http_facts_but_taints_exactness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prompt: int
) -> None:
    from mux.drivers.gemini.transport import SDKTransport

    native = NativeWire()
    gets = 0
    original = SDKTransport.get

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal gets
        response = native.handle(request)
        if request.method == "GET" and request.url.params.get("stream") != "true":
            gets += 1
            if gets == 2:
                raw = response.json()
                raw["usage"]["total_input_tokens"] = prompt
                return httpx.Response(200, json=raw)
        return response

    async def corrupted_sdk(self, interaction_id):
        raw = await original(self, interaction_id)
        return {**raw, "id": "foreign-sdk-id"} if gets == 2 else raw

    monkeypatch.setattr(SDKTransport, "get", corrupted_sdk)
    selected = admission(tmp_path, "C06")
    result = await execute(selected, mock=httpx.MockTransport(handle))
    receipt = BudgetGuard(selected.config, tmp_path / "spend.md").report()[0]
    assert result["actual_usd"] is None
    assert receipt.tokens.input_tokens == prompt
    assert receipt.status == ("overrun" if prompt > INPUT_MAX else "uncertain")
    assert receipt.admission_blocked == (prompt > INPUT_MAX)
    assert result["interactions"][0]["complete"] is False
    assert gets == 2 and len(native.posts) == len(native.deletes) == 1


@pytest.mark.asyncio
async def test_503_stops_after_one_primary_without_fallback_or_free_actual(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def refused(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, json={"error": {"message": "offline overloaded"}})

    selected = admission(tmp_path, "C06")
    result = await execute(selected, mock=httpx.MockTransport(refused))
    assert len(requests) == 1 and requests[0].method == "POST"
    assert json.loads(requests[0].content)["agent_config"]["model"] == "gemini-3.8-flash"
    assert result["status"] == "fail" and result["actual_usd"] is None
    assert result["held_usd"] == "1.032192"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"model": "gemini-3.5-pro"},
        {"interactions": 2},
        {"tools": [{"type": "google_search"}]},
        {"runtime_max_usd": "1"},
        {"maximum_usd": "0.147456"},
    ],
)
async def test_mutated_envelope_refused_before_sdk_or_key(
    tmp_path: Path, override: dict[str, object]
) -> None:
    from dataclasses import replace

    selected = admission(tmp_path, "C06")
    selected = replace(selected, envelope=selected.envelope.model_copy(update=override))

    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no native request authorized")

    with pytest.raises(ValidationError):
        await execute(selected, mock=httpx.MockTransport(forbidden))
    assert not selected.output.exists()


@pytest.mark.asyncio
async def test_n8_stop_refuses_c04_if_prior_c06_hold_remains(tmp_path: Path) -> None:
    first = admission(tmp_path, "C06")
    requests: list[httpx.Request] = []

    def refused(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, json={"error": {"message": "offline overloaded"}})

    await execute(first, mock=httpx.MockTransport(refused))
    from dataclasses import replace

    second = replace(
        first,
        envelope=first.envelope.model_copy(
            update={"case": "C04", "interactions": 3, "maximum_usd": first.envelope.maximum_usd * 3}
        ),
        output=tmp_path / "C04",
    )
    with pytest.raises(BudgetRefused):
        await execute(second, mock=httpx.MockTransport(refused))
    assert len(requests) == 1


@pytest.mark.parametrize("change", ["scope", "output", "model", "live_go"])
def test_go_semantic_changes_refuse_even_if_caller_repins_changed_file(
    tmp_path: Path, change: str
) -> None:
    selected = admission(tmp_path, "C06")
    envelope_path, go_path = tmp_path / "envelope.json", tmp_path / "go.json"
    envelope_path.write_text(selected.envelope.model_dump_json())
    pin = hashlib.sha256(envelope_path.read_bytes()).hexdigest()
    go: dict[str, object] = {
        "live_go": True,
        "case": "C06",
        "envelope_sha256": pin,
        "source_head": selected.envelope.source_head,
        "config_sha256": selected.envelope.config_sha256,
        "ledger_id": selected.envelope.ledger_id,
        "scope": SCOPE.model_dump(mode="json"),
        "output": str(selected.output),
    }
    go[change] = "wrong" if change != "live_go" else 1
    go_path.write_text(json.dumps(go))
    with pytest.raises(BudgetRefused, match="GO differs"):
        admit(
            envelope_path,
            go_path,
            envelope_sha256=pin,
            go_sha256=hashlib.sha256(go_path.read_bytes()).hexdigest(),
            config=selected.config,
            output=selected.output,
        )


@pytest.mark.parametrize(
    "name", ["GOOGLE_API_KEY", "GEMINI_API_KEY", "GoOgLe_FUTURE", "HTTPS_PROXY", "SSLKEYLOGFILE"]
)
def test_present_environment_value_trap_refuses_before_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    import os
    from collections.abc import Mapping

    class Trap(Mapping[str, str]):
        def __iter__(self):
            return iter((name,))

        def __len__(self) -> int:
            return 1

        def __getitem__(self, key: str) -> str:
            raise AssertionError("present environment value was read")

    with monkeypatch.context() as context:
        context.setattr(os, "environ", Trap())
        with pytest.raises(ValueError, match="refuses ambient"):
            admit(
                tmp_path / "must-not-read",
                tmp_path / "must-not-read",
                envelope_sha256="0" * 64,
                go_sha256="0" * 64,
                config=tmp_path / "missing",
                output=tmp_path / "out",
            )
