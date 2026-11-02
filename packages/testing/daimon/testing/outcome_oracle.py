"""Deterministic predicates over host observations, independent of provider wording.

Capture is the runner's responsibility. Missing coverage is PENDING; an observed
failure is FAIL. Neither native session lifecycle nor a preview completes a turn.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    model_validator,
)

type Status = Literal["PASS", "FAIL", "PENDING"]
type Authority = Literal["record", "reconciled", "preview", "gap"]

SUPPORTED_ASSERTION_KINDS = frozenset(
    {
        "turn_completed",
        "done_within_s",
        "same_session",
        "no_session_replacement",
        "text_present",
        "text_absent",
        "no_preamble",
        "no_blank_message",
        "message_count",
        "fences_balanced",
        "chunks_gap_max_s",
        "footer_on_last_message",
        "card_edits_min",
        "card_finalized",
        "progress_seen",
        "tool_succeeded",
        "approval_effect",
        "reply_within_s",
        "no_silent_drop",
        "log_present",
        "log_absent",
    }
)


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class SessionUse(EvidenceModel):
    evidence_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)


class TerminalEvidence(EvidenceModel):
    evidence_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    root_turn_id: str = Field(min_length=1)
    authority: Authority
    outcome: Literal["completed", "interrupted", "errored", "terminated"]
    observed_s: float = Field(ge=0)


class HostEffect(EvidenceModel):
    evidence_id: str = Field(min_length=1)
    order: int = Field(ge=0)
    observed_s: float = Field(ge=0)


class VisibleText(HostEffect):
    message_id: str = Field(min_length=1)
    text: str


class CardUpdate(HostEffect):
    card_id: str = Field(min_length=1)
    state: Literal["progress", "finalized"]


class ToolEffect(HostEffect):
    call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    phase: Literal["started", "succeeded", "errored"]


class ApprovalEffect(HostEffect):
    action_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    state: Literal["requested", "approved", "denied", "timed_out"]


class ReactionEffect(HostEffect):
    emoji: str = Field(min_length=1)


class LogObservation(HostEffect):
    event: str = Field(min_length=1)
    fields: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])


class PostMessage(EvidenceModel):
    message_id: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    order: int = Field(ge=0)
    observed_s: float = Field(ge=0)
    content: str
    embed_texts: tuple[str, ...]
    component_labels: tuple[str, ...]
    attachment_names: tuple[str, ...]
    payload_json: str


class PostEvent(EvidenceModel):
    evidence_id: str = Field(min_length=1)
    operation: Literal["send", "edit", "delete"]
    message: PostMessage
    request_json: str


class PostInputAcknowledgement(EvidenceModel):
    """An authoritative provider input record joined to the accepted send."""

    evidence_id: str = Field(min_length=1)
    input_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    root_turn_id: str = Field(min_length=1)
    authority: Authority
    submitted_text: str = Field(min_length=1)
    observed_text: str = Field(min_length=1)


class PostRootAcknowledgement(EvidenceModel):
    evidence_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    root_turn_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    status: Literal["queued", "processed", "outcome_unknown"]
    input_ids: tuple[str, ...]
    input_record: PostInputAcknowledgement | None = None


class PostHistoryEvidence(EvidenceModel):
    """Complete accepted post history and separately captured input receipt.

    Mirrors the existing Discord capture without importing its optional SDK.
    The binder supplies the original durable operation receipt after checking
    its session, immutable input and acknowledgement, never a preview identity.
    """

    boundary: Literal["discord_adapter_posts"]
    evidence_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    root_turn_id: str = Field(min_length=1)
    started_s: float = Field(ge=0)
    observed_s: float = Field(ge=0)
    events: tuple[PostEvent, ...]
    messages: tuple[PostMessage, ...]
    post_capture_complete: bool
    text_capture_complete: bool
    acknowledgement: PostRootAcknowledgement | None = None


class TurnEvidence(EvidenceModel):
    turn: int = Field(ge=1)
    slot_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    root_turn_id: str = Field(min_length=1)
    started_s: float = Field(ge=0)
    post_history: PostHistoryEvidence | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    terminals: tuple[TerminalEvidence, ...] = ()
    sessions: tuple[SessionUse, ...] = ()
    terminal_capture_complete: bool = False
    session_capture_complete: bool = False
    texts: tuple[VisibleText, ...] = ()
    cards: tuple[CardUpdate, ...] = ()
    tools: tuple[ToolEffect, ...] = ()
    approvals: tuple[ApprovalEffect, ...] = ()
    reactions: tuple[ReactionEffect, ...] = ()
    logs: tuple[LogObservation, ...] = ()
    text_capture_complete: bool = False
    card_capture_complete: bool = False
    tool_capture_complete: bool = False
    approval_capture_complete: bool = False
    reaction_capture_complete: bool = False
    log_capture_complete: bool = False

    def host_effects(self) -> tuple[HostEffect, ...]:
        return (*self.texts, *self.cards, *self.tools, *self.approvals, *self.reactions, *self.logs)

    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(
            item.evidence_id for item in (*self.terminals, *self.sessions, *self.host_effects())
        )

    @model_validator(mode="after")
    def valid_clock(self) -> TurnEvidence:
        if any(item.observed_s < self.started_s for item in self.terminals):
            raise ValueError("terminal capture predates the trigger")
        if list(self.terminals) != sorted(self.terminals, key=lambda item: item.observed_s):
            raise ValueError("terminal captures must be in observation order")
        effects = self.host_effects()
        if len({item.order for item in effects}) != len(effects):
            raise ValueError("host effect order must be unique within a turn")
        if any(item.observed_s < self.started_s for item in effects):
            raise ValueError("host effect predates the trigger")
        ordered = sorted(effects, key=lambda item: item.order)
        if [item.observed_s for item in ordered] != sorted(item.observed_s for item in effects):
            raise ValueError("host effect clock and order disagree")
        for stream in (
            self.texts,
            self.cards,
            self.tools,
            self.approvals,
            self.reactions,
            self.logs,
        ):
            if list(stream) != sorted(stream, key=lambda item: item.order):
                raise ValueError("host effect streams must retain capture order")
        return self


class RunEvidence(EvidenceModel):
    scenario_id: str = Field(min_length=1)
    backend: Literal["anthropic", "openai", "gemini"]
    turns: tuple[TurnEvidence, ...]

    @model_validator(mode="after")
    def unique_identity(self) -> RunEvidence:
        if len({turn.turn for turn in self.turns}) != len(self.turns):
            raise ValueError("duplicate turn number")
        ids = [item for turn in self.turns for item in turn.evidence_ids()]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate evidence ID")
        roots = [(turn.session_id, turn.root_turn_id) for turn in self.turns]
        if len(set(roots)) != len(roots):
            raise ValueError("duplicate root turn")
        return self


class CheckResult(EvidenceModel):
    kind: str
    turn: int | None
    status: Status
    code: str
    evidence_ids: tuple[str, ...] = ()


class OutcomeReport(EvidenceModel):
    scenario_id: str
    backend: str
    checks: tuple[CheckResult, ...]

    @property
    def status(self) -> Status:
        if any(check.status == "FAIL" for check in self.checks):
            return "FAIL"
        if not self.checks or any(check.status == "PENDING" for check in self.checks):
            return "PENDING"
        return "PASS"

    @property
    def normalized_outcomes(self) -> tuple[tuple[str, int | None, Status, str], ...]:
        """Stable comparison surface; native IDs, wording and exact times are omitted."""
        return tuple((item.kind, item.turn, item.status, item.code) for item in self.checks)


class Completion(EvidenceModel):
    kind: Literal["turn_completed", "done_within_s"]
    turn: int = Field(ge=1)
    max: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def required_bound(self) -> Completion:
        if (self.kind == "done_within_s") != (self.max is not None):
            raise ValueError("only done_within_s requires a bound")
        return self


class SessionExpectation(EvidenceModel):
    kind: Literal["same_session", "no_session_replacement"]
    turn: int = Field(ge=1)
    previous_turn: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def prior_turn(self) -> SessionExpectation:
        if self.kind == "same_session" and self.turn == 1:
            raise ValueError("same_session requires a follow-up turn")
        if self.previous_turn is not None and (
            self.kind != "same_session" or self.previous_turn >= self.turn
        ):
            raise ValueError("previous_turn must precede a same_session check")
        return self


class TextExpectation(EvidenceModel):
    kind: Literal["text_present", "text_absent", "no_preamble"]
    turn: int = Field(ge=1)
    pattern: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def valid_pattern(self) -> TextExpectation:
        if self.kind != "no_preamble" and self.pattern is None:
            raise ValueError("text assertions require a pattern")
        if self.pattern is not None:
            try:
                re.compile(self.pattern)
            except re.error:
                raise ValueError("invalid text pattern") from None
        return self


class BlankMessageExpectation(EvidenceModel):
    kind: Literal["no_blank_message"]
    turn: int = Field(ge=1)


class CardExpectation(EvidenceModel):
    kind: Literal["card_finalized", "progress_seen"]
    turn: int = Field(ge=1)
    within_s: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def progress_bound(self) -> CardExpectation:
        if self.kind == "card_finalized" and self.within_s is not None:
            raise ValueError("only progress_seen accepts within_s")
        return self


class VisibleLatency(EvidenceModel):
    kind: Literal["reply_within_s", "no_silent_drop"]
    turn: int = Field(ge=1)
    max: float = Field(gt=0)


class LogExpectation(EvidenceModel):
    kind: Literal["log_present", "log_absent"]
    # Catalog turn 0 denotes setup/admin; without its capture it stays PENDING.
    turn: int = Field(ge=0)
    event: str = Field(min_length=1)
    fields: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])


class ToolExpectation(EvidenceModel):
    kind: Literal["tool_succeeded", "approval_effect"]
    turn: int = Field(ge=1)
    tool_name: str = Field(min_length=1)
    min: int = Field(default=1, ge=1)
    max: int | None = Field(default=None, ge=1)
    decision: Literal["approved", "denied", "timed_out"] | None = None

    @model_validator(mode="after")
    def valid_limits(self) -> ToolExpectation:
        if self.max is not None and self.max < self.min:
            raise ValueError("max must be at least min")
        if (self.kind == "approval_effect") != (self.decision is not None):
            raise ValueError("only approval_effect requires a decision")
        return self


def completion_check(turn: TurnEvidence, expectation: Completion) -> CheckResult:
    def result(status: Status, code: str, ids: tuple[str, ...] = ()) -> CheckResult:
        return CheckResult(
            kind=expectation.kind, turn=turn.turn, status=status, code=code, evidence_ids=ids
        )

    authoritative = [item for item in turn.terminals if item.authority in ("record", "reconciled")]
    matching = [
        item
        for item in authoritative
        if item.session_id == turn.session_id and item.root_turn_id == turn.root_turn_id
    ]
    if not matching:
        if not turn.terminal_capture_complete:
            return result("PENDING", "TERMINAL_CAPTURE_INCOMPLETE")
        return result("FAIL", "ROOT_TERMINAL_MISSING")
    ids = tuple(item.evidence_id for item in matching)
    if len({item.outcome for item in matching}) != 1:
        return result("FAIL", "CONFLICTING_ROOT_OUTCOMES", ids)
    if expectation.kind == "turn_completed" and matching[0].outcome != "completed":
        return result("FAIL", "TURN_NOT_COMPLETED", ids)
    if expectation.max is not None and matching[0].observed_s > turn.started_s + expectation.max:
        return result("FAIL", "COMPLETION_TOO_LATE", ids)
    if not turn.terminal_capture_complete:
        return result("PENDING", "TERMINAL_CAPTURE_INCOMPLETE", ids)
    return result(
        "PASS",
        "COMPLETED" if expectation.kind == "turn_completed" else "TERMINAL_WITHIN_BOUND",
        ids,
    )


def session_check(
    turn: TurnEvidence, previous: TurnEvidence | None, expectation: SessionExpectation
) -> CheckResult:
    turns = (turn,) if expectation.kind == "no_session_replacement" else (previous, turn)

    def result(status: Status, code: str, ids: tuple[str, ...] = ()) -> CheckResult:
        return CheckResult(
            kind=expectation.kind, turn=turn.turn, status=status, code=code, evidence_ids=ids
        )

    if any(item is None for item in turns):
        return result("PENDING", "TURN_NOT_CAPTURED")
    observed = tuple(item for item in turns if item is not None)
    ids = tuple(use.evidence_id for item in observed for use in item.sessions)
    if len({item.slot_id for item in observed}) != 1:
        return result("FAIL", "FOLLOWUP_SLOT_CHANGED", ids)
    session_ids = {item.session_id for item in observed} | {
        use.session_id for item in observed for use in item.sessions
    }
    if len(session_ids) != 1:
        return result("FAIL", "SESSION_REPLACED", ids)
    if any(not item.session_capture_complete for item in observed):
        return result("PENDING", "SESSION_CAPTURE_INCOMPLETE", ids)
    if any(not item.sessions for item in observed):
        return result("FAIL", "SESSION_EVIDENCE_MISSING")
    return result("PASS", "SESSION_REUSED", ids)


_PREAMBLE = (
    r"(?i)came\s+across|conversation\b[^\n]*(?:moved|continued)|"
    r"working\s+files\s+could\s+not\s+be\s+saved"
)


def text_check(turn: TurnEvidence, expectation: TextExpectation) -> CheckResult:
    ids = tuple(item.evidence_id for item in turn.texts)
    values = [item.text for item in turn.texts]
    pattern = expectation.pattern or _PREAMBLE
    found = any(
        re.search(pattern, value) for value in (*values, "".join(values), "\n".join(values))
    )
    status: Status = "PASS"
    code = "TEXT_PRESENT" if expectation.kind == "text_present" else "TEXT_ABSENT"
    if found and expectation.kind != "text_present":
        status, code = "FAIL", "FORBIDDEN_VISIBLE_TEXT"
    elif not turn.text_capture_complete:
        status, code = "PENDING", "TEXT_CAPTURE_INCOMPLETE"
    elif expectation.kind == "text_present" and (not found or not values):
        status, code = "FAIL", "EXPECTED_TEXT_MISSING"
    elif expectation.kind == "no_preamble" and not any(value.strip() for value in values):
        status, code = "FAIL", "VISIBLE_TEXT_MISSING"
    return CheckResult(
        kind=expectation.kind, turn=turn.turn, status=status, code=code, evidence_ids=ids
    )


def _post_has_body(payload: Mapping[str, JsonValue]) -> bool:
    def visible(value: JsonValue) -> bool:
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, list):
            return any(visible(item) for item in value)
        if isinstance(value, dict):
            # Visible embed/card/component content only; metadata, author IDs,
            # colors, type tags and empty objects cannot certify a body.
            return any(
                visible(item)
                for name, item in value.items()
                if name
                in (
                    "title",
                    "description",
                    "text",
                    "name",
                    "value",
                    "label",
                    "content",
                    "fields",
                    "footer",
                    "author",
                    "image",
                    "thumbnail",
                    "url",
                    "components",
                    "emoji",
                    "media",
                )
            )
        return False

    return visible(payload.get("content")) or any(
        visible(payload.get(name)) for name in ("embeds", "components", "attachments")
    )


def no_blank_message_check(turn: TurnEvidence, expectation: BlankMessageExpectation) -> CheckResult:
    """Inspect every accepted send/edit, including drafts later deleted.

    An empty answer string is allowed; a completed card has a structural body.
    Final surviving text alone cannot certify that no transient blank was sent.
    """
    history = turn.post_history

    def result(status: Status, code: str, ids: tuple[str, ...] = ()) -> CheckResult:
        return CheckResult(
            kind=expectation.kind, turn=turn.turn, status=status, code=code, evidence_ids=ids
        )

    if history is None:
        return result("PENDING", "POST_HISTORY_NOT_CAPTURED")
    ack = history.acknowledgement
    if (
        ack is None
        or ack.status == "outcome_unknown"
        or not ack.input_ids
        and ack.input_record is None
    ):
        return result("PENDING", "POST_ROOT_NOT_ACKNOWLEDGED")
    if (
        (history.session_id, history.root_turn_id) != (turn.session_id, turn.root_turn_id)
        or (ack.session_id, ack.root_turn_id) != (turn.session_id, turn.root_turn_id)
        or any(not item.strip() for item in ack.input_ids)
    ):
        return result("FAIL", "POST_ROOT_MISMATCH", (ack.evidence_id,))
    input_record = ack.input_record
    if input_record is not None and (
        (input_record.session_id, input_record.root_turn_id) != (turn.session_id, turn.root_turn_id)
        or input_record.authority not in ("record", "reconciled")
        or not input_record.submitted_text.strip()
        or input_record.submitted_text != input_record.observed_text
    ):
        return result("FAIL", "POST_INPUT_ACK_MISMATCH", (input_record.evidence_id,))
    completion = completion_check(turn, Completion(kind="turn_completed", turn=turn.turn))
    if completion.status != "PASS":
        return result(completion.status, completion.code, completion.evidence_ids)
    if not (
        turn.started_s
        <= history.started_s
        <= history.observed_s
        <= max(item.observed_s for item in turn.terminals)
    ):
        return result("FAIL", "POST_CAPTURE_WINDOW_MISMATCH")
    ids = (
        history.evidence_id,
        ack.evidence_id,
        *(item.evidence_id for item in history.events),
        *((input_record.evidence_id,) if input_record is not None else ()),
    )
    if len(set((*turn.evidence_ids(), *ids))) != len((*turn.evidence_ids(), *ids)):
        return result("FAIL", "POST_EVIDENCE_ID_REUSED", ids)
    current: dict[str, PostMessage] = {}
    seen: set[str] = set()
    channels: set[str] = set()
    previous_s = history.started_s
    blanks: list[str] = []
    for index, event in enumerate(history.events):
        message = event.message
        if (
            event.evidence_id != f"{history.evidence_id}:post:{index}"
            or not previous_s <= message.observed_s <= history.observed_s
        ):
            return result("FAIL", "POST_HISTORY_ORDER_MISMATCH", ids)
        previous_s = message.observed_s
        channels.add(message.channel_id)
        try:
            payload = TypeAdapter(dict[str, JsonValue]).validate_json(
                message.payload_json, strict=True
            )
        except ValidationError:
            return result("FAIL", "POST_PAYLOAD_INVALID", ids)
        if (
            payload.get("id") != message.message_id
            or payload.get("channel_id") != message.channel_id
            or payload.get("content", "") != message.content
        ):
            return result("FAIL", "POST_PAYLOAD_IDENTITY_MISMATCH", ids)
        if event.operation == "send":
            if message.message_id in seen:
                return result("FAIL", "POST_LIFECYCLE_INVALID", ids)
            seen.add(message.message_id)
        elif message.message_id not in current:
            return result("FAIL", "POST_LIFECYCLE_INVALID", ids)
        if event.operation == "delete":
            if message.model_dump(exclude={"observed_s"}) != current[message.message_id].model_dump(
                exclude={"observed_s"}
            ):
                return result("FAIL", "POST_DELETE_SNAPSHOT_MISMATCH", ids)
            del current[message.message_id]
        else:
            if not _post_has_body(payload):
                blanks.append(event.evidence_id)
            current[message.message_id] = message
    if len(channels) > 1 or [item.message_id for item in history.messages] != list(current):
        return result("FAIL", "POST_SURVIVORS_MISMATCH", ids)
    for message in history.messages:
        if not history.started_s <= message.observed_s <= history.observed_s or message.model_dump(
            exclude={"observed_s"}
        ) != current[message.message_id].model_dump(exclude={"observed_s"}):
            return result("FAIL", "POST_SURVIVORS_MISMATCH", ids)
    if blanks:
        return result("FAIL", "BLANK_MESSAGE_POSTED", tuple(blanks))
    if not history.post_capture_complete:
        return result("PENDING", "POST_HISTORY_INCOMPLETE", ids)
    if not history.events:
        return result("FAIL", "POST_EVIDENCE_MISSING", ids)
    return result("PASS", "NO_BLANK_MESSAGE", ids)


def card_check(turn: TurnEvidence, expectation: CardExpectation) -> CheckResult:
    latest = {item.card_id: item for item in turn.cards}
    status: Status = "PASS"
    code = "CARDS_FINALIZED" if expectation.kind == "card_finalized" else "PROGRESS_SEEN"
    if not turn.card_capture_complete:
        status, code = "PENDING", "CARD_CAPTURE_INCOMPLETE"
    elif not latest:
        status, code = "FAIL", "CARD_EVIDENCE_MISSING"
    elif expectation.kind == "card_finalized" and any(
        item.state != "finalized" for item in latest.values()
    ):
        status, code = "FAIL", "CARD_STILL_PROGRESS"
    elif expectation.kind == "progress_seen" and not any(
        item.state == "progress" for item in turn.cards
    ):
        status, code = "FAIL", "PROGRESS_NOT_SEEN"
    elif (
        expectation.kind == "progress_seen"
        and expectation.within_s is not None
        and not any(
            item.state == "progress" and item.observed_s <= turn.started_s + expectation.within_s
            for item in turn.cards
        )
    ):
        status, code = "FAIL", "PROGRESS_TOO_LATE"
    return CheckResult(
        kind=expectation.kind,
        turn=turn.turn,
        status=status,
        code=code,
        evidence_ids=tuple(item.evidence_id for item in turn.cards),
    )


def complete_call(effects: Sequence[ToolEffect]) -> bool:
    return (
        len(effects) == 2
        and effects[0].phase == "started"
        and effects[1].phase in ("succeeded", "errored")
        and effects[0].order < effects[1].order
        and effects[0].tool_name == effects[1].tool_name
    )


def successful_call(effects: Sequence[ToolEffect]) -> bool:
    return complete_call(effects) and effects[-1].phase == "succeeded"


def tool_check(turn: TurnEvidence, expectation: ToolExpectation) -> CheckResult:
    calls: dict[str, list[ToolEffect]] = {}
    for effect in turn.tools:
        calls.setdefault(effect.call_id, []).append(effect)
    matching = {
        key: items
        for key, items in calls.items()
        if any(item.tool_name == expectation.tool_name for item in items)
    }
    selected = tuple(item for items in matching.values() for item in items)

    def result(status: Status, code: str, extra: tuple[ApprovalEffect, ...] = ()) -> CheckResult:
        return CheckResult(
            kind=expectation.kind,
            turn=turn.turn,
            status=status,
            code=code,
            evidence_ids=tuple(item.evidence_id for item in (*selected, *extra)),
        )

    if any(item.tool_name != expectation.tool_name for item in selected):
        return result("FAIL", "TOOL_IDENTITY_CHANGED")
    if turn.tool_capture_complete and any(not complete_call(items) for items in matching.values()):
        return result("FAIL", "INVALID_TOOL_LIFECYCLE")
    count = sum(successful_call(items) for items in matching.values())
    actions = tuple(item for item in turn.approvals if item.tool_name == expectation.tool_name)
    if expectation.kind == "approval_effect":
        groups: dict[str, list[ApprovalEffect]] = {}
        for action in actions:
            groups.setdefault(action.action_id, []).append(action)
        verified_calls: set[str] = set()
        authorized_calls: set[str] = set()
        for group in groups.values():
            call_ids = {item.call_id for item in group}
            if len(call_ids) != 1 or group[0].state != "requested":
                return result("FAIL", "INVALID_APPROVAL_LIFECYCLE", actions)
            linked = calls.get(group[0].call_id, [])
            decisions = [item for item in group if item.state != "requested"]
            if linked and decisions and any(item.order < decisions[0].order for item in linked):
                return result("FAIL", "TOOL_EXECUTED_BEFORE_APPROVAL", actions)
            if linked and not decisions and turn.approval_capture_complete:
                return result("FAIL", "TOOL_EXECUTED_WITHOUT_APPROVAL", actions)
            if decisions and decisions[-1].state != "approved" and linked:
                return result("FAIL", "TOOL_EXECUTED_WITHOUT_APPROVAL", actions)
            if not turn.approval_capture_complete or not turn.tool_capture_complete:
                continue
            if len(group) != 2 or group[-1].state != expectation.decision:
                return result("FAIL", "WRONG_APPROVAL_DECISION", actions)
            if expectation.decision == "approved" and not successful_call(linked):
                return result("FAIL", "APPROVED_TOOL_EFFECT_MISSING", actions)
            if any(item.tool_name != expectation.tool_name for item in linked):
                return result("FAIL", "TOOL_IDENTITY_CHANGED", actions)
            verified_calls.add(group[0].call_id)
            if group[-1].state == "approved":
                authorized_calls.add(group[0].call_id)
        if (
            turn.approval_capture_complete
            and turn.tool_capture_complete
            and (matching.keys() - authorized_calls)
        ):
            return result("FAIL", "TOOL_EXECUTED_WITHOUT_APPROVAL", actions)
        count = len(verified_calls)
        if not turn.approval_capture_complete:
            return result("PENDING", "APPROVAL_CAPTURE_INCOMPLETE", actions)
    if not turn.tool_capture_complete:
        return result("PENDING", "TOOL_CAPTURE_INCOMPLETE", actions)
    if count < expectation.min or (expectation.max is not None and count > expectation.max):
        return result("FAIL", "EFFECT_COUNT_MISMATCH", actions)
    return result(
        "PASS",
        "TOOL_EFFECT_VERIFIED"
        if expectation.kind == "tool_succeeded"
        else "APPROVAL_EFFECT_VERIFIED",
        actions,
    )


def visible_latency_check(turn: TurnEvidence, expectation: VisibleLatency) -> CheckResult:
    visible: tuple[HostEffect, ...] = (
        *(item for item in turn.texts if item.text.strip()),
        *turn.cards,
        *turn.reactions,
    )
    within = tuple(item for item in visible if item.observed_s <= turn.started_s + expectation.max)
    if within:
        status, code = "PASS", "VISIBLE_RESPONSE_WITHIN_BOUND"
    elif not (
        turn.text_capture_complete and turn.card_capture_complete and turn.reaction_capture_complete
    ):
        status, code = "PENDING", "VISIBLE_CAPTURE_INCOMPLETE"
    else:
        status, code = "FAIL", "VISIBLE_RESPONSE_MISSING_OR_LATE"
    return CheckResult(
        kind=expectation.kind,
        turn=turn.turn,
        status=status,
        code=code,
        evidence_ids=tuple(item.evidence_id for item in within or visible),
    )


def log_check(turn: TurnEvidence, expectation: LogExpectation) -> CheckResult:
    matches = tuple(
        item
        for item in turn.logs
        if item.event == expectation.event
        and all(
            key in item.fields and item.fields[key] == value
            for key, value in expectation.fields.items()
        )
    )
    if matches:
        status: Status = "PASS" if expectation.kind == "log_present" else "FAIL"
        code = "EXPECTED_LOG_PRESENT" if status == "PASS" else "FORBIDDEN_LOG_PRESENT"
    elif not turn.log_capture_complete:
        status, code = "PENDING", "LOG_CAPTURE_INCOMPLETE"
    else:
        status = "FAIL" if expectation.kind == "log_present" else "PASS"
        code = "EXPECTED_LOG_MISSING" if status == "FAIL" else "FORBIDDEN_LOG_ABSENT"
    return CheckResult(
        kind=expectation.kind,
        turn=turn.turn,
        status=status,
        code=code,
        evidence_ids=tuple(item.evidence_id for item in matches or turn.logs),
    )


type FindingJudge = Callable[[str, str, str], Status]
"""Role, full observed text, fixed expected finding; no boolean approval."""


def evaluate(
    recording: RunEvidence,
    assertions: Sequence[Mapping[str, JsonValue]],
    *,
    finding_judge: FindingJudge | None = None,
) -> OutcomeReport:
    """Evaluate every assertion; an unsupported kind never blocks catalog loading."""
    turns = {turn.turn: turn for turn in recording.turns}
    checks: list[CheckResult] = []
    for assertion in assertions:
        kind = assertion.get("kind")
        label = kind if isinstance(kind, str) else "invalid"
        raw_turn = assertion.get("turn")
        number = raw_turn if type(raw_turn) is int else None
        if not isinstance(kind, str) or not kind.strip():
            checks.append(
                CheckResult(kind="invalid", turn=number, status="FAIL", code="INVALID_ASSERTION")
            )
            continue
        try:
            if kind in ("turn_completed", "done_within_s"):
                expectation = Completion.model_validate(dict(assertion))
                turn = turns.get(expectation.turn)
                check = completion_check(turn, expectation) if turn is not None else None
            elif kind in ("same_session", "no_session_replacement"):
                session = SessionExpectation.model_validate(dict(assertion))
                turn = turns.get(session.turn)
                previous = turns.get(session.previous_turn or session.turn - 1)
                check = session_check(turn, previous, session) if turn is not None else None
            elif kind in ("text_present", "text_absent", "no_preamble"):
                text = TextExpectation.model_validate(dict(assertion))
                turn = turns.get(text.turn)
                check = text_check(turn, text) if turn is not None else None
            elif kind == "no_blank_message":
                # Existing recordings opt in only by retaining post history.
                # Without it, preserve their former serialized verdict exactly.
                legacy_turn = turns.get(number) if number is not None else None
                if legacy_turn is None or legacy_turn.post_history is None:
                    checks.append(
                        CheckResult(
                            kind=label, turn=number, status="PENDING", code="UNSUPPORTED_KIND"
                        )
                    )
                    continue
                blank = BlankMessageExpectation.model_validate(dict(assertion))
                turn = turns.get(blank.turn)
                check = no_blank_message_check(turn, blank) if turn is not None else None
            elif kind in (
                "message_count",
                "fences_balanced",
                "chunks_gap_max_s",
                "footer_on_last_message",
                "card_edits_min",
            ):
                # Post-history capture is opt-in; retain legacy verdict bytes.
                legacy_turn = turns.get(number) if number is not None else None
                if legacy_turn is None or legacy_turn.post_history is None:
                    checks.append(
                        CheckResult(
                            kind=label, turn=number, status="PENDING", code="UNSUPPORTED_KIND"
                        )
                    )
                    continue
                from daimon.testing.post_structure import parse_expectation, structure_check

                structural = parse_expectation(assertion)
                turn = turns.get(structural.turn)
                check = structure_check(turn, structural) if turn is not None else None
            elif kind in ("card_finalized", "progress_seen"):
                card = CardExpectation.model_validate(dict(assertion))
                turn = turns.get(card.turn)
                check = card_check(turn, card) if turn is not None else None
            elif kind in ("tool_succeeded", "approval_effect"):
                tool = ToolExpectation.model_validate(dict(assertion))
                turn = turns.get(tool.turn)
                check = tool_check(turn, tool) if turn is not None else None
            elif kind in ("reply_within_s", "no_silent_drop"):
                visible = VisibleLatency.model_validate(dict(assertion))
                turn = turns.get(visible.turn)
                check = visible_latency_check(turn, visible) if turn is not None else None
            elif kind in ("log_present", "log_absent"):
                log = LogExpectation.model_validate(dict(assertion))
                turn = turns.get(log.turn)
                check = log_check(turn, log) if turn is not None else None
            else:
                checks.append(
                    CheckResult(kind=label, turn=number, status="PENDING", code="UNSUPPORTED_KIND")
                )
                continue
            checks.append(
                check
                or CheckResult(kind=label, turn=number, status="PENDING", code="TURN_NOT_CAPTURED")
            )
        except ValidationError:
            checks.append(
                CheckResult(kind=label, turn=number, status="FAIL", code="INVALID_ASSERTION")
            )
    return OutcomeReport(
        scenario_id=recording.scenario_id, backend=recording.backend, checks=tuple(checks)
    )
