"""Real adapter captures plus synthetic oracle refusal controls, not cell tapes.

Acknowledgement/terminal/tool fixtures below test the structural envelope only.
No provider completion, whole catalog cell or live running window is certified.
"""

from __future__ import annotations

from typing import Any

import discord
import pytest
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState
from daimon.testing.discord_surface import DiscordSurfaceCapture, DiscordSurfaceHarness
from daimon.testing.outcome_oracle import (
    BlankMessageExpectation,
    CardUpdate,
    PostInputAcknowledgement,
    PostRootAcknowledgement,
    RunEvidence,
    TerminalEvidence,
    ToolEffect,
    TurnEvidence,
    evaluate,
    no_blank_message_check,
)
from daimon.testing.post_structure import (
    CardEditsExpectation,
    FencesExpectation,
    MessageCountExpectation,
    parse_expectation,
    post_history_from_capture,
    structure_check,
)
from mux.contracts.ids import Provider
from pydantic import ValidationError

_SESSION = "unit-control-session"
_ROOT = "unit-control-root"
_ASSERTIONS = (
    {"kind": "message_count", "turn": 1, "min": 2},
    {"kind": "fences_balanced", "turn": 1},
    {"kind": "chunks_gap_max_s", "turn": 1, "max": 5},
    {"kind": "footer_on_last_message", "turn": 1},
)


class Clock:
    now = 100.0

    def __call__(self) -> float:
        return self.now


def text_state(text: str) -> TurnState:
    return TurnState(content=[TextBlock(kind="text", text=text)])


def control_turn(capture: DiscordSurfaceCapture) -> TurnEvidence:
    # Independently authored oracle controls, explicitly not provider receipts.
    ack = PostRootAcknowledgement(
        evidence_id="unit-ack",
        session_id=_SESSION,
        root_turn_id=_ROOT,
        operation_id="unit-operation",
        status="queued",
        input_ids=("unit-input",),
    )
    return TurnEvidence(
        turn=1,
        slot_id="unit-slot",
        session_id=_SESSION,
        root_turn_id=_ROOT,
        started_s=100.0,
        terminal_capture_complete=True,
        terminals=(
            TerminalEvidence(
                evidence_id="unit-terminal",
                session_id=_SESSION,
                root_turn_id=_ROOT,
                authority="record",
                outcome="completed",
                observed_s=capture.observed_s + 1.0,
            ),
        ),
        post_history=post_history_from_capture(capture, acknowledgement=ack),
    )


def host_effects(
    turn: TurnEvidence,
    cards: tuple[CardUpdate, ...],
    tools: tuple[ToolEffect, ...] = (),
    *,
    tool_complete: bool = False,
) -> TurnEvidence:
    ordered = sorted((*cards, *tools), key=lambda item: item.observed_s)
    ordered = [item.model_copy(update={"order": index}) for index, item in enumerate(ordered)]
    return TurnEvidence.model_validate(
        {
            **turn.model_dump(),
            "cards": tuple(item for item in ordered if isinstance(item, CardUpdate)),
            "tools": tuple(item for item in ordered if isinstance(item, ToolEffect)),
            "card_capture_complete": True,
            "tool_capture_complete": tool_complete,
        }
    )


async def long_capture(*, notify: bool = False) -> tuple[DiscordSurfaceCapture, Clock]:
    clock = Clock()
    harness = DiscordSurfaceHarness(
        evidence_id="unit-capture",
        session_id=_SESSION,
        root_turn_id=_ROOT,
        clock=clock,
        notify_on_completion=notify,
    )
    code = (
        "```python\n"
        + 'def inventory():\n    """A unit rendering control."""\n    return 42\n\n' * 120
        + "```\n\n- One\n- Two\n- Three\n- Four\n- Five"
    )

    async def invoke(lc: DiscordTurnLifecycle) -> None:
        await lc.post_initial()
        clock.now += 45.0
        await lc.on_terminal_success(text_state(code))

    capture = await harness.run(invoke)
    return capture, clock


