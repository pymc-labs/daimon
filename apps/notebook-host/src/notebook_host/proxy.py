"""HTTP + WebSocket reverse proxy to per-slug marimo subprocesses."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from urllib.parse import urlsplit

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


def _filter_response_headers(h: httpx.Headers) -> dict[str, str]:
    return {k: v for k, v in h.items() if k.lower() not in _HOP_BY_HOP}


def _from_another_notebook(
    slug: str, headers: Mapping[str, str], *, require_referer: bool = True
) -> bool:
    """Whether a same-origin request for ``slug`` came from some other page.

    Every notebook shares this origin, so notebook A's JavaScript (an
    anywidget runs even in a read-only app) can ``fetch`` or frame ``/n/B/``
    and the browser attaches B's cookie if the viewer has opened B's link. The
    browser's ``Sec-Fetch-Site: same-origin`` plus a ``Referer`` outside
    ``/n/<slug>`` gives that away; a same-origin request that hides its
    Referer is refused too, since marimo's own requests always send one.
    Top-level navigation (opening the link from chat) is ``cross-site`` or
    ``none`` and passes, as do clients that send no fetch metadata at all.

    ``require_referer=False`` is for WebSocket upgrades, which browsers send
    without a Referer; only a Referer that names another notebook is refused
    there, so a page that hides its Referer can still open another
    notebook's socket (see the README's known limits).
    """
    if headers.get("sec-fetch-site") != "same-origin":
        return False
    referer = headers.get("referer")
    if not referer:
        return require_referer
    path = urlsplit(referer).path
    own = f"/n/{slug}"
    return not (path == own or path.startswith(f"{own}/"))


def create_proxy_router(state: AdminState) -> APIRouter:
    router = APIRouter()

    @router.api_route(
        "/n/{slug}/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    )
    async def proxy_http(  # pyright: ignore[reportUnusedFunction]
        slug: str, path: str, request: Request
    ) -> Response:
        if _from_another_notebook(slug, request.headers):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-notebook request refused")
        np = state.processes.get(slug)
        if np is None or not np.is_alive():
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no active notebook: {slug}")

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
        return Response(
            content=r.content,
            status_code=r.status_code,
            headers=_filter_response_headers(r.headers),
        )

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

        if _from_another_notebook(slug, websocket.headers, require_referer=False):
            await websocket.close(code=1008, reason="cross-notebook request refused")
            return

        np = state.processes.get(slug)
        if np is None or not np.is_alive():
            await websocket.close(code=1011, reason="no active notebook")
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
