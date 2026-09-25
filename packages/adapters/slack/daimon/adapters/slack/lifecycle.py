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
- The Block Kit flush rides the render tick (`on_render`), not `on_sse_event`:
  the driver awaits `on_sse_event` inline in its consume loop, so a debounced
  `chat.update` living there would stall read-timeout detection and deaden
  Cancel for as long as a rate-limited edit's Retry-After wait. `on_render`
  runs on a separate render task, so the same stall now only delays a render
  tick. First-update latency is at most one render tick (~2s); the 5.0s
  debounce already dominated flush timing and is unchanged; terminal flushes
  (`_flush_terminal`/`_flush_cancelled`) and `post_initial` are unaffected —
  they bypass both the debounce and the render path.
- `adopt_status_ts` (dead-session recovery only) seeds `_status_ts` before
  anything is posted, with three mechanical consequences: the first
  `_maybe_flush` takes the update branch, never the post branch; `_last_flush`
  starts at `0.0` against a `time.monotonic` clock, so the debounce is already
  satisfied and the first render tick updates with no wait; and `_register` is
  therefore never called, so the caller handing over the ts owns rebinding the
  cancel registry entry to the new cancel Event.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

import aiohttp
import structlog
from anthropic.types import RawMessageStreamEvent
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.adapters.slack.blockkit import EmbedEvent, State, TurnPhase, to_blocks, update
from daimon.adapters.slack.feedback import build_feedback_actions_block
from daimon.adapters.slack.mrkdwn import escape_mrkdwn_preserving_mentions
from daimon.adapters.slack.split import split_for_slack_safe
from daimon.core.observability import capture_exception_with_scope
from daimon.core.pricing import MODEL_PRICING, cost_of, format_cost
from daimon.core.turn.degraded import render_degraded_notice
from daimon.core.turn.lifecycle import InterruptSource, ReconnectReason
from daimon.core.turn.state import (
    ToolUseBlock,
    TurnState,
    extract_final_response,
    extract_sealed_responses,
)
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

__all__ = ["SlackTurnLifecycle"]

log = structlog.get_logger()

_DEBOUNCE_S = 5.0
_SEALED_RESPONSE_MIN_CHARS = 500  # Same substantive-answer threshold as Discord.

# Everything a chat.postMessage/chat.update can raise. slack_sdk wraps ok:false
# responses in SlackApiError but re-raises transport failures unwrapped
# (async_internal_utils re-raises the original aiohttp/timeout error), so any
# catch or suppress around a send must cover all three or the transport case
# escapes.
_SLACK_SEND_ERRORS = (SlackApiError, aiohttp.ClientError, asyncio.TimeoutError)

# Ceiling for the `text` notification fallback that accompanies `blocks`.
# The rendered content lives in the blocks (a markdown block holds 11 800
# safely); `text` only feeds notifications and previews. chat.update rejects
# a message whose `text` is block-sized with msg_too_long even though
# chat.postMessage accepts the identical payload — probed live 2026-08-24:
# update with text=11 800 fails, text=4 000 passes, and the block length is
# irrelevant to the error. Stay well under the observed pass point.
_NOTIFICATION_TEXT_MAX = 3000


