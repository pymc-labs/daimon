"""No network or real credentials: probe limits, receipts and recorded evidence."""

import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from mux.conformance.budget import BudgetGuard, BudgetRefused, ProbePlan, TokenLimits
from mux.conformance.live_probe import ProbeRunError
from mux.conformance.recording import Tape
from mux.drivers.gemini.fake import FakeTransport
from mux.drivers.gemini.live_cert import (
    GEMINI_STOP,
    LIVE_MODEL,
    MODEL_CHAIN,
    LimitedTransport,
    SmokeSettings,
    read_key,
    run_prepared,
    run_sdk_smoke,
    smoke,
    smoke_with_fallback,
    usage_metadata,
    validate_budget,
    write_report,
)
from mux.drivers.gemini.transport import Object, object_value
from mux.errors import ProviderError


@pytest.fixture(autouse=True)
def reviewed_gemini_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    # Offline composition with N9's required allowlist update; production stays
    # closed until that separately owned policy lands.
    import mux.conformance.budget as shared
    import mux.drivers.gemini.live_cert as cert

    policy = {**shared.LIVE_MODEL_ALLOWLIST, "gemini": frozenset(MODEL_CHAIN)}
    monkeypatch.setattr(shared, "LIVE_MODEL_ALLOWLIST", policy)
    monkeypatch.setattr(cert, "LIVE_MODEL_ALLOWLIST", policy)


def budget(
    root: Path, *, opening: str = "0", priced: bool = True, model: str = LIVE_MODEL
) -> BudgetGuard:
    config, ledger = root / "budget.json", root / "spend.md"
    write_report(
        config,
        {
            "version": 1,
            "total_cap_usd": "150",
            "ledger_path": str(ledger),
            "providers": {
                "gemini": {
                    "cap_usd": "30",
                    "opening_spend_usd": opening,
                    "models": {
                        candidate: {
                            "input": "1",
                            "cached_input": "1",
                            "cache_write_input": "1",
                            "output": "4",
                            **(
                                {
                                    "effective_from": "2026-10-10",
                                    "source": "https://ai.google.dev/gemini-api/docs/pricing",
                                }
                                if candidate != "gemini-flash-latest"
                                else {}
                            ),
                        }
                        for candidate in (*MODEL_CHAIN, model)
                    }
                    if priced
                    else {},
                }
            },
        },
    )
    BudgetGuard.initialize(config, ledger)
    return BudgetGuard(config, ledger)


def native(status: str = "completed", *, input_: int | None = 64, output: int | None = 8) -> Object:
    from datetime import UTC, datetime

    stamp = datetime.now(UTC).isoformat()
    return {
        "id": "smoke",
        "status": status,
        "created": stamp,
        "updated": stamp,
        "environment_id": "workspace",
        "steps": [
            {"type": "model_output", "content": [{"type": "text", "text": "GEMINI_SMOKE_OK"}]}
        ],
        "usage": {
            "total_input_tokens": input_,
            "total_cached_tokens": 0,
            "total_output_tokens": output,
            "total_thought_tokens": 0,
        },
    }


@pytest.mark.asyncio
async def test_mock_sdk_and_recorded_matrix_never_read_real_key_or_shared_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_key() -> str:
        raise AssertionError("mock run read a real credential")

    monkeypatch.setattr("mux.drivers.gemini.live_cert.read_key", forbidden_key)
    monkeypatch.setenv("GEMINI_API_KEY", "ambient-must-not-be-used")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    path = await run_prepared(tmp_path, SmokeSettings(model=LIVE_MODEL))
    report = json.loads(path.read_text())
    assert report["mode"] == "mock_transport" and report["certified"] is False
    assert report["gemini_cap_usd"] == "30" and report["gemini_stop_usd"] == "24"
    assert report["smoke_receipt"]["status"] == "completed"
    assert report["live_c07_status"] == "pending" and report["matrix_origin"] == "offline_driver"
    smoke_tape = Tape.model_validate_json((tmp_path / "smoke-C07.json").read_text())
    requests = [batch.request for batch in smoke_tape.batches]
    assert [request.method for request in requests] == ["POST", "GET", "GET"]
    assert all("/interactions" in request.path for request in requests)
    assert "agent_config" in requests[0].body_fields
    assert requests[0].headers["x-goog-api-key"] == "[redacted]"
    rows = report["matrix"]
    assert len(rows) == 18
    assert sum(row["status"] == "pass" for row in rows) == 8
    assert sum(row["status"] == "pending" for row in rows) == 10
    for row in rows:
        assert row["origin"] == "offline_driver"
        if row["recording"]:
            tape = Tape.model_validate_json((tmp_path / row["recording"]).read_text())
            assert tape.fixture_id == row["fixture_id"] and tape.complete
        else:
            assert row["pending_kind"] and row["pending_detail"]
    for artifact in tmp_path.iterdir():
        assert artifact.stat().st_mode & 0o777 == 0o600
        text = artifact.read_text()
        assert "ambient-must-not-be-used" not in text and "offline-fixture" not in text
    assert str(tmp_path) in (tmp_path / "mock-budget.json").read_text()


