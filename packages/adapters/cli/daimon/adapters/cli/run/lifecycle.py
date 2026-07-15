"""TurnLifecycle impl that emits one NDJSON line per hook call."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TextIO

from anthropic.types import RawMessageStreamEvent
from daimon.adapters.cli.run.events import (
    InterruptSentEvent,
    RateLimitedEvent,
    ReconnectEvent,
    RunStreamEvent,
    SseEvent,
    TerminalCancelled,
    TerminalEndTurn,
    TerminalFailed,
    TerminalMaxTurns,
    serialize_event,
    serialize_turn_state,
)
from daimon.core.turn.lifecycle import InterruptSource, ReconnectReason
from daimon.core.turn.state import TurnState


@dataclass
class NdjsonLifecycle:
    stdout: TextIO
    session_id: str
    turn_id: str

    async def on_render(self, state: TurnState) -> None:
        return None

    async def on_sse_event(self, event: RawMessageStreamEvent) -> None:
        self._emit(
            SseEvent(
                session_id=self.session_id,
                turn_id=self.turn_id,
                event=event.model_dump(mode="json"),
            )
        )

    async def on_reconnect(self, reason: ReconnectReason) -> None:
        self._emit(ReconnectEvent(session_id=self.session_id, turn_id=self.turn_id, reason=reason))

    async def on_rate_limited(self, until: datetime | None) -> None:
        self._emit(RateLimitedEvent(session_id=self.session_id, turn_id=self.turn_id, until=until))

    async def on_interrupt_sent(self, source: InterruptSource) -> None:
        self._emit(
            InterruptSentEvent(session_id=self.session_id, turn_id=self.turn_id, source=source)
        )

    async def on_terminal_success(self, state: TurnState) -> None:
        self._emit(_terminal_for_success(state, self.session_id, self.turn_id))

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:  # noqa: ARG002
        assert state.error is not None
        self._emit(
            TerminalFailed(
                session_id=self.session_id,
                turn_id=self.turn_id,
                error={"kind": state.error.kind, "message": state.error.message},
                state=serialize_turn_state(state),
            )
        )

    def _emit(self, event: RunStreamEvent) -> None:
        self.stdout.write(serialize_event(event) + "\n")
        self.stdout.flush()


def _terminal_for_success(
    state: TurnState, session_id: str, turn_id: str
) -> TerminalEndTurn | TerminalMaxTurns | TerminalCancelled:
    dumped_state = serialize_turn_state(state)
    stop_reason = state.stop_reason
    if stop_reason is not None and stop_reason.type == "retries_exhausted":
        return TerminalMaxTurns(session_id=session_id, turn_id=turn_id, state=dumped_state)
    if state.error is not None and state.error.kind == "interrupted":
        return TerminalCancelled(session_id=session_id, turn_id=turn_id, state=dumped_state)
    return TerminalEndTurn(session_id=session_id, turn_id=turn_id, state=dumped_state)