@pytest.mark.parametrize("backend", ["anthropic", "openai", "gemini"])
@pytest.mark.parametrize("notify", [False, True])
async def test_real_split_capture_passes_all_four_structural_controls(
    backend: Provider,
    notify: bool,
) -> None:
    capture, _ = await long_capture(notify=notify)
    assert backend in ("anthropic", "openai", "gemini")  # neutral envelope labels only
    assert len(capture.messages) > 2
    turn = control_turn(capture)
    checks = [structure_check(turn, parse_expectation(assertion)) for assertion in _ASSERTIONS]
    assert [check.status for check in checks] == ["PASS"] * 4
    # Initial status creation was 45s earlier, but the first actual answer
    # content arrived in its final edit alongside the overflow sends.
    assert capture.events[0].message.content == ""
    assert capture.events[0].message.observed_s == 100.0
    if not notify:
        assert any(event.operation == "edit" and event.message.content for event in capture.events)
    else:
        assert capture.events[-1].operation == "delete"
    assert all(check.evidence_ids for check in checks)


async def test_capture_helper_never_invents_native_ack_or_root() -> None:
    capture, _ = await long_capture()
    history = post_history_from_capture(capture)
    assert history.acknowledgement is None
    assert history.session_id == capture.session_id and history.root_turn_id == capture.root_turn_id
    turn = control_turn(capture).model_copy(update={"post_history": history})
    for assertion in _ASSERTIONS:
        check = structure_check(turn, parse_expectation(assertion))
        assert check.status == "PENDING" and check.code == "POST_ROOT_NOT_ACKNOWLEDGED"


@pytest.mark.parametrize("mutation", [None, "root", "preview", "text", "missing", "id_reuse"])
async def test_empty_receipt_input_ids_need_authoritative_input_record(
    mutation: str | None,
) -> None:
    capture, _ = await long_capture()
    turn = control_turn(capture)
    history = turn.post_history
    assert history is not None and history.acknowledgement is not None
    # Independently authored envelope control, not an actual provider receipt.
    record = PostInputAcknowledgement(
        evidence_id="unit-input-record",
        input_id="unit-input",
        session_id=_SESSION,
        root_turn_id=_ROOT,
        authority="record",
        submitted_text="unit control input",
        observed_text="unit control input",
    )
    if mutation == "root":
        record = record.model_copy(update={"root_turn_id": "foreign-root"})
    elif mutation == "preview":
        record = record.model_copy(update={"authority": "preview"})
    elif mutation == "text":
        record = record.model_copy(update={"observed_text": "another input"})
    elif mutation == "id_reuse":
        record = record.model_copy(update={"evidence_id": "unit-terminal"})
    ack = history.acknowledgement.model_copy(
        update={"input_ids": (), "input_record": None if mutation == "missing" else record}
    )
    turn = turn.model_copy(
        update={"post_history": history.model_copy(update={"acknowledgement": ack})}
    )
    assert ack.input_ids == ()  # Preserve SDK partial-response semantics.
    for assertion in _ASSERTIONS:
        result = structure_check(turn, parse_expectation(assertion))
        if mutation is None:
            assert result.status == "PASS" and record.evidence_id in result.evidence_ids
        else:
            assert result.status == ("PENDING" if mutation == "missing" else "FAIL")


@pytest.mark.parametrize("backend", ["anthropic", "openai", "gemini"])
async def test_public_oracle_dispatches_complete_post_history(backend: Provider) -> None:
    capture, _ = await long_capture()
    turn = control_turn(capture)
    recording = RunEvidence(scenario_id="unit-component", backend=backend, turns=(turn,))
    report = evaluate(recording, _ASSERTIONS)
    assert [check.status for check in report.checks] == ["PASS"] * 4
    malformed = evaluate(recording, ({"kind": "message_count", "turn": 1},))
    assert malformed.checks[0].code == "INVALID_ASSERTION"
    missing_card = evaluate(
        recording, ({"kind": "card_edits_min", "turn": 1, "min": 3, "during": "running"},)
    )
    assert missing_card.checks[0].status == "PENDING"
    assert missing_card.checks[0].code == "CARD_CAPTURE_INCOMPLETE"


