"""Shared bounded bearer gate for disposable QA MCP hosts."""

from __future__ import annotations

import datetime as dt
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BODY_BYTES = 256 * 1024


@dataclass(frozen=True)
class RunBearer:
    """The run's gate credential: random, short-lived, revocable by a marker file."""

    token: str
    expires_at: dt.datetime
    revoked_marker: Path

    def accepts(self, presented: str | None, now: dt.datetime) -> bool:
        if presented is None or self.revoked_marker.exists() or now >= self.expires_at:
            return False
        return hmac.compare_digest(presented.encode(), self.token.encode())

    def revoke(self) -> None:
        self.revoked_marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.revoked_marker.touch()


class McpGate:
    """Authenticate bounded MCP requests, optionally auditing calls and serving readback."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        bearer: RunBearer,
        daimon_token: str,
        allowed_tools: tuple[str, ...],
        readback: Callable[[], Mapping[str, Any]] | None = None,
        observe: Callable[[bytes], None] | None = None,
        clock: Any = None,  # noqa: ANN401 - a zero-argument callable returning an aware datetime
    ) -> None:
        self._readback = readback
        self._observe = observe
        self._app = app
        self._bearer = bearer
        self._daimon_token = daimon_token
        self._allowed = frozenset(allowed_tools)
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        is_readback = scope["path"] == "/calls" and self._readback is not None
        if scope["path"] not in ("/mcp", "/mcp/") and not is_readback:
            await _reply(send, 404, {"error": "not found"})
            return
        if scope["method"] != ("GET" if is_readback else "POST"):
            await _reply(send, 405, {"error": "method not allowed"})
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        authorization = headers.get("authorization", "")
        presented = authorization[7:] if authorization.lower().startswith("bearer ") else None
        if not self._bearer.accepts(presented, self._clock()):
            await _reply(send, 401, {"error": "unauthorized"})
            return
        if is_readback and self._readback is not None:
            await _reply(send, 200, self._readback())
            return
        body = await _read_body(receive)
        if body is None:
            await _reply(send, 413, {"error": "request too large"})
            return
        if self._observe is not None:
            self._observe(body)
        refusal = self._refusal(body)
        if refusal is not None:
            await _reply(send, 403, refusal)
            return
        forwarded = [
            (k, v) for k, v in scope["headers"] if k.lower() not in (b"authorization", b"cookie")
        ]
        forwarded.append((b"authorization", f"Bearer {self._daimon_token}".encode()))
        inner_scope = {**scope, "headers": forwarded, "path": "/mcp", "raw_path": b"/mcp"}

        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self._app(inner_scope, replay, send)

    def _refusal(self, body: bytes) -> dict[str, Any] | None:
        try:
            message = json.loads(body)
        except ValueError:
            return {"error": "invalid json"}
        if not isinstance(message, dict):
            return {"error": "batch requests are not accepted"}
        message = cast(dict[str, Any], message)
        if message.get("method") == "tools/call":
            params: object = message.get("params")
            name = cast(dict[str, Any], params).get("name") if isinstance(params, dict) else None
            if name not in self._allowed:
                return {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "error": {"code": -32601, "message": f"tool {name!r} is not available"},
                }
        return None


async def _read_body(receive: Receive) -> bytes | None:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


async def _reply(send: Send, status: int, payload: Mapping[str, Any]) -> None:
    body = json.dumps(payload).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})
