"""Live metadata tapes replay offline; these tests cannot certify native codecs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mux.conformance.recording import RecordingError, Replay
from mux.contracts.events import Event, NativeProvenance

from .recording_checks import check

CORPUS = Path(__file__).resolve().parents[5] / "tests/conformance/recordings/openai"


@pytest.mark.asyncio
@pytest.mark.parametrize("index", range(1, 19))
async def test_saved_live_matrix_replays_against_independent_metadata_oracles(index: int) -> None:
    result = await check(CORPUS / f"C{index:02}.json")
    assert result["metadata_replay"] == "pass" and result["events"] == 0


def test_partial_matrix_keeps_live_gaps_and_unknown_cost_honest() -> None:
    manifest = json.loads((CORPUS / "receipts.json").read_text())
    assert manifest["complete_certificate"] is False and manifest["native_event_count"] == 0
    rows = {row["fixture_id"]: row for row in manifest["fixtures"]}
    assert set(rows) == {f"C{i:02}" for i in range(1, 19)}
    for fixture, row in rows.items():
        result, receipt = row["result"], row["receipt"]
        assert receipt["fixture_id"] == fixture
        if fixture in {"C10", "C15", "C16"}:
            assert result["status"] == "pass" and row["cleanup"]
            assert row["cleanup_strategy"] == "driver.sessions.delete bounded idle/409 recovery"
            assert not row["cleanup_failures"]
            assert receipt["tokens"] is None and receipt["status"] == "uncertain"
            assert receipt["cost_estimate_usd"] == receipt["reserved_usd"] == "0.03575"
            assert row["resources_created"] == {"agent": 1, "session": 1}
            tape = Replay.load(CORPUS / (fixture + ".json")).tape
            assert sum(row["request_counts"].values()) == len(tape.batches)
            assert row["request_counts"].get("turn_send", 0) == 0
            assert row["request_counts"].get("cancel", 0) == 0
        else:
            assert result["status"] == "pending" and result["pending_reason"]["detail"]
            assert result["pending_reason"]["kind"] in {
                "adapter_dependency",
                "capability_unavailable",
            }
            assert not Replay.load(CORPUS / (fixture + ".json")).tape.batches
            assert set(receipt["tokens"].values()) == {0}
            assert receipt["cost_estimate_usd"] == "0.000"
    assert {row["run_id"] for row in manifest["prior_attempts"]} == {
        "3055ba122e3a41899d1642c775bab977",
        "1202fa76442046359f3c45eafb0fbd6e",
        "dea41097609b49249d68e27f524c4ce3",
        "e5f30c690a3e410f944ec8d96fc35550",
    }
    assert all(row["status"] == "failed" for row in manifest["prior_attempts"])


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["extra_write", "wrong_identity", "body_fields", "header"])
async def test_saved_replay_rejects_mutations_route_changes_and_credentials(
    tmp_path: Path, mutation: str
) -> None:
    data = json.loads((CORPUS / "C15.json").read_text())
    if mutation == "extra_write":
        data["batches"].insert(
            4,
            {
                "request": {
                    "method": "POST",
                    "path": "/v1/agents/sessions/resource-2/events",
                    "headers": {},
                    "body_fields": [],
                },
                "events": [],
            },
        )
    elif mutation == "wrong_identity":
        data["batches"][4]["request"]["path"] = "/v1/agents/sessions/resource-3"
    elif mutation == "body_fields":
        data["batches"][0]["request"]["body_fields"].append("unexpected_setting")
    else:
        data["batches"][0]["request"]["headers"]["authorization"] = "fixture-credential"
    path = tmp_path / "mutant.json"
    path.write_text(json.dumps(data))
    with pytest.raises(RecordingError):
        await check(path)


@pytest.mark.asyncio
async def test_replay_rejects_a_changed_normalized_journal(tmp_path: Path) -> None:
    data = json.loads((CORPUS / "C16.json").read_text())
    event = Event(
        id="unexpected-outcome",
        session_id="session",
        sequence=0,
        type="session.turn_ended",
        turn_id="root",
        observed_at=datetime(2026, 10, 9, tzinfo=UTC),
        authority="record",
        payload={"root_turn_id": "root", "outcome": "completed"},
        native=NativeProvenance(provider="openai", api_revision="agents=v1"),
    )
    data["batches"][7]["events"] = [event.model_dump(mode="json")]
    path = tmp_path / "changed-journal.json"
    path.write_text(json.dumps(data))
    with pytest.raises(RecordingError):
        await check(path)