@pytest.mark.asyncio
async def test_24_dollar_stop_refuses_before_provider_mutation(tmp_path: Path) -> None:
    guard = budget(tmp_path, opening=str(GEMINI_STOP))
    transport = FakeTransport()
    with pytest.raises(BudgetRefused):
        await smoke(transport, guard, tmp_path / "smoke.json", SmokeSettings(model=LIVE_MODEL))
    assert not transport.requests and not (tmp_path / "smoke.json").exists()
    assert '"status":"blocked"' in guard.spend_path.read_text()


def test_unreviewed_price_refuses(tmp_path: Path) -> None:
    guard = budget(tmp_path, priced=False)
    with pytest.raises(BudgetRefused, match="reviewed prices"):
        validate_budget(guard.config_path, SmokeSettings(model=LIVE_MODEL))


@pytest.mark.asyncio
async def test_allowed_live_model_still_requires_prices_before_key_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mux.drivers.gemini.live_cert as cert

    guard = budget(tmp_path, priced=False)
    value = json.loads(guard.config_path.read_text())
    value["ledger_path"] = str(cert.SHARED_SPEND_PATH)
    config = tmp_path / "unpriced-shared-budget.json"
    write_report(config, value)
    output = tmp_path / "output"
    output.mkdir()

    def forbidden_key() -> str:
        raise AssertionError("unpriced allowed model reached key loading")

    monkeypatch.setattr(cert, "read_key", forbidden_key)
    with pytest.raises(BudgetRefused, match="reviewed prices"):
        await run_prepared(output, SmokeSettings(model=LIVE_MODEL), live=True, budget_path=config)
    assert list(output.iterdir()) == []


@pytest.mark.asyncio
async def test_old_shared_policy_refuses_before_key_or_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mux.drivers.gemini.live_cert as cert

    guard = budget(tmp_path)
    config = json.loads(guard.config_path.read_text())
    config["ledger_path"] = str(cert.SHARED_SPEND_PATH)
    guard.config_path.write_text(json.dumps(config))
    monkeypatch.setattr(cert, "LIVE_MODEL_ALLOWLIST", {"gemini": frozenset({MODEL_CHAIN[2]})})

    def forbidden_key() -> str:
        raise AssertionError("old allowlist reached credentials")

    monkeypatch.setattr(cert, "read_key", forbidden_key)
    output = tmp_path / "output"
    output.mkdir()
    with pytest.raises(BudgetRefused, match="allowlist update"):
        await run_prepared(
            output, SmokeSettings(model=LIVE_MODEL), live=True, budget_path=guard.config_path
        )
    assert not list(output.iterdir())


@pytest.mark.asyncio
async def test_unknown_usage_retains_reservation_and_never_certifies(tmp_path: Path) -> None:
    guard = budget(tmp_path)
    transport = FakeTransport()
    transport.responses.append(native(input_=None, output=None))
    probe = await smoke(transport, guard, tmp_path / "smoke.json", SmokeSettings(model=LIVE_MODEL))
    assert probe.receipt.status == "uncertain"
    assert probe.receipt.cost_estimate_usd == probe.receipt.reserved_usd > Decimal(0)
    assert probe.result.status == "pending"
    assert len(transport.requests) == 1
    assert transport.requests[0]["agent_config"] == {
        "type": "antigravity",
        "model": LIVE_MODEL,
        "max_total_tokens": 4096,
    }


@pytest.mark.asyncio
async def test_timeout_cancels_known_interaction_and_retains_failed_reservation(
    tmp_path: Path,
) -> None:
    guard = budget(tmp_path)
    transport = FakeTransport()
    transport.responses.append(native("in_progress"))
    with pytest.raises(ProbeRunError):
        await smoke(
            transport,
            guard,
            tmp_path / "smoke.json",
            SmokeSettings(model=LIVE_MODEL, timeout_s=1.0, poll_s=0.05),
        )
    assert transport.cancelled == ["smoke"] and len(transport.requests) == 1
    assert '"status":"failed"' in guard.spend_path.read_text()
    tape = Tape.model_validate_json((tmp_path / "smoke.json").read_text())
    assert not tape.complete


@pytest.mark.asyncio
async def test_lost_acceptance_response_is_not_retried_or_claimed_cancelled(tmp_path: Path) -> None:
    guard = budget(tmp_path)
    transport = FakeTransport()
    transport.responses.append(ProviderError("transient_network", retryable=True))
    with pytest.raises(ProbeRunError):
        await smoke(transport, guard, tmp_path / "smoke.json", SmokeSettings(model=LIVE_MODEL))
    assert len(transport.requests) == 1 and not transport.cancelled
    assert '"status":"failed"' in guard.spend_path.read_text()


