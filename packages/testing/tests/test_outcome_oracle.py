"""Outcome predicates reject false certificates without provider calls or clocks."""

from typing import Literal

import pytest
from daimon.testing.outcome_oracle import (
    RunEvidence,
    SessionUse,
    TerminalEvidence,
    TurnEvidence,
    evaluate,
)
from pydantic import JsonValue, ValidationError


def turn(number: int = 1, *, session: str = "session-a", **changes: object) -> TurnEvidence:
    values: dict[str, object] = dict(
        turn=number,
        slot_id="slot-a",
        session_id=session,
        root_turn_id=f"root-{number}",
        started_s=100.0,
        terminals=(
            TerminalEvidence(
                evidence_id=f"terminal-{number}",
                session_id=session,
                root_turn_id=f"root-{number}",
                authority="record",
                outcome="completed",
                observed_s=110.0,
            ),
        ),
        sessions=(SessionUse(evidence_id=f"binding-{number}", session_id=session),),
        terminal_capture_complete=True,
        session_capture_complete=True,
    )
    return TurnEvidence.model_validate(values | changes)


def recording(
    *turns: TurnEvidence, backend: Literal["anthropic", "openai", "gemini"] = "anthropic"
) -> RunEvidence:
    return RunEvidence(scenario_id="followup", backend=backend, turns=turns)


@pytest.mark.parametrize("backend", ("anthropic", "openai", "gemini"))
def test_provider_identity_does_not_change_structural_outcomes(
    backend: Literal["anthropic", "openai", "gemini"],
) -> None:
    assertions: list[dict[str, JsonValue]] = [
        {"kind": "turn_completed", "turn": 1},
        {"kind": "done_within_s", "turn": 2, "max": 10},
        {"kind": "same_session", "turn": 2},
    ]
    actual = evaluate(
        recording(turn(session=backend), turn(2, session=backend), backend=backend), assertions
    )
    assert actual.status == "PASS"
    assert (
        actual.normalized_outcomes
        == evaluate(recording(turn(), turn(2)), assertions).normalized_outcomes
    )
    assert all(check.evidence_ids for check in actual.checks)


@pytest.mark.parametrize("authority", ("preview", "gap"))
def test_preview_or_gap_cannot_complete_a_root(authority: Literal["preview", "gap"]) -> None:
    source = turn()
    source = source.model_copy(
        update={"terminals": (source.terminals[0].model_copy(update={"authority": authority}),)}
    )
    assert (
        evaluate(recording(source), [{"kind": "turn_completed", "turn": 1}]).checks[0].code
        == "ROOT_TERMINAL_MISSING"
    )


@pytest.mark.parametrize("field", ("root_turn_id", "session_id"))
def test_another_root_or_session_cannot_complete_the_turn(field: str) -> None:
    source = turn()
    source = source.model_copy(
        update={"terminals": (source.terminals[0].model_copy(update={field: "foreign"}),)}
    )
    assert evaluate(recording(source), [{"kind": "turn_completed", "turn": 1}]).status == "FAIL"


@pytest.mark.parametrize("outcome", ("interrupted", "errored", "terminated"))
def test_non_success_terminal_is_not_completion(outcome: str) -> None:
    source = turn()
    source = source.model_copy(
        update={"terminals": (source.terminals[0].model_copy(update={"outcome": outcome}),)}
    )
    assert (
        evaluate(recording(source), [{"kind": "turn_completed", "turn": 1}]).checks[0].code
        == "TURN_NOT_COMPLETED"
    )


def test_latency_is_trigger_to_root_completion_with_an_inclusive_bound() -> None:
    assert (
        evaluate(recording(turn()), [{"kind": "done_within_s", "turn": 1, "max": 10}]).status
        == "PASS"
    )
    assert (
        evaluate(recording(turn()), [{"kind": "done_within_s", "turn": 1, "max": 9.99}])
        .checks[0]
        .code
        == "COMPLETION_TOO_LATE"
    )


