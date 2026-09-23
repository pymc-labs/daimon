"""A terminal answer the stream could not deliver still reaches the user.

``HttpStream.close()`` fails two ways: it raises (a 5xx or a terminal
stream error after retries), or it returns ``None`` (the wait for the
stream id timed out, or there was nothing to close). In both cases the
streamed message still shows the last progress text, so the answer must
go out as a new message. The watermark must name that new message, never
the progress message the answer never reached.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.stores.thread_sessions import get_thread_session_by_id, list_orphaned_turns
from daimon.core.turn.state import TextBlock, TurnState
from microsoft_teams.api import SentActivity  # pyright: ignore[reportMissingTypeStubs]
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    ENTRA_TENANT_ID,
    FALLBACK_MESSAGE_ID,
    FakeStream,
    make_dispatch_target,
    patched_turn_pipeline,
)

ANSWER = "Hello from Teams!"


class RaisingCloseStream(FakeStream):
    async def close(self) -> SentActivity:
        self._closed = True
        raise RuntimeError("Bot Framework 500 after retries")


class NoReceiptCloseStream(FakeStream):
    async def close(self) -> SentActivity | None:  # pyright: ignore[reportIncompatibleMethodOverride]
        # What HttpStream.close() returns when its wait for the id/queue times out.
        return None


class StoppedStream(FakeStream):
    """The user pressed Stop: the SDK flipped ``canceled`` and close() is a no-op."""

    async def close(self) -> SentActivity | None:  # pyright: ignore[reportIncompatibleMethodOverride]
        return None


async def _run_answering_turn(
    db_session_factory: async_sessionmaker[AsyncSession], stream: FakeStream
) -> tuple[Any, uuid.UUID]:
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    dispatcher, ctx, activity = make_dispatch_target(db_session_factory)
    ctx.stream = stream
    marked_ids: list[uuid.UUID] = []

    async def _fake_run_turn(*, lifecycle: Any, **kwargs: Any) -> TurnState:
        state = TurnState(content=[TextBlock(kind="text", text=ANSWER)])
        if isinstance(ctx.stream, StoppedStream):
            ctx.stream.canceled = True
        await lifecycle.on_terminal_success(state)
        return state

    with (
        patched_turn_pipeline(marked_ids),
        patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock) as mock_run_turn,
    ):
        mock_run_turn.side_effect = _fake_run_turn
        await dispatcher.dispatch(ctx, activity)
        await dispatcher.drain(timeout=30)
    assert marked_ids, "the turn must have passed mark_turn_active"
    return ctx, marked_ids[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_cls", [RaisingCloseStream, NoReceiptCloseStream])
async def test_undelivered_answer_is_sent_as_a_new_message(
    db_session_factory: async_sessionmaker[AsyncSession], stream_cls: type[FakeStream]
) -> None:
    ctx, mapping_id = await _run_answering_turn(db_session_factory, stream_cls())

    fallbacks = [m for m in ctx.sent if not isinstance(m, str)]
    assert len(fallbacks) == 1, "the answer must go out once as a new message"
    assert ANSWER in fallbacks[0].model_dump_json()

    async with db_session_factory() as session:
        row = await get_thread_session_by_id(session, id=mapping_id)
        assert row is not None
        assert row.watermark_message_id == FALLBACK_MESSAGE_ID, (
            "the watermark must name the message that carries the answer, "
            "not the progress message it never reached"
        )
        assert await list_orphaned_turns(session, platform="teams") == []


@pytest.mark.asyncio
async def test_delivered_answer_sends_no_fallback(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx, mapping_id = await _run_answering_turn(db_session_factory, FakeStream())

    assert ctx.sent == []
    async with db_session_factory() as session:
        row = await get_thread_session_by_id(session, id=mapping_id)
        assert row is not None
        assert row.watermark_message_id == "m-1"


@pytest.mark.asyncio
async def test_stopped_stream_sends_no_fallback(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A Stop is the user's choice; re-posting the answer would undo it."""
    ctx, _ = await _run_answering_turn(db_session_factory, StoppedStream())
    assert ctx.sent == []
