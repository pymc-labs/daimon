"""One pure Anthropic usage translation, shared by turn and accounting ports."""

from collections.abc import Mapping
from datetime import datetime
from typing import cast

from pydantic import JsonValue

from mux.contracts.ids import ModelRef, ResourceRef
from mux.contracts.usage import UsageObservation


def observation_from_event(
    raw: Mapping[str, JsonValue],
    session: ResourceRef,
    *,
    observed_at: datetime,
    turn_id: str | None = None,
    thread_id: str | None = None,
    model_id: str | None = None,
) -> UsageObservation:
    """Immutable request-span observation; unknown counts remain unknown."""
    if raw.get("type") != "span.model_request_end":
        raise ValueError("usage requires a model_request_end record")
    meter_value = raw["model_usage"]
    if not isinstance(meter_value, dict):
        raise ValueError("expected a native usage object")
    meter = meter_value

    def count(name: str) -> int | None:
        value = meter.get(name)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"invalid token count: {name}")
        return value

    uncached, cached, written = (
        count(name)
        for name in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )
    total = (
        None
        if any(v is None for v in (uncached, cached, written))
        else (cast(int, uncached) + cast(int, cached) + cast(int, written))
    )
    event_id = raw["id"]
    if not isinstance(event_id, str):
        raise ValueError("expected a native event ID")
    return UsageObservation(
        id=event_id,
        revision=1,
        session=session,
        turn_id=turn_id,
        thread_id=thread_id,
        model=ModelRef(provider="anthropic", id=model_id) if model_id is not None else None,
        grain="model_request",
        basis="increment",
        input_tokens=total,
        input_cached_tokens=cached,
        input_cache_write_tokens=written,
        output_tokens=count("output_tokens"),
        native_meter=meter,
        completeness="measured"
        if total is not None and count("output_tokens") is not None
        else "partial",
        observed_at=observed_at,
    )
