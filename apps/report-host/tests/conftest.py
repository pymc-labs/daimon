"""Shared test fixtures for report_host.

The in-process fake-seam harness (a small ``mcp.server.lowlevel.Server``
mounted on the real streamable-HTTP ASGI transport, optionally paired with a
``PUT /bundles`` route) lives here so more than one test module can drive a
``SeamClient`` against it without duplicating the MCP wire-protocol plumbing.
Exposed as fixtures (``build_fake_seam`` / ``fake_seam_lifespan``) rather than
plain module imports, since pytest's ``--import-mode=importlib`` (this
project's configuration) gives every test file its own module namespace with
no shared ``sys.path`` entry a sibling test file could import from directly.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from mcp import types as mcp_types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Message, Receive, Scope, Send

FakeToolBehavior = Callable[[dict[str, object]], dict[str, object]]
FakeBundlePut = Callable[[bytes], dict[str, object]]
FakeASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
FakeSeamBuilder = Callable[..., FakeASGIApp]
FakeSeamLifespan = Callable[[FakeASGIApp], "contextlib.AbstractAsyncContextManager[None]"]


class _StreamableHTTPEndpoint:
    """ASGI endpoint that hands every request to the session manager."""

    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self._manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._manager.handle_request(scope, receive, send)


def _build_fake_seam(
    *,
    behaviors: dict[str, FakeToolBehavior],
    captured_auth: list[str],
    captured_arguments: dict[str, dict[str, object]] | None = None,
    unauthorized_tokens: frozenset[str] = frozenset(),
    push_bundle_handler: FakeBundlePut | None = None,
    captured_bundle_puts: list[bytes] | None = None,
) -> FakeASGIApp:
    """Build a minimal MCP server exposing exactly the tools under test.

    Raising inside a behavior maps to an ``isError`` CallToolResult carrying
    exactly ``str(exception)`` as its text — the low-level server's own
    ``except Exception as e: return self._make_error_result(str(e))``, with
    no wrapping — so tests can pin exact tool-error wording.

    ``push_bundle_handler``, when given, adds a ``PUT /bundles`` route on the
    same ASGI app (returning ``push_bundle_handler(body)`` as JSON) so a
    ``SeamClient`` built with the same transport for both its MCP session and
    its plain-HTTP client can exercise ``push_bundle`` against this fake too.
    """
    server = Server("fake-seam")

    @server.call_tool()
    async def handle_call_tool(  # pyright: ignore[reportUnusedFunction]
        name: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        if captured_arguments is not None:
            captured_arguments[name] = arguments
        behavior = behaviors[name]
        return behavior(arguments)

    @server.list_tools()
    async def handle_list_tools() -> list[mcp_types.Tool]:  # pyright: ignore[reportUnusedFunction]
        # No outputSchema: the mcp client's own call_tool() fetches this list
        # to validate structuredContent against a declared schema, and skips
        # validation entirely when a tool has none — exactly what these tests
        # want, since they assert on the client's own decoding, not a schema.
        return [
            mcp_types.Tool(name=name, inputSchema={"type": "object", "properties": {}})
            for name in behaviors
        ]

    manager = StreamableHTTPSessionManager(app=server, stateless=True)
    routes = [Route("/mcp", endpoint=_StreamableHTTPEndpoint(manager))]

    if push_bundle_handler is not None:

        async def handle_bundle_put(request: Request) -> JSONResponse:
            body = await request.body()
            if captured_bundle_puts is not None:
                captured_bundle_puts.append(body)
            return JSONResponse(push_bundle_handler(body))

        routes.append(Route("/bundles", endpoint=handle_bundle_put, methods=["PUT"]))

    starlette_app = Starlette(routes=routes, lifespan=lambda _app: manager.run())

    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            auth_header = headers.get(b"authorization", b"").decode()
            captured_auth.append(auth_header)
            token = auth_header.removeprefix("Bearer ")
            if token in unauthorized_tokens:
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
        await starlette_app(scope, receive, send)

    return wrapped


@contextlib.asynccontextmanager
async def _lifespan(app: FakeASGIApp) -> AsyncIterator[None]:
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


@pytest.fixture
def build_fake_seam() -> FakeSeamBuilder:
    """The fake-seam ASGI app builder, injected so test files never duplicate it."""
    return _build_fake_seam


@pytest.fixture
def fake_seam_lifespan() -> FakeSeamLifespan:
    """The fake seam's ASGI lifespan context manager, injected the same way."""
    return _lifespan
