"""Interaction-grain usage, never inferred from text or converted from null to zero."""

from collections.abc import Mapping
from datetime import UTC, datetime

from pydantic import JsonValue

from mux.contracts.ids import ModelRef, ResourceRef
from mux.contracts.usage import UsageObservation
from mux.drivers.gemini.normalize import OUTCOMES
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
    metadata = raw.get("usageMetadata")
    meter = object_value(metadata if metadata is not None else raw.get("usage") or {})
    names = {
        "total_input_tokens": "promptTokenCount",
        "total_cached_tokens": "cachedContentTokenCount",
        "total_output_tokens": "candidatesTokenCount",
        "total_thought_tokens": "thoughtsTokenCount",
    }

    def count(name: str) -> int | None:
        value = meter.get(names[name] if metadata is not None else name)
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
        occurred_at=provider_time(raw),
        # A terminal status ends the interaction's usage, and so does requires_action:
        # its tool results continue as a new interaction (`previous_interaction_id`),
        # so this one never runs again. A running interaction may still grow.
        final=raw.get("status") in (*OUTCOMES, "requires_action"),
        web_searches=web_searches(meter),
    )


def provider_time(raw: Mapping[str, JsonValue]) -> datetime | None:
    """The interaction's last `updated` time (else `created`), in UTC; None if it has none."""
    active = raw.get("updated") or raw.get("created")
    if active is None:
        return None
    stamp = datetime.fromisoformat(string(active).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("provider timestamp has no timezone")
    return stamp.astimezone(UTC)


def web_searches(meter: Mapping[str, JsonValue]) -> int:
    """Google Search grounding queries, from the usage's `{type, count}` list.

    Other grounding types (Maps, retrieval) are not web searches and stay out.
    """
    raw = meter.get("grounding_tool_count", meter.get("groundingToolCount"))
    if raw is None:
        return 0
    if not isinstance(raw, list):
        raise ValueError("invalid provider grounding count")
    total = 0
    for item in raw:
        if not isinstance(item, dict) or item.get("type") != "google_search":
            continue
        count = item.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("invalid provider grounding count")
        total += count
    return total
