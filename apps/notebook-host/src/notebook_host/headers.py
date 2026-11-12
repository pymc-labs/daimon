"""``Referrer-Policy: no-referrer`` on every response, proxied marimo pages included.

A notebook link carries its access token in the query string
(``?access_token=``), so a notebook that links to another site would hand the
token over in a ``Referer`` header. The policy is set on every response,
replacing any value marimo sent.

There is deliberately no Content-Security-Policy. marimo's own pages inline
scripts and build code at runtime, so a policy that still lets them run
(``'unsafe-inline'``, ``'unsafe-eval'``) would not restrict anything.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REFERRER_POLICY = "no-referrer"


class ReferrerPolicyMiddleware:
    """Pure ASGI middleware, so proxied streams and websockets pass through untouched."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_policy(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Referrer-Policy"] = REFERRER_POLICY
            await send(message)

        await self.app(scope, receive, send_with_policy)
