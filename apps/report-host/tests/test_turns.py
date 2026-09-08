"""Tests for report_host.turns — the pure decision, then the shell around it.

The shell tests (added alongside `run_turn`) drive it against a real SQLite
file in `tmp_path` and an in-process fake seam — never a monkeypatch of
`run_turn`'s own collaborators.
"""

from __future__ import annotations

from report_host.turns import read_turn_progress

# --------------------------------------------------------------------------
# Task 1: the pure half — read_turn_progress
# --------------------------------------------------------------------------


def _event(id_: str, type_: str, text: str | None = None) -> dict[str, object]:
    content: list[dict[str, object]] | None = None
    if text is not None:
        content = [{"type": "text", "text": text}]
    return {"id": id_, "type": type_, "content": content}


def test_read_turn_progress_with_only_boundary_event_is_not_done_and_has_no_text() -> None:
    events = [_event("evt-0", "agent.message", "should never appear")]
    result = read_turn_progress(
        status="running", events=events, turn_event_id="evt-0", seen_event_ids=frozenset()
    )
    assert result.is_done is False, "a turn with only its own boundary event is not finished"
    assert result.new_texts == (), "the boundary event's own text must never be replayed"


def test_read_turn_progress_returns_text_from_agent_message_after_boundary() -> None:
    events = [
        _event("evt-0", "agent.message", "boundary text"),
        _event("evt-1", "agent.message", "hello there"),
    ]
    result = read_turn_progress(
        status="running", events=events, turn_event_id="evt-0", seen_event_ids=frozenset()
    )
    assert result.new_texts == ("hello there",)
    assert result.is_done is False


def test_read_turn_progress_does_not_repeat_a_previously_seen_event() -> None:
    """The pin against duplicated answers: an id already reported is dropped."""
    events = [
        _event("evt-0", "agent.message", "boundary"),
        _event("evt-1", "agent.message", "hello"),
    ]
    result = read_turn_progress(
        status="running",
        events=events,
        turn_event_id="evt-0",
        seen_event_ids=frozenset({"evt-1"}),
    )
    assert result.new_texts == (), "an id already in seen_event_ids must not be replayed"


def test_read_turn_progress_is_done_when_idle_event_survives_the_filter() -> None:
    events = [_event("evt-0", "agent.message", "boundary"), _event("evt-2", "session.status_idle")]
    result = read_turn_progress(
        status="idle", events=events, turn_event_id="evt-0", seen_event_ids=frozenset()
    )
    assert result.is_done is True
    assert result.terminal_reason == "idle"


def test_read_turn_progress_bare_idle_status_with_no_idle_event_is_not_done() -> None:
    """The pin against the prototype's bug: a session that hasn't started yet
    reads idle right after the send, and that bare status must never be
    trusted without a surviving idle EVENT."""
    events = [_event("evt-0", "agent.message", "boundary")]
    result = read_turn_progress(
        status="idle", events=events, turn_event_id="evt-0", seen_event_ids=frozenset()
    )
    assert result.is_done is False, "a bare idle status with no idle event must not end the turn"


def test_read_turn_progress_rescheduling_status_is_not_done() -> None:
    result = read_turn_progress(
        status="rescheduling", events=[], turn_event_id="evt-0", seen_event_ids=frozenset()
    )
    assert result.is_done is False, "rescheduling counts as still running"


def test_read_turn_progress_terminated_status_with_no_idle_event_is_done() -> None:
    result = read_turn_progress(
        status="terminated", events=[], turn_event_id="evt-0", seen_event_ids=frozenset()
    )
    assert result.is_done is True
    assert result.terminal_reason == "terminated"


def test_read_turn_progress_idle_event_that_is_itself_the_boundary_is_not_done() -> None:
    events = [_event("evt-0", "session.status_idle")]
    result = read_turn_progress(
        status="idle", events=events, turn_event_id="evt-0", seen_event_ids=frozenset()
    )
    assert result.is_done is False, (
        "a boundary that is itself an idle event must not end the turn it starts"
    )
