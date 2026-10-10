"""No network or real credentials: probe limits, receipts and recorded evidence."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from mux.conformance.budget import BudgetGuard, BudgetRefused, TokenLimits
from mux.conformance.live_probe import ProbeRunError
from mux.conformance.recording import Tape
from mux.drivers.gemini.fake import FakeTransport
from mux.drivers.gemini.live_cert import (
    GEMINI_STOP,
    LimitedTransport,
    SmokeSettings,
    read_key,
    run_prepared,
    smoke,
    validate_budget,
    write_report,
)
from mux.drivers.gemini.transport import Object
from mux.errors import ProviderError


def budget(root: Path, *, opening: str = "0", priced: bool = True) -> BudgetGuard:
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
                        "fixture": {
                            "input": "1",
                            "cached_input": "1",
                            "cache_write_input": "1",
                            "output": "4",
                        }
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
    path = await run_prepared(tmp_path, SmokeSettings(model="fixture"))
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
        await smoke(transport, guard, tmp_path / "smoke.json", SmokeSettings(model="fixture"))
    assert not transport.requests and not (tmp_path / "smoke.json").exists()
    assert '"status":"blocked"' in guard.spend_path.read_text()


def test_unreviewed_price_refuses(tmp_path: Path) -> None:
    guard = budget(tmp_path, priced=False)
    with pytest.raises(BudgetRefused, match="reviewed prices"):
        validate_budget(guard.config_path, SmokeSettings(model="fixture"))


@pytest.mark.asyncio
async def test_unknown_usage_retains_reservation_and_never_certifies(tmp_path: Path) -> None:
    guard = budget(tmp_path)
    transport = FakeTransport()
    transport.responses.append(native(input_=None, output=None))
    probe = await smoke(transport, guard, tmp_path / "smoke.json", SmokeSettings(model="fixture"))
    assert probe.receipt.status == "uncertain"
    assert probe.receipt.cost_estimate_usd == probe.receipt.reserved_usd > Decimal(0)
    assert probe.result.status == "pending"
    assert len(transport.requests) == 1
    assert transport.requests[0]["agent_config"] == {
        "type": "antigravity",
        "model": "fixture",
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
            SmokeSettings(model="fixture", timeout_s=1.0, poll_s=0.05),
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
        await smoke(transport, guard, tmp_path / "smoke.json", SmokeSettings(model="fixture"))
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
        SmokeSettings(model="fixture", max_total_tokens=0)
    with pytest.raises(ValueError):
        SmokeSettings(model="fixture", limits=TokenLimits(input_tokens=1, output_tokens=1))
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
        await smoke(transport, guard, tmp_path / "smoke.json", SmokeSettings(model="fixture"))
    assert transport.cancelled == ["smoke"] and len(transport.requests) == 1
    assert '"status":"overrun"' in guard.spend_path.read_text()
    later = FakeTransport()
    with pytest.raises(BudgetRefused):
        await smoke(later, guard, tmp_path / "later.json", SmokeSettings(model="fixture"))
    assert not later.requests


@pytest.mark.asyncio
async def test_wrong_saved_smoke_reply_is_not_a_success(tmp_path: Path) -> None:
    guard = budget(tmp_path)
    transport = FakeTransport()
    response = native()
    response["steps"] = [{"type": "model_output", "content": [{"type": "text", "text": "wrong"}]}]
    transport.responses.append(response)
    with pytest.raises(ProbeRunError):
        await smoke(transport, guard, tmp_path / "smoke.json", SmokeSettings(model="fixture"))
    tape = Tape.model_validate_json((tmp_path / "smoke.json").read_text())
    assert not tape.complete


@pytest.mark.asyncio
async def test_live_preparation_rejects_a_private_ledger_before_key_or_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mux.drivers.gemini.live_cert as cert

    private = tmp_path / "private"
    private.mkdir()
    guard = budget(private)
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
            output, SmokeSettings(model="fixture"), live=True, budget_path=guard.config_path
        )
    assert guard.spend_path.read_bytes() == original_ledger
    assert list(output.iterdir()) == []


@pytest.mark.asyncio
async def test_live_sdk_entry_rejects_a_private_ledger_before_client_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    from mux.drivers.gemini.live_cert import run_sdk_smoke

    guard = budget(tmp_path)

    def forbidden_client(*args: object, **kwargs: object) -> None:
        raise AssertionError("private ledger reached HTTP client creation")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden_client)
    with pytest.raises(BudgetRefused, match="N9 shared spend ledger"):
        await run_sdk_smoke(
            guard, tmp_path / "smoke.json", SmokeSettings(model="fixture"), key="fake", mock=False
        )


@pytest.mark.asyncio
async def test_live_shared_ledger_gate_reaches_key_loading_without_ledger_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mux.drivers.gemini.live_cert as cert

    guard = budget(tmp_path)
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
        await run_prepared(output, SmokeSettings(model="fixture"), live=True, budget_path=config)
    assert list(output.iterdir()) == []
