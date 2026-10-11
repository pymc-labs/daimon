"""Bind card history to accepted adapter posts and actual lifecycle references.

This observes adapter delivery, not provider tool/workspace effects. A terminal
callback without a delivered update cannot certify card finalization. Current
card refetches and attachment bytes require their own observation boundaries.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast

from daimon.adapters.discord.theme import COLOR_GREEN, COLOR_IN_PROGRESS, COLOR_RED
from daimon.testing.discord_surface import DiscordSurfaceCapture
from daimon.testing.outcome_oracle import (
    CardExpectation,
    CardUpdate,
    CheckResult,
    TurnEvidence,
    card_check,
)
from daimon.testing.platform_surface import PlatformSurfaceCapture
from pydantic import BaseModel, ConfigDict, Field, model_validator

type Surface = DiscordSurfaceCapture | PlatformSurfaceCapture
type Phase = Literal["initial", "running", "finishing", "terminal"]


class Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)


class CardCallback(Record):
    phase: Phase
    started_s: float = Field(ge=0)
    finished_s: float = Field(ge=0)
    first_post: int = Field(ge=0)
    after_post: int = Field(ge=0)
    card_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def ordered(self) -> CardCallback:
        if self.finished_s < self.started_s or self.after_post < self.first_post:
            raise ValueError("card callback has a backward window")
        return self


class CardTrace(Record):
    evidence_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    root_turn_id: str = Field(min_length=1)
    destination_id: str = Field(min_length=1)
    thread_id: str | None = None
    callbacks: tuple[CardCallback, ...]


def _copy_surface(surface: Surface) -> Surface:
    # Revalidate model_copy corruptions and use only serialized evidence.
    return type(surface).model_validate_json(surface.model_dump_json())


def _delivered_state(payload_json: str, phase: Phase, deleted: bool) -> str | None:
    """Discord status colours are the adapter's phase encoding, not model words."""
    value: object = json.loads(payload_json)
    if not isinstance(value, dict):
        raise ValueError("card payload is not an accepted Discord message")
    payload = cast(dict[str, Any], value)
    if deleted:
        return "finalized" if phase == "terminal" else None
    embeds = payload.get("embeds", [])
    if not isinstance(embeds, list) or any(
        not isinstance(e, dict) for e in cast(list[object], embeds)
    ):
        raise ValueError("card embeds have an unknown wire shape")
    colours = [e.get("color") for e in cast(list[dict[str, Any]], embeds)]
    if COLOR_IN_PROGRESS in colours:
        return "progress"
    if phase != "terminal":
        return None
    if colours and all(c in {COLOR_GREEN, COLOR_RED} for c in colours):
        return "finalized"
    if not colours and isinstance(payload.get("content"), str) and payload["content"].strip():
        return "finalized"
    return None


class CardObserver:
    """Wrap actual adapter calls; a snapshot reader and reference reader are owned.

    Call initial around post_initial, running around on_render while the native
    turn is running, and terminal around the actual terminal delivery. Do not
    wrap the recording-only outer lifecycle or substitute an answer message ID.
    Failed callbacks retain their accepted posts, but never become terminal
    delivery certificates. Concurrent callbacks are refused rather than guessed.
    """

    def __init__(
        self,
        *,
        snapshot: Callable[[], Surface],
        card_reference: Callable[[], str | None],
        destination_id: str,
        thread_id: str | None,
        clock: Callable[[], float],
    ) -> None:
        self.snapshot, self.card_reference, self.clock = snapshot, card_reference, clock
        self.destination_id, self.thread_id = destination_id, thread_id
        self.callbacks: list[CardCallback] = []
        self._busy = False
        self._failed = False

    async def observe(self, phase: Phase, invoke: Callable[[], Awaitable[None]]) -> None:
        if self._busy or any(c.phase == "terminal" for c in self.callbacks):
            raise ValueError("card callbacks overlap or follow terminal delivery")
        self._busy = True
        before = _copy_surface(self.snapshot())
        started = self.clock()
        try:
            await invoke()
        except BaseException:
            self._failed = True
            raise
        finally:
            after = _copy_surface(self.snapshot())
            finished = self.clock()
            self._busy = False
        if before.events != after.events[: len(before.events)]:
            raise ValueError("card callback rewrites accepted post history")
        self.callbacks.append(
            CardCallback(
                phase=phase,
                started_s=started,
                finished_s=finished,
                first_post=len(before.events),
                after_post=len(after.events),
                card_id=self.card_reference(),
            )
        )

    def trace(self) -> CardTrace:
        if self._busy or self._failed:
            raise ValueError("card observer did not settle successfully")
        surface = _copy_surface(self.snapshot())
        return CardTrace(
            evidence_id=surface.evidence_id,
            session_id=surface.session_id,
            root_turn_id=surface.root_turn_id,
            destination_id=self.destination_id,
            thread_id=self.thread_id,
            callbacks=tuple(self.callbacks),
        )