@pytest.mark.parametrize("captured_turn", [False, True])
@pytest.mark.parametrize(
    "assertion",
    [*_ASSERTIONS, {"kind": "card_edits_min", "turn": 1, "min": 3, "during": "running"}],
)
def test_absent_history_preserves_legacy_verdict_bytes(
    captured_turn: bool, assertion: dict[str, Any]
) -> None:
    turns = (
        (
            TurnEvidence(
                turn=1,
                slot_id="unit-slot",
                session_id=_SESSION,
                root_turn_id=_ROOT,
                started_s=100.0,
            ),
        )
        if captured_turn
        else ()
    )
    recording = RunEvidence(scenario_id="unit-component", backend="anthropic", turns=turns)
    assert "post_history" not in recording.model_dump_json()
    report = evaluate(recording, (assertion,))
    assert report.model_dump_json() == (
        '{"scenario_id":"unit-component","backend":"anthropic","checks":'
        '[{"kind":"'
        + assertion["kind"]
        + '","turn":1,"status":"PENDING","code":"UNSUPPORTED_KIND","evidence_ids":[]}]}'
    )


@pytest.mark.parametrize(
    "kind",
    [
        "message_count",
        "fences_balanced",
        "chunks_gap_max_s",
        "footer_on_last_message",
        "card_edits_min",
    ],
)
@pytest.mark.parametrize("gap", ["history", "ack", "uncertain", "input_ids", "terminal", "partial"])
async def test_missing_capture_or_ack_is_pending_for_every_kind(kind: str, gap: str) -> None:
    capture, _ = await long_capture()
    turn = control_turn(capture)
    history = turn.post_history
    assert history is not None and history.acknowledgement is not None
    if gap == "history":
        turn = turn.model_copy(update={"post_history": None})
    elif gap == "terminal":
        turn = turn.model_copy(update={"terminals": (), "terminal_capture_complete": False})
    else:
        update: dict[str, Any] = {}
        if gap == "partial":
            update["post_capture_complete"] = False
        elif gap == "ack":
            update["acknowledgement"] = None
        elif gap == "uncertain":
            update["acknowledgement"] = history.acknowledgement.model_copy(
                update={"status": "outcome_unknown"}
            )
        else:
            update["acknowledgement"] = history.acknowledgement.model_copy(update={"input_ids": ()})
        turn = turn.model_copy(update={"post_history": history.model_copy(update=update)})
    assertion: dict[str, Any] = {"kind": kind, "turn": 1}
    if kind in ("message_count", "card_edits_min"):
        assertion["min"] = 2
    if kind == "card_edits_min":
        assertion["during"] = "running"
    if kind == "chunks_gap_max_s":
        assertion["max"] = 5
    assert structure_check(turn, parse_expectation(assertion)).status == "PENDING"


@pytest.mark.parametrize(
    "mutation",
    [
        "root",
        "foreign_ack",
        "preview",
        "unknown_terminal",
        "duplicate_id",
        "order",
        "survivor",
        "payload",
        "future_window",
    ],
)
async def test_invalid_history_cannot_certify_structural_success(mutation: str) -> None:
    capture, _ = await long_capture()
    turn = control_turn(capture)
    history = turn.post_history
    assert history is not None and history.acknowledgement is not None
    if mutation == "root":
        history = history.model_copy(update={"root_turn_id": "foreign-root"})
    elif mutation == "foreign_ack":
        history = history.model_copy(
            update={
                "acknowledgement": history.acknowledgement.model_copy(
                    update={"root_turn_id": "foreign-root"}
                )
            }
        )
    elif mutation == "duplicate_id":
        history = history.model_copy(update={"evidence_id": "unit-terminal"})
    elif mutation == "order":
        history = history.model_copy(update={"events": tuple(reversed(history.events))})
    elif mutation == "survivor":
        history = history.model_copy(update={"messages": history.messages[:-1]})
    elif mutation == "payload":
        event = history.events[0]
        event = event.model_copy(
            update={"message": event.message.model_copy(update={"payload_json": "[]"})}
        )
        history = history.model_copy(update={"events": (event, *history.events[1:])})
    elif mutation == "future_window":
        history = history.model_copy(update={"observed_s": capture.observed_s + 10.0})
        turn = turn.model_copy(
            update={
                "terminals": (
                    *turn.terminals,
                    turn.terminals[0].model_copy(
                        update={
                            "evidence_id": "late-preview",
                            "authority": "preview",
                            "observed_s": capture.observed_s + 11.0,
                        }
                    ),
                )
            }
        )
    elif mutation == "preview":
        turn = turn.model_copy(
            update={"terminals": (turn.terminals[0].model_copy(update={"authority": "preview"}),)}
        )
    else:
        turn = turn.model_copy(
            update={"terminals": (turn.terminals[0].model_copy(update={"outcome": "errored"}),)}
        )
    turn = turn.model_copy(update={"post_history": history})
    for assertion in _ASSERTIONS:
        assert structure_check(turn, parse_expectation(assertion)).status != "PASS"


