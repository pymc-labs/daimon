"""Actual adapter receipts and hostile serialized card bindings."""

from __future__ import annotations

import asyncio
import json
from functools import partial

import pytest
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.adapters.discord.theme import COLOR_IN_PROGRESS
from daimon.core.turn.state import TextBlock, TurnState
from daimon.testing.card_observation import (
    CardEditExpectation,
    CardObserver,
    CardTrace,
    bind_card_posts,
    card_edits_check,
    evaluate_card_posts,
)
from daimon.testing.discord_surface import DiscordSurfaceCapture, DiscordSurfaceHarness
from daimon.testing.outcome_oracle import CardExpectation, TerminalEvidence, TurnEvidence


class Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


def root() -> TurnEvidence:
    return TurnEvidence(
        turn=1,
        slot_id="slot",
        session_id="session",
        root_turn_id="root",
        started_s=100.0,
        terminals=(
            TerminalEvidence(
                evidence_id="owned-native-terminal",
                session_id="session",
                root_turn_id="root",
                authority="record",
                outcome="completed",
                observed_s=144.0,
            ),
        ),
        terminal_capture_complete=True,
    )


async def delivered(*, edits: int = 3) -> tuple[DiscordSurfaceCapture, CardTrace]:
    clock = Clock()
    harness = DiscordSurfaceHarness(
        evidence_id="surface",
        session_id="session",
        root_turn_id="root",
        clock=clock,
    )
    observer = CardObserver(
        snapshot=harness.snapshot,
        card_reference=lambda: (
            str(harness.lifecycle.message_ref.id) if harness.lifecycle.message_ref else None
        ),
        destination_id=str(harness.thread.id),
        thread_id=None,
        clock=clock,
    )

    async def invoke(lifecycle: DiscordTurnLifecycle) -> None:
        await observer.observe("initial", lifecycle.post_initial)
        for n in range(edits):
            clock.value = 111.0 + n * 11
            state = TurnState(content=[TextBlock(kind="text", text=f"draft {n}")])
            await observer.observe("running", partial(lifecycle.on_render, state))
        clock.value = 144.0
        await observer.observe(
            "terminal",
            lambda: lifecycle.on_terminal_success(
                TurnState(content=[TextBlock(kind="text", text="FINAL")])
            ),
        )

    surface = await harness.run(invoke)
    return surface, observer.trace()


def bind(
    surface: DiscordSurfaceCapture, trace: CardTrace, turn: TurnEvidence | None = None
) -> TurnEvidence:
    return bind_card_posts(
        turn or root(),
        surface,
        trace,
        expected_id="surface",
        expected_destination_id="102",
        expected_thread_id=None,
        observed_s=144.0,
    )


def edits_check(surface: DiscordSurfaceCapture, trace: CardTrace, minimum: int = 3) -> str:
    return card_edits_check(
        root(),
        surface,
        trace,
        CardEditExpectation(turn=1, min=minimum, during="running"),
        expected_id="surface",
        expected_destination_id="102",
        expected_thread_id=None,
        observed_s=144.0,
    ).status


async def test_actual_discord_posts_certify_card_history_after_json_roundtrip() -> None:
    surface, trace = await delivered()
    surface = DiscordSurfaceCapture.model_validate_json(surface.model_dump_json())
    trace = CardTrace.model_validate_json(trace.model_dump_json())
    cards = bind(surface, trace)
    assert cards.card_capture_complete
    assert cards.cards[0].state == "progress"
    assert cards.cards[-1].state == "finalized"
    assert len({c.card_id for c in cards.cards}) == 1
    assert not cards.texts and not cards.tools and not cards.approvals
    assert (
        evaluate_card_posts(cards, CardExpectation(kind="card_finalized", turn=1)).status == "PASS"
    )
    assert (
        evaluate_card_posts(
            cards, CardExpectation(kind="progress_seen", turn=1, within_s=1.0)
        ).status
        == "PASS"
    )
    assert edits_check(surface, trace) == "PASS"
    assert edits_check(surface, trace, 4) == "FAIL"


