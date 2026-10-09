"""The pure state rules, apart from any store."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ResourceRef
from mux.contracts.receipts import Operation
from mux.state.journal import completes_turn, empty_projection, is_billable, project
from mux.state.operations import TRANSITIONS, recovery

NOW = datetime(2026, 10, 9, tzinfo=UTC)
SESSION = ResourceRef(id="s1", kind="session", provider="anthropic", account_scope_id="ws")


def _event(type_: str, payload: dict[str, Any], authority: str = "record") -> Event:
    return Event.model_validate(
        {
            "id": "e",
            "session_id": "s1",
            "sequence": 0,
            "type": type_,
            "observed_at": NOW,
            "authority": authority,
            "payload": payload,
            "native": NativeProvenance(provider="anthropic", api_revision="x"),
        }
    )


@pytest.mark.parametrize("authority", ["record", "reconciled"])
def test_authoritative_events_bill_and_complete(authority: str) -> None:
    assert is_billable(_event("usage.observed", {"observation_id": "o", "revision": 1}, authority))
    end = _event("session.turn_ended", {"root_turn_id": "r", "outcome": "completed"}, authority)
    assert completes_turn(end)


def test_previews_neither_bill_nor_complete() -> None:
    usage = _event("usage.observed", {"observation_id": "o", "revision": 1}, "preview")
    end = _event("session.turn_ended", {"root_turn_id": "r", "outcome": "completed"}, "preview")
    assert not is_billable(usage)
    assert not completes_turn(end)


def test_a_new_root_turn_replaces_one_whose_end_was_missed() -> None:
    snapshot = empty_projection(SESSION, NOW)
    snapshot = project(snapshot, _event("session.status_running", {"root_turn_id": "a"}))
    snapshot = project(snapshot, _event("session.status_running", {"root_turn_id": "b"}))
    assert snapshot.active_root_turn == "b"


def test_terminated_is_final() -> None:
    snapshot = empty_projection(SESSION, NOW)
    snapshot = project(snapshot, _event("session.status_terminated", {"reason": "deleted"}))
    snapshot = project(snapshot, _event("session.status_running", {"root_turn_id": "a"}))
    assert snapshot.state == "terminated"


def test_every_status_has_a_recovery_and_only_settled_ones_are_final() -> None:
    for status, moves in TRANSITIONS.items():
        operation = Operation(
            id="o", key="k", request_digest="d", status=status, created_at=NOW, updated_at=NOW
        )
        assert (recovery(operation) == "done") == (not moves)
