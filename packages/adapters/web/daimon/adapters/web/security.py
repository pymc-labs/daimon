"""Response headers and the cross-site write check, as one pure-ASGI middleware.

Pure ASGI (not `BaseHTTPMiddleware`) so streamed responses pass through untouched.
There is no CORS: the app and its API share one origin, so a browser never needs it.
"""

from __future__ import annotations

from daimon.core.config import WebSettings, origin_of
from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Sent by the app on every request and by no cross-site form.
CSRF_HEADER = "x-daimon-web"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_GUARDED_PREFIXES = ("/api/", "/auth/")
_NO_STORE_PREFIXES = ("/api/", "/auth/")


def content_security_policy() -> str:
    """The page policy: own scripts only, no frames."""
    return "; ".join(
        (
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: blob:",
            "font-src 'self' data:",
            "connect-src 'self'",
            "frame-src 'none'",
            "frame-ancestors 'none'",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
        )
    )


def _headers(web: WebSettings) -> dict[str, str]:
    headers = {
        "Content-Security-Policy": content_security_policy(),
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
    }
    if web.public_url.scheme == "https":
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return headers


class SecurityMiddleware:
    """Adds the security headers and refuses cross-site writes to `/api` and `/auth`."""

    def __init__(self, app: ASGIApp, *, web: WebSettings) -> None:
        self._app = app
        self._origin = origin_of(str(web.public_url))
        self._headers = _headers(web)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path: str = scope["path"]
        if scope["method"] not in _SAFE_METHODS and path.startswith(_GUARDED_PREFIXES):
            refusal = self._refusal(scope)
            if refusal is not None:
                await JSONResponse({"detail": refusal}, status_code=403, headers=self._headers)(
                    scope, receive, send
                )
                return
        no_store = path.startswith(_NO_STORE_PREFIXES)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                out = MutableHeaders(scope=message)
                for name, value in self._headers.items():
                    if name == "Content-Security-Policy" and name in out:
                        # A route's own policy (a download's sandbox) is enforced as well.
                        out.append(name, value)
                    else:
                        out[name] = value
                if no_store and "cache-control" not in out:
                    out["Cache-Control"] = "no-store"
            await send(message)

        await self._app(scope, receive, send_with_headers)

    def _refusal(self, scope: Scope) -> str | None:
        """Why a write is refused, or None when it carries the header and our own origin."""
        sent = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        if sent.get(CSRF_HEADER) != "1":
            return "Missing request header."
        if sent.get("origin") != self._origin:
            return "Cross-site request refused."
        return None
