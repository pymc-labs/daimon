"""Normalized events carry backend, kind, and optional tool context."""

from __future__ import annotations

import pytest
from mux.events import MuxEvent
from pydantic import ValidationError


def test_minimal_event_defaults_to_empty_context() -> None:
    event = MuxEvent(backend="anthropic", kind="text", text="hello")
    assert event.name == ""
    assert event.text == "hello"


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MuxEvent(backend="google", kind="hologram")  # type: ignore[arg-type]


def test_history_is_append_only() -> None:
    event = MuxEvent(backend="openai", kind="done")
    with pytest.raises(ValidationError):
        event.text = "rewritten"  # type: ignore[misc]