def bind_card_posts(
    turn: TurnEvidence,
    surface: Surface,
    trace: CardTrace,
    *,
    expected_id: str,
    expected_destination_id: str,
    expected_thread_id: str | None,
    observed_s: float,
) -> TurnEvidence:
    """Return an isolated card domain; do not overwrite other host effects.

    The dispatcher supplies destination and root ownership independently. It
    must call this after the owned adapter window settles. This is historical
    delivery coverage; it cannot satisfy current-state/refetch predicates.
    """
    turn = TurnEvidence.model_validate_json(turn.model_dump_json())
    surface = _copy_surface(surface)
    trace = CardTrace.model_validate_json(trace.model_dump_json())
    if (
        surface.evidence_id != expected_id
        or trace.evidence_id != expected_id
        or surface.session_id != turn.session_id
        or trace.session_id != turn.session_id
        or surface.root_turn_id != turn.root_turn_id
        or trace.root_turn_id != turn.root_turn_id
        or trace.destination_id != expected_destination_id
        or trace.thread_id != expected_thread_id
        or surface.started_s < turn.started_s
        or not surface.observed_s <= observed_s
    ):
        raise ValueError("card capture differs from its owned root and destination")
    cursor = 0
    previous = surface.started_s
    known: dict[str, object] = {}
    created: set[str] = set()
    cards: list[CardUpdate] = []
    references: set[str] = set()
    terminal_count = 0
    payload_complete = isinstance(surface, DiscordSurfaceCapture)
    for callback in trace.callbacks:
        if (
            callback.first_post != cursor
            or callback.after_post > len(surface.events)
            or callback.started_s < previous
            or callback.finished_s > surface.observed_s
            or terminal_count
        ):
            raise ValueError("card callback is detached from accepted post order/window")
        terminal_count += callback.phase == "terminal"
        previous, cursor = callback.finished_s, callback.after_post
        if callback.card_id:
            references.add(callback.card_id)
        for index in range(callback.first_post, callback.after_post):
            event = surface.events[index]
            message = event.message
            if isinstance(surface, DiscordSurfaceCapture):
                destination = surface.events[index].message.channel_id
                thread = None
            else:
                destination = surface.events[index].message.destination_id
                thread = surface.events[index].message.thread_id
            if (
                destination != expected_destination_id
                or thread != expected_thread_id
                or event.evidence_id != f"{expected_id}:post:{index}"
                or not callback.started_s <= message.observed_s <= callback.finished_s
            ):
                raise ValueError("card post has a foreign destination, identity or clock")
            if event.operation == "send":
                if message.message_id in created or message.order != len(created):
                    raise ValueError("card post is duplicate or unordered")
                created.add(message.message_id)
            elif message.message_id not in known:
                raise ValueError("card edit/delete has no owned live post")
            if event.operation == "delete":
                old = known.pop(message.message_id)
                if message.model_dump(exclude={"observed_s"}) != old:
                    raise ValueError("card delete differs from its owned post")
            else:
                known[message.message_id] = message.model_dump(exclude={"observed_s"})
            if message.message_id == callback.card_id:
                state = (
                    _delivered_state(
                        message.payload_json, callback.phase, event.operation == "delete"
                    )
                    if isinstance(surface, DiscordSurfaceCapture)
                    else None
                )
                if state is None:
                    payload_complete = False
                    continue
                cards.append(
                    CardUpdate(
                        evidence_id=event.evidence_id,
                        card_id=message.message_id,
                        order=index,
                        observed_s=message.observed_s,
                        state=cast(Literal["progress", "finalized"], state),
                    )
                )
    if cursor != len(surface.events) or len(references) > 1:
        raise ValueError("card history has unobserved posts or a replacement reference")
    if known != {m.message_id: m.model_dump(exclude={"observed_s"}) for m in surface.messages}:
        raise ValueError("card surviving snapshot differs from accepted post history")
    complete = surface.post_capture_complete and terminal_count == 1 and payload_complete
    # A finalized post needs an independently acknowledged terminal root.
    complete = complete and turn.terminal_capture_complete and len(turn.terminals) == 1
    if complete:
        terminal = turn.terminals[0]
        complete = (
            terminal.authority in {"record", "reconciled"}
            and terminal.session_id == turn.session_id
            and terminal.root_turn_id == turn.root_turn_id
            and terminal.observed_s <= observed_s
        )
    return TurnEvidence(
        turn=turn.turn,
        slot_id=turn.slot_id,
        session_id=turn.session_id,
        root_turn_id=turn.root_turn_id,
        started_s=turn.started_s,
        cards=tuple(cards),
        card_capture_complete=complete,
    )