def _notification_text(chunk: str) -> str:
    """Bound a content chunk for use as the `text` notification fallback."""
    if len(chunk) <= _NOTIFICATION_TEXT_MAX:
        return chunk
    return chunk[: _NOTIFICATION_TEXT_MAX - 1] + "…"


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
    When ``intent_id`` is supplied, its hex value remains in the Cancel button
    across every nonterminal render; Slack message timestamp remains the live
    registry fallback after the initial post response.
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
        register_pending: Callable[[str, asyncio.Event, str], None] | None = None,
        deregister_pending: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        adopt_status_ts: str | None = None,
        intent_id: UUID | None = None,
    ) -> None:
        self._client = client
        self._channel = channel
        self._thread_ts = thread_ts
        self._cancel = cancel
        self._author_id = author_id
        self._model_id = model_id
        self._register = register
        self._deregister = deregister
        self._register_pending = register_pending
        self._deregister_pending = deregister_pending
        self._cancel_key = intent_id.hex if intent_id is not None else uuid4().hex
        self._intent_cancel_key = intent_id.hex if intent_id is not None else None
        self._pending_registered = False
        self._clock = clock
        self._state: State = State(
            phase=TurnPhase.THINKING,
            agent_name=agent_name,
            started_at=self._clock(),
        )
        # Seeded only by dead-session recovery, which hands over the card the
        # failed attempt already posted. Without this the recovery lifecycle
        # posts a SECOND card, and the first one is never finalised — the turn
        # driver withholds the failed attempt's terminal hook while a recovery
        # is in flight, so on a successful recovery nothing ever collapses card
        # one and it sits on "thinking" beside the answer. The turn marker
        # written against the pre-recovery mapping row addresses the adopted
        # card, so a restarted process repairs the card the user is actually
        # watching rather than an abandoned one.
        self._status_ts: str | None = adopt_status_ts
        self._last_flush: float = 0.0
        self._terminal: bool = False
        self.final_ts: str | None = None
        # A continuity notice that belongs ABOVE the answer it explains. The
        # answer replaces the status card in place, so a notice posted as its
        # own message always sorts below it no matter when it was sent --
        # Slack orders by the original ts. Set before the answer is revealed
        # (a planned replacement, known at bind time); `prepend_revealed_answer`
        # covers the fact learned only after the turn ran.
        self.answer_prefix: str | None = None
        self.answer_prefix_applied: bool = False
        # The first chunk and blocks actually rendered into the status card,
        # kept so a late notice can be edited in above them exactly once.
        self._revealed_first_chunk: str | None = None
        self._revealed_first_blocks: list[dict[str, Any]] | None = None

    @property
    def status_ts(self) -> str | None:
        """The ts of the status message this lifecycle is rendering into, or
        None if nothing has been posted yet.

        Public so the caller can record the turn marker (message ts, channel,
        started_at) as soon as the card exists; nothing else should need it.
        """
        return self._status_ts

    async def post_initial(self) -> None:
        """Post the initial status card immediately, before session setup.

        Called before session binding so the user gets instant feedback and
        the Cancel button exists before the first SSE event -- MA
        sessions.create plus the history replay and image download that
        follow can hold for minutes. Runs before the turn starts (and
        therefore before any render tick exists), so this is the one place
        that deliberately flushes directly instead of waiting on an SSE
        event. _maybe_flush registers a per-turn action key before awaiting
        chat.postMessage, so a click is routable as soon as Slack exposes the
        card, before its response supplies the message ts. Once the response
        returns, the ts is registered as a recovery and legacy lookup key.
        """
        await self._maybe_flush()

    async def on_sse_event(self, event: RawMessageStreamEvent) -> None:
        # Cheap local tap per the hardened TurnLifecycle contract:
        # the pump awaits this hook inline, so it stays a local reducer
        # call only. The Block Kit flush (chat-API I/O) rides the render
        # tick instead, where a slow or rate-limited chat.update only
        # delays the render task, never the consume loop.
        embed_event = _map_sse_event(event)
        if embed_event is None:
            return
        self._state = update(self._state, embed_event)

    async def _maybe_flush(self) -> None:
        """Post or update the status message, subject to debounce. No-op after terminal."""
        if self._terminal:
            return
        now = self._clock()
        if self._intent_cancel_key is not None:
            cancel_key = self._intent_cancel_key
        elif self._status_ts is None:
            cancel_key = self._cancel_key
        else:
            cancel_key = self._status_ts
        blocks = to_blocks(self._state, now=now, cancel_key=cancel_key)
        text = f"{self._state.phase.value} …"

        if self._status_ts is None:
            # First flush — immediate, no debounce.
            # Slack may expose the card before returning its ts. Register the
            # per-turn button value first so a click in that window is routable.
            if self._register_pending is not None:
                self._register_pending(self._cancel_key, self._cancel, self._author_id)
                self._pending_registered = True
            try:
                resp = await self._client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                    channel=self._channel,
                    thread_ts=self._thread_ts,
                    blocks=blocks,
                    text=text,
                )
            except BaseException:
                if self._pending_registered and self._deregister_pending is not None:
                    self._deregister_pending(self._cancel_key)
                    self._pending_registered = False
                raise
            self._status_ts = cast(str, resp["ts"])  # pyright: ignore[reportUnknownVariableType]
            self._register(self._status_ts, self._cancel, self._author_id)
            # The card is now routable by its message ts. Remove the temporary
            # key promptly so later clicks follow any recovery rebind of that ts.
            if self._pending_registered and self._deregister_pending is not None:
                self._deregister_pending(self._cancel_key)
                self._pending_registered = False
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
        blocks = to_blocks(self._state, now=self._clock(), cancel_key=self._cancel_key)
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

    async def _repair_terminal_flush(self, text: str) -> None:
        """Best-effort collapse of the status message after a failed terminal flush.

        Replaces whatever the last debounced flush wrote — phase title, tool
        trail, cancel actions — with a plain failure notice. The cancel Event
        is deregistered on every terminal path, so a status message left on the
        live surface shows a running turn with a dead cancel button forever.
        The repair can fail for the same reason as the flush (revoked token,
        archived channel, network outage); there is nothing further to do then,
        so it is suppressed rather than retried.
        """
        if self._status_ts is None:
            return
        with contextlib.suppress(*_SLACK_SEND_ERRORS):
            # The status_ts guard above pins _post_or_update to its update branch.
            await self._post_or_update(
                [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
                text,
            )

    async def on_terminal_success(self, state: TurnState) -> None:
        """Replace status message with final answer; post overflow chunks; widen final_ts.

        If no final text (tool-only or cancelled), collapses to the done footer
        in place. Always deregisters the cancel Event in finally.

        Does not re-raise on Slack send errors — the lifecycle boundary absorbs
        render failures (same contract as on_terminal_failure), logging at
        error level and capturing to Sentry since the swallowed exception is an
        answer-delivery outage the listener boundary can no longer see. If the
        flush fails before the status message is replaced, a best-effort repair
        collapses it to a failure notice; if it fails during overflow, the
        already-replaced answer is left standing. final_ts stays None on any
        flush failure so the caller's watermark cannot advance past content
        the user never saw.
        """
        # Transition to the DONE phase BEFORE rendering so to_blocks emits the
        # terminal collapse (cost/usage footer, no cancel button) — matches the
        # Discord parity reference. Without this the status would render as still
        # running and the footer would never appear.
        self._state = update(self._state, EmbedEvent(kind="done", label=""))
        self._apply_usage(state)
        # Flipped after the status message is successfully replaced with final
        # content — past that point a repair would overwrite answer text the
        # user can already read, so the except branch skips it.
        surface_replaced = False
        # A no-answer collapse (tool-only done footer, "Turn cancelled.") must
        # not be repaired with copy that claims an answer existed.
        repair_notice = "⚠️ Something went wrong finishing this turn."
        try:
            answer_parts = [
                text
                for _, text in extract_sealed_responses(
                    state.content, min_chars=_SEALED_RESPONSE_MIN_CHARS
                )
            ]
            final_response = extract_final_response(state.content)
            if final_response:
                answer_parts.append(final_response)
            final_text = "\n\n".join(answer_parts)
            if not final_text:
                # No final answer. A tool-only turn keeps the collapsed done
                # footer; a truly empty turn (cancellation) shows "Turn
                # cancelled." — matches the Discord parity reference.
                if any(isinstance(block, ToolUseBlock) for block in state.content):
                    await self._flush_terminal()
                    # #79: no reply to hang the notice under on a tool-only
                    # turn, so a dropped server is named on its own line.
                    tool_only_notice = render_degraded_notice(state.mcp_failures)
                    if tool_only_notice is not None:
                        await self._client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                            channel=self._channel,
                            thread_ts=self._thread_ts,
                            blocks=[{"type": "markdown", "text": tool_only_notice}],
                            text=tool_only_notice,
                        )
                else:
                    await self._flush_cancelled()
                self.final_ts = self._status_ts
                return

            repair_notice = "⚠️ Something went wrong posting the answer."
            if self.answer_prefix is not None:
                final_text = f"{self.answer_prefix}\n\n{final_text}"
                self.answer_prefix_applied = True
            # #79: same trailer as Discord — a dropped server is named under
            # the reply rather than swallowing the reply.
            degraded_notice = render_degraded_notice(state.mcp_failures)
            if degraded_notice is not None:
                final_text = f"{final_text}\n\n{degraded_notice}"
            chunks = split_for_slack_safe(escape_mrkdwn_preserving_mentions(final_text))
            first_chunk = chunks[0]
            # First chunk + the cost/usage footer replace the status message
            # in place; the terminal footer carries elapsed/tokens/cost and drops
            # the cancel button. The feedback vote buttons ride the LAST chunk
            # only, so a vote's message_id keys the same message final_ts (and
            # the watermark) point at.
            self._terminal = True
            first_blocks: list[dict[str, Any]] = [
                {"type": "markdown", "text": first_chunk},
                *to_blocks(self._state, now=self._clock()),
            ]
            if len(chunks) == 1:
                first_blocks.append(build_feedback_actions_block())
            await self._post_or_update(first_blocks, _notification_text(first_chunk))
            self._revealed_first_chunk = first_chunk
            self._revealed_first_blocks = first_blocks
            surface_replaced = True
            assert self._status_ts is not None  # narrowing — _post_or_update always sets it
            current_ts = self._status_ts

            # Overflow chunks posted as new thread replies.
            for i, chunk in enumerate(chunks[1:], start=2):
                chunk_blocks: list[dict[str, Any]] = [{"type": "markdown", "text": chunk}]
                if i == len(chunks):
                    chunk_blocks.append(build_feedback_actions_block())
                resp = await self._client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                    channel=self._channel,
                    thread_ts=self._thread_ts,
                    blocks=chunk_blocks,
                    text=_notification_text(chunk),
                )
                current_ts = cast(str, resp["ts"])  # pyright: ignore[reportUnknownVariableType]

            self.final_ts = current_ts
        except _SLACK_SEND_ERRORS as exc:
            # error + Sentry, not warning: pre-repair this exception reached the
            # listener boundary's log.error/capture path, and it means a billed
            # turn whose answer the user never received.
            log.error("turn.terminal_success.flush_failed", exc_info=True)
            capture_exception_with_scope(exc)
            if not surface_replaced:
                await self._repair_terminal_flush(repair_notice)
        finally:
            if self._status_ts is not None:
                self._deregister(self._status_ts)
            if self._pending_registered and self._deregister_pending is not None:
                self._deregister_pending(self._cancel_key)
                self._pending_registered = False

    async def prepend_revealed_answer(self, notice: str) -> bool:
        """Edit `notice` in above an answer already on screen; False if it cannot go there.

        For a fact the turn only produces on its way out (an unexpected
        workspace loss, discovered by the driver's mid-call recovery): by the
        time the caller knows it, the answer has replaced the status card.
        Sending the notice afterwards puts it below the answer it explains, so
        instead the card is updated once with the notice on top.

        Returns False -- caller posts it as an ordinary message instead --
        when there is no revealed answer to sit above, when the notice would
        push the first chunk past Slack's per-block ceiling (re-splitting would
        strand the overflow messages already posted), or when the edit fails.
        """
        if self._revealed_first_chunk is None or self._revealed_first_blocks is None:
            return False
        updated = f"{escape_mrkdwn_preserving_mentions(notice)}\n\n{self._revealed_first_chunk}"
        if len(split_for_slack_safe(updated)) > 1:
            return False
        blocks = list(self._revealed_first_blocks)
        blocks[0] = {"type": "markdown", "text": updated}
        try:
            await self._post_or_update(blocks, _notification_text(updated))
        except _SLACK_SEND_ERRORS:
            log.warning("turn.answer_prefix.edit_failed", exc_info=True)
            return False
        self._revealed_first_chunk = updated
        self._revealed_first_blocks = blocks
        self.answer_prefix_applied = True
        return True

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
        except Exception:
            log.warning("turn.terminal_failure.flush_failed", exc_info=True)
            await self._repair_terminal_flush("⚠️ Something went wrong finishing this turn.")
        finally:
            if self._status_ts is not None:
                self._deregister(self._status_ts)
            if self._pending_registered and self._deregister_pending is not None:
                self._deregister_pending(self._cancel_key)
                self._pending_registered = False

    async def on_render(self, state: TurnState) -> None:
        """Sole delivery path. `state` is unused: Slack's card is
        driven by its own Block Kit `State` folded in `on_sse_event` — this
        hook is the tick that delivers it."""
        if self._terminal:
            return
        await self._maybe_flush()

    async def on_reconnect(self, reason: ReconnectReason) -> None:
        pass

    async def on_rate_limited(self, until: datetime | None) -> None:
        pass

    async def on_interrupt_sent(self, source: InterruptSource) -> None:
        pass
