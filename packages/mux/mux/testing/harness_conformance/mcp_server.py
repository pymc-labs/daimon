"""A tiny MCP streamable-HTTP server (JSON replies only) written as plain ASGI."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from ..fake_model_server.script import as_dict

Message = MutableMapping[str, Any]
SERVER_NAME = "conformance-mcp"


class TinyMCPServer:
    """Serves one tool, ``echo(text)``, and records every ``tools/call``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[Message]],
        send: Callable[[Message], Awaitable[None]],
    ) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                kind = "startup" if message["type"] == "lifespan.startup" else "shutdown"
                await send({"type": f"lifespan.{kind}.complete"})
                if kind == "shutdown":
                    return
        if scope["type"] != "http":
            return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        if scope["method"] == "POST":
            await self._rpc(json.loads(body or b"{}"), send)
        elif scope["method"] == "DELETE":
            await _reply(send, 200, None)
        else:
            await _reply(send, 405, None)

    async def _rpc(
        self, request: dict[str, Any], send: Callable[[Message], Awaitable[None]]
    ) -> None:
        method = request.get("method", "")
        params = as_dict(request.get("params"))
        if "id" not in request:
            await _reply(send, 202, None)
            return
        result: dict[str, Any]
        if method == "initialize":
            result = {
                "protocolVersion": params.get("protocolVersion", "2025-03-26"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": "0"},
            }
        elif method == "tools/list":
            schema = {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            }
            result = {
                "tools": [{"name": "echo", "description": "Echo text", "inputSchema": schema}]
            }
        elif method == "tools/call":
            self.calls.append(params)
            text = str(as_dict(params.get("arguments")).get("text", ""))
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        else:
            result = {}
        await _reply(send, 200, {"jsonrpc": "2.0", "id": request["id"], "result": result})


async def _reply(
    send: Callable[[Message], Awaitable[None]], status: int, payload: dict[str, Any] | None
) -> None:
    data = json.dumps(payload).encode() if payload is not None else b""
    headers = [(b"content-length", str(len(data)).encode())]
    if payload is not None:
        headers.append((b"content-type", b"application/json"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": data})
