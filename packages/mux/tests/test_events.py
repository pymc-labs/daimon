"""Event payload contracts are checked at construction."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from mux.contracts.events import PAYLOAD_MODELS, Event, NativeProvenance, TurnEndedPayload
from pydantic import ValidationError

NOW = datetime(2026, 10, 9, tzinfo=UTC)
PROV = NativeProvenance(provider="anthropic", api_revision="2026-07")


def _event(type_: str, payload: dict[str, Any], authority: str = "record") -> Event:
    return Event.model_validate(
        {
            "id": "j_1",
            "session_id": "s",
            "sequence": 0,
            "type": type_,
            "observed_at": NOW,
            "authority": authority,
            "payload": payload,
            "native": PROV,
        }
    )


def test_fixed_type_payload_is_validated_and_typed() -> None:
    event = _event("session.turn_ended", {"root_turn_id": "t", "outcome": "completed"})
    assert event.typed_payload() == TurnEndedPayload(root_turn_id="t", outcome="completed")
    with pytest.raises(ValidationError):
        _event("session.turn_ended", {"root_turn_id": "t", "outcome": "done"})


def test_unknown_type_is_rejected_but_open_prefixes_pass() -> None:
    with pytest.raises(ValidationError, match="unknown event type"):
        _event("session.idle", {})
    assert _event("native.anthropic.span_x", {"anything": [1]}).typed_payload() is None
    assert _event("agent.thread.created", {"thread_id": "x"}).typed_payload() is None


def test_deltas_are_preview_only() -> None:
    payload = {"item_id": "i", "content_index": 0, "text": "h", "preview_sequence": 0}
    with pytest.raises(ValidationError, match="preview-only"):
        _event("agent.message.delta", payload)
    assert _event("agent.message.delta", payload, authority="preview").authority == "preview"


def test_every_normalized_type_has_a_payload_model() -> None:
    assert set(PAYLOAD_MODELS) == {
        "user.message",
        "agent.message",
        "agent.message.delta",
        "agent.tool_use",
        "agent.tool_result",
        "session.status_running",
        "session.requires_action",
        "session.turn_ended",
        "session.error",
        "session.status_terminated",
        "tool_server.degraded",
        "usage.observed",
        "session.reconciled",
        "session.history_gap",
    }
