"""Deterministic predicates over host observations, independent of provider wording.

Capture is the runner's responsibility. Missing coverage is PENDING; an observed
failure is FAIL. Neither native session lifecycle nor a preview completes a turn.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

type Status = Literal["PASS", "FAIL", "PENDING"]
type Authority = Literal["record", "reconciled", "preview", "gap"]


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


class TurnEvidence(EvidenceModel):
    turn: int = Field(ge=1)
    slot_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    root_turn_id: str = Field(min_length=1)
    started_s: float = Field(ge=0)
    terminals: tuple[TerminalEvidence, ...] = ()
    sessions: tuple[SessionUse, ...] = ()
    terminal_capture_complete: bool = False
    session_capture_complete: bool = False

    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.evidence_id for item in (*self.terminals, *self.sessions))

    @model_validator(mode="after")
    def valid_clock(self) -> TurnEvidence:
        if any(item.observed_s < self.started_s for item in self.terminals):
            raise ValueError("terminal capture predates the trigger")
        if list(self.terminals) != sorted(self.terminals, key=lambda item: item.observed_s):
            raise ValueError("terminal captures must be in observation order")
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
    if matching[0].outcome != "completed":
        return result("FAIL", "TURN_NOT_COMPLETED", ids)
    if expectation.max is not None and matching[0].observed_s - turn.started_s > expectation.max:
        return result("FAIL", "COMPLETION_TOO_LATE", ids)
    if not turn.terminal_capture_complete:
        return result("PENDING", "TERMINAL_CAPTURE_INCOMPLETE", ids)
    return result("PASS", "COMPLETED", ids)


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


def evaluate(
    recording: RunEvidence, assertions: Sequence[Mapping[str, JsonValue]]
) -> OutcomeReport:
    """Evaluate every assertion; an unsupported kind never blocks catalog loading."""
    turns = {turn.turn: turn for turn in recording.turns}
    checks: list[CheckResult] = []
    for assertion in assertions:
        kind = assertion.get("kind")
        label = kind if isinstance(kind, str) else "invalid"
        raw_turn = assertion.get("turn")
        number = raw_turn if type(raw_turn) is int else None
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
