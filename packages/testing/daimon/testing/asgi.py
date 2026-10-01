"""In-process ASGI harness for the MCP server: lifespan, JSON-RPC session
handshake, and one tool call, over `httpx.ASGITransport`.

Adapter-agnostic on purpose: it takes any ASGI app and never imports
`daimon.adapters.*`, so the MCP tests, the integration suite, and the hub
tests can share it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

import httpx
from starlette.types import ASGIApp, Message

INIT_BODY: dict[str, object] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
INIT_HEADERS: dict[str, str] = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@contextlib.asynccontextmanager
async def asgi_lifespan(app: ASGIApp) -> AsyncIterator[None]:
    """Drive `app`'s ASGI lifespan protocol: startup on enter, shutdown on exit.

    `httpx.ASGITransport` never sends lifespan events, so an app whose
    startup wires state (the MCP session manager, DB engines) needs this
    around any request.
    """
    send_queue: asyncio.Queue[Message] = asyncio.Queue()
    receive_queue: asyncio.Queue[Message] = asyncio.Queue()

    async def receive() -> Message:
        return await receive_queue.get()

    async def send(message: Message) -> None:
        await send_queue.put(message)

    async def run_lifespan() -> None:
        await app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)

    task = asyncio.create_task(run_lifespan())

    await receive_queue.put({"type": "lifespan.startup"})
    msg = await send_queue.get()
    assert msg["type"] == "lifespan.startup.complete", msg
    try:
        yield
    finally:
        await receive_queue.put({"type": "lifespan.shutdown"})
        msg = await send_queue.get()
        assert msg["type"] == "lifespan.shutdown.complete", msg
        await task


def parse_jsonrpc_response(resp: httpx.Response) -> dict[str, object]:
    """Parse a JSON-RPC response from either JSON or SSE format."""
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])  # type: ignore[return-value]
        raise AssertionError(f"No data line in SSE response: {resp.text!r}")
    return resp.json()  # type: ignore[return-value]


async def mcp_session(
    app: ASGIApp,
    *,
    token: str,
    method: str,
    params: dict[str, object] | None = None,
) -> dict[str, object]:
    """Initialize an MCP session against `app`, then execute one JSON-RPC method.

    Returns the parsed JSON-RPC response (the whole envelope, not just
    `result`). Raises AssertionError on an unexpected HTTP status.
    """
    headers = dict(INIT_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    # httpx types `app` as its own `_ASGIApp` callable protocol, which
    # starlette's `ASGIApp` alias spells differently; they are the same
    # three-argument coroutine signature at runtime.
    transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
    async with (
        asgi_lifespan(app),
        httpx.AsyncClient(transport=transport, base_url="http://t") as client,
    ):
        init_resp = await client.post("/mcp", json=INIT_BODY, headers=headers)
        assert init_resp.status_code == 200, f"initialize failed: {init_resp.text}"
        session_id = init_resp.headers.get("mcp-session-id")
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        body = {"jsonrpc": "2.0", "id": 2, "method": method, "params": params or {}}
        resp = await client.post("/mcp", json=body, headers=headers)
        assert resp.status_code == 200, f"{method} failed ({resp.status_code}): {resp.text}"
        return parse_jsonrpc_response(resp)


async def call_mcp_tool(
    app: ASGIApp,
    *,
    token: str,
    name: str,
    arguments: dict[str, object] | None = None,
) -> dict[str, object]:
    """`mcp_session` for the common case: one `tools/call` of `name`."""
    return await mcp_session(
        app,
        token=token,
        method="tools/call",
        params={"name": name, "arguments": arguments or {}},
    )
