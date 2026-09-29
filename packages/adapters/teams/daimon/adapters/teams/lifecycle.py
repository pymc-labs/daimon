"""`TurnLifecycle` for Teams: one status card, edited in place, then the answer.

Like Slack's: the card is posted before session setup, edited at most every
five seconds, and replaced by the first answer chunk; overflow chunks follow,
the last with the usage footer and feedback buttons. Teams streaming is unused:
it works only in personal chats and stops after two minutes. Cancel clicks
route by `cancel_key` (the card intent id), carried from the first render.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

import httpx
import structlog
from anthropic.types import RawMessageStreamEvent
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.adapters.teams import card
from daimon.core.message_split import split_fenced
from daimon.core.observability import capture_exception_with_scope
from daimon.core.pricing import MODEL_PRICING, cost_of, format_cost
from daimon.core.turn.degraded import render_degraded_notice
from daimon.core.turn.lifecycle import InterruptSource, ReconnectReason
from daimon.core.turn.notices import render_termination_notice
from daimon.core.turn.state import (
    ToolUseBlock,
    TurnState,
    extract_final_response,
    extract_sealed_responses,
)
from daimon.core.turn.termination import termination_reason
from microsoft_teams.api import MessageActivityInput, SentActivity

log = structlog.get_logger()

_DEBOUNCE_S = 5.0
_SEALED_RESPONSE_MIN_CHARS = 500  # Same substantive-answer threshold as Slack.
_DELIVERY_FAILED = "⚠️ Something went wrong posting the answer."
# Slack's copy for a no-answer turn, which must not claim an answer existed.
_FINISH_FAILED = "⚠️ Something went wrong finishing this turn."
_DELIVERY_UNCERTAIN = "⚠️ Posting the answer timed out. If it isn't above, ask again."
_ANSWER_CUT_SHORT = "⚠️ Part of this answer may be missing. Ask again if it stops short."
# Everything an SDK send can raise: httpx.HTTPError for the Bot Framework
# call, OSError for timeouts and MSAL's transport, ValueError when the SDK
# cannot get a bot token.
TEAMS_SEND_ERRORS = (httpx.HTTPError, OSError, ValueError)
# A send that timed out may still have landed.
_TIMEOUTS = (TimeoutError, httpx.TimeoutException)
SEND_TIMEOUT_S = 30.0


def bound_request_id() -> str:
    """The `rid` bound in this turn's log context, or a fresh one."""
    rid = structlog.contextvars.get_contextvars().get("rid")
    return rid if isinstance(rid, str) and rid else uuid.uuid4().hex


class TeamsSender(Protocol):
    """The slice of `microsoft_teams.apps.App` a turn needs. A set `id` edits."""

    async def send(
        self, conversation_id: str, activity: MessageActivityInput, *, service_url: str | None
    ) -> SentActivity: ...


class TimedSender:
    """Bounds every send: neither the SDK's HTTP client nor MSAL sets a timeout."""

    def __init__(self, inner: TeamsSender, *, timeout: float = SEND_TIMEOUT_S) -> None:
        self._inner = inner
        self._timeout = timeout

    async def send(
        self, conversation_id: str, activity: MessageActivityInput, *, service_url: str | None
    ) -> SentActivity:
        return await asyncio.wait_for(
            self._inner.send(conversation_id, activity, service_url=service_url), self._timeout
        )


