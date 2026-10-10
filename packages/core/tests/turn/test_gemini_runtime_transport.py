"""Offline private deployment lifecycle, routing and single-attempt proofs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import cast

import httpx
import pytest
from mux.drivers.gemini import deployment
from mux.drivers.gemini.transport import Object
from mux.errors import ProviderError


def clients_for(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.AsyncClient]:
    clients: list[httpx.AsyncClient] = []

    class OfflineClient(httpx.AsyncClient):
        def __init__(self, *, trust_env: bool, timeout: float) -> None:
            assert trust_env is False
            super().__init__(
                transport=httpx.MockTransport(handler), trust_env=False, timeout=timeout
            )
            clients.append(self)

    monkeypatch.setattr(deployment.httpx, "AsyncClient", OfflineClient)
    return clients


@pytest.mark.parametrize("operation", ["create", "get", "cancel", "download"])
async def test_explicit_credentials_origin_and_owned_request_lifetime(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    for name, value in {
        "GOOGLE_API_KEY": "hostile",
        "GEMINI_API_KEY": "hostile",
        "GOOGLE_GENAI_USE_VERTEXAI": "true",
        "GOOGLE_GENAI_USE_ENTERPRISE": "true",
        "GOOGLE_GEMINI_BASE_URL": "https://hostile.invalid",
        "GEMINI_NEXT_GEN_API_BASE_URL": "https://hostile.invalid",
        "HTTPS_PROXY": "http://hostile.invalid",
    }.items():
        monkeypatch.setenv(name, value)
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.host == "generativelanguage.googleapis.com"
        assert request.headers["x-goog-api-key"] == "offline-selected"
        if operation == "download":
            return httpx.Response(200, content=b"\x00\xffsnapshot")
        return httpx.Response(200, json={"id": "native", "status": "completed", "steps": []})

    clients = clients_for(monkeypatch, handle)
    transport = deployment.configured_transport(api_key="offline-selected")
    assert not clients and not calls
    if operation == "create":
        result = await transport.create(
            {"agent": "antigravity-preview-05-2026", "input": [], "tools": []}
        )
    elif operation == "get":
        result = await transport.get("native")
    elif operation == "cancel":
        result = await transport.cancel("native")
    else:
        result = await transport.download_snapshot("workspace")
    assert result and len(calls) == len(clients) == 1 and clients[0].is_closed


async def test_lost_post_ack_sends_once_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[httpx.Request] = []

    def lost(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadError("private-secret-lost-ack")

    clients = clients_for(monkeypatch, lost)
    with pytest.raises(ProviderError) as error:
        await deployment.configured_transport(api_key="offline-selected").create(
            {"agent": "antigravity-preview-05-2026", "input": [], "tools": []}
        )
    assert error.value.category == "transient_network" and "private-secret" not in str(error.value)
    assert [request.method for request in calls] == ["POST"] and clients[0].is_closed


class Body(httpx.AsyncByteStream):
    def __init__(self, block: bool) -> None:
        self.closed = False
        self.block = block
        self.started = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        yield b'data: {"event_type":"step.delta","index":0,"delta":{"type":"text","text":"hi"}}\n\n'

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("end", ["exhaust", "close", "cancel"])
async def test_stream_lifetime_closes_on_every_end(
    monkeypatch: pytest.MonkeyPatch, end: str
) -> None:
    body = Body(end == "cancel")
    clients = clients_for(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=body
        ),
    )
    stream = await deployment.configured_transport(api_key="offline-selected").open_stream("native")
    assert not clients[0].is_closed
    if end == "exhaust":
        assert [item async for item in stream]
    elif end == "close":
        close = cast(Callable[[], Awaitable[None]], getattr(stream, "aclose", None))
        await close()
        await close()
    else:

        async def receive() -> object:
            return await anext(stream)

        task = asyncio.create_task(receive())
        await body.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert body.closed and clients[0].is_closed


async def test_stream_open_failure_disposes_client(monkeypatch: pytest.MonkeyPatch) -> None:
    clients = clients_for(
        monkeypatch,
        lambda request: httpx.Response(401, json={"error": {"message": "offline", "code": 401}}),
    )
    with pytest.raises(ProviderError):
        await deployment.configured_transport(api_key="offline-selected").open_stream("native")
    assert len(clients) == 1 and clients[0].is_closed


async def test_source_close_failure_still_disposes_lifetime_exactly_once() -> None:
    disposed: list[bool] = []

    class Failing(AsyncIterator[Object]):
        def __aiter__(self) -> Failing:
            return self

        async def __anext__(self) -> Object:
            raise StopAsyncIteration

        async def aclose(self) -> None:
            raise RuntimeError("offline close failure")

    async def close() -> None:
        disposed.append(True)

    stream = deployment._OwnedStream(Failing(), close)
    with pytest.raises(RuntimeError, match="offline close failure"):
        await stream.aclose()
    await stream.aclose()
    assert disposed == [True]
