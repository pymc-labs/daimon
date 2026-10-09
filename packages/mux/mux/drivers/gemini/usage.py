"""Interaction-grain usage, never inferred from text or converted from null to zero."""

from collections.abc import Mapping
from datetime import datetime

from pydantic import JsonValue

from mux.contracts.ids import ModelRef, ResourceRef
from mux.contracts.usage import UsageObservation
from mux.drivers.gemini.transport import object_value, string


def observation_from_interaction(
    raw: Mapping[str, JsonValue],
    session: ResourceRef,
    *,
    revision: int,
    observed_at: datetime,
    root_turn: str,
    model: ModelRef,
) -> UsageObservation:
    meter = object_value(raw["usage"]) if raw.get("usage") is not None else {}

    def count(name: str) -> int | None:
        value = meter.get(name)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("invalid provider token count")
        return value

    input_tokens = count("total_input_tokens")
    visible, thoughts = count("total_output_tokens"), count("total_thought_tokens")
    # Gemini reports response and thought tokens separately (the official
    # examples' total_tokens includes both). Neutral output includes reasoning.
    output = None if visible is None or thoughts is None else visible + thoughts
    updated = raw.get("updated")
    return UsageObservation(
        id=f"gemini:{string(raw['id'])}:usage",
        revision=revision,
        native_revision=updated if isinstance(updated, str) else None,
        session=session,
        turn_id=root_turn,
        model=model,
        grain="turn",
        basis="cumulative",
        input_tokens=input_tokens,
        input_cached_tokens=count("total_cached_tokens"),
        output_tokens=output,
        output_reasoning_tokens=thoughts,
        native_meter=meter,
        completeness="unknown"
        if not meter
        else ("measured" if input_tokens is not None and output is not None else "partial"),
        observed_at=observed_at,
    )
