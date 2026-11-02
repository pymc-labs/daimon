"""A turn stream replays what a reader missed and ends each segment at `done`."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from daimon.adapters.web import stream as stream_module
from daimon.adapters.web.stream import (
    KEEP_CLOSED_S,
    OTHER_TURN,
    Event,
    StreamRegistry,
    TurnStream,
)


async def _read(stream: TurnStream, after: int) -> list[tuple[int, Event]]:
    return [item async for item in stream.segment(after)]


async def test_a_reader_resumes_after_the_last_event_it_saw() -> None:
    stream = TurnStream(uuid.uuid4())
    for text in ("a", "b", "c"):
        await stream.publish({"type": "delta", "text": text})
    await stream.publish({"type": "done", "message": {}})

    resumed = await _read(stream, 2)

    assert [i for i, _ in resumed] == [3, 4], "Last-Event-ID 2 gets 3 and the done"


async def test_a_segment_ends_at_done_and_the_next_starts_after_it() -> None:
    stream = TurnStream(uuid.uuid4())
    await stream.publish({"type": "card", "card": {}})
    await stream.publish({"type": "done", "message": {}})
    reader = asyncio.create_task(_read(stream, stream.last_id))
    await stream.publish({"type": "delta", "text": "more"})
    await stream.publish({"type": "done", "message": {}})

    assert [e["type"] for _, e in await _read(stream, 0)] == ["card", "done"]
    assert [e["type"] for _, e in await reader] == ["delta", "done"], "a live reader waits"


async def test_closing_ends_a_waiting_reader() -> None:
    stream = TurnStream(uuid.uuid4())
    reader = asyncio.create_task(_read(stream, 0))
    await stream.close()
    assert await reader == []


async def test_a_reader_too_far_behind_gets_a_snapshot_or_the_done() -> None:
    stream = TurnStream(uuid.uuid4())
    stream.snapshot = lambda: [{"type": "text", "text": "all so far"}]
    for _ in range(5000):
        await stream.publish({"type": "delta", "text": "x"})
    reader = asyncio.create_task(_read(stream, 1))
    await asyncio.sleep(0)
    await stream.publish({"type": "done", "message": {}})

    assert [e["type"] for _, e in await reader] == ["text", "done"]
    assert [e["type"] for _, e in await _read(stream, 1)] == ["done"], "done holds it all"


def test_one_running_turn_per_thread_and_finished_ones_expire() -> None:
    now = [0.0]
    registry = StreamRegistry(clock=lambda: now[0])
    thread = uuid.uuid4()
    stream = registry.open(thread, uuid.uuid4())
    try:
        registry.open(thread, uuid.uuid4())
        raise AssertionError("a second turn must not start")
    except RuntimeError:
        pass
    stream.closed_at = 0.0
    assert registry.running(thread) is None and registry.get(thread) is stream
    now[0] = KEEP_CLOSED_S + 1
    assert registry.get(thread) is None, "dropped a while after it finished"


async def test_event_ids_name_their_turn_and_another_turns_id_is_not_a_position() -> None:
    first, second = TurnStream(uuid.uuid4()), TurnStream(uuid.uuid4())
    for _ in range(3):
        await first.publish({"type": "delta", "text": "a"})

    assert first.epoch != second.epoch, "each turn has an epoch of its own"
    assert first.position(first.event_id(2)) == 2, "its own id reads back as a position"
    assert first.position(None) == 0, "no id starts from the beginning"
    assert first.position(second.event_id(2)) == OTHER_TURN, "another turn's id is no position"
    assert first.position("3") == OTHER_TURN, "an id without an epoch is no position"


async def test_a_reader_with_another_turns_id_gets_the_reply_so_far() -> None:
    stream = TurnStream(uuid.uuid4())
    stream.snapshot = lambda: [{"type": "text", "text": "so far"}]
    await stream.publish({"type": "delta", "text": "x"})
    reader = asyncio.create_task(_read(stream, OTHER_TURN))
    await asyncio.sleep(0)
    await stream.publish({"type": "done", "message": {}})

    assert [e["type"] for _, e in await reader] == ["text", "done"], "a snapshot, then live"


async def test_a_closed_stream_is_dropped_though_nobody_asks_for_it(monkeypatch: Any) -> None:
    monkeypatch.setattr(stream_module, "KEEP_CLOSED_S", 0.01)
    registry = StreamRegistry()
    thread = uuid.uuid4()
    stream = registry.open(thread, uuid.uuid4())
    stream.snapshot = lambda: [{"type": "text", "text": "big"}]
    await stream.close()
    registry.close_later(thread, stream)
    await asyncio.sleep(0.05)

    assert registry._streams.get(thread) is None, "dropped without a later get"  # pyright: ignore[reportPrivateUsage]
    assert stream.snapshot() == [], "the turn's state is released when the stream closes"


async def test_a_newer_turn_is_not_dropped_by_an_older_ones_timer(monkeypatch: Any) -> None:
    monkeypatch.setattr(stream_module, "KEEP_CLOSED_S", 0.01)
    registry = StreamRegistry()
    thread = uuid.uuid4()
    old = registry.open(thread, uuid.uuid4())
    await old.close()
    old.closed_at = 0.0
    registry.close_later(thread, old)
    newer = registry.open(thread, uuid.uuid4())
    await asyncio.sleep(0.05)

    assert registry.get(thread) is newer, "the timer drops only the stream it was set for"