@pytest.mark.asyncio
async def test_one_post_limit_survives_native_failure() -> None:
    transport = FakeTransport()
    transport.responses.append(ProviderError("transient_network", retryable=True))
    bounded = LimitedTransport(transport, 4096)
    request: Object = {"agent_config": {"type": "antigravity"}}
    with pytest.raises(ProviderError):
        await bounded.create(request)
    with pytest.raises(ProviderError, match="probe_send_limit"):
        await bounded.create(request)
    assert len(transport.requests) == 1


def test_key_loader_refuses_missing_symlink_permissions_and_ambient_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "ambient")
    path = tmp_path / "gemini.env"
    with pytest.raises(BudgetRefused):
        read_key(path)
    path.write_text('GEMINI_API_KEY="private-test-key"\n')
    path.chmod(0o644)
    with pytest.raises(BudgetRefused):
        read_key(path)
    path.chmod(0o600)
    assert read_key(path) == "private-test-key"
    link = tmp_path / "link.env"
    link.symlink_to(path)
    with pytest.raises(BudgetRefused):
        read_key(link)
    path.write_text("GEMINI_API_KEY=$OTHER_KEY\n")
    with pytest.raises(BudgetRefused):
        read_key(path)


def test_invalid_limits_and_output_overwrite_refuse(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SmokeSettings(model=LIVE_MODEL, max_total_tokens=0)
    with pytest.raises(ValueError):
        SmokeSettings(model=LIVE_MODEL, limits=TokenLimits(input_tokens=1, output_tokens=1))
    report = tmp_path / "report.json"
    write_report(report, {"certified": False})
    with pytest.raises(FileExistsError):
        write_report(report, {"certified": True})
    assert json.loads(report.read_text())["certified"] is False


@pytest.mark.asyncio
async def test_observed_overrun_cancels_and_blocks_later_admission(tmp_path: Path) -> None:
    guard = budget(tmp_path)
    transport = FakeTransport()
    transport.responses.append(native("in_progress", input_=32769))
    with pytest.raises(ProbeRunError, match="exceeded its reservation"):
        await smoke(transport, guard, tmp_path / "smoke.json", SmokeSettings(model=LIVE_MODEL))
    assert transport.cancelled == ["smoke"] and len(transport.requests) == 1
    assert '"status":"overrun"' in guard.spend_path.read_text()
    later = FakeTransport()
    with pytest.raises(BudgetRefused):
        await smoke(later, guard, tmp_path / "later.json", SmokeSettings(model=LIVE_MODEL))
    assert not later.requests


@pytest.mark.asyncio
async def test_wrong_saved_smoke_reply_is_not_a_success(tmp_path: Path) -> None:
    guard = budget(tmp_path)
    transport = FakeTransport()
    response = native()
    response["steps"] = [{"type": "model_output", "content": [{"type": "text", "text": "wrong"}]}]
    transport.responses.append(response)
    with pytest.raises(ProbeRunError):
        await smoke(transport, guard, tmp_path / "smoke.json", SmokeSettings(model=LIVE_MODEL))
    tape = Tape.model_validate_json((tmp_path / "smoke.json").read_text())
    assert not tape.complete


@pytest.mark.asyncio
async def test_live_preparation_rejects_a_private_ledger_before_key_or_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mux.drivers.gemini.live_cert as cert

    private = tmp_path / "private"
    private.mkdir()
    guard = budget(private, model=LIVE_MODEL)
    output = tmp_path / "output"
    output.mkdir()
    original_ledger = guard.spend_path.read_bytes()

    def forbidden_key() -> str:
        raise AssertionError("private ledger reached key loading")

    async def forbidden_dispatch(*args: object, **kwargs: object) -> None:
        raise AssertionError("private ledger reached SDK dispatch")

    monkeypatch.setattr(cert, "read_key", forbidden_key)
    monkeypatch.setattr(cert, "run_sdk_smoke", forbidden_dispatch)
    with pytest.raises(BudgetRefused, match="N9 shared spend ledger"):
        await run_prepared(
            output, SmokeSettings(model=LIVE_MODEL), live=True, budget_path=guard.config_path
        )
    assert guard.spend_path.read_bytes() == original_ledger
    assert list(output.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        "gemini-3.8-pro",
        "gemini-flash-latest",
        "gemini-3.5-flash-lite",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite-latest",
        " gemini-3.5-flash-lite",
        "fixture",
    ],
)
@pytest.mark.parametrize("entry", ["preparation", "sdk"])
async def test_live_rejects_every_other_model_even_when_priced_before_key_or_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str, entry: str
) -> None:
    import httpx
    import mux.drivers.gemini.live_cert as cert

    # Synthetic test prices deliberately admit the forbidden ID financially;
    # the model policy must refuse independently, before credentials or I/O.
    guard = budget(tmp_path, model=model.strip())
    original_ledger = guard.spend_path.read_bytes()
    value = json.loads(guard.config_path.read_text())
    value["providers"]["gemini"]["models"][model] = value["providers"]["gemini"]["models"].pop(
        model.strip()
    )
    value["ledger_path"] = str(cert.SHARED_SPEND_PATH)
    config = tmp_path / "approved-path-budget.json"
    write_report(config, value)
    output = tmp_path / "output"
    output.mkdir()

    def forbidden_key() -> str:
        raise AssertionError("forbidden model reached key loading")

    def forbidden_client(*args: object, **kwargs: object) -> None:
        raise AssertionError("forbidden model reached HTTP client creation")

    monkeypatch.setattr(cert, "read_key", forbidden_key)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden_client)
    with pytest.raises(BudgetRefused, match="gemini-3.8-flash as the primary"):
        if entry == "preparation":
            await run_prepared(output, SmokeSettings(model=model), live=True, budget_path=config)
        else:
            await cert.run_sdk_smoke(
                BudgetGuard(config, cert.SHARED_SPEND_PATH),
                output / "smoke.json",
                SmokeSettings(model=model),
                key="fake",
                mock=False,
            )
    assert guard.spend_path.read_bytes() == original_ledger
    assert list(output.iterdir()) == []


