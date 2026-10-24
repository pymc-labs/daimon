"""Serve ASGI apps on 127.0.0.1 with an ephemeral port, for tests."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from .app import FakeModelServer

ASGIApp = Callable[..., Awaitable[None]]


@asynccontextmanager
async def serve_app(app: ASGIApp, *, port: int = 0) -> AsyncIterator[str]:
    """Yield the base URL while ``app`` listens on loopback.

    uvicorn is not a mux dependency; it is imported here, and only here, so the rest of the
    package works with just httpx and pydantic. Install uvicorn in the test environment.
    """
    import uvicorn

    listener = socket.socket()
    listener.bind(("127.0.0.1", port))
    port = listener.getsockname()[1]
    config: Any = uvicorn.Config(app, log_level="error", lifespan="on", timeout_graceful_shutdown=5)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=15)
        listener.close()


@asynccontextmanager
async def serve(
    fake: FakeModelServer | None = None, *, port: int = 0
) -> AsyncIterator[tuple[str, FakeModelServer]]:
    """Serve a fake model server on loopback (port 0 picks one); yield ``(base_url, server)``."""
    fake = fake or FakeModelServer()
    async with serve_app(fake, port=port) as url:
        yield url, fake
