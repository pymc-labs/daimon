"""Neutral telemetry keeps legacy stage totals, deduplication and unknowns."""

from datetime import UTC, datetime

import pytest
from anthropic.types.beta.sessions import BetaManagedAgentsSpanModelRequestEndEvent
from daimon.core.turn.reducers import apply
from daimon.core.turn.state import TurnState, UsageTotals
from daimon.testing.ma_models import ma_model_usage
from mux.contracts.ids import ResourceRef
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


def span():
    return BetaManagedAgentsSpanModelRequestEndEvent(
        id="usage",
        type="span.model_request_end",
        model_request_start_id="start",
        processed_at=NOW,
        model_usage=ma_model_usage(
            input_tokens=11,
            output_tokens=17,
            cache_creation_input_tokens=5,
            cache_read_input_tokens=3,
        ),
    )


def observation(raw=None):
    return observation_from_event(raw or span().model_dump(mode="json"), SESSION, observed_at=NOW)


def test_owned_usage_matches_legacy_totals_and_dedupes_the_native_event():
    event = span()
    owned = observation()
    folded = apply(TurnState(), event, usage=owned)
    assert folded == apply(TurnState(), event)
    assert folded.usage_totals == UsageTotals(
        input_tokens=11,
        output_tokens=17,
        cache_creation_input_tokens=5,
        cache_read_input_tokens=3,
    )
    assert apply(folded, event, usage=owned) is folded


@pytest.mark.parametrize("missing", ["input_tokens", "cache_read_input_tokens", "output_tokens"])
def test_partial_observation_sums_each_reported_stage_independently(missing):
    raw = span().model_dump(mode="json")
    raw["model_usage"][missing] = None
    owned = observation(raw)
    totals = UsageTotals().add_observation(owned)
    assert totals.cache_creation_input_tokens == 5
    assert totals.cache_read_input_tokens == (0 if missing == "cache_read_input_tokens" else 3)
    assert totals.output_tokens == (0 if missing == "output_tokens" else 17)
    assert totals.input_tokens == (11 if missing == "output_tokens" else 0)
    # The immutable DTO retains unknown; it does not become a measured zero.
    assert owned.native_meter[missing] is None


@pytest.mark.parametrize(
    "changed", [{"grain": "turn"}, {"grain": "session"}, {"basis": "cumulative"}]
)
def test_overlapping_grains_and_cumulative_counts_are_rejected(changed):
    with pytest.raises(ValueError, match="disjoint model-request"):
        UsageTotals().add_observation(observation().model_copy(update=changed))


def test_observation_for_a_different_native_event_is_rejected():
    with pytest.raises(ValueError, match="does not belong"):
        apply(TurnState(), span(), usage=observation().model_copy(update={"id": "other"}))
