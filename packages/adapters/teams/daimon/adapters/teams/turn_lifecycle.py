"""`TurnLifecycle` over one Teams streamed message.

The SDK's ``HttpStream`` sends the first informative update as a new Teams
message and every later update — plus the final card — as an in-place edit
of that same message, so ``on_render`` maps to ``stream.update`` and the
terminal hooks map to ``stream.emit`` + ``stream.close``. The real Teams
message id is captured off the first chunk's ``SentActivity``; it is what the
dispatcher writes as the turn's orphan marker, and what the boot sweep later
addresses when it edits a frozen progress message to interrupted.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import datetime

import structlog
from anthropic.types import RawMessageStreamEvent
from daimon.adapters.teams.lifecycle import (
    FAILURE_MESSAGE,
    NO_ANSWER_MESSAGE,
    WORKING_MESSAGE,
    bounded_text,
    terminal_card,
)
from daimon.core.turn.lifecycle import InterruptSource, ReconnectReason
from daimon.core.turn.state import ToolUseBlock, TurnState, extract_final_response
from microsoft_teams.api import (  # pyright: ignore[reportMissingTypeStubs]
    MessageActivityInput,
    SentActivity,
)
from microsoft_teams.apps.plugins.streamer import (  # pyright: ignore[reportMissingTypeStubs]
    StreamerProtocol,
)

log = structlog.get_logger(__name__)

# The SDK's conversation client substitutes this id when the service's
# response body carries none (every streaming response after the first).
# It is never a real Teams message id and must not be persisted as one.
SDK_PLACEHOLDER_MESSAGE_ID = "DO_NOT_USE_PLACEHOLDER_ID"

# How long post_initial waits for the first chunk's SentActivity before
# letting the turn proceed unmarked. Long enough to absorb the send retry
# path; a stream that still has no id after this is almost certainly broken.
FIRST_CHUNK_TIMEOUT_S = 15.0


def _usable_message_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if stripped and stripped != SDK_PLACEHOLDER_MESSAGE_ID else None


def sent_message_id(sent: SentActivity | None) -> str | None:
    """The real Teams id of a sent activity, or None when it cannot be named.

    ``SentActivity.id`` is the placeholder whenever the service answered an
    update with an empty body; the outgoing activity params still carry the
    real id in that case, so the request-side id is the fallback.
    """
    if sent is None:
        return None
    return _usable_message_id(sent.id) or _usable_message_id(sent.activity_params.id)


def _progress_text(state: TurnState) -> str:
    tool_calls = sum(1 for block in state.content if isinstance(block, ToolUseBlock))
    if tool_calls == 0:
        return WORKING_MESSAGE
    noun = "call" if tool_calls == 1 else "calls"
    return f"{WORKING_MESSAGE} ({tool_calls} tool {noun} so far)"


class TeamsTurnLifecycle:
    """Bounded progress then the terminal answer, on one Teams message.

    Implements ``daimon.core.turn.lifecycle.TurnLifecycle``. Not thread-safe:
    the driver is the only caller, and each hook is awaited in turn order.
    """

    def __init__(
        self,
        *,
        stream: StreamerProtocol,
        message_id: str | None = None,
        fallback_send: Callable[[MessageActivityInput], Awaitable[SentActivity]] | None = None,
    ) -> None:
        self._stream = stream
        self._fallback_send = fallback_send
        self._message_id = message_id
        self._first_chunk = asyncio.Event()
        if message_id is not None:
            # An adopted stream (recovery lifecycle) already carries its id.
            self._first_chunk.set()
        self._closed = False
        self._final_message_id: str | None = None
        stream.on_chunk(self._capture_chunk)

    @property
    def message_id(self) -> str | None:
        """The Teams id of the message this lifecycle renders into."""
        return self._message_id

    @property
    def final_message_id(self) -> str | None:
        """The Teams id carrying the terminal content; None until close lands."""
        return self._final_message_id

    async def _capture_chunk(self, sent: SentActivity) -> None:
        if self._message_id is None:
            self._message_id = sent_message_id(sent)
        self._first_chunk.set()

    async def post_initial(self) -> None:
        """Send the first progress update, then wait for the message id.

        Runs before ``bind_session`` — session creation can take minutes and
        the user must see something first. The id is what the orphan marker
        names, so a turn whose first send never lands proceeds UNMARKED
        rather than marked against a message nobody can address: on a crash
        such a turn freezes mid-progress but is not swept (acceptable — its
        marker was never written).
        """
        self._stream.update(WORKING_MESSAGE)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._first_chunk.wait(), timeout=FIRST_CHUNK_TIMEOUT_S)

    async def on_render(self, state: TurnState) -> None:
        if self._closed or self._stream.canceled:
            return
        self._stream.update(bounded_text(_progress_text(state)))

    async def on_terminal_success(self, state: TurnState) -> None:
        answer = extract_final_response(state.content).strip() or NO_ANSWER_MESSAGE
        await self._close_with_card(answer)

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        reason = state.error.message if state.error is not None else str(err)
        if not reason:
            reason = str(err) or "unknown error"
        await self._close_with_card(f"{FAILURE_MESSAGE}\n\n{reason}")

    async def close_with_text(self, text: str) -> None:
        """Terminal render for adapter-side bailouts (bind failures)."""
        await self._close_with_card(text)

    async def _close_with_card(self, text: str) -> None:
        if self._closed or self._stream.canceled:
            return
        self._closed = True
        sent: SentActivity | None = None
        try:
            # A stream that never got a first chunk has no id for close() to
            # wait on — it would stall for the SDK's internal timeout and
            # return None. Update once so the final card has a message to
            # land on.
            if not self._first_chunk.is_set():
                self._stream.update(WORKING_MESSAGE)
            self._stream.clear_text()
            self._stream.emit(terminal_card(text))
            sent = await self._stream.close()
        except Exception:
            # Delivery failures are absorbed at the lifecycle boundary, same
            # contract as Slack's terminal hooks: the turn's outcome is not
            # masked by a render error.
            log.warning("teams.turn.terminal_render_failed", exc_info=True)
        # close() also reports failure by returning None (stream wait timed
        # out, nothing to send) or a receipt with no usable id. Only a real
        # receipt means the card landed; otherwise post the same content as
        # a new message (Slack's `_repair_terminal_flush`). A Stop is the
        # user's choice, not a failure, so it gets no fallback.
        final_id = sent_message_id(sent)
        if final_id is None and not self._stream.canceled:
            final_id = await self._send_fallback(text)
        self._final_message_id = final_id

    async def _send_fallback(self, text: str) -> str | None:
        if self._fallback_send is None:
            return None
        try:
            sent = await self._fallback_send(terminal_card(text))
        except Exception:
            log.warning("teams.turn.terminal_fallback_failed", exc_info=True)
            return None
        return sent_message_id(sent)

    async def on_sse_event(self, event: RawMessageStreamEvent) -> None:
        return None

    async def on_reconnect(self, reason: ReconnectReason) -> None:
        return None

    async def on_rate_limited(self, until: datetime | None) -> None:
        return None

    async def on_interrupt_sent(self, source: InterruptSource) -> None:
        return None
