"""Unit tests for daimon.core.health — the stdlib-asyncio liveness responder.

Tests cover:
  - a raw "GET / HTTP/1.1" against the bound port returns HTTP/1.1 200
  - any path (not just "/") still returns 200 (liveness-only, no parsing)
  - the server shuts down cleanly via close() + wait_closed()

No DB; pure asyncio against a loopback ephemeral port.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from daimon.core.health import start_liveness_responder


def _free_port() -> int:
    """Pick an OS-assigned free TCP port on loopback for the test responder."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest_asyncio.fixture
async def liveness_server() -> AsyncIterator[tuple[asyncio.Server, int]]:
    """Start a liveness responder on a free loopback port; tear it down cleanly."""
    port = _free_port()
    server = await start_liveness_responder(port, host="127.0.0.1")
    try:
        yield server, port
    finally:
        server.close()
        await server.wait_closed()


async def _send_raw_get(port: int, path: str) -> bytes:
    """Open a connection, send a raw HTTP/1.1 GET for `path`, return the response bytes."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        await writer.drain()
        return await reader.read(1024)
    finally:
        writer.close()
        await writer.wait_closed()


async def test_liveness_responder_returns_200_when_root_path_requested(
    liveness_server: tuple[asyncio.Server, int],
) -> None:
    """A raw GET / against the bound port yields an HTTP/1.1 200 response."""
    _server, port = liveness_server

    response = await _send_raw_get(port, "/")

    assert response.startswith(b"HTTP/1.1 200"), (
        f"liveness responder should answer 200 on /, got: {response[:40]!r}"
    )


async def test_liveness_responder_returns_200_when_arbitrary_path_requested(
    liveness_server: tuple[asyncio.Server, int],
) -> None:
    """Any path returns 200 — the responder is liveness-only and does not route."""
    _server, port = liveness_server

    response = await _send_raw_get(port, "/anything/else?x=1")

    assert response.startswith(b"HTTP/1.1 200"), (
        f"liveness responder should answer 200 on any path, got: {response[:40]!r}"
    )


async def test_liveness_responder_shuts_down_cleanly_when_closed() -> None:
    """close() + wait_closed() stops serving with no leaked listener."""
    port = _free_port()
    server = await start_liveness_responder(port, host="127.0.0.1")
    server.close()
    await server.wait_closed()

    assert not server.is_serving(), "server should no longer be serving after close()"

    with pytest.raises((ConnectionRefusedError, OSError)):
        # Nothing should be listening on the port anymore.
        _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await writer.wait_closed()
