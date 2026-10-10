"""Immutable Gemini live evidence replay; no SDK/client/alias-code dependency."""

import socket
from decimal import Decimal
from pathlib import Path
from typing import Never

import pytest
from mux.conformance.budget import SpendReceipt
from mux.conformance.recording import Replay, Tape
from mux.contracts.events import AgentMessagePayload, Event, TextPart, TurnEndedPayload
from pydantic import JsonValue, TypeAdapter

ROOT = Path(__file__).parents[3] / "mux/drivers/gemini/certification/2026-10-10"
OBJECT = TypeAdapter(dict[str, JsonValue])


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: object, **kwargs: object) -> Never:
        raise AssertionError("certificate replay attempted network I/O")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setenv("GEMINI_API_KEY", "replay-must-not-use-ambient-key")


def read(path: Path) -> dict[str, JsonValue]:
    return OBJECT.validate_json(path.read_text())


async def consume(tape: Tape, *, native_case: bool) -> tuple[Event, ...]:
    replay = Replay(tape)
    events: list[Event] = []
    for batch in replay.tape.batches:
        events.extend(await replay.events(batch.request))
    replay.finish()
    assert tape.provider == "gemini" and tape.model == "gemini-3.8-flash"
    requests = [batch.request for batch in tape.batches]
    assert sum(r.method == "POST" for r in requests) == 1
    assert sum(r.method == "DELETE" for r in requests) == int(native_case)
    assert requests[0].method == "POST"
    assert all(r.method in ("POST", "GET", "DELETE") for r in requests)
    assert requests[-1].method == ("DELETE" if native_case else "GET")
    assert all("resource-" in r.path for r in requests[1:])
    assert all(r.headers.get("x-goog-api-key") == "[redacted]" for r in requests)
    assert "agent_config" in requests[0].body_fields
    assert len({event.session_id for event in events}) == 1
    assert len({event.id for event in events}) == len(events)
    ends = [event for event in events if event.type == "session.turn_ended"]
    assert len(ends) == 1 and ends[0].authority in ("record", "reconciled")
    end = ends[0].typed_payload()
    assert isinstance(end, TurnEndedPayload) and end.outcome == "completed"
    assert end.root_turn_id == ends[0].turn_id
    texts: list[str] = []
    for event in events:
        if event.type == "agent.message":
            payload = event.typed_payload()
            assert isinstance(payload, AgentMessagePayload)
            texts.extend(part.text for part in payload.content if isinstance(part, TextPart))
    assert texts == ["GEMINI_SMOKE_OK"]
    assert any(event.type == "user.message" for event in events)
    assert any(event.type == "usage.observed" for event in events)
    return tuple(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture_id", ["C07", "C10", "C15", "C16"])
async def test_saved_native_tapes_and_exact_receipts_replay_without_alias_code(
    fixture_id: str,
) -> None:
    folder = ROOT / ("smoke" if fixture_id == "C07" else f"native-cases/{fixture_id}")
    stem = "smoke-C07" if fixture_id == "C07" else fixture_id
    tape = Replay.load(folder / f"{stem}.json").tape
    assert tape.fixture_id == fixture_id
    await consume(tape, native_case=fixture_id != "C07")
    receipt = SpendReceipt.model_validate(read(folder / f"{stem}-receipt-1.json")["spend_receipt"])
    assert receipt.fixture_id == fixture_id and receipt.accounting_status == "actual"
    assert receipt.actual_usd is not None and receipt.actual_usd > 0 and receipt.held_usd == 0
    assert receipt.price is not None and receipt.actual_evidence is not None
    assert receipt.actual_evidence.usage_complete and receipt.actual_evidence.containers == ()
    assert len(receipt.actual_evidence.requests) == 1
    request = receipt.actual_evidence.requests[0]
    assert request.tokens == receipt.tokens
    assert receipt.price.source == "https://ai.google.dev/gemini-api/docs/pricing"
    assert str(receipt.price.effective_from) == "2026-10-10"
    assert receipt.price.actual(request.tokens, request.observed_at) == receipt.actual_usd
    assert receipt.token_usd == receipt.actual_usd and receipt.container_usd == 0
    responses = [read(path) for path in folder.glob(f"{stem}-usage-*.json")]
    completed = [row for row in responses if row["interaction_status"] == "completed"]
    assert completed
    for row in completed:
        counters = OBJECT.validate_python(row["usageMetadata"])
        prompt, visible, cached, thoughts = (
            counters[name]
            for name in (
                "promptTokenCount",
                "candidatesTokenCount",
                "cachedContentTokenCount",
                "thoughtsTokenCount",
            )
        )
        assert type(prompt) is int and type(visible) is int
        assert type(cached) is int and type(thoughts) is int
        assert request.tokens.input_tokens == prompt
        assert request.tokens.input_cached_tokens == cached
        assert request.tokens.output_tokens == visible + thoughts
        assert row["usage_observation_sha256"] == request.id
    # Repeated cumulative GET snapshots are one billable interaction, never summed.
    assert len(completed) > 1 and len(receipt.actual_evidence.requests) == 1
    if fixture_id != "C07":
        deletes = [row for row in responses if row["method"] == "DELETE"]
        assert len(deletes) == 1 and type(deletes[0]["http_status"]) is int
        assert 200 <= deletes[0]["http_status"] < 300


@pytest.mark.asyncio
async def test_all_eighteen_matrix_rows_keep_honest_origins_and_typed_pending() -> None:
    report = read(ROOT / "matrix/matrix-report.json")
    rows = TypeAdapter(list[dict[str, JsonValue]]).validate_python(report["matrix"])
    assert [row["fixture_id"] for row in rows] == [f"C{i:02}" for i in range(1, 19)]
    assert {str(row["fixture_id"]) for row in rows if row["status"] == "pass"} == {
        "C10",
        "C13",
        "C15",
        "C16",
    }
    assert sum(row["status"] == "pending" for row in rows) == 14
    assert report["certified"] is False and report["provider_calls"] == 0
    for row in rows:
        assert row["provider_calls"] == 0 and row["actual_usd"] == "0" and row["held_usd"] == "0"
        if row["status"] == "pending":
            assert row["pending_kind"] in ("adapter_dependency", "capability_unavailable")
            assert row["pending_detail"] and row["origin"] == "live_adapter_pending"
        else:
            assert row["origin"] == "driver_contract_no_provider_io"
        fid = str(row["fixture_id"])
        replay = Replay.load(ROOT / f"matrix/{fid}.json")
        assert not replay.tape.batches
        replay.finish()
        receipt = SpendReceipt.model_validate_json(
            (ROOT / f"matrix/{fid}-receipt.json").read_text()
        )
        assert receipt.actual_usd == 0 and receipt.held_usd == 0


def test_verified_cost_total_matches_independent_receipts() -> None:
    receipts = [
        SpendReceipt.model_validate(read(ROOT / "smoke/smoke-C07-receipt-1.json")["spend_receipt"]),
        *(
            SpendReceipt.model_validate(
                read(ROOT / f"native-cases/{fid}/{fid}-receipt-1.json")["spend_receipt"]
            )
            for fid in ("C10", "C15", "C16")
        ),
    ]
    assert sum((r.actual_usd or Decimal(0) for r in receipts), Decimal(0)) == Decimal("0.01163100")
    assert all(r.held_usd == 0 for r in receipts)
    assert len({r.run_id for r in receipts}) == 4
    assert all(r.model == "gemini-3.8-flash" for r in receipts)
    for path in ROOT.rglob("*.json"):
        value = path.read_text()
        assert "AIza" not in value and "replay-must-not-use-ambient-key" not in value
        assert "sk-proj-" not in value and "Bearer " not in value


@pytest.mark.asyncio
async def test_replay_rejects_changed_saved_output(tmp_path: Path) -> None:
    source = ROOT / "native-cases/C10/C10.json"
    altered = source.read_text().replace('"text":"GEMINI_SMOKE_OK"', '"text":"CORRUPTED_OUTPUT"')
    assert altered != source.read_text()
    target = tmp_path / "altered.json"
    target.write_text(altered)
    with pytest.raises(AssertionError):
        await consume(Replay.load(target).tape, native_case=True)