async def test_later_matching_reconciliation_can_close_actual_post_window() -> None:
    capture, _ = await long_capture()
    turn = control_turn(capture)
    history = turn.post_history
    assert history is not None
    late = turn.terminals[0].model_copy(
        update={
            "evidence_id": "unit-reconciled",
            "authority": "reconciled",
            "observed_s": capture.observed_s + 11.0,
        }
    )
    # Later reconciliation of the same root is authoritative; an unrelated
    # preview cannot extend this bound (covered by future_window above).
    turn = turn.model_copy(
        update={
            "terminals": (*turn.terminals, late),
            "post_history": history.model_copy(update={"observed_s": capture.observed_s + 10.0}),
        }
    )
    assert all(
        structure_check(turn, parse_expectation(item)).status == "PASS" for item in _ASSERTIONS
    )


@pytest.mark.parametrize(
    "text,balanced",
    [
        ("```python\nprint('hello')\n```", True),
        ("```python\nprint('hello')", False),
        ("````python\n```\n````", True),
        ("````python\n```", False),
        ("~~~python\nprint('hello')\n~~~", True),
        ("```python\n~~~", False),
        ("Inline `code` and escaped \\``` markers", True),
        ("\\```python\nnot a fenced block", True),
    ],
)
async def test_fence_matching_uses_delimiter_length_and_actual_content(
    text: str, balanced: bool
) -> None:
    clock = Clock()
    harness = DiscordSurfaceHarness(
        evidence_id="unit-capture", session_id=_SESSION, root_turn_id=_ROOT, clock=clock
    )

    async def invoke(lc: DiscordTurnLifecycle) -> None:
        await lc.post_initial()
        await lc.on_terminal_success(text_state("original rendering"))
        assert lc.message_ref is not None
        # Mutate the accepted wire output, rather than allowing the splitter to
        # repair a malformed model return before the predicate ever sees it.
        await lc.message_ref.edit(content=text)

    capture = await harness.run(invoke)
    turn = control_turn(capture)
    result = structure_check(turn, FencesExpectation(kind="fences_balanced", turn=1))
    assert result.status == ("PASS" if balanced else "FAIL")


async def test_message_count_excludes_status_and_draft_edits() -> None:
    clock = Clock()
    harness = DiscordSurfaceHarness(
        evidence_id="unit-capture", session_id=_SESSION, root_turn_id=_ROOT, clock=clock
    )

    async def invoke(lc: DiscordTurnLifecycle) -> None:
        await lc.post_initial()
        for _ in range(4):
            clock.now += 11.0
            await lc.on_render(text_state("draft in the embed"))
        await lc.on_terminal_success(text_state("one final answer"))

    capture = await harness.run(invoke)
    assert len(capture.events) > 4
    result = structure_check(
        control_turn(capture), MessageCountExpectation(kind="message_count", turn=1, min=2)
    )
    assert result.code == "MESSAGE_COUNT_BELOW_MIN" and result.status == "FAIL"


async def test_actual_delayed_overflow_send_fails_chunk_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    harness = DiscordSurfaceHarness(
        evidence_id="unit-capture", session_id=_SESSION, root_turn_id=_ROOT, clock=clock
    )
    original = harness.gateway.send_message

    async def delayed(channel_id: int, **kwargs: Any) -> Any:
        # Delay the accepting gateway, not a subsequently manufactured snapshot.
        if harness.gateway.events:
            clock.now += 6.0
        return await original(channel_id, **kwargs)

    monkeypatch.setattr(harness.gateway, "send_message", delayed)

    async def invoke(lc: DiscordTurnLifecycle) -> None:
        await lc.post_initial()
        await lc.on_terminal_success(text_state("Long answer sentence. " * 350))

    capture = await harness.run(invoke)
    assert len(capture.messages) > 2
    result = structure_check(
        control_turn(capture), parse_expectation({"kind": "chunks_gap_max_s", "turn": 1, "max": 5})
    )
    assert result.status == "FAIL" and result.code == "CHUNK_GAP_TOO_LONG"


