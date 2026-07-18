"""Slack adapter implementation of TurnLifecycle — full rendering.

SlackTurnLifecycle receives SSE events from the turn driver, accumulates
Block Kit state via the blockkit module, debounces chat.update at 5s,
replaces the status message in-place on terminal success with overflow
chunk support (final_ts widened to the last posted message), applies
the cost/usage footer, and registers/deregisters its cancel Event
in the SlackApp registry.

Design decisions:
- register/deregister injected as callables (not a dict ref) — keeps the
  lifecycle decoupled from SlackApp's internal registry representation.
- _DEBOUNCE_S=5.0 — half of Discord's 10s; Slack threads are more real-time.
- Final answer rendered as a native markdown block (DEFAULT path,
  locked by live-workspace probe).
- text= always passed alongside blocks= to satisfy Slack's notification
  fallback requirement (Pitfall 3).
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

import structlog
from anthropic.types import RawMessageStreamEvent
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.adapters.slack.blockkit import EmbedEvent, State, TurnPhase, to_blocks, update
from daimon.adapters.slack.mrkdwn import escape_mrkdwn_preserving_mentions
from daimon.adapters.slack.split import split_for_slack_safe
from daimon.core.pricing import MODEL_PRICING, cost_of, format_cost
from daimon.core.turn.lifecycle import InterruptSource, ReconnectReason
from daimon.core.turn.state import ToolUseBlock, TurnState, extract_final_response
from slack_sdk.web.async_client import AsyncWebClient

__all__ = ["SlackTurnLifecycle"]

log = structlog.get_logger()

_DEBOUNCE_S = 5.0


def _map_sse_event(event: RawMessageStreamEvent) -> EmbedEvent | None:
    """Map a Managed Agents session SSE event to an EmbedEvent, or None if irrelevant."""
    event_type: str = getattr(event, "type", "")
    if event_type == "agent.thinking":
        return EmbedEvent(kind="thinking", label="")
    if event_type == "agent.tool_use":
        name: str = getattr(event, "name", "tool")
        return EmbedEvent(kind="tool_use", label=name)
    if event_type == "agent.message":
        parts: list[object] = getattr(event, "content", [])
        text = "".join(getattr(p, "text", "") for p in parts).strip()
        return EmbedEvent(kind="message", label=text)
    return None


class SlackTurnLifecycle:
    """TurnLifecycle implementation for Slack. Created fresh per turn.

    Accumulates Block Kit state from SSE events, debounces chat.update at
    _DEBOUNCE_S seconds, replaces the status message in-place on terminal
    success, applies the cost/usage footer, and registers/deregisters the
    cancel Event in the caller-supplied registry.

    Constructor args are all keyword-only (mirrors DiscordTurnLifecycle's
    DI shape). register/deregister are injected as plain callables so the
    lifecycle never holds a reference to SlackApp's internal registry dict.
    """

    def __init__(
        self,
        *,
        client: AsyncWebClient,
        channel: str,
        thread_ts: str,
        cancel: asyncio.Event,
        author_id: str,
        agent_name: str,
        model_id: str,
        register: Callable[[str, asyncio.Event, str], None],
        deregister: Callable[[str], None],
    ) -> None:
        self._client = client
        self._channel = channel
        self._thread_ts = thread_ts
        self._cancel = cancel
        self._author_id = author_id
        self._model_id = model_id
        self._register = register
        self._deregister = deregister
        self._state: State = State(
            phase=TurnPhase.THINKING,
            agent_name=agent_name,
            started_at=time.monotonic(),
        )
        self._status_ts: str | None = None
        self._last_flush: float = 0.0
        self._terminal: bool = False
        self.final_ts: str | None = None

    async def on_sse_event(self, event: RawMessageStreamEvent) -> None:
        embed_event = _map_sse_event(event)
        if embed_event is None:
            return
        self._state = update(self._state, embed_event)
        await self._maybe_flush()

    async def _maybe_flush(self) -> None:
        """Post or update the status message, subject to debounce. No-op after terminal."""
        if self._terminal:
            return
        now = time.monotonic()
        blocks = to_blocks(self._state, now=now)
        text = f"{self._state.phase.value} …"

        if self._status_ts is None:
            # First flush — immediate, no debounce.
            resp = await self._client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                channel=self._channel,
                thread_ts=self._thread_ts,
                blocks=blocks,
                text=text,
            )
            self._status_ts = cast(str, resp["ts"])  # pyright: ignore[reportUnknownVariableType]
            self._register(self._status_ts, self._cancel, self._author_id)
            self._last_flush = now
        elif now - self._last_flush >= _DEBOUNCE_S:
            # Debounce elapsed — update the status message in place.
            await self._client.chat_update(  # pyright: ignore[reportUnknownMemberType]
                channel=self._channel,
                ts=self._status_ts,
                blocks=blocks,
                text=text,
            )
            self._last_flush = now

    def _apply_usage(self, state: TurnState) -> None:
        """Fold accumulated token totals + priced cost onto the Block Kit state.

        Reconstructs a per-turn BetaManagedAgentsSpanModelUsage from the four
        cache-split totals and prices it through cost_of, so the footer cost
        matches the billing ledger to the cent. An unpriced model yields None
        cost — the footer omits the cost segment.
        """
        t = state.usage_totals
        usage = BetaManagedAgentsSpanModelUsage(
            input_tokens=t.input_tokens,
            cache_creation_input_tokens=t.cache_creation_input_tokens,
            cache_read_input_tokens=t.cache_read_input_tokens,
            output_tokens=t.output_tokens,
            speed="standard",
        )
        cost = cost_of(usage, MODEL_PRICING.get(self._model_id))
        merged_in = t.input_tokens + t.cache_creation_input_tokens + t.cache_read_input_tokens
        self._state = dataclasses.replace(
            self._state,
            usage_in=merged_in,
            usage_out=t.output_tokens,
            cost_str=format_cost(cost),
        )

    async def _post_or_update(self, blocks: list[dict[str, Any]], text: str) -> None:
        """Post the status message the first time, or update it in place after.

        On first post, records status_ts and registers the cancel Event so a
        cancel click is routable even for turns that reach terminal without any
        prior SSE flush.
        """
        if self._status_ts is None:
            resp = await self._client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                channel=self._channel,
                thread_ts=self._thread_ts,
                blocks=blocks,
                text=text,
            )
            self._status_ts = cast(str, resp["ts"])  # pyright: ignore[reportUnknownVariableType]
            self._register(self._status_ts, self._cancel, self._author_id)
        else:
            await self._client.chat_update(  # pyright: ignore[reportUnknownMemberType]
                channel=self._channel,
                ts=self._status_ts,
                blocks=blocks,
                text=text,
            )

    async def _flush_terminal(self) -> None:
        """Unconditionally flush the terminal Block Kit surface, bypassing debounce.

        Sets _terminal=True so subsequent _maybe_flush calls become no-ops. The
        caller MUST have already transitioned the state to a terminal phase
        (done/error) so to_blocks emits the collapsed footer with no cancel button.
        """
        self._terminal = True
        blocks = to_blocks(self._state, now=time.monotonic())
        await self._post_or_update(blocks, f"{self._state.phase.value} …")

    async def _flush_cancelled(self) -> None:
        """Replace the status message with a plain 'Turn cancelled.' notice.

        Used when a turn ends with no final text and no tool activity (a bare
        cancellation) — mirrors the Discord adapter's cancelled-turn message and
        drops the cancel button.
        """
        self._terminal = True
        await self._post_or_update(
            [{"type": "section", "text": {"type": "mrkdwn", "text": "Turn cancelled."}}],
            "Turn cancelled.",
        )

    async def on_terminal_success(self, state: TurnState) -> None:
        """Replace status message with final answer; post overflow chunks; widen final_ts.

        If no final text (tool-only or cancelled), collapses to the done footer
        in place. Always deregisters the cancel Event in finally.
        """
        # Transition to the DONE phase BEFORE rendering so to_blocks emits the
        # terminal collapse (cost/usage footer, no cancel button) — matches the
        # Discord parity reference. Without this the status would render as still
        # running and the footer would never appear.
        self._state = update(self._state, EmbedEvent(kind="done", label=""))
        self._apply_usage(state)
        try:
            final_text = extract_final_response(state.content)
            if not final_text:
                # No final answer. A tool-only turn keeps the collapsed done
                # footer; a truly empty turn (cancellation) shows "Turn
                # cancelled." — matches the Discord parity reference.
                if any(isinstance(block, ToolUseBlock) for block in state.content):
                    await self._flush_terminal()
                else:
                    await self._flush_cancelled()
                self.final_ts = self._status_ts
                return

            chunks = split_for_slack_safe(escape_mrkdwn_preserving_mentions(final_text))
            first_chunk = chunks[0]
            # First chunk + the cost/usage footer replace the status message
            # in place; the terminal footer carries elapsed/tokens/cost and drops
            # the cancel button.
            self._terminal = True
            first_blocks: list[dict[str, Any]] = [
                {"type": "markdown", "text": first_chunk},
                *to_blocks(self._state, now=time.monotonic()),
            ]
            await self._post_or_update(first_blocks, first_chunk)
            assert self._status_ts is not None  # narrowing — _post_or_update always sets it
            current_ts = self._status_ts

            # Overflow chunks posted as new thread replies.
            for chunk in chunks[1:]:
                resp = await self._client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                    channel=self._channel,
                    thread_ts=self._thread_ts,
                    blocks=[{"type": "markdown", "text": chunk}],
                    text=chunk,
                )
                current_ts = cast(str, resp["ts"])  # pyright: ignore[reportUnknownVariableType]

            self.final_ts = current_ts
        finally:
            if self._status_ts is not None:
                self._deregister(self._status_ts)

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        """Log failure, attempt to flush error state, then deregister.

        Does not re-raise — the lifecycle boundary absorbs all failures.
        """
        try:
            log.warning("turn.terminal_failure", error=str(err))
            # Transition to the ERROR phase so to_blocks renders the ❌ error
            # footer (reason + usage) and removes the cancel button — Discord parity.
            self._state = update(self._state, EmbedEvent(kind="error", label=str(err)[:100]))
            self._apply_usage(state)
            await self._flush_terminal()
            self.final_ts = self._status_ts
        except Exception:  # noqa: BLE001
            log.warning("turn.terminal_failure.flush_failed", exc_info=True)
        finally:
            if self._status_ts is not None:
                self._deregister(self._status_ts)

    async def on_render(self, state: TurnState) -> None:
        """No-op — Block Kit state is driven by SSE events, not render ticks."""

    async def on_reconnect(self, reason: ReconnectReason) -> None:
        pass

    async def on_rate_limited(self, until: datetime | None) -> None:
        pass

    async def on_interrupt_sent(self, source: InterruptSource) -> None:
        pass
