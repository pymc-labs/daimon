"""J3 (and J4): an OpenAI conversation worker whose turn ended before its event stream
connected still finishes, from the saved turn, well inside its deadline.

SMOKE e0a7e7c3 J3 Astra 3a487e87: its input went with the session create, inside a
14.7 s bind (send:0 processed 23:44:33); the stream opened ~5 s later (baseline
23:44:38). OpenAI's stream replays nothing, so the turn that ran in that gap was never
reported, and keep-alive frames kept the read clock from ever timing out: the worker
waited out its 10-minute deadline ("ran out of time") on a turn that was long over.
"""

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID

import httpx
from daimon.core.fleet_orchestration import FleetOrchestrator

from .test_fleet_native_dispatch import _settled
from .test_openai_conversation_worker import conversation_root as conversation_root
from .test_openai_conversation_worker import terms
from .test_openai_host import SESSION_ID
from .test_openai_worker_plan import db_preflight as db_preflight
from .test_openai_worker_plan import owned_database_preflight as owned_database_preflight

#: Well under the 10-minute worker deadline the smoke waited out; above the stream's
#: idle check, so a fix that asks for the session status in time passes.
FINISHES_WITHIN_S = 45


class KeepAlives(httpx.AsyncByteStream):
    """A connected event stream that only ever sends SSE keep-alive comments."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            yield b": keepalive\n\n"
            await asyncio.sleep(0.5)

    async def aclose(self) -> None:
        return None


async def test_a_turn_that_ended_before_the_stream_connected_still_finishes(
    conversation_root, monkeypatch
):
    deps, parent, policy, resolved, wire, ports, _, _ = conversation_root
    handle = wire.handle
    streams: list[KeepAlives] = []

    def ended_before_subscribe(call):
        response = handle(call)
        if call.url.path.endswith("/events") and call.method == "GET":
            # The input rode the session create and its turn is already complete in
            # the saved turns and items; this stream connected after it ended.
            assert wire.turns and wire.turns[-1]["status"] == "completed"
            streams.append(KeepAlives())
            return httpx.Response(
                200, stream=streams[-1], headers={"Content-Type": "text/event-stream"}
            )
        return response

    monkeypatch.setattr(wire, "handle", ended_before_subscribe)
    owner = FleetOrchestrator(ports)
    spawned = await owner.spawn(parent, policy, **terms(parent, resolved))
    task = ports._tasks.get(UUID(spawned["worker_id"]))
    async with asyncio.timeout(FINISHES_WITHIN_S):
        while task is not None and not task.done():
            await asyncio.sleep(0.1)
    worker = await _settled(ports, parent, spawned["worker_id"])
    assert streams  # an event stream connected only after the turn had ended
    assert worker.status == "succeeded" and worker.turns_used == 1, worker.error_code
    assert worker.result.excerpt == "answer turn-1"
    events = await deps.state_store.read_events(SESSION_ID, after=-1)
    assert any(e.type == "session.turn_ended" for e in events)
