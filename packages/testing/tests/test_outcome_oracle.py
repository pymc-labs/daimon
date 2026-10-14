"""Outcome predicates reject false certificates without provider calls or clocks."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

import pytest
from daimon.core.continuity.wakes import FrozenWorkerBatch
from daimon.core.fleet import (
    EXCERPT_LIMIT,
    FleetRun,
    FleetWorker,
    ResultPointer,
    result_digest,
    result_source_key,
    worker_slot,
)
from daimon.testing.outcome_oracle import (
    FLEET_FINDINGS,
    ApprovalEffect,
    CardUpdate,
    FleetCollectCapture,
    FleetEvidence,
    FleetReviewFacts,
    FleetWakeCapture,
    FleetWorkerCapture,
    LogObservation,
    PostEvent,
    PostHistoryEvidence,
    PostInputAcknowledgement,
    PostMessage,
    PostRootAcknowledgement,
    ReactionEffect,
    RunEvidence,
    SessionUse,
    TerminalEvidence,
    ToolEffect,
    TurnEvidence,
    VisibleText,
    evaluate,
)
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.resources import ProviderBinding
from mux.profiles import backend_selection
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


@pytest.mark.parametrize("decision", ("approved", "denied", "timed_out"))
def test_approval_cannot_authorize_an_unlinked_execution(
    decision: Literal["approved", "denied", "timed_out"],
) -> None:
    source = rich_turn()
    approvals = (source.approvals[0], source.approvals[1].model_copy(update={"state": decision}))
    extra = tuple(
        item.model_copy(
            update={
                "call_id": "unapproved-call",
                "evidence_id": f"unapproved-{item.evidence_id}",
                "order": item.order + 4,
            }
        )
        for item in source.tools
    )
    linked = source.tools if decision == "approved" else ()
    report = evaluate(
        recording(rich_turn(approvals=approvals, tools=(*linked, *extra))),
        [
            {
                "kind": "approval_effect",
                "turn": 1,
                "tool_name": "publish",
                "decision": decision,
            }
        ],
    )
    assert report.status == "FAIL"
    assert report.checks[0].code == "TOOL_EXECUTED_WITHOUT_APPROVAL"
    assert all(item.evidence_id in report.checks[0].evidence_ids for item in extra)


@pytest.mark.parametrize("kind", ("log_present", "log_absent"))
def test_setup_log_window_is_valid_but_pending_without_capture(kind: str) -> None:
    report = evaluate(
        recording(turn()), [{"kind": kind, "turn": 0, "event": "agent_fork.dropped_skills"}]
    )
    assert report.status == "PENDING"
    assert report.checks[0].code == "TURN_NOT_CAPTURED"


def test_every_unapproved_matching_call_is_checked_without_effect_count_maximum() -> None:
    source = rich_turn()
    tools = tuple(item.model_copy(update={"call_id": "unapproved"}) for item in source.tools)
    report = evaluate(
        recording(rich_turn(tools=tools)),
        [
            {
                "kind": "approval_effect",
                "turn": 1,
                "tool_name": "publish",
                "decision": "approved",
            }
        ],
    )
    assert report.status == "FAIL"


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


# Independent offline observations; none are staging receipts or live credit.
FLEET_TEXT = {
    "W1": "A is unsafe: missing usage is not zero. Keep the conservative reserve.",
    "W2": "B is unsafe: a cloned worker cannot add an ungranted MCP server.",
    "W3": "C retains unknown exposure, narrows permissions, and joins native and platform evidence.",
    "Root": "Reject A and B, recommend C. W1 explained unknown costs; W2 rejected expansion; W3 verified evidence. OpenAI approved W1's exact journal result.",
}


def fixture_finding_judge(role, text, criterion):
    assert criterion
    return "PASS" if text == FLEET_TEXT[role] else "FAIL"


def fleet_recording() -> tuple[RunEvidence, dict]:
    now = datetime(2026, 10, 13, tzinfo=UTC)
    run = FleetRun(
        id=UUID(int=100),
        tenant_id=UUID(int=101),
        orchestrator_account_id=UUID(int=102),
        orchestrator_session_id="root-session",
        orchestrator_binding_id="root-binding",
        platform="discord",
        channel_id="channel",
        thread_id="thread",
        status="completed",
        total_budget_usd=Decimal(1),
        reserved_usd=Decimal(0),
        observed_usd=Decimal(".01"),
        deadline_at=now,
        max_workers=4,
        quorum=None,
        version=3,
    )
    common = dict(
        attempt_id="attempt", run_id=str(run.id), root_session_id=run.orchestrator_session_id
    )
    captures = []
    for ordinal, (backend, model, _) in FLEET_FINDINGS.items():
        worker = FleetWorker(
            id=UUID(int=ordinal),
            run_id=run.id,
            tenant_id=run.tenant_id,
            ordinal=ordinal,
            role=f"W{ordinal}",
            spec={"model": model},
            spec_digest="spec",
            backend=backend,
            profile=backend_selection(backend).profile,
            model=model,
            binding_id=f"binding-{ordinal}",
            status="succeeded",
            budget_usd=Decimal(".1"),
            reserved_usd=Decimal(0),
            deadline_at=now,
            max_turns=1,
            turns_used=1,
            attempts=1,
            reviewer_of=UUID(int=1) if ordinal == 4 else None,
            result_contract={
                "format": "review",
                "reviewed_digest": captures[0].worker.result.digest,
            }
            if ordinal == 4
            else {"format": "text", "review_required": ordinal == 1},
            result=None,
            error_code=None,
            version=2,
        )
        text = (
            json.dumps({"verdict": "approved", "reviewed_digest": captures[0].worker.result.digest})
            if ordinal == 4
            else FLEET_TEXT[f"W{ordinal}"]
        )
        result = Event(
            id=result_source_key(worker.id),
            session_id=f"session-{ordinal}",
            sequence=1,
            type="agent.message",
            turn_id=f"worker-root-{ordinal}",
            observed_at=now,
            authority="record",
            native=NativeProvenance(provider=backend, api_revision="fixture"),
            payload={
                "item_id": "message",
                "complete": True,
                "content": [{"type": "text", "text": text}],
            },
        )
        worker = replace(
            worker,
            result=ResultPointer(
                result.session_id, 1, result_digest(dict(result.payload)), text[:EXCERPT_LIMIT]
            ),
        )
        binding = ProviderBinding(
            id=worker.binding_id,
            thread=worker_slot(run, worker).thread,
            provider=backend,
            profile=worker.profile,
            native_refs={"session": result.session_id},
            generation=0,
            config_revision=1,
        )
        captures.append(
            FleetWorkerCapture(
                **common,
                evidence_id=f"capture-{ordinal}",
                worker=worker,
                binding=binding,
                input_record=PostInputAcknowledgement(
                    evidence_id=f"worker-input-{ordinal}",
                    input_id=f"input-{ordinal}",
                    session_id=result.session_id,
                    root_turn_id=result.turn_id,
                    authority="record",
                    submitted_text="attempt task",
                    observed_text="attempt task",
                ),
                terminal=TerminalEvidence(
                    evidence_id=f"worker-terminal-{ordinal}",
                    session_id=result.session_id,
                    root_turn_id=result.turn_id,
                    authority="record",
                    outcome="completed",
                    observed_s=40.0 if ordinal == 4 else 20.0 + ordinal,
                ),
                native_backend=backend,
                native_model=model,
                native_started_s=31.0 if ordinal == 4 else 10.0 + ordinal,
                result=result,
            )
        )
    turns = tuple(
        turn(n, session=run.orchestrator_session_id).model_copy(
            update={
                "slot_id": "root-slot",
                "started_s": start,
                "terminals": (
                    TerminalEvidence(
                        evidence_id=f"root-terminal-{n}",
                        session_id=run.orchestrator_session_id,
                        root_turn_id=f"root-{n}",
                        authority="record",
                        outcome="completed",
                        observed_s=end,
                    ),
                ),
            }
        )
        for n, start, end in ((1, 0.0, 5.0), (2, 30.0, 35.0), (3, 50.0, 60.0))
    )
    payload = json.dumps({"id": "answer", "channel_id": "thread", "content": FLEET_TEXT["Root"]})
    message = PostMessage(
        message_id="answer",
        channel_id="thread",
        order=0,
        observed_s=59.0,
        content=FLEET_TEXT["Root"],
        embed_texts=(),
        component_labels=(),
        attachment_names=(),
        payload_json=payload,
    )
    history = PostHistoryEvidence(
        boundary="discord_adapter_posts",
        evidence_id="history",
        session_id=run.orchestrator_session_id,
        root_turn_id="root-3",
        started_s=50.0,
        observed_s=59.0,
        events=(
            PostEvent(
                evidence_id="history:post:0",
                operation="send",
                message=message,
                request_json=json.dumps({"content": FLEET_TEXT["Root"]}),
            ),
        ),
        messages=(message,),
        post_capture_complete=True,
        text_capture_complete=True,
        acknowledgement=PostRootAcknowledgement(
            evidence_id="root-ack",
            session_id=run.orchestrator_session_id,
            root_turn_id="root-3",
            operation_id="root-operation",
            status="processed",
            input_ids=("root-input",),
            input_record=PostInputAcknowledgement(
                evidence_id="root-input-record",
                input_id="root-input",
                session_id=run.orchestrator_session_id,
                root_turn_id="root-3",
                authority="record",
                submitted_text="attempt root",
                observed_text="attempt root",
            ),
        ),
    )
    turns = (
        *turns[:2],
        turns[2].model_copy(
            update={
                "post_history": history,
                "card_capture_complete": True,
                "cards": (
                    CardUpdate(
                        evidence_id="final-card",
                        order=0,
                        observed_s=59.0,
                        card_id="answer",
                        state="finalized",
                    ),
                ),
            }
        ),
    )
    wakes = tuple(
        FleetWakeCapture(
            **common,
            evidence_id=f"wake-{i}",
            continuation_id=f"continuation-{i}",
            root_turn_id=f"root-{i + 1}",
            observed_s=t,
            batch=FrozenWorkerBatch(
                run_id=run.id,
                worker_ids=tuple(c.worker.id for c in members),
                quorum=len(members),
                binding_id=run.orchestrator_binding_id,
                session_id=run.orchestrator_session_id,
            ),
        )
        for i, members, t in ((1, captures[:3], 30.0), (2, captures[3:], 50.0))
    )
    collect = FleetCollectCapture(
        **common,
        evidence_id="collect",
        root_turn_id="root-3",
        observed_s=55.0,
        response={
            "run_id": str(run.id),
            "status": "ready",
            "results": [
                {"worker_id": str(c.worker.id), "result": FLEET_TEXT[f"W{c.worker.ordinal}"]}
                for c in captures[:3]
            ],
        },
    )
    source = RunEvidence(
        scenario_id="I7",
        backend="anthropic",
        turns=turns,
        fleet=FleetEvidence(
            run=run,
            workers=tuple(captures),
            workers_capture_complete=True,
            wakes=wakes,
            wakes_capture_complete=True,
            collect=collect,
        ),
    )
    return source, dict(
        kind="fleet_outcome",
        **common,
        root_binding_id=run.orchestrator_binding_id,
        root_card_id="answer",
    )


def replace_capture(source, index=0, **changes):
    captures = list(source.fleet.workers)
    captures[index] = captures[index].model_copy(update=changes)
    return source.model_copy(
        update={"fleet": source.fleet.model_copy(update={"workers": tuple(captures)})}
    )


def test_fleet_complete_fixture_and_json_roundtrip_pass():
    source, assertion = fleet_recording()
    for recording_ in (source, RunEvidence.model_validate_json(source.model_dump_json())):
        report = evaluate(recording_, [assertion], finding_judge=fixture_finding_judge)
        assert report.status == "PASS", report.normalized_outcomes


@pytest.mark.parametrize("started_s", [12.0, 22.0, 29.0])
@pytest.mark.parametrize("missing_wakes", [False, True])
def test_fleet_review_cannot_start_before_ready_cohort_and_resumed_root(started_s, missing_wakes):
    source, assertion = fleet_recording()
    source = replace_capture(source, 3, native_started_s=started_s)
    if missing_wakes:
        source = replace_fleet(source, wakes=(), wakes_capture_complete=False)
    restored = RunEvidence.model_validate_json(source.model_dump_json())
    report = evaluate(restored, [assertion], finding_judge=fixture_finding_judge)
    assert report.status == "FAIL", report.normalized_outcomes
    assert any(c.code == "REVIEW_STARTED_BEFORE_READY_ROOT" for c in report.checks)


def test_fleet_review_start_is_fenced_by_actual_resumed_root_input():
    source, assertion = fleet_recording()
    source = replace_capture(source, 3, native_started_s=27.0)
    source = replace_fleet(
        source,
        wakes=(
            source.fleet.wakes[0].model_copy(update={"observed_s": 25.0}),
            source.fleet.wakes[1],
        ),
    )
    restored = RunEvidence.model_validate_json(source.model_dump_json())
    report = evaluate(restored, [assertion], finding_judge=fixture_finding_judge)
    assert report.status == "FAIL"
    assert any(c.code == "REVIEW_STARTED_BEFORE_READY_ROOT" for c in report.checks)


@pytest.mark.parametrize("started_s", [30.0, 31.0, 36.0])
def test_fleet_review_may_start_during_or_after_resumed_root_turn(started_s):
    source, assertion = fleet_recording()
    source = replace_capture(source, 3, native_started_s=started_s)
    restored = RunEvidence.model_validate_json(source.model_dump_json())
    report = evaluate(restored, [assertion], finding_judge=fixture_finding_judge)
    assert report.status == "PASS", report.normalized_outcomes


def test_fleet_missing_review_start_is_pending():
    source, assertion = fleet_recording()
    source = replace_capture(source, 3, native_started_s=None)
    restored = RunEvidence.model_validate_json(source.model_dump_json())
    assert evaluate(restored, [assertion], finding_judge=fixture_finding_judge).status == "PENDING"


def test_fleet_known_early_review_fails_without_root_or_wake_capture():
    source, assertion = fleet_recording()
    source = replace_capture(source, 3, native_started_s=22.0)
    source = replace_fleet(source, wakes=(), wakes_capture_complete=False)
    source = source.model_copy(update={"turns": ()})
    restored = RunEvidence.model_validate_json(source.model_dump_json())
    report = evaluate(restored, [assertion], finding_judge=fixture_finding_judge)
    assert report.status == "FAIL"
    assert any(c.code == "REVIEW_STARTED_BEFORE_READY_ROOT" for c in report.checks)


@pytest.mark.parametrize("ordinal", [1, 2, 3])
def test_fleet_wrong_native_worker_finding_cannot_be_repaired_by_root(ordinal):
    source, assertion = fleet_recording()
    capture = source.fleet.workers[ordinal - 1]
    bad = capture.result.model_copy(
        update={
            "payload": {
                **dict(capture.result.payload),
                "content": [{"type": "text", "text": "Bare label C; wrong rationale."}],
            }
        }
    )
    pointer = replace(
        capture.worker.result,
        digest=result_digest(dict(bad.payload)),
        excerpt="Bare label C; wrong rationale.",
    )
    source = replace_capture(
        source, ordinal - 1, result=bad, worker=replace(capture.worker, result=pointer)
    )
    report = evaluate(source, [assertion], finding_judge=fixture_finding_judge)
    assert report.status == "FAIL"
    assert any(
        c.kind == f"fleet_outcome:W{ordinal}" and c.code == "FINDING_NOT_VERIFIED"
        for c in report.checks
    )


@pytest.mark.parametrize(
    "damage",
    [
        "attempt",
        "run",
        "root",
        "tenant",
        "backend",
        "model",
        "binding",
        "native_backend",
        "native_model",
        "native_session",
        "native_root",
        "input_text",
        "result_digest",
        "result_session",
        "result_id",
        "result_turn",
        "result_provider",
        "result_sequence",
        "extra_input",
    ],
)
def test_fleet_foreign_or_inconsistent_worker_evidence_fails(damage):
    source, assertion = fleet_recording()
    c = source.fleet.workers[0]
    changes = {}
    if damage in ("attempt", "run", "root"):
        changes[{"attempt": "attempt_id", "run": "run_id", "root": "root_session_id"}[damage]] = (
            "foreign"
        )
    elif damage in ("tenant", "backend", "model", "extra_input"):
        changes["worker"] = replace(
            c.worker,
            **{"tenant_id": UUID(int=999)}
            if damage == "tenant"
            else {"turns_used": 2}
            if damage == "extra_input"
            else {damage: "foreign"},
        )
    elif damage == "binding":
        changes["binding"] = c.binding.model_copy(update={"id": "foreign"})
    elif damage in ("native_backend", "native_model"):
        changes[damage] = "foreign"
    elif damage in ("native_session", "native_root", "input_text"):
        field = {
            "native_session": "session_id",
            "native_root": "root_turn_id",
            "input_text": "observed_text",
        }[damage]
        changes["input_record"] = c.input_record.model_copy(update={field: "foreign"})
    elif damage == "result_digest":
        changes["worker"] = replace(c.worker, result=replace(c.worker.result, digest="0" * 64))
    elif damage == "result_provider":
        changes["result"] = c.result.model_copy(
            update={"native": c.result.native.model_copy(update={"provider": "gemini"})}
        )
    else:
        changes["result"] = c.result.model_copy(
            update={
                {
                    "result_session": "session_id",
                    "result_id": "id",
                    "result_turn": "turn_id",
                    "result_sequence": "sequence",
                }[damage]: 99 if damage == "result_sequence" else "foreign"
            }
        )
    report = evaluate(
        replace_capture(source, **changes), [assertion], finding_judge=fixture_finding_judge
    )
    assert report.status == "FAIL", (damage, report.normalized_outcomes)


@pytest.mark.parametrize(
    "missing",
    [
        "binding",
        "input_record",
        "terminal",
        "result",
        "native_started_s",
        "native_backend",
        "native_model",
    ],
)
def test_fleet_partial_capture_is_pending_without_invented_failure(missing):
    source, assertion = fleet_recording()
    report = evaluate(
        replace_capture(source, **{missing: None}), [assertion], finding_judge=fixture_finding_judge
    )
    assert report.status == "PENDING", (missing, report.normalized_outcomes)


@pytest.mark.parametrize("missing", ["run", "workers", "wakes", "collect"])
@pytest.mark.parametrize("failure", ["native", "durable", "review"])
def test_fleet_known_failure_outranks_missing_downstream_evidence(missing, failure):
    source, assertion = fleet_recording()
    c = source.fleet.workers[0]
    if failure == "native":
        source = replace_capture(
            source, terminal=c.terminal.model_copy(update={"outcome": "errored"})
        )
    elif failure == "durable":
        source = replace_capture(source, worker=replace(c.worker, status="cancelled"))
    else:
        source = source.model_copy(
            update={
                "fleet": source.fleet.model_copy(
                    update={
                        "collect": source.fleet.collect.model_copy(
                            update={"response": {"status": "blocked", "reason": "review_refused"}}
                        )
                    }
                )
            }
        )
    if missing == "workers":
        source = source.model_copy(
            update={
                "fleet": source.fleet.model_copy(
                    update={"workers": source.fleet.workers[:1], "workers_capture_complete": False}
                )
            }
        )
    elif missing == "collect" and failure == "review":
        source = source.model_copy(
            update={
                "fleet": source.fleet.model_copy(
                    update={"wakes": (), "wakes_capture_complete": False}
                )
            }
        )
    else:
        source = source.model_copy(
            update={
                "fleet": source.fleet.model_copy(
                    update={missing: () if missing == "wakes" else None}
                )
            }
        )
    assert evaluate(source, [assertion], finding_judge=fixture_finding_judge).status == "FAIL"


def test_fleet_unbound_semantic_judge_never_passes():
    source, assertion = fleet_recording()
    assert evaluate(source, [assertion]).status == "PENDING"
    assert evaluate(source, [assertion], finding_judge=lambda *_: True).status == "FAIL"


def replace_fleet(source, **changes):
    return source.model_copy(update={"fleet": source.fleet.model_copy(update=changes)})


def replace_result_text(source, index, text):
    capture = source.fleet.workers[index]
    event = capture.result.model_copy(
        update={
            "payload": {**dict(capture.result.payload), "content": [{"type": "text", "text": text}]}
        }
    )
    pointer = replace(
        capture.worker.result,
        digest=result_digest(dict(event.payload)),
        excerpt=text[:EXCERPT_LIMIT],
    )
    return replace_capture(
        source, index, result=event, worker=replace(capture.worker, result=pointer)
    )


@pytest.mark.parametrize("damage", ["refused", "digest", "duplicate", "extra"])
@pytest.mark.parametrize("missing_run", [False, True])
def test_fleet_review_failures_survive_missing_run(damage, missing_run):
    source, assertion = fleet_recording()
    digest = source.fleet.workers[0].worker.result.digest
    verdict = {"verdict": "approved", "reviewed_digest": digest}
    if damage == "refused":
        verdict["verdict"] = "refused"
    elif damage == "digest":
        verdict["reviewed_digest"] = "foreign"
    elif damage == "extra":
        verdict["extra"] = "unbound"
    text = json.dumps(verdict)
    if damage == "duplicate":
        text = text[:-1] + ', "verdict":"approved"}'
    source = replace_result_text(source, 3, text)
    if missing_run:
        source = replace_fleet(source, run=None)
    report = evaluate(
        RunEvidence.model_validate_json(source.model_dump_json()),
        [assertion],
        finding_judge=fixture_finding_judge,
    )
    assert report.status == "FAIL"
    assert any(c.code == "REVIEW_REFUSED_OR_UNBOUND" for c in report.checks)


def test_fleet_known_bad_finding_survives_missing_run_and_delivery():
    source, assertion = fleet_recording()
    source = replace_fleet(
        replace_result_text(source, 1, "B expands permissions; accept it."), run=None
    )
    source = source.model_copy(update={"turns": ()})
    report = evaluate(source, [assertion], finding_judge=fixture_finding_judge)
    assert report.status == "FAIL"
    assert any(c.kind == "fleet_outcome:W2" and c.status == "FAIL" for c in report.checks)


@pytest.mark.parametrize(
    "damage",
    [
        "sequential",
        "wake_id",
        "wake_root",
        "batch",
        "quorum",
        "wake_early",
        "wake_late",
        "collect_early",
        "collect_empty",
        "collect_root",
        "root_session",
        "root_slot",
        "card",
        "message",
        "channel",
    ],
)
def test_fleet_root_and_parallel_observations_are_bound(damage):
    source, assertion = fleet_recording()
    fleet = source.fleet
    if damage == "sequential":
        source = replace_capture(source, 2, native_started_s=22.5)
    elif damage.startswith("wake") or damage in ("batch", "quorum"):
        wake = fleet.wakes[1]
        changes = {
            "wake_id": {"continuation_id": fleet.wakes[0].continuation_id},
            "wake_root": {"root_turn_id": "foreign"},
            "batch": {"batch": wake.batch.model_copy(update={"worker_ids": (UUID(int=1),)})},
            "quorum": {"batch": wake.batch.model_copy(update={"quorum": 0})},
            "wake_early": {"observed_s": 39.0},
            "wake_late": {"observed_s": 51.0},
        }[damage]
        source = replace_fleet(source, wakes=(fleet.wakes[0], wake.model_copy(update=changes)))
    elif damage.startswith("collect"):
        changes = {
            "collect_early": {"observed_s": 49.0},
            "collect_empty": {"response": {}},
            "collect_root": {"root_turn_id": "foreign"},
        }[damage]
        source = replace_fleet(source, collect=fleet.collect.model_copy(update=changes))
    else:
        final = source.turns[-1]
        if damage == "card":
            final = final.model_copy(
                update={"cards": (final.cards[0].model_copy(update={"card_id": "foreign"}),)}
            )
        elif damage in ("channel", "message"):
            history = final.post_history
            message = history.messages[0]
            payload = json.loads(message.payload_json)
            payload["channel_id" if damage == "channel" else "id"] = "foreign"
            message = message.model_copy(
                update={
                    "channel_id" if damage == "channel" else "message_id": "foreign",
                    "payload_json": json.dumps(payload),
                }
            )
            history = history.model_copy(
                update={
                    "messages": (message,),
                    "events": (history.events[0].model_copy(update={"message": message}),),
                }
            )
            final = final.model_copy(update={"post_history": history})
        else:
            final = final.model_copy(
                update={"session_id" if damage == "root_session" else "slot_id": "foreign"}
            )
        source = source.model_copy(update={"turns": (*source.turns[:-1], final)})
    report = evaluate(source, [assertion], finding_judge=fixture_finding_judge)
    assert report.status == "FAIL", (damage, report.normalized_outcomes)


@pytest.mark.parametrize(
    "damage",
    [
        "native_preview",
        "result_gap",
        "worker_pending",
        "missing_run",
        "worker_coverage",
        "wake_coverage",
        "card_unbound",
    ],
)
def test_fleet_missing_authority_or_coverage_never_passes(damage):
    source, assertion = fleet_recording()
    c = source.fleet.workers[0]
    if damage == "native_preview":
        source = replace_capture(
            source, terminal=c.terminal.model_copy(update={"authority": "preview"})
        )
    elif damage == "result_gap":
        source = replace_capture(source, result=c.result.model_copy(update={"authority": "gap"}))
    elif damage == "worker_pending":
        source = replace_capture(source, worker=replace(c.worker, status="running"))
    elif damage == "card_unbound":
        assertion.pop("root_card_id")
    else:
        key = {
            "missing_run": "run",
            "worker_coverage": "workers_capture_complete",
            "wake_coverage": "wakes_capture_complete",
        }[damage]
        source = replace_fleet(source, **{key: None if damage == "missing_run" else False})
    assert evaluate(source, [assertion], finding_judge=fixture_finding_judge).status == "PENDING"


def test_fleet_judge_receives_full_authoritative_payload_not_excerpt():
    source, assertion = fleet_recording()
    long_text = FLEET_TEXT["W2"] + " " * EXCERPT_LIMIT + " Full trailing rationale."
    source = replace_result_text(source, 1, long_text)
    collected = source.fleet.collect
    response = dict(collected.response)
    response["results"] = [
        {
            "worker_id": str(c.worker.id),
            "result": long_text if c.worker.ordinal == 2 else FLEET_TEXT[f"W{c.worker.ordinal}"],
        }
        for c in source.fleet.workers[:3]
    ]
    source = replace_fleet(source, collect=collected.model_copy(update={"response": response}))
    calls = []

    def judge(role, text, criterion):
        calls.append((role, text, criterion))
        return "PASS"

    restored = RunEvidence.model_validate_json(source.model_dump_json())
    assert evaluate(restored, [assertion], finding_judge=judge).status == "PASS"
    assert next(text for role, text, _ in calls if role == "W2") == long_text
    assert "parent grant" in next(c for role, _, c in calls if role == "W2")
    assert "fleet" not in recording(turn()).model_dump()


@pytest.mark.parametrize("damage", ["incomplete", "user_message", "zero_inputs"])
def test_fleet_completed_native_does_not_rescue_invalid_result_or_input(damage):
    source, assertion = fleet_recording()
    capture = source.fleet.workers[0]
    if damage == "zero_inputs":
        source = replace_capture(source, worker=replace(capture.worker, turns_used=0))
    else:
        changes = (
            {"type": "user.message"}
            if damage == "user_message"
            else {"payload": {**dict(capture.result.payload), "complete": False}}
        )
        event = capture.result.model_copy(update=changes)
        pointer = replace(capture.worker.result, digest=result_digest(dict(event.payload)))
        source = replace_capture(
            source, result=event, worker=replace(capture.worker, result=pointer)
        )
    assert evaluate(source, [assertion], finding_judge=fixture_finding_judge).status == "FAIL"


def test_fleet_disabled_required_review_fails_even_if_reviewer_is_missing():
    source, assertion = fleet_recording()
    worker = source.fleet.workers[0].worker
    source = replace_capture(source, worker=replace(worker, review=FleetReviewFacts(format="text")))
    source = replace_fleet(source, workers=source.fleet.workers[:3], workers_capture_complete=False)
    assert evaluate(source, [assertion], finding_judge=fixture_finding_judge).status == "FAIL"
