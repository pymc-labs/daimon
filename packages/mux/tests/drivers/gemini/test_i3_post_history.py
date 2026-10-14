"""Structural oracle refusal controls; these synthetic controls are not cell tapes."""

import json
from pathlib import Path
from typing import Literal

import pytest
from daimon.testing.outcome_oracle import (
    OutcomeReport,
    PostEvent,
    PostHistoryEvidence,
    PostMessage,
    PostRootAcknowledgement,
    RunEvidence,
    TerminalEvidence,
    TurnEvidence,
    evaluate,
)


def message(*, text: str = "", card: bool = True, id_: str = "message") -> PostMessage:
    payload = {
        "id": id_,
        "channel_id": "channel",
        "content": text,
        "embeds": [{"title": "Done"}] if card else [],
        "components": [],
        "attachments": [],
    }
    return PostMessage(
        message_id=id_,
        channel_id="channel",
        order=0,
        observed_s=101.0,
        content=text,
        embed_texts=("Done",) if card else (),
        component_labels=(),
        attachment_names=(),
        payload_json=json.dumps(payload),
    )


def event(
    operation: Literal["send", "edit", "delete"], post: PostMessage, index: int = 0
) -> PostEvent:
    return PostEvent(
        evidence_id=f"capture:post:{index}", operation=operation, message=post, request_json="{}"
    )


def source(*, events: tuple[PostEvent, ...] | None = None, complete: bool = True) -> TurnEvidence:
    post = message()
    events = events if events is not None else (event("send", post),)
    survivors: dict[str, PostMessage] = {}
    for item in events:
        if item.operation == "delete":
            survivors.pop(item.message.message_id, None)
        else:
            survivors[item.message.message_id] = item.message
    return TurnEvidence(
        turn=1,
        slot_id="slot",
        session_id="session",
        root_turn_id="root",
        started_s=100.0,
        terminals=(
            TerminalEvidence(
                evidence_id="terminal",
                session_id="session",
                root_turn_id="root",
                authority="record",
                outcome="completed",
                observed_s=110.0,
            ),
        ),
        terminal_capture_complete=True,
        post_history=PostHistoryEvidence(
            boundary="discord_adapter_posts",
            evidence_id="capture",
            session_id="session",
            root_turn_id="root",
            started_s=100.0,
            observed_s=109.0,
            events=events,
            messages=tuple(survivors.values()),
            post_capture_complete=complete,
            text_capture_complete=complete,
            acknowledgement=PostRootAcknowledgement(
                evidence_id="durable-ack",
                session_id="session",
                root_turn_id="root",
                operation_id="operation",
                status="queued",
                input_ids=("native-input",),
            ),
        ),
    )


def report(
    turn: TurnEvidence, backend: Literal["anthropic", "openai", "gemini"] = "gemini"
) -> OutcomeReport:
    return evaluate(
        RunEvidence(scenario_id="direct-structural-control", backend=backend, turns=(turn,)),
        [{"kind": "no_blank_message", "turn": 1}],
    )


@pytest.mark.parametrize("backend", ("anthropic", "openai", "gemini"))
def test_completed_card_is_structurally_nonblank_without_an_answer(
    backend: Literal["anthropic", "openai", "gemini"],
) -> None:
    result = report(source(), backend)
    assert result.status == "PASS" and result.checks[0].code == "NO_BLANK_MESSAGE"


@pytest.mark.parametrize("text", ("", " ", "\n\t"))
@pytest.mark.parametrize("later", ("none", "edit", "delete"))
def test_blank_send_cannot_hide_in_survivors(text: str, later: str) -> None:
    blank = message(text=text, card=False)
    events = (event("send", blank),)
    if later == "edit":
        events += (event("edit", message(), 1),)
    elif later == "delete":
        events += (event("delete", blank, 1),)
    assert report(source(events=events)).checks[0].code == "BLANK_MESSAGE_POSTED"


def test_blank_edit_is_retained_even_if_subsequently_repaired_and_deleted() -> None:
    blank, card = message(card=False), message()
    events = (
        event("send", card),
        event("edit", blank, 1),
        event("edit", card, 2),
        event("delete", card, 3),
    )
    assert report(source(events=events)).checks[0].code == "BLANK_MESSAGE_POSTED"


def test_zero_posts_are_not_vacuous_success() -> None:
    assert report(source(events=())).checks[0].code == "POST_EVIDENCE_MISSING"


def test_observed_blank_fails_even_with_incomplete_history() -> None:
    assert (
        report(source(events=(event("send", message(card=False)),), complete=False)).status
        == "FAIL"
    )


def test_nonblank_incomplete_history_stays_pending() -> None:
    assert report(source(complete=False)).checks[0].code == "POST_HISTORY_INCOMPLETE"


@pytest.mark.parametrize("change", ("history", "ack", "input_ids", "uncertain"))
def test_missing_independent_ack_or_history_stays_pending(change: str) -> None:
    turn = source()
    history = turn.post_history
    assert history is not None and history.acknowledgement is not None
    if change == "history":
        turn = turn.model_copy(update={"post_history": None})
    elif change == "ack":
        turn = turn.model_copy(
            update={"post_history": history.model_copy(update={"acknowledgement": None})}
        )
    else:
        ack = history.acknowledgement.model_copy(
            update={"input_ids": ()} if change == "input_ids" else {"status": "outcome_unknown"}
        )
        turn = turn.model_copy(
            update={"post_history": history.model_copy(update={"acknowledgement": ack})}
        )
    assert report(turn).status == "PENDING"


