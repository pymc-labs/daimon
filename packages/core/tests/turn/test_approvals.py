"""Tests for daimon.core.turn.approvals — the pure fresh-id selection and
`user.tool_confirmation` payload construction Task 2's driver branch uses.

Covers empty / partial / fully-seen `confirmed` sets, order preservation,
non-mutation of the caller's set, non-`requires_action` stop reasons
(including `None`), and payload construction. `pending_confirmation_ids`
takes the `StopReason` union directly (not a `session.status_idle` event)
so these tests construct the stop reason with the same builders the
eventless-cycle reconnect branch's folded `TurnState.stop_reason` uses.
"""

from __future__ import annotations

from daimon.core.turn.approvals import build_confirmation_events, pending_confirmation_ids

from .conftest import make_end_turn, make_requires_action, make_retries_exhausted


def test_pending_confirmation_ids_returns_all_ids_for_empty_confirmed_set() -> None:
    stop_reason = make_requires_action(event_ids=["tu_1", "tu_2"])

    assert pending_confirmation_ids(stop_reason, confirmed=set()) == ["tu_1", "tu_2"]


def test_pending_confirmation_ids_returns_only_unseen_ids_for_partial_overlap() -> None:
    stop_reason = make_requires_action(event_ids=["tu_1", "tu_2", "tu_3"])

    assert pending_confirmation_ids(stop_reason, confirmed={"tu_2"}) == ["tu_1", "tu_3"]


def test_pending_confirmation_ids_returns_empty_list_when_all_ids_already_seen() -> None:
    stop_reason = make_requires_action(event_ids=["tu_1", "tu_2"])

    assert pending_confirmation_ids(stop_reason, confirmed={"tu_1", "tu_2"}) == []


def test_pending_confirmation_ids_preserves_event_ids_order() -> None:
    stop_reason = make_requires_action(event_ids=["tu_3", "tu_1", "tu_2"])

    assert pending_confirmation_ids(stop_reason, confirmed=set()) == ["tu_3", "tu_1", "tu_2"]


def test_pending_confirmation_ids_does_not_mutate_confirmed() -> None:
    stop_reason = make_requires_action(event_ids=["tu_1", "tu_2"])
    confirmed: set[str] = {"tu_2"}

    pending_confirmation_ids(stop_reason, confirmed=confirmed)

    assert confirmed == {"tu_2"}, "the caller owns adding newly-claimed ids, not this function"


def test_pending_confirmation_ids_returns_empty_list_for_non_requires_action_stop_reason() -> None:
    assert pending_confirmation_ids(make_end_turn(), confirmed=set()) == []
    assert pending_confirmation_ids(make_retries_exhausted(), confirmed=set()) == []


def test_pending_confirmation_ids_returns_empty_list_for_none_stop_reason() -> None:
    assert pending_confirmation_ids(None, confirmed=set()) == []


def test_build_confirmation_events_produces_one_allow_payload_per_id_in_order() -> None:
    events = build_confirmation_events(["tu_1", "tu_2"])

    assert events == [
        {"type": "user.tool_confirmation", "result": "allow", "tool_use_id": "tu_1"},
        {"type": "user.tool_confirmation", "result": "allow", "tool_use_id": "tu_2"},
    ]


def test_build_confirmation_events_returns_empty_list_for_no_ids() -> None:
    assert build_confirmation_events([]) == []
