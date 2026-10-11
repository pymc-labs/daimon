"""Structural predicates over acknowledged, complete accepted post history.

These certify the captured adapter boundary, not provider wording, ingress or
live delivery. Status embeds and deleted drafts never inflate answer chunks.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal

from daimon.testing.outcome_oracle import (
    BlankMessageExpectation,
    CheckResult,
    EvidenceModel,
    PostHistoryEvidence,
    PostMessage,
    PostRootAcknowledgement,
    TurnEvidence,
    complete_call,
    no_blank_message_check,
)
from pydantic import Field, JsonValue, TypeAdapter, model_validator

if TYPE_CHECKING:
    from daimon.testing.discord_surface import DiscordSurfaceCapture

STRUCTURAL_KINDS = frozenset(
    {
        "message_count",
        "fences_balanced",
        "chunks_gap_max_s",
        "footer_on_last_message",
        "card_edits_min",
    }
)


def post_history_from_capture(
    capture: DiscordSurfaceCapture,
    *,
    acknowledgement: PostRootAcknowledgement | None = None,
) -> PostHistoryEvidence:
    """Retain the actual gateway payloads, with an independently supplied ack.

    No root, input acknowledgement, completion or coverage is inferred from
    adapter rendering. Missing acknowledgements stay missing for the predicate.
    """
    return PostHistoryEvidence.model_validate(
        {**capture.model_dump(), "acknowledgement": acknowledgement}
    )


class MessageCountExpectation(EvidenceModel):
    kind: Literal["message_count"]
    turn: int = Field(ge=1)
    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def bounds(self) -> MessageCountExpectation:
        if self.min is None and self.max is None:
            raise ValueError("message_count needs an explicit bound")
        if self.min is not None and self.max is not None and self.max < self.min:
            raise ValueError("message_count max must be at least min")
        return self


class FencesExpectation(EvidenceModel):
    kind: Literal["fences_balanced"]
    turn: int = Field(ge=1)


class ChunkGapExpectation(EvidenceModel):
    kind: Literal["chunks_gap_max_s"]
    turn: int = Field(ge=1)
    max: float = Field(ge=0)


class FooterPositionExpectation(EvidenceModel):
    kind: Literal["footer_on_last_message"]
    turn: int = Field(ge=1)


class CardEditsExpectation(EvidenceModel):
    kind: Literal["card_edits_min"]
    turn: int = Field(ge=1)
    min: int = Field(ge=0)
    during: Literal["running"]


type StructureExpectation = (
    MessageCountExpectation
    | FencesExpectation
    | ChunkGapExpectation
    | FooterPositionExpectation
    | CardEditsExpectation
)


def parse_expectation(assertion: Mapping[str, JsonValue]) -> StructureExpectation:
    kind = assertion.get("kind")
    model = {
        "message_count": MessageCountExpectation,
        "fences_balanced": FencesExpectation,
        "chunks_gap_max_s": ChunkGapExpectation,
        "footer_on_last_message": FooterPositionExpectation,
        "card_edits_min": CardEditsExpectation,
    }.get(kind if isinstance(kind, str) else "")
    if model is None:
        raise ValueError("Not a structural post-history predicate")
    return model.model_validate(dict(assertion))


def _payload(message: PostMessage) -> dict[str, JsonValue]:
    return TypeAdapter(dict[str, JsonValue]).validate_json(message.payload_json, strict=True)


def _history(turn: TurnEvidence, kind: str) -> PostHistoryEvidence | CheckResult:
    # Reuse the owner's root, acknowledgement, chronology, lifecycle and survivor
    # validation. A structurally valid blank post is not an I3 prerequisite for
    # these other predicates; each examines its own declared domain.
    guarded = no_blank_message_check(
        turn, BlankMessageExpectation(kind="no_blank_message", turn=turn.turn)
    )
    if guarded.status != "PASS" and guarded.code not in (
        "BLANK_MESSAGE_POSTED",
        "POST_EVIDENCE_MISSING",
    ):
        return guarded.model_copy(update={"kind": kind})
    history = turn.post_history
    if history is None:
        return CheckResult(
            kind=kind, turn=turn.turn, status="PENDING", code="POST_HISTORY_NOT_CAPTURED"
        )
    if not history.post_capture_complete:
        return CheckResult(
            kind=kind, turn=turn.turn, status="PENDING", code="POST_HISTORY_INCOMPLETE"
        )
    authoritative = [
        item
        for item in turn.terminals
        if item.authority in ("record", "reconciled")
        and (item.session_id, item.root_turn_id) == (turn.session_id, turn.root_turn_id)
    ]
    if not authoritative or history.observed_s > max(item.observed_s for item in authoritative):
        return CheckResult(
            kind=kind, turn=turn.turn, status="FAIL", code="POST_CAPTURE_WINDOW_MISMATCH"
        )
    return history


def _balanced(text: str) -> bool:
    """Match line fences by delimiter and length, ignoring inline/escaped ticks."""
    opened: str | None = None
    for line in text.splitlines():
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if match is None:
            continue
        fence, suffix = match.groups()
        if opened is None:
            if fence[0] == "`" and "`" in suffix:
                continue
            opened = fence
        elif fence[0] == opened[0] and len(fence) >= len(opened) and not suffix.strip():
            opened = None
    return opened is None


def _has_footer(message: PostMessage) -> bool:
    embeds = _payload(message).get("embeds")
    if not isinstance(embeds, list):
        return False
    for embed in embeds:
        if not isinstance(embed, dict):
            continue
        footer = embed.get("footer")
        if isinstance(footer, dict):
            text = footer.get("text")
            if isinstance(text, str) and text.strip():
                return True
    return False


def structure_check(turn: TurnEvidence, expectation: StructureExpectation) -> CheckResult:
    if expectation.turn != turn.turn:
        return CheckResult(
            kind=expectation.kind,
            turn=expectation.turn,
            status="FAIL",
            code="TURN_NUMBER_MISMATCH",
        )
    history = _history(turn, expectation.kind)
    if isinstance(history, CheckResult):
        return history
    ids = (
        history.evidence_id,
        *((history.acknowledgement.evidence_id,) if history.acknowledgement else ()),
        *(
            (history.acknowledgement.input_record.evidence_id,)
            if history.acknowledgement and history.acknowledgement.input_record
            else ()
        ),
        *turn.evidence_ids(),
        *(item.evidence_id for item in history.events),
    )

    def result(status: Literal["PASS", "FAIL", "PENDING"], code: str) -> CheckResult:
        return CheckResult(
            kind=expectation.kind, turn=turn.turn, status=status, code=code, evidence_ids=ids
        )

    # Use actual surviving content, never flattened card/footer text or the
    # callback return. Attachments with unseen text cannot certify a text domain.
    chunks = [item for item in history.messages if item.content.strip()]
    if isinstance(expectation, MessageCountExpectation):
        count = len(chunks)
        if expectation.min is not None and count < expectation.min:
            return result("FAIL", "MESSAGE_COUNT_BELOW_MIN")
        if expectation.max is not None and count > expectation.max:
            return result("FAIL", "MESSAGE_COUNT_ABOVE_MAX")
        return result("PASS", "MESSAGE_COUNT_WITHIN_BOUNDS")
    if isinstance(expectation, CardEditsExpectation):
        if not turn.card_capture_complete:
            return result("PENDING", "CARD_CAPTURE_INCOMPLETE")
        progress = {
            (card.card_id, card.observed_s) for card in turn.cards if card.state == "progress"
        }
        edits = [
            event
            for event in history.events
            if event.operation == "edit"
            and (event.message.message_id, event.message.observed_s) in progress
        ]
        if len(edits) < expectation.min:
            return result("FAIL", "CARD_EDITS_BELOW_MIN")
        # A completed native tool interval proves the turn was running within
        # that interval. This is a lower-bound proof, not a manufactured full
        # turn-running window. Uncovered edits can never become a false FAIL.
        if not turn.tool_capture_complete:
            return result("PENDING", "NATIVE_RUNNING_WINDOW_NOT_CAPTURED")
        calls = {item.call_id for item in turn.tools}
        intervals: list[tuple[float, float]] = []
        for call_id in calls:
            records = [item for item in turn.tools if item.call_id == call_id]
            if not complete_call(records):
                return result("PENDING", "NATIVE_RUNNING_WINDOW_NOT_CAPTURED")
            first, last = records
            if not history.started_s <= first.observed_s < last.observed_s <= history.observed_s:
                return result("FAIL", "NATIVE_RUNNING_WINDOW_MISMATCH")
            intervals.append((first.observed_s, last.observed_s))
        witnessed = sum(
            any(begin <= event.message.observed_s < end for begin, end in intervals)
            for event in edits
        )
        if intervals and witnessed >= expectation.min:
            return result("PASS", "CARD_EDITS_DURING_RUNNING")
        return result("PENDING", "NATIVE_RUNNING_WINDOW_NOT_CAPTURED")
    if not history.text_capture_complete:
        return result("PENDING", "POST_TEXT_CAPTURE_INCOMPLETE")
    if not chunks:
        return result("FAIL", "ANSWER_MESSAGES_MISSING")
    if isinstance(expectation, FencesExpectation):
        if any(not _balanced(item.content) for item in chunks):
            return result("FAIL", "FENCES_UNBALANCED")
        return result("PASS", "FENCES_BALANCED")
    if isinstance(expectation, ChunkGapExpectation):
        revealed: list[float] = []
        for chunk in chunks:
            matching = [
                event.message.observed_s
                for event in history.events
                if event.operation != "delete"
                and event.message.message_id == chunk.message_id
                and event.message.content == chunk.content
            ]
            if not matching:
                return result("FAIL", "FINAL_CHUNK_NOT_OBSERVED")
            revealed.append(min(matching))
        # Posting order, not final snapshot/edit time, defines chunk delivery.
        revealed.sort()
        if any(b - a > expectation.max for a, b in zip(revealed, revealed[1:], strict=False)):
            return result("FAIL", "CHUNK_GAP_TOO_LONG")
        return result("PASS", "CHUNK_GAPS_WITHIN_BOUND")
    footer_messages = [item.message_id for item in history.messages if _has_footer(item)]
    if not footer_messages:
        return result("FAIL", "FINAL_FOOTER_MISSING")
    if footer_messages != [chunks[-1].message_id]:
        return result("FAIL", "FOOTER_NOT_ONLY_ON_LAST_MESSAGE")
    return result("PASS", "FOOTER_ON_LAST_MESSAGE")
