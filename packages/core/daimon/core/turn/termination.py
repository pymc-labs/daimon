"""Why a turn ended: one closed vocabulary for every exit path. Pure, no I/O.

`TerminationReason` is the single answer to "how did this turn end?", shared
by everything downstream that has to say or store it -- the notice a user
sees, the outcome row an operator queries, per-turn telemetry. It is additive
to `TurnError.kind`: adapters that branch on the kind keep working, and every
`TurnKind` value maps 1:1 onto the member with the same string.

Two sources fill it in:

- Turns that ran: the driver sets `TurnState.termination` in each finalizer
  (and the ceiling handlers set it on the state they build), before any
  terminal hook fires, so lifecycles and `RunOutcome` both see it.
- Turns refused before a driver ran (admission, bind): nothing builds a
  `TurnState`, so callers hand the exception they caught to
  `termination_reason` and get the member back.

`termination_reason` is the one exception→reason mapper, and it is total: it
never raises, whatever it is handed. A new admission gate
should raise `AdmissionDenied` with a new `AdmissionDenialReason` value: it
maps to `ADMISSION_DENIED` until someone gives it a member of its own, so no
call site grows a string literal. Anything unclassified maps to `UNKNOWN`.
"""

from __future__ import annotations

from enum import StrEnum

from daimon.core.errors import TurnError
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.turn.errors import (
    AdmissionDenied,
    MissingTurnConfigError,
    SessionAgentMismatch,
    SessionBusyError,
    SessionPreparationFailed,
)
from mux.contracts.events import Event, TurnEndedPayload

__all__ = ["TerminationReason", "denial_termination_reason", "termination_reason"]


class TerminationReason(StrEnum):
    """How a turn ended. Values are stable storage strings; never rename one."""

    # The turn ran.
    COMPLETED = "completed"
    """Terminal idle with an answer (or a degraded one that still answered)."""
    INTERRUPTED = "interrupted"
    """The user stopped the turn: acked mid-stream, or during a reconnect."""
    INTERRUPT_TIMEOUT = "interrupt_timeout"
    """A stop was asked for but MA never acknowledged it."""
    CONNECTION_LOST = "connection_lost"
    """The event stream dropped and the reconnect budget ran out."""
    UPSTREAM = "upstream"
    """MA or the API returned an error the turn could not continue past."""
    RATE_LIMITED = "rate_limited"
    """The API refused the turn with a 429."""
    SESSION_TERMINATED = "session_terminated"
    """MA terminated the session mid-turn."""
    MCP_DEGRADED_EMPTY = "mcp_degraded_empty"
    """An MCP server failed and the turn produced nothing without it."""
    RETRYING_UNSETTLED = "retrying_unsettled"
    """MA was still retrying an error when the stream ended with no answer."""
    REQUIRES_ACTION = "requires_action"
    """The agent asked for tool approval this surface cannot give."""
    CEILING = "ceiling"
    """The per-turn wall-clock ceiling fired."""
    RECOVERY_CANCELLED = "recovery_cancelled"
    """The session was lost and the user stopped the turn before it was retried."""
    RECOVERY_FAILED = "recovery_failed"
    """The session was lost and replacing it raised. `run_prepared_turn` hands
    the caller's terminal hook this member, then re-raises the exception, which
    `termination_reason` maps to `UNKNOWN`; record this member for it instead."""
    REDUCER_BUG = "reducer_bug"
    """Reserved: mirrors `TurnKind`; no production path raises it."""

    # The turn was refused before a driver ran.
    ADMISSION_BALANCE_DEPLETED = "admission_balance_depleted"
    ADMISSION_CAP_EXCEEDED = "admission_cap_exceeded"
    ADMISSION_CHANNEL_BUDGET_EXCEEDED = "admission_channel_budget_exceeded"
    ADMISSION_CHANNEL_PROTECTED = "admission_channel_protected"
    ADMISSION_AGENT_PINNED_ELSEWHERE = "admission_agent_pinned_elsewhere"
    ADMISSION_CHANNEL_ISOLATED = "admission_channel_isolated"
    ADMISSION_DENIED = "admission_denied"
    """Any other admission refusal (the invoker allowlist, a future gate)."""
    ADMISSION_CONCURRENCY_SHED = "admission_concurrency_shed"
    """Too many turns in flight: the turn queue was full, or a queued turn
    waited past its max wait (`daimon.core.turn_queue`). Admission returns
    rather than raising there, so callers set this member themselves;
    `termination_reason` never produces it."""
    MISSING_CONFIG = "missing_config"
    RESOLVER_MISS = "resolver_miss"
    SESSION_PREPARATION_FAILED = "session_preparation_failed"
    SESSION_BUSY = "session_busy"
    SESSION_AGENT_MISMATCH = "session_agent_mismatch"

    UNKNOWN = "unknown"
    """An exception nothing above classifies."""

    @property
    def is_failure(self) -> bool:
        """False for the ends a user chose or that answered; True otherwise."""
        return self not in _NOT_FAILURES


