"""The FastAPI app: the shared middleware, the JSON routes and the single-page app."""

from __future__ import annotations

import anthropic
from daimon.adapters.web.admin import admin_router
from daimon.adapters.web.agents import agents_router
from daimon.adapters.web.auth import auth_router
from daimon.adapters.web.avatars import avatars_router
from daimon.adapters.web.chat import chat_router
from daimon.adapters.web.credits import credits_router
from daimon.adapters.web.invites import invites_router
from daimon.adapters.web.me import me_router
from daimon.adapters.web.nodes import nodes_router
from daimon.adapters.web.runtime import WebRuntime
from daimon.adapters.web.security import SecurityMiddleware
from daimon.adapters.web.static import spa_router
from daimon.adapters.web.upload_limit import UploadLimitMiddleware
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.types import ASGIApp, Receive, Scope, Send


def _health_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter()

    @router.get("/healthz", include_in_schema=False)
    async def healthz(response: Response) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        try:
            async with runtime.sessionmaker() as session:
                await session.execute(text("select 1"))
        except Exception:
            response.status_code = 503
            return {"status": "unavailable"}
        return {"status": "ok"}

    return router


async def _unparsable_id_is_not_found(request: Request, exc: RequestValidationError) -> Response:
    """An id in the path that is no UUID names nothing: 404 like any unknown one, not 422."""
    if exc.errors() and all(
        e["loc"][:1] == ("path",) and e["type"].startswith("uuid_") for e in exc.errors()
    ):
        return JSONResponse({"detail": "This doesn't exist."}, status_code=404)
    return await request_validation_exception_handler(request, exc)


async def _provider_unreachable(request: Request, exc: anthropic.APIConnectionError) -> Response:
    """The model provider did not answer: 503 with a short refusal, not a 500."""
    detail = "The model provider is unreachable. Try again soon."
    return JSONResponse({"detail": detail}, status_code=503)


# Files people download and avatars: already packed, so gzip would only stall the event loop.
_UNCOMPRESSED = ("/api/chat/attachments/", "/api/chat/files/", "/avatars/")


class _Compress:
    """Gzip answers over 1 KB: a skill's files or the page bundle run to megabytes.

    Event streams pass as they are (Starlette never compresses them), and so does
    `_UNCOMPRESSED`. A GET must never return a token or link beside text a caller
    chose, or compressed sizes could leak it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._plain = app
        self._gzip = GZipMiddleware(app, minimum_size=1024, compresslevel=6)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        plain = scope["type"] == "http" and str(scope["path"]).startswith(_UNCOMPRESSED)
        await (self._plain if plain else self._gzip)(scope, receive, send)


def create_app(runtime: WebRuntime) -> FastAPI:
    """Build the app. Routes before the page catch-all; no I/O until a request arrives."""
    app = FastAPI(title="Daimon web", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime
    app.add_exception_handler(RequestValidationError, _unparsable_id_is_not_found)  # pyright: ignore[reportArgumentType]
    app.add_exception_handler(anthropic.APIConnectionError, _provider_unreachable)  # pyright: ignore[reportArgumentType]
    app.add_middleware(UploadLimitMiddleware)
    app.add_middleware(SecurityMiddleware, web=runtime.web)
    app.add_middleware(_Compress)
    app.include_router(_health_router(runtime))
    app.include_router(auth_router(runtime))
    app.include_router(avatars_router(runtime))
    app.include_router(me_router(runtime))
    app.include_router(nodes_router(runtime))
    app.include_router(agents_router(runtime))
    app.include_router(chat_router(runtime))
    app.include_router(credits_router(runtime))
    app.include_router(invites_router(runtime))
    app.include_router(admin_router(runtime))
    app.include_router(spa_router(runtime.web))
    return app
