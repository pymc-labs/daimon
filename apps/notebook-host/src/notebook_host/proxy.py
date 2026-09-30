"""HTTP + WebSocket reverse proxy to per-slug marimo subprocesses."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Mapping

import httpx
import websockets
from fastapi import (
    APIRouter,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from websockets.exceptions import ConnectionClosed, WebSocketException

from notebook_host.admin import AdminState
from notebook_host.lifecycle import NotebookProcess

_log = logging.getLogger(__name__)

# Headers we strip when forwarding (hop-by-hop and per-connection).
# RFC 2616 §13.5.1 + additional connection-specific headers that MUST NOT
# be forwarded by a proxy.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


# The browser headers a WebSocket upgrade forwards to marimo: its session
# auth, which every subprocess requires (``spawn_marimo``).
_WS_AUTH_HEADERS = {"cookie", "authorization"}


def _filter_request_headers(h: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in h.items() if k.lower() not in _HOP_BY_HOP}


_COOKIE_DOMAIN = re.compile(r";\s*domain=[^;]*", re.IGNORECASE)


def _response_headers(h: httpx.Headers, *, own_origin: bool) -> list[tuple[bytes, bytes]]:
    """Backend headers to relay, every ``Set-Cookie`` kept and host-only.

    A ``Domain`` attribute would share a notebook's cookie with every sibling
    ``<label>.<origin_base>``, so it is stripped: each cookie stays on the
    exact origin that set it. On its own origin a notebook may only be framed
    by itself.
    """
    out: list[tuple[bytes, bytes]] = []
    for k, v in h.multi_items():
        name = k.lower()
        if name in _HOP_BY_HOP:
            continue
        if name == "set-cookie":
            v = _COOKIE_DOMAIN.sub("", v)
        out.append((k.encode("latin-1"), v.encode("latin-1")))
    if own_origin:
        out.append((b"content-security-policy", b"frame-ancestors 'self'"))
    return out


def _resolve(
    state: AdminState, slug: str, headers: Mapping[str, str]
) -> tuple[NotebookProcess, str | None] | None:
    """The live notebook this request may reach, and the origin it must come from.

    Path mode (no ``origin_base``): by slug, no origin. Per-notebook-origin
    mode: the Host must be exactly ``<label>.<origin_base>`` for this slug's
    label, so ``/n/<slug>/`` on the shared host, or on another notebook's
    origin, reaches nothing.
    """
    np = state.processes.get(slug)
    if np is None or not np.is_alive():
        return None
    base = state.settings.origin_base
    if base is None:
        return np, None
    if not np.origin_label:
        return None
    own_host = f"{np.origin_label}.{base}".lower()
    if headers.get("host", "").lower() != own_host:
        return None
    return np, f"{state.settings.origin_scheme}://{own_host}"


def _cross_origin(headers: Mapping[str, str], own_origin: str) -> bool:
    """Whether the browser says this request comes from another origin.

    ``Origin`` and ``Sec-Fetch-*`` are set by the browser, and page JavaScript
    cannot forge them. Another notebook's page (a sibling origin, same site)
    is refused whatever it asks for, including form posts and frames. The one
    cross-origin request allowed is a top-level navigation: opening the link
    from chat.
    """
    origin = headers.get("origin")
    if origin is not None and origin.lower() != own_origin:
        return True
    site = headers.get("sec-fetch-site")
    if site is None or site in ("same-origin", "none"):
        return False
    return not (
        headers.get("sec-fetch-mode") == "navigate" and headers.get("sec-fetch-dest") == "document"
    )


def create_proxy_router(state: AdminState) -> APIRouter:
    router = APIRouter()

    @router.api_route(
        "/n/{slug}/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    )
    async def proxy_http(  # pyright: ignore[reportUnusedFunction]
        slug: str, path: str, request: Request
    ) -> Response:
        resolved = _resolve(state, slug, request.headers)
        if resolved is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no active notebook: {slug}")
        np, own_origin = resolved
        if own_origin is not None and _cross_origin(request.headers, own_origin):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-origin request refused")

        backend_url = f"http://localhost:{np.port}/n/{slug}/{path}"
        if request.url.query:
            backend_url += "?" + request.url.query

        headers = _filter_request_headers(dict(request.headers))
        body = await request.body()

        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.request(
                method=request.method,
                url=backend_url,
                headers=headers,
                content=body,
            )
        response = Response(content=r.content, status_code=r.status_code)
        response.raw_headers.extend(_response_headers(r.headers, own_origin=own_origin is not None))
        return response

    @router.websocket("/n/{slug}/{ws_path:path}")
    async def proxy_ws(  # pyright: ignore[reportUnusedFunction]
        websocket: WebSocket, slug: str, ws_path: str
    ) -> None:
        # CSRF mitigation for browser-borne WS upgrades. marimo's session
        # cookie authenticates /n/<slug>/*; if a slug ever leaks into Referer,
        # browser history, server logs, or a paste, a page at evil.com could
        # otherwise open `new WebSocket('ws://host/n/<slug>/ws')` and ride
        # the user's network position. When `allowed_origins` is configured,
        # only those origins are accepted; missing Origin is also rejected.
        # Empty list (default) = check disabled, suitable for trusted-network
        # deployments where the host isn't browser-reachable from outside.
        if state.settings.allowed_origins:
            origin = websocket.headers.get("origin")
            if origin not in state.settings.allowed_origins:
                await websocket.close(code=1008, reason="origin not allowed")
                return

        resolved = _resolve(state, slug, websocket.headers)
        if resolved is None:
            await websocket.close(code=1011, reason="no active notebook")
            return
        np, own_origin = resolved
        # Browsers always send Origin on a WebSocket upgrade, and page JS can't
        # change it: on its own origin a notebook's socket opens only from that
        # origin, never from a sibling notebook's page.
        if own_origin is not None and websocket.headers.get("origin", "").lower() != own_origin:
            await websocket.close(code=1008, reason="origin not allowed")
            return

        # Forward every WS path marimo serves under the base-url, not just
        # /ws: 0.23+ opens a second socket at /ws_sync (loro RTC document
        # sync) and the kernel-ready handshake stalls into "kernel not found"
        # if it 403s. {ws_path:path} mirrors the HTTP catch-all so future
        # marimo WS endpoints pass through without another code change.
        # marimo's ?file=...&session_id=... must survive the hop.
        query = websocket.url.query
        backend_url = f"ws://localhost:{np.port}/n/{slug}/{ws_path}"
        if query:
            backend_url += "?" + query

        # marimo authenticates the socket with the session cookie it set on
        # the page load (or an Authorization header), so both must reach the
        # backend; nothing else of the browser's handshake is forwarded.
        auth_headers = {k: v for k, v in websocket.headers.items() if k.lower() in _WS_AUTH_HEADERS}

        await websocket.accept()

        try:
            async with websockets.connect(  # type: ignore[attr-defined]
                backend_url, open_timeout=10.0, additional_headers=auth_headers
            ) as backend:

                async def client_to_backend() -> None:
                    try:
                        while True:
                            msg = await websocket.receive()
                            if msg["type"] == "websocket.disconnect":
                                return
                            if "text" in msg and msg["text"] is not None:
                                await backend.send(msg["text"])
                            elif "bytes" in msg and msg["bytes"] is not None:
                                await backend.send(msg["bytes"])
                    except (WebSocketDisconnect, ConnectionClosed):
                        return

                async def backend_to_client() -> None:
                    try:
                        async for frame in backend:
                            if isinstance(frame, bytes):
                                await websocket.send_bytes(frame)
                            else:
                                await websocket.send_text(frame)
                    except (ConnectionClosed, WebSocketDisconnect):
                        return

                c2b = asyncio.create_task(client_to_backend())
                b2c = asyncio.create_task(backend_to_client())
                try:
                    await asyncio.wait({c2b, b2c}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for t in (c2b, b2c):
                        if not t.done():
                            t.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await t
        except (WebSocketException, WebSocketDisconnect, OSError, TimeoutError) as e:
            _log.exception("backend ws error for slug=%s", slug)
            with contextlib.suppress(RuntimeError):
                await websocket.close(code=1011, reason=f"backend ws error: {type(e).__name__}")
        finally:
            with contextlib.suppress(RuntimeError):
                await websocket.close()

    return router
