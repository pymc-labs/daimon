"""Tests for the in-process ASGI harness in daimon.testing.asgi.

Runs against a tiny Starlette app standing in for the MCP server: a
lifespan that records startup/shutdown, and one `/mcp` route speaking
just enough JSON-RPC to prove the handshake and tool-call plumbing.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from daimon.testing.asgi import (
    INIT_BODY,
    INIT_HEADERS,
    asgi_lifespan,
    call_mcp_tool,
    mcp_session,
    parse_jsonrpc_response,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route


def _make_app(*, lifecycle: list[str], sse: bool = False) -> Starlette:
    @contextlib.asynccontextmanager
    async def recording_lifespan(_app: Starlette) -> AsyncIterator[None]:
        lifecycle.append("startup")
        yield
        lifecycle.append("shutdown")

    async def mcp(request: Request) -> Response:
        body = json.loads(await request.body())
        assert request.headers.get("authorization") == "Bearer tok", "harness must send the token"
        if body["method"] == "initialize":
            return JSONResponse(
                {"jsonrpc": "2.0", "id": body["id"], "result": {"ok": True}},
                headers={"mcp-session-id": "S1"},
            )
        assert request.headers.get("mcp-session-id") == "S1", "the session id must be echoed back"
        result = {"jsonrpc": "2.0", "id": body["id"], "result": {"echo": body["params"]}}
        if sse:
            return PlainTextResponse(
                f"event: message\ndata: {json.dumps(result)}\n\n", media_type="text/event-stream"
            )
        return JSONResponse(result)

    return Starlette(routes=[Route("/mcp", mcp, methods=["POST"])], lifespan=recording_lifespan)


async def test_asgi_lifespan_runs_startup_on_enter_and_shutdown_on_exit() -> None:
    lifecycle: list[str] = []
    app = _make_app(lifecycle=lifecycle)
    async with asgi_lifespan(app):
        assert lifecycle == ["startup"], "startup must have completed before the body runs"
    assert lifecycle == ["startup", "shutdown"], "shutdown must run when the context exits"


async def test_mcp_session_initializes_then_calls_the_method() -> None:
    lifecycle: list[str] = []
    result = await mcp_session(
        _make_app(lifecycle=lifecycle), token="tok", method="tools/list", params={"cursor": None}
    )
    assert result["result"] == {"echo": {"cursor": None}}, (
        "the parsed JSON-RPC envelope must carry the method's params back"
    )
    assert lifecycle == ["startup", "shutdown"], "mcp_session must run the app's lifespan"


async def test_call_mcp_tool_is_a_tools_call_with_arguments() -> None:
    result = await call_mcp_tool(
        _make_app(lifecycle=[]), token="tok", name="whoami", arguments={"verbose": True}
    )
    assert result["result"] == {"echo": {"name": "whoami", "arguments": {"verbose": True}}}, (
        "call_mcp_tool must send tools/call with the tool name and arguments"
    )


async def test_mcp_session_parses_an_sse_response() -> None:
    result = await mcp_session(_make_app(lifecycle=[], sse=True), token="tok", method="ping")
    assert result["result"] == {"echo": {}}, "an SSE-framed JSON-RPC response must be parsed"


def test_parse_jsonrpc_response_handles_json_and_sse() -> None:
    envelope = {"jsonrpc": "2.0", "id": 2, "result": {"n": 1}}
    plain = httpx.Response(200, json=envelope)
    assert parse_jsonrpc_response(plain) == envelope, "a JSON body must be returned as-is"

    sse = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=f"event: message\ndata: {json.dumps(envelope)}\n\n".encode(),
    )
    assert parse_jsonrpc_response(sse) == envelope, "the first SSE data line must be parsed"

    empty = httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"")
    with pytest.raises(AssertionError, match="No data line"):
        parse_jsonrpc_response(empty)


def test_init_constants_describe_the_mcp_handshake() -> None:
    assert INIT_BODY["method"] == "initialize", "INIT_BODY must be the initialize request"
    assert "text/event-stream" in INIT_HEADERS["Accept"], "the server may answer over SSE"