def evaluate_card_posts(cards: TurnEvidence, expectation: CardExpectation) -> CheckResult:
    """Use the unchanged core predicates over the isolated, bound card history."""
    cards = TurnEvidence.model_validate_json(cards.model_dump_json())
    expectation = CardExpectation.model_validate_json(expectation.model_dump_json())
    if expectation.turn != cards.turn:
        raise ValueError("card expectation refers to a foreign turn")
    return card_check(cards, expectation)


class CardEditExpectation(Record):
    kind: Literal["card_edits_min"] = "card_edits_min"
    turn: int = Field(ge=1)
    min: int = Field(ge=0)
    during: Literal["running"]


def card_edits_check(
    turn: TurnEvidence,
    surface: Surface,
    trace: CardTrace,
    expectation: CardEditExpectation,
    *,
    expected_id: str,
    expected_destination_id: str,
    expected_thread_id: str | None,
    observed_s: float,
) -> CheckResult:
    """Count accepted edits of the actual progress card during running renders.

    Re-bind the serialized source here so a caller cannot provide cards
    detached from this transcript.
    Initial sends, repeated renders without I/O, terminal edits, other answers
    and finalizer renders do not earn progress-edit credit.
    """
    cards = bind_card_posts(
        turn,
        surface,
        trace,
        expected_id=expected_id,
        expected_destination_id=expected_destination_id,
        expected_thread_id=expected_thread_id,
        observed_s=observed_s,
    )
    surface = _copy_surface(surface)
    trace = CardTrace.model_validate_json(trace.model_dump_json())
    expectation = CardEditExpectation.model_validate_json(expectation.model_dump_json())
    ids = {
        surface.events[index].evidence_id
        for callback in trace.callbacks
        if callback.phase == "running"
        and len(turn.terminals) == 1
        and callback.finished_s <= turn.terminals[0].observed_s
        for index in range(callback.first_post, callback.after_post)
        if index < len(surface.events)
        and surface.events[index].operation == "edit"
        and surface.events[index].message.message_id == callback.card_id
    }
    observed = tuple(
        c.evidence_id for c in cards.cards if c.state == "progress" and c.evidence_id in ids
    )
    if expectation.turn != cards.turn:
        raise ValueError("card edit expectation refers to a foreign turn")
    status: Literal["PASS", "FAIL", "PENDING"] = "PENDING"
    code = "CARD_CAPTURE_INCOMPLETE"
    if cards.card_capture_complete:
        status = "PASS" if len(observed) >= expectation.min else "FAIL"
        code = "CARD_EDITS_SUFFICIENT" if status == "PASS" else "CARD_EDITS_TOO_FEW"
    return CheckResult(
        kind="card_edits_min", turn=cards.turn, status=status, code=code, evidence_ids=observed
    )
