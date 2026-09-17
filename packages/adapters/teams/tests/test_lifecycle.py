"""Presentation and stream-identity tests — no DB, no network.

``FakeStream`` records what a real ``HttpStream`` would have put on the
wire; ``sent_message_id`` and the lifecycle's capture path are what keep
progress and terminal content on one Teams message id.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from daimon.adapters.teams.lifecycle import (
    MAX_TEAMS_CARD_TEXT_BYTES,
    TRUNCATION_MARKER,
    WORKING_MESSAGE,
    bounded_text,
    terminal_card,
)
from daimon.adapters.teams.turn_lifecycle import (
    SDK_PLACEHOLDER_MESSAGE_ID,
    TeamsTurnLifecycle,
    sent_message_id,
)
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState
from microsoft_teams.api import (  # pyright: ignore[reportMissingTypeStubs]
    MessageActivityInput,
    SentActivity,
)


class FakeStream:
    """A ``StreamerProtocol``-shaped fake recording the wire-level sequence.

    The first ``update``/``emit`` resolves the send asynchronously — like
    ``HttpStream._flush`` — so ``post_initial``'s first-chunk wait completes
    without the 15s timeout path.
    """

    def __init__(self, *, chunk_id: str | None = "m-1") -> None:
        self.chunk_id = chunk_id
        self.updates: list[str] = []
        self.emitted: list[Any] = []
        self.cleared = 0
        self.close_calls = 0
        self._chunk_handlers: list[Any] = []
        self._chunk_sent = False
        self._canceled = False

    @property
    def canceled(self) -> bool:
        return self._canceled

    def on_chunk(self, handler: Any) -> None:
        self._chunk_handlers.append(handler)

    def _schedule_chunk(self) -> None:
        if self._chunk_sent:
            return
        self._chunk_sent = True
        asyncio.get_running_loop().create_task(self._fire_chunk())

    async def _fire_chunk(self) -> None:
        await asyncio.sleep(0)
        sent = SentActivity(
            id=self.chunk_id or SDK_PLACEHOLDER_MESSAGE_ID,
            activity_params=MessageActivityInput(id=self.chunk_id),
        )
        for handler in self._chunk_handlers:
            await handler(sent)

    def emit(self, activity: Any) -> None:
        self.emitted.append(activity)
        self._schedule_chunk()

    def update(self, text: str) -> None:
        self.updates.append(text)
        self._schedule_chunk()

    def clear_text(self) -> None:
        self.cleared += 1

    async def close(self) -> SentActivity | None:
        self.close_calls += 1
        sent = SentActivity(
            id=self.chunk_id or SDK_PLACEHOLDER_MESSAGE_ID,
            activity_params=MessageActivityInput(id=self.chunk_id),
        )
        return sent


def _sent(id_: str | None, params_id: str | None = None) -> SentActivity:
    return SentActivity(id=id_, activity_params=MessageActivityInput(id=params_id))


class TestBoundedText:
    def test_short_text_passes_through(self) -> None:
        assert bounded_text("hello") == "hello"

    def test_oversize_text_is_clipped_within_budget(self) -> None:
        text = "x" * (MAX_TEAMS_CARD_TEXT_BYTES + 500)
        out = bounded_text(text)
        assert len(out.encode("utf-8")) <= MAX_TEAMS_CARD_TEXT_BYTES
        assert out.endswith(TRUNCATION_MARKER)

    def test_multibyte_boundary_is_never_split(self) -> None:
        # '€' is 3 bytes in UTF-8 — a byte-level cut mid-character would
        # produce mojibake; bounded_text must walk back to a boundary.
        euro = "€"
        prefix_len = MAX_TEAMS_CARD_TEXT_BYTES - len(TRUNCATION_MARKER.encode()) - 1
        text = "x" * prefix_len + euro + "tail" * 1000
        out = bounded_text(text)
        assert len(out.encode("utf-8")) <= MAX_TEAMS_CARD_TEXT_BYTES
        assert out.endswith(TRUNCATION_MARKER)
        assert "�" not in out


class TestTerminalCard:
    def test_card_wraps_text_and_carries_fallback(self) -> None:
        activity = terminal_card("the answer")
        assert activity.attachments, "terminal_card must attach a card"
        assert activity.type == "message"

    def test_card_content_is_bounded(self) -> None:
        activity = terminal_card("y" * (MAX_TEAMS_CARD_TEXT_BYTES * 2))
        dumped = activity.model_dump_json()
        # JSON-escaped form of the marker — the literal \n\n never appears raw.
        assert TRUNCATION_MARKER.strip() in dumped


class TestSentMessageId:
    def test_real_id_wins(self) -> None:
        assert sent_message_id(_sent("m-9")) == "m-9"

    def test_placeholder_falls_back_to_activity_params(self) -> None:
        sent = _sent(SDK_PLACEHOLDER_MESSAGE_ID, params_id="m-real")
        assert sent_message_id(sent) == "m-real"

    def test_placeholder_everywhere_returns_none(self) -> None:
        assert sent_message_id(_sent(SDK_PLACEHOLDER_MESSAGE_ID)) is None
        assert sent_message_id(None) is None
        assert sent_message_id(_sent("  ")) is None


class TestTeamsTurnLifecycle:
    @pytest.mark.asyncio
    async def test_progress_then_terminal_on_one_stream(self) -> None:
        stream = FakeStream()
        lifecycle = TeamsTurnLifecycle(stream=stream)

        await lifecycle.post_initial()
        state = TurnState(
            content=[
                ToolUseBlock(kind="tool_use", id="tu-1", type="agent.tool_use", name="t", input={}),
                TextBlock(kind="text", text="partial"),
            ]
        )
        await lifecycle.on_render(state)
        await lifecycle.on_terminal_success(
            TurnState(content=[TextBlock(kind="text", text="done answer")])
        )

        assert stream.updates[0] == WORKING_MESSAGE
        assert stream.updates[1].startswith(WORKING_MESSAGE)
        assert "1 tool call" in stream.updates[1]
        assert stream.cleared == 1, "terminal render clears accumulated progress"
        assert stream.close_calls == 1
        assert lifecycle.message_id == "m-1"
        assert lifecycle.final_message_id == "m-1"

    @pytest.mark.asyncio
    async def test_empty_final_response_renders_done(self) -> None:
        stream = FakeStream()
        lifecycle = TeamsTurnLifecycle(stream=stream)
        await lifecycle.post_initial()
        await lifecycle.on_terminal_success(TurnState(content=[]))
        assert stream.close_calls == 1

    @pytest.mark.asyncio
    async def test_terminal_failure_renders_failure_card(self) -> None:
        stream = FakeStream()
        lifecycle = TeamsTurnLifecycle(stream=stream)
        await lifecycle.post_initial()
        await lifecycle.on_terminal_failure(TurnState(content=[]), RuntimeError("boom"))
        assert stream.close_calls == 1

    @pytest.mark.asyncio
    async def test_adopted_stream_preserves_message_id(self) -> None:
        """Recovery adoption: a second lifecycle on the same stream starts
        with the first message id — the marker stays valid."""
        stream = FakeStream()
        adopted = TeamsTurnLifecycle(stream=stream, message_id="m-1")
        await adopted.on_terminal_success(
            TurnState(content=[TextBlock(kind="text", text="recovered")])
        )
        assert adopted.message_id == "m-1"
        assert adopted.final_message_id == "m-1"

    @pytest.mark.asyncio
    async def test_close_failure_is_absorbed(self) -> None:
        class BoomStream(FakeStream):
            async def close(self) -> SentActivity | None:
                raise RuntimeError("service 500")

        lifecycle = TeamsTurnLifecycle(stream=BoomStream())
        await lifecycle.post_initial()
        # Must not raise — a render failure cannot mask the turn's outcome.
        await lifecycle.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="x")]))

    @pytest.mark.asyncio
    async def test_post_initial_without_first_chunk_proceeds_unmarked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stream that never reports a chunk leaves message_id None —
        the dispatcher then writes no marker rather than a false one."""
        monkeypatch.setattr("daimon.adapters.teams.turn_lifecycle.FIRST_CHUNK_TIMEOUT_S", 0.05)

        class SilentStream(FakeStream):
            def _schedule_chunk(self) -> None:
                return None

        lifecycle = TeamsTurnLifecycle(stream=SilentStream())
        await lifecycle.post_initial()
        assert lifecycle.message_id is None
