"""Validate provenance, missing telemetry, replay effects and latency gates offline."""

from __future__ import annotations

import json
from datetime import date

import pytest
from pydantic import ValidationError

from .convert import convert


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