@pytest.mark.asyncio
async def test_live_sdk_entry_rejects_a_private_ledger_before_client_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    from mux.drivers.gemini.live_cert import run_sdk_smoke

    guard = budget(tmp_path, model=LIVE_MODEL)

    def forbidden_client(*args: object, **kwargs: object) -> None:
        raise AssertionError("private ledger reached HTTP client creation")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden_client)
    with pytest.raises(BudgetRefused, match="N9 shared spend ledger"):
        await run_sdk_smoke(
            guard, tmp_path / "smoke.json", SmokeSettings(model=LIVE_MODEL), key="fake", mock=False
        )


@pytest.mark.asyncio
async def test_live_shared_ledger_gate_reaches_key_loading_without_ledger_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mux.drivers.gemini.live_cert as cert

    guard = budget(tmp_path, model=LIVE_MODEL)
    value = json.loads(guard.config_path.read_text())
    value["ledger_path"] = str(cert.SHARED_SPEND_PATH)
    config = tmp_path / "approved-path-budget.json"
    write_report(config, value)
    output = tmp_path / "output"
    output.mkdir()

    class KeyRequired(Exception):
        pass

    def stop_at_key() -> str:
        raise KeyRequired

    def no_ledger_io(*args: object, **kwargs: object) -> None:
        raise AssertionError("preparation accessed the shared ledger before key loading")

    monkeypatch.setattr(cert, "read_key", stop_at_key)
    monkeypatch.setattr(BudgetGuard, "reserve", no_ledger_io)
    with pytest.raises(KeyRequired):
        await run_prepared(output, SmokeSettings(model=LIVE_MODEL), live=True, budget_path=config)
    assert list(output.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("overloads", [0, 1, 2, 3])
async def test_only_http503_advances_model_chain_with_separate_receipts(
    tmp_path: Path,
    overloads: int,
) -> None:
    guard = budget(tmp_path)
    source = FakeTransport()
    source.responses.extend(
        ProviderError("overloaded", retryable=True, native_code="503") for _ in range(overloads)
    )
    if overloads < 3:
        response = native()
        response["usage"] = {
            "total_input_tokens": 64,
            "total_cached_tokens": 16,
            "total_output_tokens": 8,
            "total_thought_tokens": 3,
        }
        source.responses.append(response)
        probe = await smoke_with_fallback(
            source, guard, tmp_path / "smoke.json", SmokeSettings(model=LIVE_MODEL)
        )
        assert probe.receipt.model == MODEL_CHAIN[overloads]
        if overloads == 1:
            assert probe.receipt.status == "uncertain"
            assert probe.receipt.tokens is not None and probe.receipt.tokens.output_tokens == 11
            assert probe.receipt.cost_estimate_usd == probe.receipt.reserved_usd
        else:
            assert probe.receipt.tokens is not None and probe.receipt.tokens.output_tokens == 11
            assert probe.receipt.tokens.input_cached_tokens == 16
            assert probe.receipt.tokens.input_cache_write_tokens == 0
            assert probe.receipt.cost_estimate_usd == Decimal("0.000108")
    else:
        with pytest.raises(ProbeRunError):
            await smoke_with_fallback(
                source, guard, tmp_path / "smoke.json", SmokeSettings(model=LIVE_MODEL)
            )
    assert [object_value(request["agent_config"])["model"] for request in source.requests] == list(
        MODEL_CHAIN[: min(overloads + 1, 3)]
    )
    receipts = sorted(tmp_path.glob("smoke-receipt-*.json"))
    assert len(receipts) == min(overloads + 1, 3)
    for index, path in enumerate(receipts):
        value = json.loads(path.read_text())
        assert value["model"] == MODEL_CHAIN[index]
        assert value["fallback_from"] == (MODEL_CHAIN[index - 1] if index else None)
        assert value["fallback_trigger_http_status"] == (503 if index else None)
        spend = value["spend_receipt"]
        if index < overloads:
            assert spend["status"] == "failed"
            assert Decimal(spend["cost_estimate_usd"]) == Decimal(spend["reserved_usd"]) > 0
            assert not Tape.model_validate_json(
                (tmp_path / value["recording"]).read_text()
            ).complete
        assert spend["run_id"] in guard.spend_path.read_text()
        assert value["price_review_date"] == "2026-10-10"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "category,code",
    [
        ("auth", "401"),
        ("permission", "403"),
        ("rate_limited", "429"),
        ("upstream", "500"),
        ("transient_network", None),
        ("overloaded", None),
        ("overloaded", "504"),
    ],
)
async def test_other_refusals_and_ambiguous_delivery_never_fallback(
    tmp_path: Path,
    category: str,
    code: str | None,
) -> None:
    from mux.contracts.errors import ProviderErrorCategory
    from pydantic import TypeAdapter

    source = FakeTransport()
    source.responses.append(
        ProviderError(
            TypeAdapter[ProviderErrorCategory](ProviderErrorCategory).validate_python(category),
            retryable=True,
            native_code=code,
        )
    )
    with pytest.raises(ProbeRunError):
        await smoke_with_fallback(
            source, budget(tmp_path), tmp_path / "smoke.json", SmokeSettings(model=LIVE_MODEL)
        )
    assert len(source.requests) == 1
    value = json.loads((tmp_path / "smoke-receipt-1.json").read_text())
    assert value["create_refusal_http_status"] is None