@pytest.mark.parametrize("mutation", ["missing", "first", "duplicate", "body_bait"])
async def test_footer_must_be_actual_raw_footer_on_last_answer(mutation: str) -> None:
    clock = Clock()
    harness = DiscordSurfaceHarness(
        evidence_id="unit-capture", session_id=_SESSION, root_turn_id=_ROOT, clock=clock
    )

    async def invoke(lc: DiscordTurnLifecycle) -> None:
        await lc.post_initial()
        await lc.on_terminal_success(text_state("Long answer sentence. " * 350))
        posted = harness.gateway.surviving_messages()
        first = await harness.thread.fetch_message(int(posted[0].message_id))
        last = await harness.thread.fetch_message(int(posted[-1].message_id))
        assert last.embeds and last.embeds[0].footer.text
        final_embed = last.embeds[0].copy()
        if mutation in ("first", "duplicate"):
            await first.edit(embeds=[final_embed])
        if mutation != "duplicate":
            await last.edit(embeds=[])
        if mutation == "body_bait":
            # Body/convenience text resembling a footer is not a raw embed footer.
            await last.edit(content="A final answer with $0.01 footer-looking body text")
            await first.edit(embeds=[discord.Embed(description="$0.01 footer-looking embed text")])

    capture = await harness.run(invoke)
    result = structure_check(
        control_turn(capture), parse_expectation({"kind": "footer_on_last_message", "turn": 1})
    )
    assert result.status == "FAIL"
    assert result.code == (
        "FOOTER_NOT_ONLY_ON_LAST_MESSAGE"
        if mutation in ("first", "duplicate")
        else "FINAL_FOOTER_MISSING"
    )


async def test_count_max_text_coverage_and_turn_identity_are_independent() -> None:
    capture, _ = await long_capture()
    turn = control_turn(capture)
    assert (
        structure_check(turn, MessageCountExpectation(kind="message_count", turn=1, max=1)).code
        == "MESSAGE_COUNT_ABOVE_MAX"
    )
    assert (
        structure_check(turn, MessageCountExpectation(kind="message_count", turn=2, min=2)).code
        == "TURN_NUMBER_MISMATCH"
    )
    history = turn.post_history
    assert history is not None
    turn = turn.model_copy(
        update={"post_history": history.model_copy(update={"text_capture_complete": False})}
    )
    assert (
        structure_check(turn, MessageCountExpectation(kind="message_count", turn=1, min=2)).status
        == "PASS"
    )
    for assertion in _ASSERTIONS[1:]:
        result = structure_check(turn, parse_expectation(assertion))
        assert result.status == "PENDING" and result.code == "POST_TEXT_CAPTURE_INCOMPLETE"


@pytest.mark.parametrize("complete", [True, False])
async def test_transient_blank_body_does_not_bypass_capture_coverage(complete: bool) -> None:
    clock = Clock()
    harness = DiscordSurfaceHarness(
        evidence_id="unit-capture", session_id=_SESSION, root_turn_id=_ROOT, clock=clock
    )

    async def invoke(lc: DiscordTurnLifecycle) -> None:
        await lc.post_initial()
        assert lc.message_ref is not None
        await lc.message_ref.edit(content="", embeds=[])
        await lc.on_terminal_success(text_state("A complete long answer. " * 350))

    capture = await harness.run(invoke)
    turn = control_turn(capture)
    history = turn.post_history
    assert history is not None
    if not complete:
        turn = turn.model_copy(
            update={"post_history": history.model_copy(update={"post_capture_complete": False})}
        )
    assert (
        no_blank_message_check(turn, BlankMessageExpectation(kind="no_blank_message", turn=1)).code
        == "BLANK_MESSAGE_POSTED"
    )
    checks = [structure_check(turn, parse_expectation(assertion)) for assertion in _ASSERTIONS]
    assert [check.status for check in checks] == ["PASS" if complete else "PENDING"] * 4
    if not complete:
        assert all(check.code == "POST_HISTORY_INCOMPLETE" for check in checks)


