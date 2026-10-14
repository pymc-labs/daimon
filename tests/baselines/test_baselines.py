"""Validate provenance, missing telemetry, replay effects and latency gates offline."""

from __future__ import annotations

import json
from datetime import date

import pytest
from pydantic import ValidationError

from .convert import convert
from .replay import Evidence, TurnPath, measure


def export() -> dict[str, object]:
    return {
        "schema_version": 1,
        "provider": "anthropic",
        "window_start": "2026-09-25T00:00:00Z",
        "window_end": "2026-10-09T00:00:00Z",
        "token_source": "usage_events",
        "latency_source": "turn_outcomes",
        "first_token_status": "unavailable",
        "models": [
            {
                "model": "fixture",
                "usage_events": 1,
                "turns": 2,
                "uncached_input_tokens": 10,
                "cache_read_input_tokens": 20,
                "cache_write_input_tokens": 10,
                "output_tokens": 3,
                "p50_total_ms": 100.0,
                "p95_total_ms": 190.0,
                "p50_first_token_ms": None,
                "p95_first_token_ms": None,
            }
        ],
    }


def test_pin_date_and_observed_token_mix() -> None:
    result = convert(
        json.dumps(export()), sdk_pin="anthropic==0.117.0", baseline_date=date(2026, 10, 9)
    )
    assert result["sdk_pin"] == "anthropic==0.117.0" and result["baseline_date"] == "2026-10-09"
    telemetry = result["telemetry"]
    assert isinstance(telemetry, dict)
    assert telemetry["models"][0]["input_token_mix"] == {
        "uncached": 0.25,
        "cache_read": 0.5,
        "cache_write": 0.25,
    }
    assert telemetry["models"][0]["p50_first_token_ms"] is None
    with pytest.raises(ValueError, match="date"):
        convert(json.dumps(export()), sdk_pin="anthropic==0.117.0", baseline_date=date(2026, 10, 8))
    with pytest.raises(ValueError, match="SDK pin"):
        convert(json.dumps(export()), sdk_pin="anthropic>=0.117", baseline_date=date(2026, 10, 9))


def test_refuse_identifiers_unknown_columns_and_fake_first_token() -> None:
    e = export()
    e["tenant_id"] = "must-not-export-identifiers"
    with pytest.raises(ValidationError):
        convert(json.dumps(e), sdk_pin="anthropic==0.117.0", baseline_date=date(2026, 10, 9))
    e = export()
    rows = e["models"]
    assert isinstance(rows, list)
    rows[0]["p50_first_token_ms"] = 0
    with pytest.raises(ValidationError):
        convert(json.dumps(e), sdk_pin="anthropic==0.117.0", baseline_date=date(2026, 10, 9))


class Offline:
    offline = True

    async def replay(self, path: TurnPath) -> Evidence:
        return Evidence(path, "same-normalized-effects", 3, first_event_ms=0)


async def test_pending_never_passes() -> None:
    result = await measure(None)
    assert result["status"] == "pending" and result["gate_passed"] is None
    assert result["p50_added_ms"] is None and result["p95_added_ms"] is None


@pytest.mark.parametrize("added_ms,passed", [(4, True), (6, False), (21, False)])
async def test_paired_latency_gate(added_ms: int, passed: bool) -> None:
    # Alternate order with fixed fake durations; warmups are excluded from samples.
    durations = [1, 1 + added_ms, 1 + added_ms, 1] * 3
    values = []
    current = 0
    for duration in durations:
        values.extend((current, current + duration * 1_000_000))
        current += duration * 1_000_000
    ticks = iter(values)
    result = await measure(Offline(), iterations=4, warmups=2, clock=lambda: next(ticks))
    assert result["p50_added_ms"] == added_ms and result["p95_added_ms"] == added_ms
    assert result["gate_passed"] is passed


async def test_replay_refuses_wrong_path_differing_effects_or_external_io() -> None:
    class WrongPath(Offline):
        async def replay(self, path: TurnPath) -> Evidence:
            return Evidence("legacy", "same", 1, first_event_ms=0)

    class Different(Offline):
        async def replay(self, path: TurnPath) -> Evidence:
            return Evidence(path, path, 1, first_event_ms=0)

    class External(Offline):
        offline = False

    with pytest.raises(ValueError, match="wrong turn path"):
        await measure(WrongPath(), iterations=2, warmups=0)
    with pytest.raises(ValueError, match="effects"):
        await measure(Different(), iterations=2, warmups=0)
    with pytest.raises(ValueError, match="offline"):
        await measure(External(), iterations=2, warmups=0)
