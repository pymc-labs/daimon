"""Product constructor proofs: invented environments and owned offline HTTP only."""

from __future__ import annotations

# Deliberate private constructor/client identity probes, not product dependencies.
# pyright: reportPrivateUsage=false
import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from typing import NoReturn, cast

import httpx
import pytest
from google import auth, genai
from mux.drivers.gemini import deployment
from mux.drivers.gemini.transport import Object
from mux.errors import ProviderError
from mux.testing.drivers.gemini.owned_clients import (
    FORBIDDEN_NAMES,
    forbidden,
    owned_clients,
)


class PresentValueTrap(Mapping[str, str]):
    def __init__(self, name: str | None = None, value: str = "private-test-sentinel") -> None:
        self.names = (name,) if name is not None else ()
        self.value = value
        self.reads: list[str] = []

    def __iter__(self) -> Iterator[str]:
        return iter(self.names)

    def __len__(self) -> int:
        return len(self.names)

    def __getitem__(self, name: str) -> str:
        if name in self.names:
            self.reads.append(name)
            raise AssertionError("constructor read a present environment value")
        raise KeyError(name)


@pytest.mark.parametrize("name", FORBIDDEN_NAMES)
@pytest.mark.parametrize("value", ("", "private-test-sentinel"))
@pytest.mark.parametrize("operation", ("create", "get", "cancel", "download", "stream"))
async def test_every_request_refuses_present_values_before_sdk_tls_or_http_allocation(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, operation: str
) -> None:
    transport = deployment.configured_transport(api_key="offline-selected")
    environment = PresentValueTrap(name, value)
    # Restore the Mapping before pytest's own environment bookkeeping/teardown.
    with monkeypatch.context() as guard:
        guard.setattr(os, "environ", environment)
        guard.setattr(deployment.httpx, "create_ssl_context", forbidden)
        guard.setattr(deployment.httpx, "Client", forbidden)
        guard.setattr(deployment.httpx, "AsyncClient", forbidden)
        guard.setattr(genai, "Client", forbidden)
        guard.setattr(auth, "default", forbidden)
        with pytest.raises(ValueError, match="refuses ambient SDK/TLS/proxy") as error:
            if operation == "create":
                await transport.create({"agent": "antigravity-preview-05-2026", "tools": []})
            elif operation == "get":
                await transport.get("native")
            elif operation == "cancel":
                await transport.cancel("native")
            elif operation == "download":
                await transport.download_snapshot("workspace")
            else:
                await transport.open_stream("native")
        if environment.reads:
            raise AssertionError("preflight read a present value")
        if name in str(error.value) or value and value in str(error.value):
            raise AssertionError("preflight disclosed a setting name/value")


async def test_complete_product_constructor_all_four_sdk_boundaries_owned_and_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "/interactions/" in request.url.path:
            return httpx.Response(200, json={"id": "native", "status": "completed", "outputs": []})
        return httpx.Response(200, json={"models": []})

    owned = owned_clients(monkeypatch, reply)
    # Other providers' settings are not Gemini overrides and are never read.
    environment = PresentValueTrap("ANTHROPIC_API_KEY")
    with monkeypatch.context() as guard:
        guard.setattr(os, "environ", environment)
        async with deployment._ConfiguredTransport("offline-selected")._client() as client:
            assert client._api_client.vertexai is False
            assert client._api_client._credentials is None
            assert client._api_client.project is client._api_client.location is None
            assert client._api_client._httpx_client is owned.synchronous[0]
            assert client._api_client._async_httpx_client is owned.asynchronous[0]
            assert client._nextgen_client.max_retries == client.aio._nextgen_client.max_retries == 0
            list(client.models.list())
            await client.aio.models.list()
            client.interactions.get("native")
            await client.aio.interactions.get("native")
        if environment.reads:
            raise AssertionError("constructor read unrelated provider credentials")
    assert len(requests) == 4
    for request in requests:
        assert request.url.host == "generativelanguage.googleapis.com"
        assert request.headers["x-goog-api-key"] == "offline-selected"
        assert "authorization" not in request.headers
        assert "x-goog-user-project" not in request.headers
    owned.require_closed()


@pytest.mark.parametrize("operation", ("create", "get", "cancel", "download"))
async def test_product_request_owns_sync_and_async_lifetime(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    requests: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if operation == "download":
            return httpx.Response(200, content=b"snapshot")
        return httpx.Response(200, json={"id": "native", "status": "completed", "steps": []})

    owned = owned_clients(monkeypatch, reply)
    transport = deployment.configured_transport(api_key="offline-selected")
    with monkeypatch.context() as guard:
        guard.setattr(os, "environ", PresentValueTrap())
        if operation == "create":
            result = await transport.create({"agent": "antigravity-preview-05-2026", "tools": []})
        elif operation == "get":
            result = await transport.get("native")
        elif operation == "cancel":
            result = await transport.cancel("native")
        else:
            result = await transport.download_snapshot("workspace")
    assert result and len(requests) == 1
    assert requests[0].headers["x-goog-api-key"] == "offline-selected"
    owned.require_closed()


@pytest.mark.parametrize("failure", ("sdk_init", "lost_ack", "503"))
async def test_constructor_or_post_failure_closes_both_clients_without_native_retry(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    requests: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if failure == "lost_ack":
            raise httpx.ReadError("synthetic lost acknowledgement")
        return httpx.Response(503, json={"error": {"code": 503, "message": "offline overloaded"}})

    owned = owned_clients(monkeypatch, reply)
    transport = deployment.configured_transport(api_key="offline-selected")
    with monkeypatch.context() as guard:
        guard.setattr(os, "environ", PresentValueTrap())
        if failure == "sdk_init":

            def failed_sdk(*args: object, **kwargs: object) -> NoReturn:
                raise RuntimeError("synthetic constructor failure")

            guard.setattr(genai, "Client", failed_sdk)
            with pytest.raises(RuntimeError, match="synthetic constructor"):
                await transport.get("native")
        else:
            with pytest.raises(ProviderError) as error:
                await transport.create({"agent": "antigravity-preview-05-2026", "tools": []})
            assert error.value.category == (
                "overloaded" if failure == "503" else "transient_network"
            )
    assert len(requests) == (0 if failure == "sdk_init" else 1)
    owned.require_closed()


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


@pytest.mark.parametrize("end", ("exhaust", "close", "cancel"))
async def test_stream_owns_both_http_clients_until_exact_close(
    monkeypatch: pytest.MonkeyPatch, end: str
) -> None:
    body = Body(end == "cancel")
    owned = owned_clients(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=body
        ),
    )
    with monkeypatch.context() as guard:
        guard.setattr(os, "environ", PresentValueTrap())
        source = await deployment.configured_transport(api_key="offline-selected").open_stream(
            "native"
        )
        assert not owned.synchronous[0].is_closed and not owned.asynchronous[0].is_closed
        if end == "exhaust":
            assert [item async for item in source]
        elif end == "close":
            closer = getattr(source, "aclose", None)
            if not callable(closer):
                raise AssertionError("owned stream has no close boundary")
            close = cast(Callable[[], Awaitable[None]], closer)
            await close()
            await close()
        else:

            async def receive() -> Object:
                return await anext(source)

            task = asyncio.create_task(receive())
            await body.started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert body.closed
    owned.require_closed()
