"""SDK-owned FastAPI ingress and health lifecycle for Microsoft Teams."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import structlog
from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.feedback import record_feedback
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.config import TeamsSettings
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from microsoft_teams.api import MessageSubmitActionInvokeActivity
from microsoft_teams.apps import ActivityContext, App, FastAPIAdapter
from microsoft_teams.common import Client, ClientOptions
from sqlalchemy.exc import SQLAlchemyError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_TEAMS_HTTP_BODY_BYTES = 64 * 1024
log = structlog.get_logger(__name__)


class _IngressGateMiddleware:
    """Keep the service healthy while the public Teams capability is disabled."""

    def __init__(self, app: ASGIApp, *, enabled: bool) -> None:
        self._app = app
        self._enabled = enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") == "/api/messages" and not self._enabled:
            response = JSONResponse({"error": "Teams ingress disabled"}, status_code=503)
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


class _RequestBodyLimitMiddleware:
    """Reject an oversize Teams body before SDK parsing or authentication."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_TEAMS_HTTP_BODY_BYTES) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != "/api/messages":
            await self._app(scope, receive, send)
            return
        messages: list[Message] = []
        size = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > self._max_bytes:
                response = JSONResponse({"error": "Request too large"}, status_code=413)
                await response(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        index = 0

        async def replay() -> Message:
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            return {"type": "http.disconnect"}

        await self._app(scope, replay, send)


@dataclass
class _Readiness:
    initialized: bool = False


@dataclass(frozen=True)
class TeamsHttpService:
    """Objects that make up one Teams HTTP process.

    Keeping the SDK app and adapter visible makes the ownership boundary
    inspectable in tests: the Microsoft SDK owns ``/api/messages`` while this
    service owns process lifecycle and health routes.
    """

    app: FastAPI
    teams_app: App
    http_adapter: FastAPIAdapter
    runtime: TeamsRuntime
    turns: TeamsApp
    _readiness: _Readiness

    @property
    def ready(self) -> bool:
        """Whether SDK initialization completed inside the active lifespan."""
        return self._readiness.initialized


def create_teams_http_service(
    *,
    settings: TeamsSettings,
    runtime: TeamsRuntime,
    client: Client | ClientOptions | None = None,
) -> TeamsHttpService:
    """Build the FastAPI/Teams SDK composition without starting a server.

    ``App.initialize`` is deliberately called by FastAPI lifespan rather than
    at import or factory time. That keeps construction side-effect free and
    ensures readiness cannot open before the SDK installs its authenticated
    messaging route.

    ``client`` is the SDK's own outbound-HTTP seam, passed through to
    ``AppOptions.client`` unchanged: the SDK clones it into the ApiClient and
    ActivitySender, carrying any registered middlewares with it. Production
    leaves it None; tests use it to intercept Bot Framework calls.
    """
    readiness = _Readiness()
    holder: dict[str, tuple[App, TeamsApp]] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        teams_app, turns = holder["app"]
        await teams_app.initialize()
        # Spawned, not awaited: a slow Teams API or MA reconcile must not
        # delay readiness. Turns wait for the sweep on their own.
        turns.start()
        readiness.initialized = True
        try:
            yield
        finally:
            readiness.initialized = False
            await turns.drain(timeout=15.0)
            await teams_app.stop()

    async def _healthz() -> dict[str, str]:
        return {"status": "live"}

    async def _readyz() -> JSONResponse:
        if readiness.initialized:
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "not_ready"}, status_code=503)

    fastapi_app = FastAPI(
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    fastapi_app.add_middleware(_RequestBodyLimitMiddleware)
    # Starlette applies the last-added middleware first. The capability gate
    # therefore rejects disabled ingress before buffering a request body or
    # entering Microsoft SDK authentication/dispatch.
    fastapi_app.add_middleware(_IngressGateMiddleware, enabled=settings.enabled)
    fastapi_app.add_api_route("/healthz", _healthz, methods=["GET"])
    fastapi_app.add_api_route("/readyz", _readyz, methods=["GET"])

    http_adapter = FastAPIAdapter(app=fastapi_app)
    teams_app = App(
        client_id=settings.client_id,
        client_secret=settings.client_secret.get_secret_value(),
        tenant_id=settings.tenant_id,
        http_server_adapter=http_adapter,
        client=client,
        # JWT validation stays on by default: the SDK reads its own
        # DANGEROUSLY_ALLOW_UNAUTHENTICATED_REQUESTS env var when the option
        # is left unset, which is the supported way to exercise real ingress
        # in local development and tests.
    )

    turns = TeamsApp(runtime=runtime, sender=teams_app)

    async def handle_feedback(ctx: ActivityContext[MessageSubmitActionInvokeActivity]) -> None:
        try:
            await record_feedback(
                runtime.sessionmaker, ctx.activity, configured_tenant=settings.tenant_id
            )
        except SQLAlchemyError:
            log.exception("teams.feedback.failed")

    teams_app.on_message(turns.handle_message)
    teams_app.on_card_action_execute(turns.handle_card_action)
    teams_app.on_message_submit_feedback(handle_feedback)
    holder["app"] = (teams_app, turns)
    return TeamsHttpService(
        app=fastapi_app,
        teams_app=teams_app,
        http_adapter=http_adapter,
        runtime=runtime,
        turns=turns,
        _readiness=readiness,
    )
