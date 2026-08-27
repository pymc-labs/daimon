"""Lifecycle protocol the driver calls into. Implemented by adapters
(CLI admin, agentic-CLI subprocess, Discord, MCP). Kept in its own module
so adapters can depend on the protocol without dragging the driver's
asyncio/tenacity machinery.

**The cost contract, per hook — this is what makes the protocol safe to
call from inside the pump:**

  on_render              — the SOLE content-delivery path. Called on every
                           non-empty render tick and once synchronously
                           after the terminal event folds (the guarded
                           final render every finalizer performs). Slow
                           implementations are safe here: the render loop
                           runs as a separate task on its own timer and
                           never stalls the consume loop. Adapter
                           exceptions raised from `on_render` are caught by
                           the driver PER TICK, logged (`turn.render_failed`),
                           and retried on the next tick against the same
                           delta — the render anchor only advances on
                           success, so a failure costs one tick, never the
                           turn. `on_render` may talk to the network
                           (chat-API posts/edits, HTTP calls, retry-after
                           sleeps) — that is what it is for.
  on_sse_event           — a cheap local tap: bookkeeping only,
                           no network I/O.
                           The pump `await`s this hook INLINE, in the
                           consume loop, before folding the event into
                           state — so any I/O here stalls the whole
                           consume loop. Two concrete costs:
                           (1) it arbitrarily delays the read-timeout
                           liveness clock (`stream_read_timeout_s`),
                           blurring what that timeout actually measures,
                           since the clock only runs while the pump is
                           awaiting a read; and (2) it makes the Cancel
                           button dead for the stall's duration, because
                           the cancel race only re-enters at the top of
                           the consume loop. Permitted: transcript
                           appends, counters, local state reducers,
                           writing to an already-open stdout. Forbidden:
                           chat-API posts/edits, HTTP calls, anything with
                           a retry-after sleep — move that to `on_render`
                           instead. Default no-op. Known, scheduled
                           deviations (not silent contradictions): both
                           Discord's and Slack's implementations still do
                           their turn I/O here today (a chat-API post/edit
                           on every event); each moves to `on_render` in
                           its own follow-up piece of work, Discord's
                           landing immediately after this contract change.
  on_terminal_success    — bookkeeping only (structlog, transcripts).
                           Must NOT render content — same cheap-tap
                           contract as `on_sse_event`.
  on_terminal_failure    — bookkeeping only. Same contract.
  on_reconnect           — driver reconnected to an in-flight session.
                           Cheap tap, same no-I/O expectation as
                           `on_sse_event`. Default no-op. `reason` names
                           the cause: `connection_dropped` = error-path
                           reconnect under the bounded 2-attempt retry
                           budget; `clean_close` = the server ended the
                           stream with no terminal event and the session is
                           still running; `read_timeout` = no bytes for
                           `stream_read_timeout_s` and the session is still
                           running.
  on_rate_limited        — driver hit a 429 and is about to sleep. Cheap
                           tap, same no-I/O expectation as `on_sse_event`.
                           Default no-op.
  on_interrupt_sent      — driver just posted user.interrupt to MA. Cheap
                           tap, same no-I/O expectation as `on_sse_event`.
                           Default no-op.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from anthropic.types import RawMessageStreamEvent
from daimon.core.turn.state import TurnState

ReconnectReason = Literal["connection_dropped", "clean_close", "read_timeout"]
InterruptSource = Literal["sigint", "cancel_event"]


class TurnLifecycle(Protocol):
    async def on_render(self, state: TurnState) -> None: ...

    async def on_terminal_success(self, state: TurnState) -> None: ...

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None: ...

    async def on_sse_event(self, event: RawMessageStreamEvent) -> None:
        return None

    async def on_reconnect(self, reason: ReconnectReason) -> None:
        return None

    async def on_rate_limited(self, until: datetime | None) -> None:
        return None

    async def on_interrupt_sent(self, source: InterruptSource) -> None:
        return None