@pytest.mark.asyncio
async def test_poll503_after_acceptance_never_starts_another_model(tmp_path: Path) -> None:
    source = FakeTransport()
    source.responses.append(native("in_progress"))
    source.reads["smoke"] = [ProviderError("overloaded", retryable=True, native_code="503")]
    with pytest.raises(ProbeRunError):
        await smoke_with_fallback(
            source, budget(tmp_path), tmp_path / "smoke.json", SmokeSettings(model=LIVE_MODEL)
        )
    assert len(source.requests) == 1 and source.cancelled == ["smoke"]


@pytest.mark.asyncio
async def test_fallback_budget_stop_prevents_second_dispatch(tmp_path: Path) -> None:
    guard = budget(tmp_path, opening="23.80")
    source = FakeTransport()
    source.responses.append(ProviderError("overloaded", retryable=True, native_code="503"))
    with pytest.raises(BudgetRefused):
        await smoke_with_fallback(
            source, guard, tmp_path / "smoke.json", SmokeSettings(model=LIVE_MODEL)
        )
    assert len(source.requests) == 1
    ledger = guard.spend_path.read_text()
    assert '"status":"failed"' in ledger and '"status":"blocked"' in ledger


def test_native_usage_metadata_retains_four_counts_without_free_form_data() -> None:
    observed = usage_metadata(
        {
            "usageMetadata": {
                "promptTokenCount": 64,
                "candidatesTokenCount": 8,
                "cachedContentTokenCount": 16,
                "thoughtsTokenCount": 3,
                "secret": "must-be-omitted",
            }
        }
    )
    assert observed == {
        "source": "usageMetadata",
        "promptTokenCount": 64,
        "candidatesTokenCount": 8,
        "cachedContentTokenCount": 16,
        "thoughtsTokenCount": 3,
    }
    unknown = usage_metadata({})
    assert all(unknown[key] is None for key in observed if key != "source")
    with pytest.raises(ValueError):
        usage_metadata({"usage": {"total_input_tokens": True}})


