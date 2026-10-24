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
