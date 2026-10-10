"""Offline ownership and single-attempt proofs for deployment SDK construction."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import cast

import httpx
import pytest
from mux.drivers.openai import deployment
from mux.errors import ProviderError
from openai import AsyncOpenAI


class Source(httpx.AsyncByteStream):
    def __init__(self, *, block: bool = False) -> None:
        self.closed = False
        self.block = block
        self.started = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        yield b'data: {"event_id":"event"}\n\n'

    async def aclose(self) -> None:
        self.closed = True


def clients_for(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[AsyncOpenAI]:
    clients: list[AsyncOpenAI] = []

    def factory(
        *,
        api_key: str,
        project: str,
        organization: str,
        base_url: str,
        max_retries: int,
    ) -> AsyncOpenAI:
        assert api_key == "offline" and project == "selected-project"
        assert organization == "" and base_url == "https://api.openai.com/v1"
        assert max_retries == 0
        client = AsyncOpenAI(
            api_key=api_key,
            project=project,
            organization=organization,
            base_url=base_url,
            max_retries=max_retries,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        clients.append(client)
        return client

    monkeypatch.setattr(deployment, "AsyncOpenAI", factory)
    return clients


@pytest.mark.parametrize("operation", ["request", "multipart"])
async def test_no_inherited_provider_routing_and_owned_request_close(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://other-project.invalid/v1")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "another-project")
    monkeypatch.setenv("OPENAI_ORG_ID", "another-organization")
    monkeypatch.setenv("OPENAI_API_KEY", "unselected-offline-key")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "api.openai.com"
        assert request.headers["OpenAI-Project"] == "selected-project"
        assert request.headers["authorization"] == "Bearer offline"
        assert request.headers["OpenAI-Beta"] == "agents=v1"
        assert request.headers.get("OpenAI-Organization", "") == ""
        return httpx.Response(200, json={"id": "native"})

    clients = clients_for(monkeypatch, handle)
    transport = deployment.configured_transport(api_key="offline", project="selected-project")
    assert clients == [] and requests == []
    if operation == "request":
        result = await transport.request("POST", "/agents/sessions", body={}, key="claimed")
    else:
        result = await transport.multipart(
            "/files",
            files=(("file", "test.txt", b"test", "text/plain"),),
            fields={"purpose": "agents"},
            key="claimed",
        )
    assert result == {"id": "native"} and len(requests) == len(clients) == 1
    assert requests[0].headers["Idempotency-Key"] == "claimed"
    assert clients[0].is_closed()


@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("operation", ["request", "multipart", "stream", "download"])
async def test_ambient_custom_headers_refused_before_sdk_construction(
    monkeypatch: pytest.MonkeyPatch,
    late: bool,
    operation: str,
) -> None:
    def forbidden(**kwargs: object) -> AsyncOpenAI:
        pytest.fail("ambient headers reached SDK construction")

    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
    monkeypatch.setattr(deployment, "AsyncOpenAI", forbidden)
    transport = deployment.configured_transport(api_key="offline", project="selected-project")
    monkeypatch.setenv(
        "OPENAI_CUSTOM_HEADERS",
        "Authorization: Bearer hostile\nOpenAI-Project: hostile-project\n"
        "OpenAI-Organization: hostile-org\nX-Injected: hostile-extra",
    )
    with pytest.raises(ValueError, match="^ambient OpenAI custom headers are unsupported$"):
        if not late:
            deployment.configured_transport(api_key="offline", project="selected-project")
        elif operation == "request":
            await transport.request("POST", "/agents/sessions", body={}, key="claimed")
        elif operation == "multipart":
            await transport.multipart("/files", files=(), fields={}, key="claimed")
        elif operation == "stream":
            await transport.open_stream("/agents/sessions/native/events")
        else:
            await transport.download("/files/native/content")


async def test_lost_mutation_acknowledgment_is_one_post_and_closes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def lost(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadError("offline lost ack")

    clients = clients_for(monkeypatch, lost)
    transport = deployment.configured_transport(api_key="offline", project="selected-project")
    with pytest.raises(ProviderError) as error:
        await transport.request("POST", "/agents/sessions", body={}, key="claimed")
    assert error.value.category == "transient_network"
    assert [request.method for request in requests] == ["POST"]
    assert len(clients) == 1 and clients[0].is_closed()


@pytest.mark.parametrize("operation", ["stream", "download"])
@pytest.mark.parametrize("end", ["exhaust", "close", "cancel"])
async def test_owned_stream_and_download_close_on_every_end(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    end: str,
) -> None:
    source = Source(block=end == "cancel")

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=source, headers={"content-type": "text/event-stream"})

    clients = clients_for(monkeypatch, handle)
    transport = deployment.configured_transport(api_key="offline", project="selected-project")
    stream = (
        await transport.open_stream("/agents/sessions/native/events")
        if operation == "stream"
        else await transport.download("/files/native/content")
    )
    assert len(clients) == 1 and not clients[0].is_closed()
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
        await source.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert source.closed and clients[0].is_closed()


@pytest.mark.parametrize("operation", ["request", "multipart", "stream", "download"])
async def test_open_or_request_failure_closes_owned_client(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    clients = clients_for(
        monkeypatch,
        lambda request: httpx.Response(
            401, json={"error": {"message": "offline", "type": "authentication_error"}}
        ),
    )
    transport = deployment.configured_transport(api_key="offline", project="selected-project")
    with pytest.raises(ProviderError):
        if operation == "request":
            await transport.request("GET", "/agents/native")
        elif operation == "multipart":
            await transport.multipart(
                "/files", files=(("file", "t", b"t", "text/plain"),), fields={}, key="claimed"
            )
        elif operation == "stream":
            await transport.open_stream("/agents/sessions/native/events")
        else:
            stream = await transport.download("/files/native/content")
            await anext(stream)
    assert len(clients) == 1 and clients[0].is_closed()