@pytest.mark.parametrize("field", ("session_id", "root_turn_id"))
@pytest.mark.parametrize("target", ("history", "ack"))
def test_foreign_root_or_session_is_refused(field: str, target: str) -> None:
    turn = source()
    history = turn.post_history
    assert history is not None and history.acknowledgement is not None
    if target == "history":
        history = history.model_copy(update={field: "foreign"})
    else:
        history = history.model_copy(
            update={
                "acknowledgement": history.acknowledgement.model_copy(update={field: "foreign"})
            }
        )
    assert (
        report(turn.model_copy(update={"post_history": history})).checks[0].code
        == "POST_ROOT_MISMATCH"
    )


@pytest.mark.parametrize("authority", ("preview", "gap"))
def test_provider_preview_cannot_certify_a_completed_card(authority: str) -> None:
    turn = source()
    terminal = turn.terminals[0].model_copy(update={"authority": authority})
    assert report(turn.model_copy(update={"terminals": (terminal,)})).status == "FAIL"


@pytest.mark.parametrize("outcome", ("interrupted", "errored", "terminated"))
def test_failed_root_cannot_certify_i3(outcome: str) -> None:
    turn = source()
    terminal = turn.terminals[0].model_copy(update={"outcome": outcome})
    assert (
        report(turn.model_copy(update={"terminals": (terminal,)})).checks[0].code
        == "TURN_NOT_COMPLETED"
    )


@pytest.mark.parametrize(
    "bad",
    (
        "root_window",
        "event_order",
        "duplicate_id",
        "edit_before_send",
        "survivor",
        "payload_id",
        "payload_channel",
        "payload_invalid",
        "delete_snapshot",
    ),
)
def test_complete_capture_rejects_structural_forgeries(bad: str) -> None:
    turn = source()
    history = turn.post_history
    assert history is not None
    first = history.events[0]
    if bad == "root_window":
        history = history.model_copy(update={"started_s": 99.0})
    elif bad == "event_order":
        history = history.model_copy(
            update={"events": (first.model_copy(update={"evidence_id": "capture:post:1"}),)}
        )
    elif bad == "duplicate_id":
        history = history.model_copy(update={"evidence_id": "terminal"})
    elif bad == "edit_before_send":
        history = history.model_copy(
            update={"events": (first.model_copy(update={"operation": "edit"}),)}
        )
    elif bad == "survivor":
        history = history.model_copy(update={"messages": ()})
    elif bad == "delete_snapshot":
        history = history.model_copy(
            update={
                "events": (first, event("delete", message(text="unobserved"), 1)),
                "messages": (),
            }
        )
    else:
        payload = json.loads(first.message.payload_json)
        if bad == "payload_id":
            payload["id"] = "foreign"
        elif bad == "payload_channel":
            payload["channel_id"] = "foreign"
        post = first.message.model_copy(
            update={"payload_json": "[" if bad == "payload_invalid" else json.dumps(payload)}
        )
        history = history.model_copy(
            update={"events": (first.model_copy(update={"message": post}),), "messages": (post,)}
        )
    assert report(turn.model_copy(update={"post_history": history})).status == "FAIL"


def test_absent_optional_field_preserves_default_serialized_shape() -> None:
    turn = source().model_copy(update={"post_history": None})
    assert "post_history" not in turn.model_dump()
    assert "post_history" not in json.loads(turn.model_dump_json())
    assert report(turn).checks[0].code == "UNSUPPORTED_KIND"


@pytest.mark.parametrize("explicit_none", (False, True))
def test_absent_history_is_byte_identical_to_integration_turn_and_nested_serialization(
    explicit_none: bool,
) -> None:
    # Captured from unchanged 630aedd773b00eaa6b2d4d3ef036e3745e4830e9.
    baseline = (
        (Path(__file__).parent / "fixtures/i3-absent-turn-baseline.json")
        .read_bytes()
        .removesuffix(b"\n")
    )
    turn = TurnEvidence.model_validate_json(baseline)
    if explicit_none:
        turn = turn.model_copy(update={"post_history": None})
    assert turn.post_history is None
    assert turn.model_dump_json().encode() == baseline
    assert turn.model_dump(mode="json") == json.loads(baseline)
    run = RunEvidence(scenario_id="unchanged-consumer", backend="gemini", turns=(turn,))
    expected = (
        b'{"scenario_id":"unchanged-consumer","backend":"gemini","turns":[' + baseline + b"]}"
    )
    assert run.model_dump_json().encode() == expected
    assert evaluate(run, [{"kind": "turn_completed", "turn": 1}]).status == "PASS"
    legacy = evaluate(run, [{"kind": "no_blank_message", "turn": 1}])
    assert legacy.model_dump_json() == (
        '{"scenario_id":"unchanged-consumer","backend":"gemini","checks":'
        '[{"kind":"no_blank_message","turn":1,"status":"PENDING",'
        '"code":"UNSUPPORTED_KIND","evidence_ids":[]}]}'
    )
