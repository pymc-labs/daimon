"""Response headers and the cross-site write check, as one pure-ASGI middleware.

Pure ASGI (not `BaseHTTPMiddleware`) so streamed responses pass through untouched.
There is no CORS: the app and its API share one origin, so a browser never needs it.
"""

from __future__ import annotations

from daimon.core.artifact_pages.content import (
    ContentHost,
    legacy_location,
    legacy_slug,
    route_host,
)
from daimon.core.config import WebSettings, origin_of
from daimon.core.stores.artifact_pages import find_imported
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Sent by the app on every request and by no cross-site form.
CSRF_HEADER = "x-daimon-web"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_GUARDED_PREFIXES = ("/api/", "/auth/", "/a/")
_NO_STORE_PREFIXES = ("/api/", "/auth/", "/a/")
# Artifact pages: their routes set their own policy, framing and referrer headers.
_PAGES_PREFIX = "/a/"


def content_security_policy() -> str:
    """The page policy: own scripts only; frames only of this site's artifact pages."""
    return "; ".join(
        (
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: blob:",
            "font-src 'self' data:",
            "connect-src 'self'",
            "frame-src 'self'",
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
    """Adds the security headers and refuses cross-site writes to `/api`, `/auth` and `/a`."""

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
                body = {"detail": refusal}
                if path.startswith(_PAGES_PREFIX):
                    body = {"error": {"code": "forbidden", "message": refusal}}
                await JSONResponse(body, status_code=403, headers=self._headers)(
                    scope, receive, send
                )
                return
        no_store = path.startswith(_NO_STORE_PREFIXES)
        page = path.startswith(_PAGES_PREFIX)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                out = MutableHeaders(scope=message)
                for name, value in self._headers.items():
                    if page and name == "X-Frame-Options" and _framed_by_policy(out):
                        continue
                    if page:
                        out.setdefault(name, value)
                    elif name == "Content-Security-Policy" and name in out:
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


def _framed_by_policy(headers: MutableHeaders) -> bool:
    """Page code on a content host is framed cross-origin: its `frame-ancestors` says by
    whom, and X-Frame-Options would forbid it."""
    return "frame-ancestors" in headers.get("content-security-policy", "")


def content_host_of(web: WebSettings) -> ContentHost | None:
    url = web.artifact_content_url
    return None if url is None else ContentHost.parse(url)


class PageHostsMiddleware:
    """Holds artifact paths to their hosts (`route_host`): the content host serves page
    content only, pages opened on another host move to the public one, and a legacy host
    sends `/a/<name>` to the page imported from that address."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        web: WebSettings,
        sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        self._app = app
        self._sessions = sessionmaker
        self._legacy = frozenset(web.artifact_legacy_hosts)
        self._public_url = str(web.public_url)
        self._public = origin_of(str(web.public_url))
        self._netloc = self._public.split("://", 1)[1]
        self._content = content_host_of(web)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        host = Headers(scope=scope).get("host", "")
        route = route_host(
            host,
            scope["path"],
            public_netloc=self._netloc,
            content=self._content,
            legacy_hosts=self._legacy,
        )
        response: Response
        match route:
            case "legacy":
                response = await self._moved(host.lower(), scope)
            case "serve":
                await self._app(scope, receive, send)
                return
            case "redirect":
                path = (scope.get("raw_path") or scope["path"].encode()).decode("latin-1")
                query = scope["query_string"].decode("latin-1")
                response = RedirectResponse(
                    f"{self._public}{path}{'?' + query if query else ''}", status_code=308
                )
            case "not_found":
                response = _not_found()
        await response(scope, receive, send)

    async def _moved(self, host: str, scope: Scope) -> Response:
        """The 308 to the page imported from `https://<host>/a/<name>`, else not found."""
        slug = legacy_slug(scope["path"])
        assert slug is not None, "route_host sends only /a/<name> here"
        async with self._sessions() as db:
            found = await find_imported(db, address=f"https://{host}/a/{slug}")
        if found is None:
            return _not_found()
        public_id, name = found
        query = scope["query_string"].decode("latin-1")
        return RedirectResponse(
            legacy_location(self._public_url, public_id, name, query), status_code=308
        )


def _not_found() -> Response:
    # Sandboxed: a page that opened this could otherwise script it, a document on its own
    # origin without its policy.
    return PlainTextResponse(
        "Not found",
        status_code=404,
        headers={
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