async def test_initial_and_terminal_edits_do_not_earn_running_edit_credit() -> None:
    surface, trace = await delivered(edits=0)
    assert edits_check(surface, trace, 1) == "FAIL"
    assert (
        evaluate_card_posts(
            bind(surface, trace), CardExpectation(kind="card_finalized", turn=1)
        ).status
        == "PASS"
    )


@pytest.mark.parametrize(
    "field", ["session_id", "root_turn_id", "evidence_id", "destination_id", "thread_id"]
)
async def test_foreign_trace_identity_refuses_after_json_roundtrip(field: str) -> None:
    surface, trace = await delivered()
    blob = trace.model_dump(mode="json")
    blob[field] = "foreign"
    with pytest.raises(ValueError, match="owned root and destination"):
        bind(surface, CardTrace.model_validate_json(json.dumps(blob)))


@pytest.mark.parametrize("field", ["session_id", "root_turn_id", "evidence_id"])
async def test_foreign_surface_identity_refuses(field: str) -> None:
    surface, trace = await delivered()
    with pytest.raises(ValueError, match="owned root and destination"):
        bind(surface.model_copy(update={field: "foreign"}), trace)


@pytest.mark.parametrize(
    "mutation",
    ["start", "overlap", "future", "past", "cursor", "reference", "tail", "terminal_twice"],
)
async def test_detached_callback_windows_or_references_refuse(mutation: str) -> None:
    surface, trace = await delivered()
    blob = trace.model_dump(mode="json")
    if mutation == "start":
        blob["callbacks"][0]["first_post"] = 1
    elif mutation == "overlap":
        blob["callbacks"][1]["first_post"] = 0
    elif mutation == "future":
        blob["callbacks"][1]["finished_s"] = 145.0
    elif mutation == "past":
        blob["callbacks"][1]["started_s"] = 99.0
    elif mutation == "cursor":
        blob["callbacks"][1]["after_post"] = 100
    elif mutation == "reference":
        blob["callbacks"][1]["card_id"] = "another-card"
    elif mutation == "tail":
        blob["callbacks"].pop()
    else:
        blob["callbacks"].append(blob["callbacks"][-1])
    with pytest.raises(ValueError):
        bind(surface, CardTrace.model_validate_json(json.dumps(blob)))


@pytest.mark.parametrize("mutation", ["channel", "post_id", "snapshot", "time", "duplicate_send"])
async def test_foreign_or_forged_post_history_refuses(mutation: str) -> None:
    surface, trace = await delivered()
    blob = surface.model_dump(mode="json")
    if mutation == "channel":
        blob["events"][1]["message"]["channel_id"] = "foreign"
    elif mutation == "post_id":
        blob["events"][1]["evidence_id"] = "surface:post:0"
    elif mutation == "snapshot":
        blob["messages"][0]["content"] = "forged"
    elif mutation == "time":
        blob["events"][1]["message"]["observed_s"] = 143.0
    else:
        blob["events"][1]["operation"] = "send"
    with pytest.raises(ValueError):
        bind(DiscordSurfaceCapture.model_validate_json(json.dumps(blob)), trace)


@pytest.mark.parametrize(
    "mutation",
    [
        "preview",
        "gap",
        "foreign_root",
        "foreign_session",
        "missing",
        "duplicate",
        "future",
        "trigger",
    ],
)
async def test_unverified_native_root_keeps_card_domain_pending_or_refuses(mutation: str) -> None:
    surface, trace = await delivered()
    blob = root().model_dump(mode="json")
    if mutation in {"preview", "gap"}:
        blob["terminals"][0]["authority"] = mutation
    elif mutation == "foreign_root":
        blob["terminals"][0]["root_turn_id"] = "foreign"
    elif mutation == "foreign_session":
        blob["terminals"][0]["session_id"] = "foreign"
    elif mutation == "missing":
        blob["terminals"] = []
    elif mutation == "duplicate":
        blob["terminals"].append(blob["terminals"][0])
    elif mutation == "future":
        blob["terminals"][0]["observed_s"] = 145.0
    else:
        blob["started_s"] = 101.0
    if mutation == "trigger":
        with pytest.raises(ValueError):
            bind(surface, trace, TurnEvidence.model_validate_json(json.dumps(blob)))
    else:
        cards = bind(surface, trace, TurnEvidence.model_validate_json(json.dumps(blob)))
        assert not cards.card_capture_complete
        assert (
            evaluate_card_posts(cards, CardExpectation(kind="card_finalized", turn=1)).status
            == "PENDING"
        )


