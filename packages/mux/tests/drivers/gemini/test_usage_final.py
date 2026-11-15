"""A Gemini usage observation is final only once its interaction is terminal."""

from datetime import UTC, datetime

import pytest
from mux.contracts.ids import ModelRef, ResourceRef
from mux.drivers.gemini.usage import observation_from_interaction

SESSION = ResourceRef(
    provider="gemini", kind="session", id="s", tenant_id="t", account_scope_id="a"
)
USAGE = {"total_input_tokens": 64, "total_cached_tokens": 16, "total_output_tokens": 8}


@pytest.mark.parametrize(
    ("status", "final"),
    [
        ("in_progress", False),
        # Tool results continue as a new interaction; this one is done.
        ("requires_action", True),
        (None, False),
        ("completed", True),
        ("failed", True),
        ("cancelled", True),
        ("incomplete", True),
        ("budget_exceeded", True),
    ],
)
def test_only_a_terminal_interaction_is_final(status, final):
    raw = {"id": "i1", "usage": {**USAGE, "total_thought_tokens": 3}}
    if status is not None:
        raw["status"] = status
    observation = observation_from_interaction(
        raw,
        SESSION,
        revision=1,
        observed_at=datetime(2026, 10, 23, tzinfo=UTC),
        root_turn="i1",
        model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
    )
    assert observation.final is final
    # A non-final observation serializes exactly as before the field existed.
    assert ("final" in observation.model_dump()) is final


def _observed(usage: dict[str, object]):
    return observation_from_interaction(
        {"id": "i1", "status": "completed", "usage": usage},
        SESSION,
        revision=1,
        observed_at=datetime(2026, 10, 23, tzinfo=UTC),
        root_turn="i1",
        model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
    )


def test_web_searches_count_only_google_search_grounding():
    grounding = [
        {"type": "google_search", "count": 3},
        {"type": "google_maps", "count": 5},
        {"type": "google_search", "count": 1},
    ]
    observation = _observed({**USAGE, "grounding_tool_count": grounding})
    assert observation.web_searches == 4
    # No searches serialize exactly as before the field existed.
    assert "web_searches" not in _observed(USAGE).model_dump()


@pytest.mark.parametrize("grounding", [{"google_search": 1}, [{"type": "google_search"}]])
def test_malformed_grounding_count_is_refused(grounding):
    with pytest.raises(ValueError, match="grounding"):
        _observed({**USAGE, "grounding_tool_count": grounding})


@pytest.mark.parametrize(
    ("stamps", "occurred"),
    [
        ({"updated": "2026-10-31T23:59:30Z", "created": "2026-10-31T23:50:00Z"}, "23:59:30"),
        ({"created": "2026-10-31T23:50:00+00:00"}, "23:50:00"),
        ({}, None),
    ],
)
def test_observation_when_interaction_is_stamped_then_occurred_at_is_its_update(stamps, occurred):
    """The provider's own time is kept apart from when the driver read it."""
    observation = observation_from_interaction(
        {"id": "i1", "usage": USAGE, **stamps},
        SESSION,
        revision=1,
        observed_at=datetime(2026, 11, 2, tzinfo=UTC),
        root_turn="i1",
        model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
    )
    expected = None if occurred is None else datetime.fromisoformat(f"2026-10-31T{occurred}+00:00")
    assert observation.occurred_at == expected, observation
