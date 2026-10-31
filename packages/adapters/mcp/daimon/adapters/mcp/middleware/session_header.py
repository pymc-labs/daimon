"""Drop the client's ``mcp-session-id`` header before the stateless endpoint.

fastmcp keys session state, including the visibility rules
``IdentityMiddleware`` appends on every request, by that header and only falls
back to a fresh id when it is absent. A client resending a cached id grew one
key without bound, and two identities sharing an id shared visibility: a member
saw admin tools after an admin request. Without the header every request gets
its own key.

The signed-identity relay is exempt: the id belongs to the server it forwards
to, which may keep a session under it.
"""

from __future__ import annotations

from daimon.core.mcp_identity import RELAY_PATH
from starlette.types import ASGIApp, Receive, Scope, Send

_SESSION_HEADER = b"mcp-session-id"
_RELAY_PREFIX = RELAY_PATH + "/"


class StripSessionIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not str(scope["path"]).startswith(_RELAY_PREFIX):
            headers: list[tuple[bytes, bytes]] = scope["headers"]
            scope = {
                **scope,
                "headers": [(k, v) for k, v in headers if k.lower() != _SESSION_HEADER],
            }
        await self.app(scope, receive, send)
