"""Stdlib-asyncio liveness responder for non-HTTP process groups.

The discord and scheduler process groups run an asyncio loop but expose no HTTP
server, so Fly has nothing to health-check. This module starts a minimal raw-socket
responder ON THE CURRENT RUNNING LOOP that answers 200 for any request. Because it
shares the loop with the real work, a hung loop stops answering — the Fly checker
fails and restarts the machine (that co-location is the point; do not move it onto a
separate loop/thread).

Core has no HTTP-server dependency and must not gain one for "return 200" — this uses
`asyncio.start_server` only. MCP keeps its own Starlette `/healthz`; it is not migrated.
"""

from __future__ import annotations

import asyncio

_RESPONSE = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: text/plain\r\n"
    b"Content-Length: 2\r\n"
    b"Connection: close\r\n"
    b"\r\n"
    b"ok"
)


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Drain the request (do not parse — liveness-only) and write a constant 200."""
    try:
        await reader.read(1024)
        writer.write(_RESPONSE)
        await writer.drain()
    finally:
        writer.close()


async def start_liveness_responder(port: int, *, host: str = "0.0.0.0") -> asyncio.Server:
    """Start the liveness responder on the current running loop.

    Binds 0.0.0.0 by default so Fly's checker (a separate network namespace) can
    reach it. Safe to run unconditionally — there is no config gate. The caller owns
    shutdown: `server.close()` then `await server.wait_closed()`.
    """
    return await asyncio.start_server(_handle, host=host, port=port)
