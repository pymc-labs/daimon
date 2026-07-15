"""Lifecycle protocol the driver calls into. Implemented by adapters
(CLI admin, agentic-CLI subprocess, Discord, MCP). Kept in its own module
so adapters can depend on the protocol without dragging the driver's
asyncio/tenacity machinery.

Seven hooks:
  on_render              — sole content-display path; called on every
                           non-empty render tick and once synchronously
                           after the terminal event folds.
  on_terminal_success    — bookkeeping only (structlog, transcripts).
                           Must NOT render content.
  on_terminal_failure    — bookkeeping only. Same contract.
  on_sse_event           — upstream Anthropic SSE event, verbatim, before
                           the reducer folds it into state. Default no-op.
  on_reconnect           — driver reconnected to an in-flight session.
                           Default no-op.
  on_rate_limited        — driver hit a 429 and is about to sleep.
                           Default no-op.
  on_interrupt_sent      — driver just posted user.interrupt to MA.
                           Default no-op.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from anthropic.types import RawMessageStreamEvent
from daimon.core.turn.state import TurnState

ReconnectReason = Literal["connection_dropped"]
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
