"""`TurnLifecycle` for the web chat: render a turn into its stream and its saved reply.

A card splits a reply into segments: the first ends at the card (saved, then `done`),
and the answer starts the next under a new message id. Each segment shows only what is
new since the last one.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal

import structlog
from daimon.adapters.web.chat_json import message_json
from daimon.adapters.web.stream import TurnStream
from daimon.adapters.web.stream_events import ReplyView, events_between, snapshot_events, view_of
from daimon.core.stores.domain import WebCard, WebMessageRow, WebMessageStatus, WebOutputFile
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.state import ToolUseBlock, TurnState, extract_final_response

log = structlog.get_logger(__name__)

#: A running reply is saved this often, so a reload shows it close to live.
CHECKPOINT_S = 10.0

# Saves a reply segment: (message id, view, status, card, duration ms, files, cost) -> the row.
SaveReply = Callable[
    [
        uuid.UUID,
        ReplyView,
        WebMessageStatus,
        WebCard | None,
        int | None,
        tuple[WebOutputFile, ...],
        Decimal | None,
    ],
    Awaitable[WebMessageRow],
]


class WebTurnLifecycle(TurnLifecycle):
    def __init__(
        self, stream: TurnStream, save: SaveReply, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.stream = stream
        self.reply_id = stream.reply_id = uuid.uuid4()
        self._save = save
        self._clock = clock
        self._started = clock()
        self._state = TurnState()
        self._view = ReplyView()
        self._hidden: frozenset[str] = frozenset()
        self._shown_text = ""
        self._seen: dict[str, float] = {}
        self._took_ms: dict[str, int] = {}
        self._saved_at = clock()
        # A card raised by a tool while the turn runs; the reply keeps it when it ends.
        self.raised: WebCard | None = None
        # The files the turn made, set before it finishes.
        self.files: tuple[WebOutputFile, ...] = ()
        # What the turn cost, with the markup, set before it finishes; None while unknown.
        self.cost_usd: Decimal | None = None
        self.finished = False
        stream.snapshot = lambda: snapshot_events(self._view)

    @property
    def elapsed_ms(self) -> int:
        return int((self._clock() - self._started) * 1000)

    def _current(self) -> ReplyView:
        now = self._clock()
        for block in self._state.content:
            if isinstance(block, ToolUseBlock):
                seen = self._seen.setdefault(block.id, now)
                if block.status != "pending" and block.id not in self._took_ms:
                    self._took_ms[block.id] = int((now - seen) * 1000)
        return view_of(
            self._state.content,
            durations_ms=self._took_ms,
            hidden=self._hidden,
            shown_text=self._shown_text,
        )

    async def _publish_changes(self) -> None:
        view = self._current()
        for event in events_between(self._view, view):
            await self.stream.publish(event)
        self._view = view

    async def on_render(self, state: TurnState) -> None:
        self._state = state
        await self._publish_changes()
        if self._clock() - self._saved_at >= CHECKPOINT_S:
            self._saved_at = self._clock()
            await self._save(self.reply_id, self._view, "running", self.raised, None, (), None)

    async def on_terminal_success(self, state: TurnState) -> None:
        self._state = state

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        self._state = state

    async def raise_card(self, card: WebCard) -> None:
        """Show `card` now; the reply carries it unless it ends on a card of its own."""
        self.raised = card
        await self.stream.publish({"type": "card", "card": card.model_dump(mode="json")})

    async def end_segment(self, card: WebCard) -> WebMessageRow:
        """Show `card` and end the segment; the turn waits for its answer."""
        await self._publish_changes()
        await self.stream.publish({"type": "card", "card": card.model_dump(mode="json")})
        row = await self._save(
            self.reply_id, self._view, "done", card, self.elapsed_ms, (), self.cost_usd
        )
        self.stream.waiting = True
        await self.stream.publish({"type": "done", "message": message_json(row)})
        return row

    def next_segment(self) -> None:
        """The card was answered: what follows goes into a new reply."""
        finished = (b.id for b in self._state.content if isinstance(b, ToolUseBlock))
        self._hidden = frozenset(i for i in finished if i in self._took_ms)
        self._shown_text = extract_final_response(self._state.content)
        self._view = ReplyView()  # the new reply starts empty in the browser
        self.reply_id = self.stream.reply_id = uuid.uuid4()
        self.stream.waiting = False

    async def finish(
        self, status: WebMessageStatus, *, card: WebCard | None = None, text: str | None = None
    ) -> WebMessageRow:
        """Save the reply as it ended and tell the browser; `text` replaces an empty answer."""
        await self._publish_changes()
        view = self._view
        if status != "done":
            # A step cut off by a stop never finished.
            view = ReplyView(
                text=view.text,
                steps=tuple(
                    s.model_copy(update={"status": "error"}) if s.status == "running" else s
                    for s in view.steps
                ),
            )
        if text is not None and not view.text:
            view = ReplyView(text=text, steps=view.steps)
        if card is None:
            card = self.raised
        else:
            await self.stream.publish({"type": "card", "card": card.model_dump(mode="json")})
        self.finished = True
        row = await self._save(
            self.reply_id, view, status, card, self.elapsed_ms, self.files, self.cost_usd
        )
        await self.stream.publish({"type": "done", "message": message_json(row)})
        return row