async def test_real_card_edits_need_independent_native_running_intervals() -> None:
    clock = Clock()
    harness = DiscordSurfaceHarness(
        evidence_id="unit-capture", session_id=_SESSION, root_turn_id=_ROOT, clock=clock
    )
    tool = ToolUseBlock(
        kind="tool_use", id="unit-tool", type="agent.tool_use", name="bash", input={}
    )
    running = TurnState(content=[tool])

    async def invoke(lc: DiscordTurnLifecycle) -> None:
        await lc.post_initial()
        for _ in range(3):
            clock.now += 11.0
            await lc.on_render(running)
        clock.now += 12.0
        await lc.on_terminal_success(text_state("SLEPT"))

    capture = await harness.run(invoke)
    turn = control_turn(capture)
    card_id = capture.events[0].message.message_id
    cards = tuple(
        CardUpdate(
            evidence_id=f"card:{index}",
            card_id=card_id,
            order=index,
            observed_s=event.message.observed_s,
            state="progress" if event.message.observed_s < 145.0 else "finalized",
        )
        for index, event in enumerate(capture.events)
        if event.message.message_id == card_id
    )
    turn = host_effects(turn, cards)
    expectation = CardEditsExpectation(kind="card_edits_min", turn=1, min=3, during="running")
    assert structure_check(turn, expectation).code == "NATIVE_RUNNING_WINDOW_NOT_CAPTURED"
    # Synthetic native interval controls test lower-bound arithmetic only; the
    # real catalog still needs its independently captured provider/ingress path.
    tools = (
        ToolEffect(
            evidence_id="unit-tool-start",
            call_id="unit-tool",
            tool_name="bash",
            phase="started",
            order=100,
            observed_s=100.5,
        ),
        ToolEffect(
            evidence_id="unit-tool-end",
            call_id="unit-tool",
            tool_name="bash",
            phase="succeeded",
            order=101,
            observed_s=144.0,
        ),
    )
    turn = host_effects(turn, cards, tools, tool_complete=True)
    assert structure_check(turn, expectation).code == "CARD_EDITS_DURING_RUNNING"
    assert {item.evidence_id for item in turn.tools} <= set(
        structure_check(turn, expectation).evidence_ids
    )
    incomplete = host_effects(turn, cards, tools[:1], tool_complete=True)
    assert structure_check(incomplete, expectation).status == "PENDING"
    outside = (tools[0], tools[1].model_copy(update={"observed_s": 145.5}))
    assert (
        structure_check(host_effects(turn, cards, outside, tool_complete=True), expectation).code
        == "NATIVE_RUNNING_WINDOW_MISMATCH"
    )
    shorter = (tools[0], tools[1].model_copy(update={"observed_s": 115.0}))
    assert (
        structure_check(host_effects(turn, cards, shorter, tool_complete=True), expectation).status
        == "PENDING"
    )
    fewer = tuple(replace_card for replace_card in cards if replace_card.observed_s < 111.0)
    assert (
        structure_check(host_effects(turn, fewer, tools, tool_complete=True), expectation).code
        == "CARD_EDITS_BELOW_MIN"
    )
    # Terminal handling also edits an empty-content card. Its independent final
    # state and native end time exclude it even if its rendered body looks like
    # progress; it cannot supply the fourth running edit.
    assert any(
        event.message.observed_s == 145.0
        for event in capture.events
        if event.operation == "edit" and not event.message.content
    )
    assert (
        structure_check(
            turn, CardEditsExpectation(kind="card_edits_min", turn=1, min=4, during="running")
        ).code
        == "CARD_EDITS_BELOW_MIN"
    )


@pytest.mark.parametrize(
    "assertion",
    [
        {"kind": "message_count", "turn": 1},
        {"kind": "message_count", "turn": 1, "min": 3, "max": 2},
        {"kind": "message_count", "turn": 1, "min": True},
        {"kind": "chunks_gap_max_s", "turn": 1, "max": -1},
        {"kind": "chunks_gap_max_s", "turn": 1, "max": float("nan")},
        {"kind": "fences_balanced", "turn": 0},
        {"kind": "footer_on_last_message", "turn": 1, "pattern": "invented"},
        {"kind": "card_edits_min", "turn": 1, "min": 3, "during": "completed"},
    ],
)
def test_structural_assertion_bounds_are_strict(assertion: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse_expectation(assertion)
