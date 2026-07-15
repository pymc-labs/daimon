"""Discord adapter implementation of the TurnLifecycle protocol.

Created fresh per turn (D-08). Accumulates embed state via SSE events,
debounces Discord API calls, and performs a clean replace on terminal success.

Design decisions D-06, D-07, D-09, D-11:
- send/edit callables injected at construction (no discord.py imports required
  for testing)
- 10s debounce between intermediate embed edits (SPEC-R5)
- First SSE event causes immediate embed post (SPEC-R1)
- Terminal success replaces embed with plain text (SPEC-R6)
- Terminal failure shows red error embed that stays visible (SPEC-R7)
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import structlog
from anthropic.types import RawMessageStreamEvent
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.adapters.discord.embed import (
    EmbedData,
    EmbedEvent,
    EmbedState,
    TurnPhase,
    to_embed_data,
    to_preview_embed_data,
    update,
)
from daimon.adapters.discord.split import split_for_discord_safe
from daimon.core.pricing import MODEL_PRICING, cost_of, format_cost
from daimon.core.turn.lifecycle import InterruptSource, ReconnectReason
from daimon.core.turn.state import (
    ToolUseBlock,
    TurnState,
    extract_final_response,
    extract_sealed_responses,
)

import discord

log = structlog.get_logger()

SendFn = Callable[..., Awaitable[Any]]
EditFn = Callable[..., Awaitable[None]]

_DEBOUNCE_S = 10.0

# A text block sealed by a later tool use posts permanently once it reaches
# this size; shorter sealed blocks are pre-tool narration and stay in the
# ephemeral preview embed. Calibrated on prod sessions 2026-07-04..13: the
# largest narration block was 429 chars, the smallest swallowed answer 542.
_SEALED_RESPONSE_MIN_CHARS = 500


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
        # Full text — the embed state machine caps it for the preview embed.
        text = "".join(getattr(p, "text", "") for p in parts).strip()
        return EmbedEvent(kind="message", label=text)
    return None


def build_discord_embed(data: EmbedData) -> discord.Embed:
    """Convert EmbedData to a discord.Embed.

    Empty title/description (the collapsed terminal state) are passed as None so
    Discord renders just the colored bar + footer — no blank title/body element.
    """
    embed = discord.Embed(
        title=data.title or None, description=data.description or None, color=data.color
    )
    if data.footer is not None:
        embed.set_footer(text=data.footer)
    return embed


class DiscordTurnLifecycle:
    """TurnLifecycle implementation for Discord. Created fresh per turn (D-08).

    Receives SSE events, accumulates embed state, debounces Discord API calls.
    send and edit callables are injected so the class is testable without
    a real discord.py connection.
    """

    def __init__(
        self,
        *,
        send: SendFn,
        edit: EditFn,
        agent_name: str,
        model_id: str,
        cancel_view: discord.ui.View | None = None,
    ) -> None:
        self._send = send
        self._edit = edit
        self._agent_name = agent_name
        self._model_id = model_id
        self._state = EmbedState(
            phase=TurnPhase.THINKING,
            trail=(),
            agent_name=agent_name,
            started_at=time.monotonic(),
        )
        self._message_ref: Any | None = None
        self._last_flush: float = 0.0
        self._terminal: bool = False
        self._cancel_view = cancel_view
        self._persisted_sealed_indices: set[int] = set()

    async def post_initial(self) -> None:
        """Post the initial thinking embed immediately, before the turn starts.

        Called before session setup so the user gets instant feedback —
        MA sessions.create can hold its response for minutes while it
        provisions the session. The posted message becomes the lifecycle's
        message ref, so subsequent SSE flushes edit it in place.
        """
        await self._maybe_flush()

    async def on_sse_event(self, event: RawMessageStreamEvent) -> None:
        embed_event = _map_sse_event(event)
        if embed_event is None:
            return
        self._state = update(self._state, embed_event)
        await self._maybe_flush()

    def _build_embeds(self, now: float) -> list[discord.Embed]:
        """Render the activity embed plus the optional text-preview embed below it."""
        embeds = [build_discord_embed(to_embed_data(self._state, now=now))]
        preview = to_preview_embed_data(self._state)
        if preview is not None:
            embeds.append(build_discord_embed(preview))
        return embeds

    async def _maybe_flush(self) -> None:
        """Post or edit the embeds, subject to debounce. No-op after terminal."""
        if self._terminal:
            return
        now = time.monotonic()
        if self._message_ref is None:
            # First post — immediate, no debounce
            self._message_ref = await self._send(
                embeds=self._build_embeds(now), view=self._cancel_view
            )
            self._last_flush = now
        elif now - self._last_flush >= _DEBOUNCE_S:
            # Debounce elapsed — edit
            await self._edit(
                self._message_ref, embeds=self._build_embeds(now), view=self._cancel_view
            )
            self._last_flush = now
        # else: within debounce window — skip

    def _apply_usage(self, state: TurnState) -> None:
        """Fold the turn's accumulated token totals + priced cost onto the
        embed state before a terminal flush.

        Reconstructs a per-turn ``BetaManagedAgentsSpanModelUsage`` from the four
        cache-split totals and prices it through the same ``cost_of`` the billing
        ledger uses, so the footer cost matches the ledger to the cent. The
        displayed input count stays merged (input + cache_creation + cache_read);
        only the cost math is stage-split, inside ``cost_of``. An unpriced model
        yields ``cost_of`` -> None -> ``cost_str`` None -> footer omits the cost.
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

    async def _flush_terminal(self) -> None:
        """Unconditionally flush terminal state as a single collapsed embed,
        bypassing debounce. ``embeds=[...]`` also drops the preview embed."""
        self._terminal = True
        now = time.monotonic()
        data = to_embed_data(self._state, now=now)
        embed = build_discord_embed(data)
        if self._message_ref is None:
            self._message_ref = await self._send(embeds=[embed], view=None)
        else:
            await self._edit(self._message_ref, embeds=[embed], view=None)

    async def _persist_sealed_responses(self, state: TurnState) -> None:
        """Post sealed answers (text blocks a later tool call made immutable)
        as permanent messages, once each. Without this, an answer composed
        before a trailing tool call (e.g. a memory-repo write) is discarded by
        the final-response extraction and only the post-tool recap survives."""
        for index, text in extract_sealed_responses(
            state.content, min_chars=_SEALED_RESPONSE_MIN_CHARS
        ):
            if index in self._persisted_sealed_indices:
                continue
            self._persisted_sealed_indices.add(index)
            for chunk in split_for_discord_safe(text):
                await self._send(content=chunk)
            log.info("turn.sealed_response_posted", block_index=index, chars=len(text))

    async def on_terminal_success(self, state: TurnState) -> None:
        await self._persist_sealed_responses(state)
        self._apply_usage(state)
        self._state = update(self._state, EmbedEvent(kind="done", label=""))
        await self._flush_terminal()

        response_text = extract_final_response(state.content)
        if not response_text:
            # D-06: if tools ran but no final text, leave done embed visible.
            # If content is entirely empty (cancellation), show "Turn cancelled."
            has_tool_activity = any(isinstance(block, ToolUseBlock) for block in state.content)
            if has_tool_activity:
                log.info("turn.terminal_success", has_text=False, tool_only=True)
                return
            await self._edit(self._message_ref, content="Turn cancelled.", embed=None, view=None)
            log.info("turn.terminal_success", has_text=False)
            return

        chunks = split_for_discord_safe(response_text)
        # Clean replace: first chunk replaces the embed
        await self._edit(self._message_ref, content=chunks[0], embed=None, view=None)
        # Overflow: subsequent chunks posted as new messages
        for chunk in chunks[1:]:
            await self._send(content=chunk)

        log.info("turn.terminal_success")

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        self._apply_usage(state)
        self._state = update(self._state, EmbedEvent(kind="error", label=str(err)[:100]))
        await self._flush_terminal()
        log.warning("turn.terminal_failure", error=str(err))

    async def on_render(self, state: TurnState) -> None:
        # Embed state is driven by on_sse_event and terminal hooks (D-06);
        # render ticks only flush sealed answers so they land near-live.
        if self._terminal:
            return
        await self._persist_sealed_responses(state)

    async def on_reconnect(self, reason: ReconnectReason) -> None:
        pass

    async def on_rate_limited(self, until: datetime | None) -> None:
        pass

    async def on_interrupt_sent(self, source: InterruptSource) -> None:
        pass

    @property
    def final_message_id(self) -> str | None:
        """Discord message id of the last embed the bot posted this turn, or None
        if nothing was sent (the watermark source for session-per-thread reuse)."""
        if self._message_ref is None:
            return None
        return str(self._message_ref.id)
