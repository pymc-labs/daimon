"""Response headers that keep capability tokens out of third-party hands.

Recipient links carry their token in the query string (``?k=``), so a
browser that sent the page URL as a ``Referer`` would leak it. Every
response says ``Referrer-Policy: no-referrer``. HTML documents also carry a
Content-Security-Policy that confines the viewer to this origin: its own
scripts and styles, same-origin fetches and the pdf.js worker. Non-HTML
responses (JSON, the PDF itself) get no CSP, because a restrictive policy on
a PDF can stop the browser's built-in viewer from opening it.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REFERRER_POLICY = "no-referrer"

# 'unsafe-inline' is for styles only: the viewer sets a few style attributes
# in markup it builds. Scripts are external files, so script-src has none.
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "connect-src 'self'",
        "worker-src 'self' blob:",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    )
)


class SecurityHeadersMiddleware:
    """Pure ASGI middleware, so streamed file responses pass through untouched."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Referrer-Policy"] = REFERRER_POLICY
                if headers.get("content-type", "").startswith("text/html"):
                    headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
            await send(message)

        await self.app(scope, receive, send_with_headers)
