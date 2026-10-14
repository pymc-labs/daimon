"""Main's instant interrupt acknowledgement also survives the opted-in mux path."""

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from daimon.core.turn.io import turn_io
from daimon.testing import build_fake_anthropic
from mux.contracts.ids import Scope
from pydantic import JsonValue


@pytest.mark.parametrize("path", ["legacy", "mux"])
@pytest.mark.parametrize("stop", ["end_turn", "retries_exhausted", "requires_action"])
async def test_interrupt_subscribes_before_post_and_closes_stream(path, stop):
    subscribed = False
    closed = False
    requests = []
    received: asyncio.Queue[bytes] = asyncio.Queue()

    class LiveStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield await received.get()

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal subscribed
        requests.append(request.method)
        if request.method == "GET":
            assert request.url.path == "/v1/sessions/sesn_test/events/stream"
            subscribed = True
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=LiveStream()
            )
        assert request.url.path == "/v1/sessions/sesn_test/events"
        assert json.loads(request.content)["events"] == [{"type": "user.interrupt"}]
        if subscribed:
            event: dict[str, JsonValue] = {
                "id": "sevt_instant",
                "type": "session.status_idle",
                "processed_at": "2026-10-11T00:00:00Z",
                "stop_reason": {"type": stop},
            }
            if stop == "requires_action":
                event["stop_reason"] = {"type": stop, "event_ids": []}
            received.put_nowait(
                f"event: session.status_idle\ndata: {json.dumps(event)}\n\n".encode()
            )
        return httpx.Response(200, json={"data": None})

    scope = Scope(
        tenant_id="tenant",
        account_id="account",
        principal_id="requester",
        authorization_id="interrupt-test",
    )
    async with build_fake_anthropic(handle) as client:
        result = await turn_io(client, "sesn_test", path=path, scope=scope).interrupt(timeout_s=1.0)
    assert result is None or result.stopped
    assert requests == ["GET", "POST"]
    assert closed


@pytest.mark.parametrize("path", ["legacy", "mux"])
@pytest.mark.parametrize("exit_kind", ["post_failure", "timeout", "cancelled"])
async def test_interrupt_closes_subscribed_stream_on_every_failed_exit(path, exit_kind):
    from anthropic import APIConnectionError
    from daimon.core.errors import TurnError

    opened = asyncio.Event()
    closed = False
    requests = []
    waiting = asyncio.Event()

    class WaitingStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            await waiting.wait()
            yield b""

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        if request.method == "GET":
            opened.set()
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=WaitingStream()
            )
        if exit_kind == "post_failure":
            raise httpx.ReadError("offline lost acknowledgement")
        return httpx.Response(200, json={"data": None})

    scope = Scope(
        tenant_id="tenant",
        account_id="account",
        principal_id="requester",
        authorization_id="interrupt-test",
    )
    async with build_fake_anthropic(handle) as client:
        client.max_retries = 0
        io = turn_io(client, "sesn_test", path=path, scope=scope)
        task = asyncio.create_task(io.interrupt(timeout_s=0.5 if exit_kind == "timeout" else 10))
        if exit_kind == "cancelled":
            await asyncio.wait_for(opened.wait(), timeout=5)
            # Wait for the POST before cancellation, so this probes closing an
            # opened subscription while blocked on the acknowledgement stream.
            while len(requests) < 2:
                await asyncio.sleep(0)
            task.cancel()
        with pytest.raises((APIConnectionError, TurnError, asyncio.CancelledError)):
            await task
    assert requests == ["GET", "POST"]
    assert closed