_NOT_FAILURES = frozenset({TerminationReason.COMPLETED, TerminationReason.INTERRUPTED})

_BY_VALUE: dict[str, TerminationReason] = {m.value: m for m in TerminationReason}

_BY_DENIAL: dict[str, TerminationReason] = {
    "balance_depleted": TerminationReason.ADMISSION_BALANCE_DEPLETED,
    "cap_exceeded": TerminationReason.ADMISSION_CAP_EXCEEDED,
    "channel_budget_exceeded": TerminationReason.ADMISSION_CHANNEL_BUDGET_EXCEEDED,
    "writers_none": TerminationReason.ADMISSION_CHANNEL_PROTECTED,
    "runs_elsewhere": TerminationReason.ADMISSION_AGENT_PINNED_ELSEWHERE,
    "own_agents_only": TerminationReason.ADMISSION_CHANNEL_ISOLATED,
}


def denial_termination_reason(denial: str | None) -> TerminationReason:
    """The member for an admission denial reason, `ADMISSION_DENIED` when it has none.

    Takes a plain string so a gate that decides with `authorize` rather than
    raising `AdmissionDenied` records the same member for the same reason.
    """
    return _BY_DENIAL.get(str(denial), TerminationReason.ADMISSION_DENIED)


def termination_reason(err: BaseException | None) -> TerminationReason:
    """Map what a caller already holds to the reason: `None` means it completed.

    A `TurnError` maps by its kind; the driver-only distinctions that share a
    kind (`SESSION_TERMINATED`, `MCP_DEGRADED_EMPTY`, `RETRYING_UNSETTLED`,
    `RATE_LIMITED`) are set where they are known, on `TurnState.termination`,
    so prefer that when there is a state.
    """
    match err:
        case None:
            return TerminationReason.COMPLETED
        case TurnError():
            return _BY_VALUE.get(str(err.kind), TerminationReason.UNKNOWN)
        case AdmissionDenied():
            return denial_termination_reason(getattr(err, "reason", None))
        case MissingTurnConfigError():
            return TerminationReason.MISSING_CONFIG
        case MAResolverMissError():
            return TerminationReason.RESOLVER_MISS
        case SessionPreparationFailed():
            return TerminationReason.SESSION_PREPARATION_FAILED
        case SessionBusyError():
            return TerminationReason.SESSION_BUSY
        case SessionAgentMismatch():
            return TerminationReason.SESSION_AGENT_MISMATCH
        case _:
            return TerminationReason.UNKNOWN


def normalized_stop_reason(event: Event) -> str | None:
    """Only authoritative root boundaries stop the neutral live consume loop."""
    if event.authority not in {"record", "reconciled"}:
        return None
    if event.type == "session.requires_action":
        return "requires_action"
    if event.type == "session.turn_ended":
        payload = TurnEndedPayload.model_validate(event.payload)
        return payload.native_reason or payload.outcome
    return None


def normalized_termination_reason(event: Event) -> TerminationReason | None:
    """Map the neutral root outcome; host failure refinements still take precedence."""
    if event.authority not in {"record", "reconciled"}:
        return None
    if event.type == "session.status_terminated":
        return TerminationReason.SESSION_TERMINATED
    if event.type != "session.turn_ended":
        return None
    payload = TurnEndedPayload.model_validate(event.payload)
    return {
        "completed": TerminationReason.COMPLETED,
        "interrupted": TerminationReason.INTERRUPTED,
        "errored": TerminationReason.UPSTREAM,
        "terminated": TerminationReason.SESSION_TERMINATED,
    }[payload.outcome]