@pytest.mark.asyncio
@pytest.mark.parametrize("overloads", [0, 1, 2, 3])
async def test_real_sdk_request_bytes_and_every_response_usage_remain_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overloads: int
) -> None:
    original = httpx.MockTransport
    bodies: list[bytes] = []
    methods: list[str] = []

    def factory(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
        def respond(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "generativelanguage.googleapis.com"
            methods.append(request.method)
            if request.method == "POST":
                bodies.append(request.content)
                if len(bodies) <= overloads:
                    # Non-JSON refusals must still map the exact HTTP status.
                    return httpx.Response(503, text="upstream unavailable")
            value = json.loads(handler(request).content)
            value.pop("usage")
            value["usageMetadata"] = {
                "promptTokenCount": 64,
                "candidatesTokenCount": 8,
                "cachedContentTokenCount": 16,
                "thoughtsTokenCount": 3,
            }
            return httpx.Response(200, json=value)

        return original(respond)

    monkeypatch.setattr(httpx, "MockTransport", factory)
    guard = budget(tmp_path)
    if overloads == 3:
        with pytest.raises(ProbeRunError):
            await run_sdk_smoke(
                guard,
                tmp_path / "sdk.json",
                SmokeSettings(model=LIVE_MODEL),
                key="offline-sdk-secret",
                mock=True,
            )
    else:
        probe = await run_sdk_smoke(
            guard,
            tmp_path / "sdk.json",
            SmokeSettings(model=LIVE_MODEL),
            key="offline-sdk-secret",
            mock=True,
        )
        if overloads != 1:
            assert probe.receipt.tokens is not None
            assert probe.receipt.tokens.output_tokens == 11
            assert probe.receipt.tokens.input_cached_tokens == 16
    assert len(bodies) == min(overloads + 1, 3)  # Zero SDK retries.
    for index, body in enumerate(bodies):
        parsed = json.loads(body)
        assert parsed["agent_config"]["model"] == MODEL_CHAIN[index]
        assert parsed["agent_config"]["max_total_tokens"] == 4096
        assert parsed["background"] is True and parsed["store"] is True
    usages = sorted(tmp_path.glob("sdk-usage-*.json"))
    assert len(usages) == len(methods)
    for index, path in enumerate(usages):
        value = json.loads(path.read_text())
        assert value["method"] == methods[index]
        assert value["model"] == MODEL_CHAIN[min(index, overloads, 2)]
        counts = value["usageMetadata"]
        assert counts["promptTokenCount"] == (None if index < overloads else 64)
        assert counts["thoughtsTokenCount"] == (None if index < overloads else 3)
        assert "offline-sdk-secret" not in path.read_text()
    if overloads < 3:
        receipt = json.loads((tmp_path / f"sdk-receipt-{overloads + 1}.json").read_text())
        assert receipt["reported_tokens"]["output_tokens"] == 11
        assert receipt["verification"] == ("estimated_unverified" if overloads == 1 else "actual")


@pytest.mark.asyncio
async def test_dated_primary_price_settles_cached_and_thought_tokens_once(tmp_path: Path) -> None:
    guard = budget(tmp_path)
    config = json.loads(guard.config_path.read_text())
    example = Path(__file__).parents[3] / "mux/drivers/gemini/live-budget.example.json"
    config["providers"]["gemini"]["models"] = json.loads(example.read_text())["providers"][
        "gemini"
    ]["models"]
    guard.config_path.write_text(json.dumps(config))
    source = FakeTransport()
    value = native()
    value["usage"] = {
        "total_input_tokens": 64,
        "total_output_tokens": 8,
        "total_cached_tokens": 16,
        "total_thought_tokens": 3,
    }
    source.responses.append(value)
    probe = await smoke_with_fallback(
        source, guard, tmp_path / "smoke.json", SmokeSettings(model=LIVE_MODEL)
    )
    # Reconcile and usage fetch the same cumulative snapshot; do not sum reads.
    assert probe.receipt.cost_estimate_usd == Decimal("0.00007845")
    receipt = json.loads((tmp_path / "smoke-receipt-1.json").read_text())
    assert receipt["actual_usd"] == "0.00007845" and receipt["held_usd"] == "0"


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", [False, True])
async def test_known_overrun_latches_even_with_unverified_tariff_or_null_cache(
    tmp_path: Path,
    alias: bool,
) -> None:
    guard = budget(tmp_path)
    source = FakeTransport()
    if alias:
        source.responses.append(ProviderError("overloaded", retryable=True, native_code="503"))
    response = native(input_=1_000_000)
    response["usage"] = {
        "total_input_tokens": 1_000_000,
        "total_cached_tokens": 16 if alias else None,
        "total_output_tokens": 8,
        "total_thought_tokens": 3,
    }
    source.responses.append(response)
    with pytest.raises(ProbeRunError, match="provider is blocked"):
        await smoke_with_fallback(
            source, guard, tmp_path / "overrun.json", SmokeSettings(model=LIVE_MODEL)
        )
    index = 2 if alias else 1
    receipt = json.loads((tmp_path / f"overrun-receipt-{index}.json").read_text())
    assert receipt["spend_receipt"]["status"] == "overrun"
    assert receipt["spend_receipt"]["tokens"]["input_tokens"] == 1_000_000
    assert receipt["spend_receipt"]["tokens"]["output_tokens"] == 11
    assert receipt["actual_usd"] is None
    assert receipt["verification"] == "estimated_unverified"
    assert Decimal(receipt["held_usd"]) >= Decimal(receipt["spend_receipt"]["reserved_usd"])
    with pytest.raises(BudgetRefused):
        guard.reserve(
            ProbePlan(
                provider="gemini",
                model=LIVE_MODEL,
                fixture_id="C07",
                limits=SmokeSettings(model=LIVE_MODEL).limits,
            )
        )
    assert len(source.requests) == index


@pytest.mark.asyncio
async def test_sdk_accepted_then_get503_cancellation_reaches_transport_and_tape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = httpx.MockTransport
    requests: list[httpx.Request] = []

    def factory(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(503, text="poll unavailable")
            if request.url.path.endswith(":cancel") or request.url.path.endswith("/cancel"):
                return httpx.Response(200, json=native("cancelled"))
            assert request.url.path.rstrip("/").endswith("/interactions")
            assert json.loads(request.content)["agent_config"]["model"] == LIVE_MODEL
            return httpx.Response(200, json=native("in_progress"))

        return original(respond)

    monkeypatch.setattr(httpx, "MockTransport", factory)
    with pytest.raises(ProbeRunError):
        await run_sdk_smoke(
            budget(tmp_path),
            tmp_path / "cancel.json",
            SmokeSettings(model=LIVE_MODEL),
            key="offline-cancel-key",
            mock=True,
        )
    assert [request.method for request in requests] == ["POST", "GET", "POST"]
    assert "cancel" in requests[-1].url.path
    tape = Tape.model_validate_json((tmp_path / "cancel.json").read_text())
    assert [batch.request.method for batch in tape.batches] == ["POST", "GET", "POST"]
    assert "cancel" in tape.batches[-1].request.path
    assert len(list(tmp_path.glob("cancel-usage-*.json"))) == 3
    for path in tmp_path.glob("cancel-usage-*.json"):
        assert json.loads(path.read_text())["model"] == LIVE_MODEL
    assert len(list(tmp_path.glob("cancel-receipt-*.json"))) == 1


@pytest.mark.asyncio
async def test_post_usage_overrun_survives_failed_poll_and_blocks_reserve(tmp_path: Path) -> None:
    guard = budget(tmp_path)
    source = FakeTransport()
    source.responses.append(native("in_progress", input_=1_000_000))
    source.reads["smoke"] = [ProviderError("overloaded", retryable=True, native_code="503")]
    with pytest.raises(ProbeRunError):
        await smoke_with_fallback(
            source, guard, tmp_path / "post-overrun.json", SmokeSettings(model=LIVE_MODEL)
        )
    receipt = json.loads((tmp_path / "post-overrun-receipt-1.json").read_text())
    spend = receipt["spend_receipt"]
    assert spend["status"] == "overrun"
    assert spend["tokens"]["input_tokens"] == 1_000_000
    assert spend["accounting_status"] == "estimated_unverified"
    with pytest.raises(BudgetRefused):
        guard.reserve(
            ProbePlan(
                provider="gemini",
                model=LIVE_MODEL,
                fixture_id="C07",
                limits=SmokeSettings(model=LIVE_MODEL).limits,
            )
        )
    assert len(source.requests) == 1 and source.cancelled == ["smoke"]


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_missing_or_expired_dates_refuse_before_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expired: bool,
) -> None:
    import mux.drivers.gemini.live_cert as cert

    guard = budget(tmp_path)
    config = json.loads(guard.config_path.read_text())
    config["ledger_path"] = str(cert.SHARED_SPEND_PATH)
    price = config["providers"]["gemini"]["models"][LIVE_MODEL]
    if expired:
        price["effective_until"] = "2026-10-10"
        price["effective_from"] = "2026-10-09"
    else:
        price.pop("effective_from")
    guard.config_path.write_text(json.dumps(config))

    def forbidden_key() -> str:
        raise AssertionError("unverified price accessed key")

    monkeypatch.setattr(cert, "read_key", forbidden_key)
    output = tmp_path / "output"
    output.mkdir()
    with pytest.raises(BudgetRefused, match="dated official prices"):
        await run_prepared(
            output, SmokeSettings(model=LIVE_MODEL), live=True, budget_path=guard.config_path
        )
    assert not list(output.iterdir())


@pytest.mark.asyncio
async def test_opaque_native_identity_is_aliased_before_unchanged_recorder(tmp_path: Path) -> None:
    from mux.conformance.recording import Recorder, RecordingError, RequestMetadata
    from mux.drivers.gemini.live_cert import EvidenceAliases

    opaque = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstuvwx"
    aliases = EvidenceAliases()
    raw_path = f"/v1beta/interactions/{opaque}/cancel"
    with pytest.raises(RecordingError):
        Recorder().record(RequestMetadata(method="POST", path=raw_path), ())
    assert aliases.path(raw_path) == "/v1beta/interactions/resource-1/cancel"
    assert aliases.path(raw_path.removesuffix("/cancel")).endswith("/resource-1")
    transport = FakeTransport()
    reply = native()
    reply["id"] = opaque
    transport.responses.append(reply)
    probe = await smoke(
        transport, budget(tmp_path), tmp_path / "aliased.json", SmokeSettings(model=LIVE_MODEL)
    )
    assert probe.receipt.actual_usd is not None and probe.receipt.held_usd == 0
    tape = Tape.model_validate_json(probe.recording.read_text())
    assert opaque not in probe.recording.read_text()
    assert any(
        event.type == "session.turn_ended" for batch in tape.batches for event in batch.events
    )
    assert opaque in transport.saved  # Native transport still gets the real identity.


@pytest.mark.asyncio
@pytest.mark.parametrize("export_failure", [False, True])
async def test_sdk_opaque_paths_and_terminal_usage_survive_export_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, export_failure: bool
) -> None:
    from mux.conformance.recording import Recorder, RecordingError

    original = httpx.MockTransport
    opaque = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstuvwx"
    requests: list[httpx.Request] = []

    def factory(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            response = (
                native("in_progress", input_=None, output=None)
                if request.method == "POST"
                else native()
            )
            response["id"] = opaque
            return httpx.Response(200, json=response)

        return original(respond)

    monkeypatch.setattr(httpx, "MockTransport", factory)
    if export_failure:

        def refused_export(self: Recorder, *args: object, **kwargs: object) -> None:
            raise RecordingError("synthetic export failure")

        monkeypatch.setattr(Recorder, "save", refused_export)
        with pytest.raises(RecordingError):
            await run_sdk_smoke(
                budget(tmp_path),
                tmp_path / "sdk.json",
                SmokeSettings(model=LIVE_MODEL),
                key="offline-key",
                mock=True,
            )
    else:
        probe = await run_sdk_smoke(
            budget(tmp_path),
            tmp_path / "sdk.json",
            SmokeSettings(model=LIVE_MODEL),
            key="offline-key",
            mock=True,
        )
        tape = Tape.model_validate_json(probe.recording.read_text())
        assert tape.complete and opaque not in probe.recording.read_text()
        assert any(batch.request.path.endswith("/resource-1") for batch in tape.batches)
        assert any(opaque in str(request.url) for request in requests)
    receipt = json.loads((tmp_path / "sdk-receipt-1.json").read_text())
    assert receipt["verification"] == "actual" and receipt["held_usd"] == "0"
    assert receipt["reported_tokens"]["input_tokens"] == 64
    assert receipt["reported_tokens"]["output_tokens"] == 8


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        "offline-status-secret",
        "AIza" + "Z" * 40,
        "QmVhcmVyIHN5bnRoZXRpYy1jcmVkZW50aWFsLWZvci1hdWRpdA==",
    ],
)
async def test_unknown_native_status_never_exports_credential_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    original = httpx.MockTransport

    def factory(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
        def respond(request: httpx.Request) -> httpx.Response:
            reply = native()
            reply["status"] = status
            return httpx.Response(200, json=reply)

        return original(respond)

    monkeypatch.setattr(httpx, "MockTransport", factory)
    with pytest.raises((ProbeRunError, ValueError)):
        await run_sdk_smoke(
            budget(tmp_path),
            tmp_path / "status.json",
            SmokeSettings(model=LIVE_MODEL, timeout_s=0.1, poll_s=0),
            key="offline-status-secret",
            mock=True,
        )
    for path in tmp_path.glob("status-usage-*.json"):
        assert json.loads(path.read_text())["interaction_status"] is None
    assert all(status not in path.read_text() for path in tmp_path.iterdir())


@pytest.mark.asyncio
async def test_stale_cancel_cannot_erase_accepted_overrun_or_release_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = httpx.MockTransport
    requests: list[httpx.Request] = []

    def factory(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(503, text="poll unavailable")
            cancel = "cancel" in request.url.path
            reply = native(
                "cancelled" if cancel else "in_progress", input_=64 if cancel else 1_000_000
            )
            reply["updated"] = "2026-10-10T00:00:00Z" if cancel else "2026-10-10T01:00:00Z"
            return httpx.Response(200, json=reply)

        return original(respond)

    monkeypatch.setattr(httpx, "MockTransport", factory)
    guard = budget(tmp_path)
    with pytest.raises(ProbeRunError):
        await run_sdk_smoke(
            guard,
            tmp_path / "stale.json",
            SmokeSettings(model=LIVE_MODEL),
            key="offline-key",
            mock=True,
        )
    assert [request.method for request in requests] == ["POST", "GET", "POST"]
    receipt = json.loads((tmp_path / "stale-receipt-1.json").read_text())
    assert receipt["spend_receipt"]["status"] == "overrun"
    assert receipt["reported_tokens"]["input_tokens"] == 1_000_000
    assert receipt["verification"] == "estimated_unverified" and receipt["actual_usd"] is None
    assert Decimal(receipt["held_usd"]) > 0
    with pytest.raises(BudgetRefused):
        guard.reserve(
            ProbePlan(
                provider="gemini",
                model=LIVE_MODEL,
                fixture_id="C10",
                limits=SmokeSettings(model=LIVE_MODEL).limits,
            )
        )