async def test_terminal_callback_with_still_running_delivered_payload_fails() -> None:
    surface, trace = await delivered()
    blob = surface.model_dump(mode="json")
    for event in blob["events"][trace.callbacks[-1].first_post :]:
        payload = json.loads(event["message"]["payload_json"])
        payload["embeds"] = [{"color": COLOR_IN_PROGRESS}]
        event["message"]["payload_json"] = json.dumps(payload)
    blob["messages"][0] = blob["events"][-1]["message"]
    cards = bind(DiscordSurfaceCapture.model_validate_json(json.dumps(blob)), trace)
    assert cards.card_capture_complete
    assert (
        evaluate_card_posts(cards, CardExpectation(kind="card_finalized", turn=1)).status == "FAIL"
    )


async def test_finalizer_renders_and_late_edits_do_not_count_as_running() -> None:
    surface, trace = await delivered()
    blob = trace.model_dump(mode="json")
    blob["callbacks"][1]["phase"] = "finishing"
    changed = CardTrace.model_validate_json(json.dumps(blob))
    assert edits_check(surface, changed, 3) == "FAIL"
    terminal = root().terminals[0].model_copy(update={"observed_s": 110.0})
    early_root = root().model_copy(update={"terminals": (terminal,)})
    result = card_edits_check(
        early_root,
        surface,
        trace,
        CardEditExpectation(turn=1, min=1, during="running"),
        expected_id="surface",
        expected_destination_id="102",
        expected_thread_id=None,
        observed_s=144.0,
    )
    assert result.status == "FAIL"


async def test_unknown_delivered_card_state_never_passes() -> None:
    surface, trace = await delivered()
    blob = surface.model_dump(mode="json")
    for event in blob["events"][trace.callbacks[-1].first_post :]:
        payload = json.loads(event["message"]["payload_json"])
        payload["embeds"] = [{"color": 123}]
        event["message"]["payload_json"] = json.dumps(payload)
    blob["messages"][0] = blob["events"][-1]["message"]
    cards = bind(DiscordSurfaceCapture.model_validate_json(json.dumps(blob)), trace)
    assert (
        evaluate_card_posts(cards, CardExpectation(kind="card_finalized", turn=1)).status
        == "PENDING"
    )


async def test_foreign_expectations_and_model_copy_bypasses_refuse() -> None:
    surface, trace = await delivered()
    cards = bind(surface, trace)
    with pytest.raises(ValueError, match="foreign turn"):
        evaluate_card_posts(cards, CardExpectation(kind="card_finalized", turn=2))
    with pytest.raises(ValueError):
        card_edits_check(
            root(),
            surface,
            trace,
            CardEditExpectation(turn=1, min=3, during="running").model_copy(update={"min": -1}),
            expected_id="surface",
            expected_destination_id="102",
            expected_thread_id=None,
            observed_s=144.0,
        )


async def test_failed_and_concurrent_callbacks_never_produce_a_settled_trace() -> None:
    clock = Clock()
    harness = DiscordSurfaceHarness(
        evidence_id="surface", session_id="session", root_turn_id="root", clock=clock
    )
    observer = CardObserver(
        snapshot=harness.snapshot,
        card_reference=lambda: None,
        destination_id="102",
        thread_id=None,
        clock=clock,
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def waiting() -> None:
        entered.set()
        await release.wait()

    task = asyncio.create_task(observer.observe("initial", waiting))
    await entered.wait()
    try:
        with pytest.raises(ValueError, match="overlap"):
            await observer.observe("running", waiting)
        with pytest.raises(ValueError, match="settle"):
            observer.trace()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    with pytest.raises(ValueError, match="settle"):
        observer.trace()
