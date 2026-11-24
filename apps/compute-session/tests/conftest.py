from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest
from compute_session.server import create_app
from pydantic import JsonValue
from starlette.types import ASGIApp

TOKEN = "per-session-token"
URL = "https://session.test/mcp/"
#: The headers daimon.core.compute_fly sends on every request of a call.
HEADERS = {"Authorization": "Bearer " + TOKEN, "Accept": "application/json, text/event-stream"}

type Call = Callable[[str, dict[str, JsonValue]], Awaitable[dict[str, JsonValue]]]


@pytest.fixture
def app() -> ASGIApp:
    return create_app(TOKEN)


@pytest.fixture
async def client(app: ASGIApp) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as session:
        yield session


@pytest.fixture
def rpc(client: httpx.AsyncClient) -> Call:
    """Send one JSON-RPC request with the session token and return the response message."""

    async def send(method: str, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        response = await client.post(
            URL,
            headers=HEADERS,
            json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params},
        )
        assert response.status_code == 200, response.text
        message = response.json()
        assert message["jsonrpc"] == "2.0" and message["id"] == 2
        return message

    return send


@pytest.fixture
def tool(rpc: Call) -> Call:
    """Call a tool and return its structured result; the call itself must not fail."""

    async def call(name: str, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        result = (await rpc("tools/call", {"name": name, "arguments": arguments}))["result"]
        assert isinstance(result, dict) and result["isError"] is False, result
        structured = result["structuredContent"]
        assert isinstance(structured, dict)
        return structured

    return call
