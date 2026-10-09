"""Pure request-span accounting translation; no provider or database calls."""

import pickle
from datetime import UTC, datetime

import pytest
from mux.contracts.ids import ResourceRef
from mux.contracts.usage import UsageObservation
from mux.drivers.anthropic.usage import observation_from_event

NOW = datetime(2026, 10, 9, tzinfo=UTC)
SESSION = ResourceRef(
    id="session",
    kind="session",
    provider="anthropic",
    account_scope_id="workspace",
    tenant_id="tenant",
    account_id="account",
)


def span(**meter):
    return {
        "type": "span.model_request_end",
        "id": "native-event",
        "model_request_start_id": "start",
        "model_usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": 20,
            "cache_creation_input_tokens": 30,
            "output_tokens": 40,
            "speed": "standard",
            **meter,
        },
    }


def test_inclusive_input_preserves_native_meter_and_observation_identity():
    raw = span()
    observation = observation_from_event(
        raw, SESSION, observed_at=NOW, turn_id="turn", thread_id="thread", model_id="claude-model"
    )
    assert observation.id == "native-event" and observation.revision == 1
    assert observation.input_tokens == 60
    assert observation.input_cached_tokens == 20
    assert observation.input_cache_write_tokens == 30
    assert observation.output_tokens == 40
    assert observation.native_meter == raw["model_usage"]
    assert observation.session == SESSION
    assert observation.turn_id == "turn" and observation.thread_id == "thread"
    assert observation.model.provider == "anthropic" and observation.model.id == "claude-model"
    assert observation.grain == "model_request" and observation.basis == "increment"
    assert observation.completeness == "measured" and observation.observed_at == NOW
    assert UsageObservation.model_validate_json(observation.model_dump_json()) == observation
    assert pickle.loads(pickle.dumps(observation)) == observation
    assert observation_from_event(raw, SESSION, observed_at=NOW).id == observation.id


@pytest.mark.parametrize(
    "bucket",
    ["input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"],
)
@pytest.mark.parametrize("missing", [True, False], ids=["omitted", "null"])
def test_unknown_buckets_are_not_zero(bucket, missing):
    raw = span()
    if missing:
        raw["model_usage"].pop(bucket)
    else:
        raw["model_usage"][bucket] = None
    observation = observation_from_event(raw, SESSION, observed_at=NOW)
    if bucket == "output_tokens":
        assert observation.output_tokens is None and observation.input_tokens == 60
    else:
        assert observation.input_tokens is None
    if bucket == "cache_read_input_tokens":
        assert observation.input_cached_tokens is None
    if bucket == "cache_creation_input_tokens":
        assert observation.input_cache_write_tokens is None
    assert observation.native_meter == raw["model_usage"]
    assert observation.completeness == "partial"


def test_reported_zero_is_measured_and_model_default_is_not_invented():
    observation = observation_from_event(
        span(
            input_tokens=0,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            output_tokens=0,
        ),
        SESSION,
        observed_at=NOW,
    )
    assert observation.input_tokens == observation.output_tokens == 0
    assert observation.input_cached_tokens == observation.input_cache_write_tokens == 0
    assert observation.completeness == "measured" and observation.model is None


@pytest.mark.parametrize("value", [-1, True, 1.5, "12"])
def test_invalid_bucket_is_rejected(value):
    with pytest.raises(ValueError, match="invalid token count"):
        observation_from_event(span(input_tokens=value), SESSION, observed_at=NOW)


def test_non_request_event_cannot_create_an_observation():
    raw = span()
    raw["type"] = "span.model_request_start"
    with pytest.raises(ValueError, match="model_request_end"):
        observation_from_event(raw, SESSION, observed_at=NOW)
