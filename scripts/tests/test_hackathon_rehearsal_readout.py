"""Readout handles both Cloud Logging payload layouts and stage windows."""

import json
import sys

import pytest

from scripts.hackathon_rehearsal_readout import main, payload, table, timestamp


def test_gcplogs_message_and_cloud_run_payload() -> None:
    health = {"event": "runtime.health", "process": "discord"}
    assert payload({"jsonPayload": {"message": json.dumps(health)}}) == health
    assert payload({"jsonPayload": health}) == health


def test_stage_counts_windowed_health_and_events() -> None:
    start = timestamp("2026-10-01T00:00:00Z")
    end = timestamp("2026-10-01T00:01:00Z")
    health = {
        "event": "runtime.health",
        "interval_s": 30,
        "anthropic_responses": {"messages": {"429": 2}},
        "db_pool": {"checkedout": 3, "size": 5},
        "loop_lag_ms": {"max": 12, "p95": 8},
        "turns_in_flight": {"global": 4, "per_tenant_max": 2},
    }
    entries = [
        {"timestamp": "2026-10-01T00:00:30Z", "jsonPayload": {"message": json.dumps(health)}},
        {
            "timestamp": "2026-10-01T00:00:30Z",
            "jsonPayload": {
                "message": json.dumps({**health, "anthropic_responses": {"messages": {"429": 1}}})
            },
        },
        {
            "timestamp": "2026-10-01T00:00:31Z",
            "jsonPayload": {"event": "turn.skipped.concurrency_shed"},
        },
        {
            "timestamp": "2026-10-01T00:01:01Z",
            "jsonPayload": {"event": "turn.skipped.concurrency_shed"},
        },
    ]
    result = table("burst", start, end, entries, {}, [(start, "completed", 100)])
    assert "peak 429/min by endpoint | messages=6.0" in result
    assert "peak pool checkedout/size | 3/5" in result
    assert "peak per-tenant turns in flight | 2" in result
    assert "turn.skipped.concurrency_shed | 1" in result
    assert "turn outcomes by reason | {'completed': 1}" in result


def test_production_guard_precedes_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbid_read(*_args: str) -> str:
        pytest.fail("production read attempted")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "readout",
            "--start",
            "2026-10-01T00:00:00Z",
            "--end",
            "2026-10-01T01:00:00Z",
            "--project",
            "example-prod",
        ],
    )
    monkeypatch.setattr(
        "scripts.hackathon_rehearsal_readout.gcloud",
        forbid_read,
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