def test_conflicting_terminal_observations_fail() -> None:
    source = turn()
    conflict = source.terminals[0].model_copy(
        update={"evidence_id": "conflict", "outcome": "errored", "authority": "reconciled"}
    )
    source = source.model_copy(update={"terminals": (*source.terminals, conflict)})
    assert (
        evaluate(recording(source), [{"kind": "turn_completed", "turn": 1}]).checks[0].code
        == "CONFLICTING_ROOT_OUTCOMES"
    )


@pytest.mark.parametrize(
    "changes",
    (
        {
            "sessions": (
                SessionUse(evidence_id="transient", session_id="replacement"),
                SessionUse(evidence_id="back", session_id="session-a"),
            )
        },
        {"session_id": "replacement"},
        {"slot_id": "sibling-slot"},
        {"sessions": ()},
    ),
)
def test_followup_cannot_hide_transient_or_final_session_replacement(
    changes: dict[str, object],
) -> None:
    assert (
        evaluate(
            recording(turn(), turn(2).model_copy(update=changes)),
            [{"kind": "same_session", "turn": 2}],
        ).status
        == "FAIL"
    )


@pytest.mark.parametrize(
    "kind,changes",
    (
        ("turn_completed", {"terminal_capture_complete": False}),
        ("no_session_replacement", {"session_capture_complete": False}),
    ),
)
def test_missing_capture_is_pending(kind: str, changes: dict[str, object]) -> None:
    assert (
        evaluate(recording(turn().model_copy(update=changes)), [{"kind": kind, "turn": 1}]).status
        == "PENDING"
    )


def test_missing_turn_unknown_kind_and_empty_assertions_cannot_pass() -> None:
    source = recording(turn())
    for assertions in ([], [{"kind": "turn_completed", "turn": 2}], [{"kind": "judge", "turn": 1}]):
        assert evaluate(source, assertions).status == "PENDING"
    mixed = evaluate(source, [{"kind": "judge"}, {"kind": "turn_completed", "turn": 1}])
    assert [check.status for check in mixed.checks] == ["PENDING", "PASS"]


@pytest.mark.parametrize(
    "assertion",
    (
        {"kind": "done_within_s", "turn": 1},
        {"kind": "done_within_s", "turn": 1, "max": -1},
        {"kind": "done_within_s", "turn": 1, "max": float("nan")},
        {"kind": "turn_completed", "turn": True},
        {"kind": "turn_completed", "turn": 1, "typo": 1},
        {"kind": "same_session", "turn": 1},
        {"kind": "same_session", "turn": 2, "previous_turn": 2},
    ),
)
def test_malformed_known_assertions_fail(assertion: dict[str, JsonValue]) -> None:
    assert evaluate(recording(turn()), [assertion]).checks[0].code == "INVALID_ASSERTION"


def test_invalid_evidence_is_rejected_before_grading() -> None:
    with pytest.raises(ValidationError, match="duplicate turn"):
        recording(turn(), turn())
    with pytest.raises(ValidationError, match="duplicate evidence"):
        recording(turn(), turn(2, sessions=turn().sessions))
    with pytest.raises(ValidationError, match="predates"):
        turn(started_s=111.0)
    with pytest.raises(ValidationError):
        turn(started_s=float("inf"))


def test_known_failure_is_not_hidden_by_incomplete_capture() -> None:
    source = turn(terminal_capture_complete=False)
    terminal = source.terminals[0].model_copy(update={"outcome": "errored"})
    source = source.model_copy(update={"terminals": (terminal,)})
    assert evaluate(recording(source), [{"kind": "turn_completed", "turn": 1}]).status == "FAIL"
    followup = turn(2, session="changed", session_capture_complete=False)
    assert (
        evaluate(recording(turn(), followup), [{"kind": "same_session", "turn": 2}]).status
        == "FAIL"
    )
