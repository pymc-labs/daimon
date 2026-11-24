"""The authenticated MCP endpoint: JSON-RPC over HTTP POST, one JSON body per request.

No session is kept. Every request carries the bearer token and stands alone, so
the control plane can open a new client for each call and a restart loses nothing.
The token is checked before the path, the method or the body are looked at, so a
request without it learns only that the server exists.
"""

from __future__ import annotations

import hmac
import json
from importlib.metadata import version
from typing import cast

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from compute_session.tools import (
    MIB,
    TOOLS,
    InvalidArguments,
    Json,
    call_tool,
    encode,
    tool_error,
    tool_result,
)

#: Also the distribution name, which is where the reported version comes from.
SERVER_NAME = "daimon-compute-session"
_SERVER_VERSION = version(SERVER_NAME)
#: Newest last. A client that asks for another version is offered the first.
PROTOCOL_VERSIONS = ("2025-03-26", "2025-06-18")
#: A 4 MiB file write, with every character escaped as JSON, stays under this.
MAX_REQUEST_BYTES = 32 * MIB
_TOOL_NAMES = frozenset(
    tool["name"] for tool in TOOLS if isinstance(tool, dict) and isinstance(tool["name"], str)
)


def _json(payload: Json, status: int = 200) -> Response:
    return Response(encode(payload), status_code=status, media_type="application/json")


def _error(request_id: Json, code: int, message: str, status: int = 200) -> Response:
    error: Json = {"code": code, "message": message}
    return _json({"jsonrpc": "2.0", "id": request_id, "error": error}, status)


async def _body(request: Request) -> bytes | None:
    """The request body, or None once it passes the size limit."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_REQUEST_BYTES:
            return None
    return bytes(body)


def _object(value: object) -> dict[str, Json] | None:
    # A JSON object decodes to a dict with string keys and JSON values.
    return cast("dict[str, Json]", value) if isinstance(value, dict) else None


class _Refused(Exception):
    """A JSON-RPC error for the request being handled."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def _result(method: str, params: dict[str, Json]) -> Json:
    if method == "initialize":
        asked = params.get("protocolVersion")
        return {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": _SERVER_VERSION},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        arguments = _object(params.get("arguments", {}))
        if not isinstance(name, str) or name not in _TOOL_NAMES or arguments is None:
            raise _Refused(-32602, "Invalid params")
        try:
            return tool_result(await call_tool(name, arguments))
        except InvalidArguments as error:
            return tool_error(str(error))
    raise _Refused(-32601, "Method not found")


async def _rpc(request: Request) -> Response:
    body = await _body(request)
    if body is None:
        return Response(status_code=413)
    try:
        message = _object(json.loads(body))
    except (ValueError, RecursionError):
        return _error(None, -32700, "Parse error", 400)
    method = None if message is None else message.get("method")
    if message is None or message.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return _error(None, -32600, "Invalid Request", 400)
    if "id" not in message:
        # A notification, such as notifications/initialized: accepted, never answered.
        return Response(status_code=202)
    request_id = message["id"]
    if isinstance(request_id, bool) or not isinstance(request_id, int | str):
        return _error(None, -32600, "Invalid Request", 400)
    params = _object(message.get("params", {}))
    try:
        if params is None:
            raise _Refused(-32602, "Invalid params")
        return _json({"jsonrpc": "2.0", "id": request_id, "result": await _result(method, params)})
    except _Refused as refused:
        return _error(request_id, refused.code, refused.message)


def create_app(token: str) -> ASGIApp:
    """The server for one session token. An empty token is refused: it would match
    a request that sends an empty bearer."""
    # A header value cannot carry the line end a secret store may leave on a token.
    token = token.strip()
    if not token:
        raise ValueError("a session token is required")
    expected = b"Bearer " + token.encode()

    async def respond(request: Request) -> Response:
        path = request.url.path
        if path == "/health":
            return Response("ok", media_type="text/plain")
        # Header values arrive decoded as Latin-1, which maps back to the same bytes.
        supplied = request.headers.get("authorization", "").encode("latin-1")
        if not hmac.compare_digest(supplied, expected):
            return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
        if path not in ("/mcp", "/mcp/"):
            return Response(status_code=404)
        if request.method != "POST":
            return Response(status_code=405, headers={"Allow": "POST"})
        return await _rpc(request)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        response = await respond(Request(scope, receive))
        await response(scope, receive, send)

    return app
