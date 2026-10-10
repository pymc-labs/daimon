"""Outcome predicates reject false certificates without provider calls or clocks."""

from typing import Literal

import pytest
from daimon.testing.outcome_oracle import (
    ApprovalEffect,
    CardUpdate,
    LogObservation,
    ReactionEffect,
    RunEvidence,
    SessionUse,
    TerminalEvidence,
    ToolEffect,
    TurnEvidence,
    VisibleText,
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


def rich_turn(**changes: object) -> TurnEvidence:
    values = dict(
        texts=(
            VisibleText(
                evidence_id="answer", order=6, observed_s=101.0, message_id="msg", text="Done"
            ),
        ),
        cards=(
            CardUpdate(
                evidence_id="progress", order=0, observed_s=101.0, card_id="card", state="progress"
            ),
            CardUpdate(
                evidence_id="final", order=5, observed_s=101.0, card_id="card", state="finalized"
            ),
        ),
        tools=(
            ToolEffect(
                evidence_id="tool-start",
                order=3,
                observed_s=101.0,
                call_id="call",
                tool_name="publish",
                phase="started",
            ),
            ToolEffect(
                evidence_id="tool-end",
                order=4,
                observed_s=101.0,
                call_id="call",
                tool_name="publish",
                phase="succeeded",
            ),
        ),
        approvals=(
            ApprovalEffect(
                evidence_id="ask",
                order=1,
                observed_s=101.0,
                action_id="approval",
                call_id="call",
                tool_name="publish",
                state="requested",
            ),
            ApprovalEffect(
                evidence_id="decision",
                order=2,
                observed_s=101.0,
                action_id="approval",
                call_id="call",
                tool_name="publish",
                state="approved",
            ),
        ),
        text_capture_complete=True,
        card_capture_complete=True,
        tool_capture_complete=True,
        approval_capture_complete=True,
    )
    return TurnEvidence.model_validate(turn().model_dump() | values | changes)


@pytest.mark.parametrize("backend", ("anthropic", "openai", "gemini"))
def test_all_outcome_families_are_usable_on_every_backend(
    backend: Literal["anthropic", "openai", "gemini"],
) -> None:
    source = rich_turn()
    source = rich_turn(texts=(source.texts[0].model_copy(update={"text": f"Done from {backend}"}),))
    assertions: list[dict[str, JsonValue]] = [
        {"kind": "turn_completed", "turn": 1},
        {"kind": "done_within_s", "turn": 1, "max": 10},
        {"kind": "text_present", "turn": 1, "pattern": "Done"},
        {"kind": "text_absent", "turn": 1, "pattern": "BAD"},
        {"kind": "no_preamble", "turn": 1},
        {"kind": "progress_seen", "turn": 1},
        {"kind": "card_finalized", "turn": 1},
        {"kind": "tool_succeeded", "turn": 1, "tool_name": "publish"},
        {"kind": "approval_effect", "turn": 1, "tool_name": "publish", "decision": "approved"},
    ]
    result = evaluate(recording(source, backend=backend), assertions)
    assert result.status == "PASS"
    assert all(check.evidence_ids for check in result.checks)
    assert (
        result.normalized_outcomes
        == evaluate(recording(rich_turn()), assertions).normalized_outcomes
    )


@pytest.mark.parametrize(
    "text",
    (
        "I came across your conversation",
        "This conversation moved here",
        "Working files could not be saved",
    ),
)
def test_visible_preamble_is_rejected_even_on_incomplete_capture(text: str) -> None:
    source = rich_turn()
    source = rich_turn(
        texts=(source.texts[0].model_copy(update={"text": text}),), text_capture_complete=False
    )
    assert evaluate(recording(source), [{"kind": "no_preamble", "turn": 1}]).status == "FAIL"


def test_text_patterns_cover_visible_chunks_and_require_capture() -> None:
    source = rich_turn(
        texts=(
            VisibleText(
                evidence_id="part1", order=6, observed_s=101.0, message_id="msg", text="I came "
            ),
            VisibleText(
                evidence_id="part2",
                order=7,
                observed_s=101.0,
                message_id="msg2",
                text="across this",
            ),
        )
    )
    assert evaluate(recording(source), [{"kind": "no_preamble", "turn": 1}]).status == "FAIL"
    assert (
        evaluate(
            recording(rich_turn(text_capture_complete=False)),
            [{"kind": "text_absent", "turn": 1, "pattern": "BAD"}],
        ).status
        == "PENDING"
    )
    assert (
        evaluate(recording(rich_turn(texts=())), [{"kind": "no_preamble", "turn": 1}]).status
        == "FAIL"
    )
    assert (
        evaluate(
            recording(rich_turn(texts=())), [{"kind": "text_present", "turn": 1, "pattern": ".*"}]
        ).status
        == "FAIL"
    )


@pytest.mark.parametrize("mutation", ("missing", "unfinished", "regressed", "other-card"))
def test_every_progress_card_must_finish_in_its_latest_state(mutation: str) -> None:
    cards = rich_turn().cards
    if mutation == "missing":
        cards = ()
    elif mutation == "unfinished":
        cards = cards[:1]
    elif mutation == "regressed":
        cards += (cards[0].model_copy(update={"evidence_id": "regressed", "order": 7}),)
    else:
        cards += (
            cards[0].model_copy(update={"evidence_id": "other", "order": 7, "card_id": "other"}),
        )
    assert (
        evaluate(recording(rich_turn(cards=cards)), [{"kind": "card_finalized", "turn": 1}]).status
        == "FAIL"
    )


def test_card_finalization_is_not_inferred_from_a_completed_turn() -> None:
    assert evaluate(recording(turn()), [{"kind": "card_finalized", "turn": 1}]).status == "PENDING"
    source = rich_turn(cards=rich_turn().cards[1:])
    assert evaluate(recording(source), [{"kind": "progress_seen", "turn": 1}]).status == "FAIL"


@pytest.mark.parametrize("mutation", ("orphan", "error", "wrong-name", "duplicate", "started-only"))
def test_tool_success_requires_one_linked_actual_execution(mutation: str) -> None:
    tools = rich_turn().tools
    if mutation == "orphan":
        tools = tools[1:]
    elif mutation == "error":
        tools = (tools[0], tools[1].model_copy(update={"phase": "errored"}))
    elif mutation == "wrong-name":
        tools = (tools[0], tools[1].model_copy(update={"tool_name": "other"}))
    elif mutation == "duplicate":
        tools += (tools[1].model_copy(update={"evidence_id": "duplicate", "order": 7}),)
    else:
        tools = tools[:1]
    assert (
        evaluate(
            recording(rich_turn(tools=tools)),
            [{"kind": "tool_succeeded", "turn": 1, "tool_name": "publish"}],
        ).status
        == "FAIL"
    )


@pytest.mark.parametrize("decision", ("denied", "timed_out"))
def test_denied_or_timed_out_approval_requires_no_tool_execution(decision: str) -> None:
    approvals = rich_turn().approvals
    approvals = (approvals[0], approvals[1].model_copy(update={"state": decision}))
    assertion: dict[str, JsonValue] = {
        "kind": "approval_effect",
        "turn": 1,
        "tool_name": "publish",
        "decision": decision,
    }
    assert (
        evaluate(recording(rich_turn(approvals=approvals, tools=())), [assertion]).status == "PASS"
    )
    assert evaluate(recording(rich_turn(approvals=approvals)), [assertion]).status == "FAIL"


@pytest.mark.parametrize(
    "mutation",
    ("missing", "wrong-call", "wrong-action", "wrong-decision", "missing-tool", "early-execution"),
)
def test_approval_effect_is_bound_to_the_decision_and_execution(mutation: str) -> None:
    source = rich_turn()
    approvals, tools = source.approvals, source.tools
    if mutation == "missing":
        approvals = ()
    elif mutation == "wrong-call":
        approvals = (approvals[0], approvals[1].model_copy(update={"call_id": "foreign"}))
    elif mutation == "wrong-action":
        approvals = (approvals[0], approvals[1].model_copy(update={"action_id": "foreign"}))
    elif mutation == "wrong-decision":
        approvals = (approvals[0], approvals[1].model_copy(update={"state": "denied"}))
    elif mutation == "missing-tool":
        tools = ()
    else:
        # Clocks are equal: the global capture order still proves unsafe execution.
        approvals = (approvals[0], approvals[1].model_copy(update={"order": 7}))
    assertion: dict[str, JsonValue] = {
        "kind": "approval_effect",
        "turn": 1,
        "tool_name": "publish",
        "decision": "approved",
    }
    assert (
        evaluate(recording(rich_turn(approvals=approvals, tools=tools)), [assertion]).status
        == "FAIL"
    )


@pytest.mark.parametrize(
    "kind,coverage",
    (
        ("text_absent", "text_capture_complete"),
        ("card_finalized", "card_capture_complete"),
        ("tool_succeeded", "tool_capture_complete"),
        ("approval_effect", "approval_capture_complete"),
    ),
)
def test_effect_domains_require_complete_capture(kind: str, coverage: str) -> None:
    assertion: dict[str, JsonValue] = {"kind": kind, "turn": 1}
    if kind == "text_absent":
        assertion["pattern"] = "BAD"
    elif kind in ("tool_succeeded", "approval_effect"):
        assertion["tool_name"] = "publish"
    if kind == "approval_effect":
        assertion["decision"] = "approved"
    assert evaluate(recording(rich_turn(**{coverage: False})), [assertion]).status == "PENDING"


@pytest.mark.parametrize(
    "assertion",
    (
        {"kind": "text_absent", "turn": 1, "pattern": "["},
        {"kind": "text_present", "turn": 1},
        {"kind": "card_finalized", "turn": 1, "max": 10},
        {"kind": "approval_effect", "turn": 1, "tool_name": "publish"},
        {"kind": "tool_succeeded", "turn": 1, "tool_name": "publish", "min": 2, "max": 1},
    ),
)
def test_malformed_effect_assertions_fail(assertion: dict[str, JsonValue]) -> None:
    assert evaluate(recording(rich_turn()), [assertion]).checks[0].code == "INVALID_ASSERTION"


def test_invalid_host_capture_order_is_rejected() -> None:
    source = rich_turn()
    with pytest.raises(ValidationError, match="order must be unique"):
        rich_turn(cards=(source.cards[0].model_copy(update={"order": 6}),))
    with pytest.raises(ValidationError, match="retain capture order"):
        rich_turn(cards=tuple(reversed(source.cards)))
    with pytest.raises(ValidationError, match="clock and order disagree"):
        rich_turn(texts=(source.texts[0].model_copy(update={"observed_s": 100.5}),))


def test_effect_counts_are_distinct_calls_and_malformed_extra_calls_fail() -> None:
    source = rich_turn()
    tools = source.tools + tuple(
        item.model_copy(
            update={
                "evidence_id": f"second-{item.evidence_id}",
                "call_id": "second",
                "order": item.order + 4,
            }
        )
        for item in source.tools
    )
    assertion: dict[str, JsonValue] = {
        "kind": "tool_succeeded",
        "turn": 1,
        "tool_name": "publish",
        "min": 2,
        "max": 2,
    }
    assert evaluate(recording(rich_turn(tools=tools)), [assertion]).status == "PASS"
    assert evaluate(recording(source), [assertion]).status == "FAIL"
    assert (
        evaluate(recording(rich_turn(tools=tools)), [assertion | {"max": 1, "min": 1}]).status
        == "FAIL"
    )
    assert (
        evaluate(recording(rich_turn(tools=tools[:-1])), [assertion | {"min": 1}]).status == "FAIL"
    )


def test_two_approval_cards_for_one_call_cannot_inflate_effect_count() -> None:
    source = rich_turn()
    approvals = source.approvals + tuple(
        item.model_copy(
            update={
                "evidence_id": f"second-{item.evidence_id}",
                "action_id": "second",
                "order": item.order + 10,
            }
        )
        for item in source.approvals
    )
    # Denial has no execution. Two decision cards still concern one proposed call.
    approvals = tuple(
        item.model_copy(update={"state": "denied"}) if item.state == "approved" else item
        for item in approvals
    )
    source = rich_turn(approvals=approvals, tools=())
    assert (
        evaluate(
            recording(source),
            [
                {
                    "kind": "approval_effect",
                    "turn": 1,
                    "tool_name": "publish",
                    "decision": "denied",
                    "min": 2,
                }
            ],
        ).status
        == "FAIL"
    )


@pytest.mark.parametrize("outcome", ("completed", "interrupted", "errored", "terminated"))
def test_catalog_terminal_latency_is_separate_from_success(outcome: str) -> None:
    source = turn()
    terminal = source.terminals[0].model_copy(update={"outcome": outcome, "observed_s": 100.3})
    source = turn(started_s=100.1, terminals=(terminal,))
    assert (
        evaluate(recording(source), [{"kind": "done_within_s", "turn": 1, "max": 0.2}]).status
        == "PASS"
    )
    assert evaluate(recording(source), [{"kind": "turn_completed", "turn": 1}]).status == (
        "PASS" if outcome == "completed" else "FAIL"
    )


@pytest.mark.parametrize("kind", (None, False, 1, "", " "))
def test_invalid_kind_shape_fails(kind: JsonValue) -> None:
    assert evaluate(recording(turn()), [{"kind": kind}]).status == "FAIL"


@pytest.mark.parametrize("kind", ("reply_within_s", "no_silent_drop"))
@pytest.mark.parametrize("signal", ("text", "card", "reaction"))
def test_first_visible_latency_accepts_an_actual_visible_signal(kind: str, signal: str) -> None:
    effects: dict[str, object] = dict(texts=(), cards=(), tools=(), approvals=())
    if signal == "text":
        effects["texts"] = rich_turn().texts
    elif signal == "card":
        effects["cards"] = rich_turn().cards
    else:
        effects["reactions"] = (
            ReactionEffect(evidence_id="reaction", order=0, observed_s=101.0, emoji="👀"),
        )
    assert (
        evaluate(recording(rich_turn(**effects)), [{"kind": kind, "turn": 1, "max": 1}]).status
        == "PASS"
    )


def test_silence_requires_complete_visible_capture_and_blank_text_is_not_a_reply() -> None:
    assertion: dict[str, JsonValue] = {"kind": "no_silent_drop", "turn": 1, "max": 1}
    source = rich_turn(texts=(), cards=())
    assert evaluate(recording(source), [assertion]).status == "PENDING"
    source = rich_turn(texts=(), cards=(), reaction_capture_complete=True)
    assert evaluate(recording(source), [assertion]).status == "FAIL"
    source = rich_turn(
        cards=(),
        texts=(rich_turn().texts[0].model_copy(update={"text": " "}),),
        reaction_capture_complete=True,
    )
    assert evaluate(recording(source), [assertion]).status == "FAIL"
    assert (
        evaluate(
            recording(rich_turn(reaction_capture_complete=True)), [assertion | {"max": 0.99}]
        ).status
        == "FAIL"
    )


def test_progress_latency_honors_catalog_within_s() -> None:
    assert (
        evaluate(
            recording(rich_turn()), [{"kind": "progress_seen", "turn": 1, "within_s": 1}]
        ).status
        == "PASS"
    )
    assert (
        evaluate(
            recording(rich_turn()), [{"kind": "progress_seen", "turn": 1, "within_s": 0.5}]
        ).status
        == "FAIL"
    )


def test_log_presence_and_absence_match_event_and_exact_requested_fields() -> None:
    source = rich_turn(
        logs=(
            LogObservation(
                evidence_id="replacement-log",
                order=7,
                observed_s=101.0,
                event="session_preparation.replaced",
                fields={"reasons": ["skills"], "other": "preserved"},
            ),
        ),
        log_capture_complete=True,
    )
    assertion: dict[str, JsonValue] = {
        "kind": "log_present",
        "turn": 1,
        "event": "session_preparation.replaced",
        "fields": {"reasons": ["skills"]},
    }
    assert evaluate(recording(source), [assertion]).status == "PASS"
    wrong_fields: dict[str, JsonValue] = {"reasons": ["environment"]}
    assert evaluate(recording(source), [assertion | {"fields": wrong_fields}]).status == "FAIL"
    assert evaluate(recording(source), [assertion | {"kind": "log_absent"}]).status == "FAIL"
    assert (
        evaluate(recording(rich_turn()), [assertion | {"kind": "log_absent"}]).status == "PENDING"
    )
    assert (
        evaluate(
            recording(rich_turn(log_capture_complete=True)), [assertion | {"kind": "log_absent"}]
        ).status
        == "PASS"
    )


def test_recording_round_trip_is_replayable_without_sdk_objects() -> None:
    source = recording(rich_turn())
    restored = RunEvidence.model_validate_json(source.model_dump_json())
    assertions: list[dict[str, JsonValue]] = [
        {"kind": "turn_completed", "turn": 1},
        {"kind": "card_finalized", "turn": 1},
    ]
    assert (
        evaluate(restored, assertions).normalized_outcomes
        == evaluate(source, assertions).normalized_outcomes
    )


def test_missing_decision_in_partial_capture_is_pending_and_observed_early_execution_fails() -> (
    None
):
    source = rich_turn()
    assertion: dict[str, JsonValue] = {
        "kind": "approval_effect",
        "turn": 1,
        "tool_name": "publish",
        "decision": "approved",
    }
    assert (
        evaluate(
            recording(rich_turn(approvals=source.approvals[:1], approval_capture_complete=False)),
            [assertion],
        ).status
        == "PENDING"
    )
    approvals = (source.approvals[0], source.approvals[1].model_copy(update={"order": 7}))
    assert (
        evaluate(
            recording(rich_turn(approvals=approvals, approval_capture_complete=False)), [assertion]
        ).status
        == "FAIL"
    )