class TeamsTurnLifecycle:
    """Implements `daimon.core.turn.lifecycle.TurnLifecycle`. One per turn attempt."""

    def __init__(
        self,
        *,
        sender: TeamsSender,
        conversation_id: str,
        service_url: str | None,
        cancel_key: str,
        agent_name: str,
        model_id: str,
        clock: Callable[[], float] = time.monotonic,
        adopt_message_id: str | None = None,
        request_id: Callable[[], str] = bound_request_id,
    ) -> None:
        self._sender = sender
        self._request_id = request_id
        self._conversation_id = conversation_id
        self._service_url = service_url
        self._cancel_key = cancel_key
        self._model_id = model_id
        self._clock = clock
        self._state = card.CardState(agent_name=agent_name, started_at=clock())
        # Seeded by dead-session recovery so the retry edits the card the
        # person is already watching instead of posting a second one.
        self._message_id = adopt_message_id
        self._last_flush = 0.0
        self._terminal = False
        self.final_message_id: str | None = None
        # The card no longer shows a live turn, so the boot sweep must not
        # touch it: its card intent can retire.
        self.card_closed = False
        # A continuity notice that belongs above the answer it explains.
        self.answer_prefix: str | None = None
        self.answer_prefix_applied = False
        # The first answer message as sent (text, footer), for `prepend_revealed_answer`.
        self._revealed: tuple[str, str | None] | None = None

    @property
    def message_id(self) -> str | None:
        """The status card's Teams id, once posted."""
        return self._message_id

    async def _send(self, activity: MessageActivityInput, *, message_id: str | None) -> str:
        activity.id = message_id
        sent = await self._sender.send(
            self._conversation_id, activity, service_url=self._service_url
        )
        return message_id or sent.id

    async def _edit(self, activity: MessageActivityInput, message_id: str | None) -> str:
        """An edit is idempotent, so one that timed out is sent once more.

        Once an attempt has timed out it may have landed, so any failure of
        the retry is raised as a timeout too.
        """
        try:
            return await self._send(activity, message_id=message_id)
        except _TIMEOUTS:
            if message_id is None:
                raise
        try:
            return await self._send(activity, message_id=message_id)
        except TEAMS_SEND_ERRORS as exc:
            raise TimeoutError from exc

    async def post_initial(self) -> None:
        """Post the card now, before session setup, so Cancel exists from the start."""
        self._message_id = await self._send(self._status(), message_id=self._message_id)
        self._last_flush = self._clock()

    def _status(self) -> MessageActivityInput:
        return card.status_card(self._state, now=self._clock(), cancel_key=self._cancel_key)

    async def on_sse_event(self, event: RawMessageStreamEvent) -> None:
        # Local reducer only: the driver awaits this inline in its read loop.
        kind: str = getattr(event, "type", "")
        if kind == "agent.thinking":
            self._state = card.on_thinking(self._state)
        elif kind == "agent.tool_use":
            self._state = card.on_tool(self._state, str(getattr(event, "name", "tool")))
        elif kind == "agent.message":
            parts: list[object] = getattr(event, "content", [])
            text = "".join(str(getattr(p, "text", "")) for p in parts).strip()
            self._state = card.on_message(self._state, text)

    async def on_render(self, state: TurnState) -> None:
        if self._terminal or self._message_id is None:
            return
        now = self._clock()
        if now - self._last_flush < _DEBOUNCE_S:
            return
        self._last_flush = now
        await self._send(self._status(), message_id=self._message_id)

    def _footer(self, state: TurnState) -> str:
        totals = state.usage_totals
        usage = BetaManagedAgentsSpanModelUsage(
            input_tokens=totals.input_tokens,
            cache_creation_input_tokens=totals.cache_creation_input_tokens,
            cache_read_input_tokens=totals.cache_read_input_tokens,
            output_tokens=totals.output_tokens,
            speed="standard",
        )
        tokens_in = (
            totals.input_tokens
            + totals.cache_creation_input_tokens
            + totals.cache_read_input_tokens
        )
        return card.footer_text(
            self._state,
            now=self._clock(),
            tokens_in=tokens_in,
            tokens_out=totals.output_tokens,
            cost=format_cost(cost_of(usage, MODEL_PRICING.get(self._model_id))),
        )

    async def close_with_notice(self, text: str) -> None:
        """Terminal render for adapter-side bailouts. Never raises on a send error."""
        if self._terminal:
            return
        self._terminal = True
        try:
            await self._close(text)
        except TEAMS_SEND_ERRORS:
            log.warning("teams.turn.notice_failed", exc_info=True)

    async def _close(self, text: str) -> None:
        """Replace the card with a final notice; an edit that timed out is sent once more."""
        self._message_id = await self._edit(card.notice_card(text), self._message_id)
        self.final_message_id = self._message_id
        self.card_closed = True

    def _answer_text(self, state: TurnState) -> str:
        sealed = extract_sealed_responses(state.content, min_chars=_SEALED_RESPONSE_MIN_CHARS)
        parts = [text for _, text in sealed]
        if final := extract_final_response(state.content):
            parts.append(final)
        return "\n\n".join(parts)

    async def on_terminal_success(self, state: TurnState) -> None:
        """Replace the card with the answer; overflow follows as new messages.

        A send failure is logged and captured, never raised: the turn already
        ran and billed. `final_message_id` stays None so the watermark cannot
        pass an answer nobody saw.
        """
        self._terminal = True
        footer = self._footer(state)
        answer = self._answer_text(state)
        degraded = render_degraded_notice(state.mcp_failures)
        replaced = False
        try:
            if not answer:
                tool_only = any(isinstance(b, ToolUseBlock) for b in state.content)
                text = f"✅ {footer}" if tool_only else card.CANCELLED_NOTICE
                if tool_only and degraded is not None:
                    text = f"{degraded}\n\n{text}"
                await self._close(text)
                return
            if self.answer_prefix is not None:
                answer = f"{self.answer_prefix}\n\n{answer}"
                self.answer_prefix_applied = True
            if degraded is not None:
                answer = f"{answer}\n\n{degraded}"
            chunks = split_fenced(answer, card.TEAMS_LIMIT)
            last = len(chunks) - 1
            current = self._message_id
            for index, chunk in enumerate(chunks):
                message = card.answer_message(chunk, footer=footer if index == last else None)
                if index == 0:
                    current = self._message_id = await self._edit(message, current)
                    replaced = self.card_closed = True
                    self._revealed = (chunk, footer if last == 0 else None)
                else:
                    current = await self._send(message, message_id=None)
            self.final_message_id = current
        except TEAMS_SEND_ERRORS as exc:
            log.error("teams.turn.answer_delivery_failed", exc_info=True)
            capture_exception_with_scope(exc)
            if replaced:
                # A later part failed: the answer on screen may stop short.
                with contextlib.suppress(*TEAMS_SEND_ERRORS):
                    await self._send(card.notice_card(_ANSWER_CUT_SHORT), message_id=None)
                return
            if self._message_id is None:
                return
            if answer and isinstance(exc, _TIMEOUTS):
                # The edit may have landed: neither this nor the boot sweep may overwrite
                # it, so a new message covers the case where it did not.
                self.card_closed = True
                with contextlib.suppress(*TEAMS_SEND_ERRORS):
                    await self._send(card.notice_card(_DELIVERY_UNCERTAIN), message_id=None)
                return
            # Collapse the card so it does not show a live turn forever.
            with contextlib.suppress(*TEAMS_SEND_ERRORS):
                failed = card.notice_card(_DELIVERY_FAILED if answer else _FINISH_FAILED)
                await self._send(failed, message_id=self._message_id)
                self.card_closed = True

    async def prepend_revealed_answer(self, notice: str) -> bool:
        """Edit `notice` in above the answer on screen; False if it cannot go there.

        An unexpected workspace loss is known only after the answer replaced
        the card, and a message sent then lands below the answer it explains.
        False when no answer was shown, the notice would overflow the first
        message, or the edit fails; the caller then sends it on its own.
        """
        if self._revealed is None or self._message_id is None:
            return False
        chunk, footer = self._revealed
        updated = f"{notice}\n\n{chunk}"
        if len(updated) > card.TEAMS_LIMIT:
            return False
        try:
            await self._edit(card.answer_message(updated, footer=footer), self._message_id)
        except _TIMEOUTS:
            log.warning("teams.turn.answer_prefix_timed_out", exc_info=True)  # may have landed
        except TEAMS_SEND_ERRORS:
            log.warning("teams.turn.answer_prefix_failed", exc_info=True)
            return False
        self._revealed = (updated, footer)
        return True

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        footer = self._footer(state)
        label = state.error.message if state.error is not None else str(err)
        text = f"❌ {label or 'error'} · {footer}"
        reason = request_id = None
        # The notice is words on the ❌ card, never a reason not to close it:
        # if building it fails, the card falls back to the raw error.
        try:
            reason = state.termination or termination_reason(err)
            request_id = self._request_id()
            notice = render_termination_notice(reason, state=state, request_id=request_id)
            if notice is not None:
                text = card.termination_text(notice, footer=footer)
        except Exception:
            log.warning("turn.terminal_notice_failed", exc_info=True)
        log.warning(
            "turn.terminal_failure",
            error=str(err),
            reason=str(reason) if reason is not None else None,
            request_id=request_id,
        )
        await self.close_with_notice(text)

    async def on_reconnect(self, reason: ReconnectReason) -> None:
        return None

    async def on_rate_limited(self, until: datetime | None) -> None:
        return None

    async def on_interrupt_sent(self, source: InterruptSource) -> None:
        return None
