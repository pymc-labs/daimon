"""SDK-owned FastAPI ingress and health lifecycle for Microsoft Teams."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import structlog
from daimon.adapters.teams import (
    billing_panel,
    card,
    credential_requests,
    privacy_card,
    routines_card,
    setup_card,
)
from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.billing_panel import BillingPanel
from daimon.adapters.teams.commands import CommandHandler
from daimon.adapters.teams.feedback import record_feedback
from daimon.adapters.teams.help import send_help
from daimon.adapters.teams.memory import show_memory
from daimon.adapters.teams.privacy_panel import PrivacyPanel
from daimon.adapters.teams.routines_panel import RoutinesPanel
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.adapters.teams.setup_conversation import new_command
from daimon.adapters.teams.setup_panel import SetupPanel
from daimon.core.config import TeamsSettings
from daimon.core.posted_controls.teams_card import CREDENTIAL_DIALOG
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from microsoft_teams.api import MessageSubmitActionInvokeActivity
from microsoft_teams.apps import ActivityContext, App, FastAPIAdapter
from microsoft_teams.common import Client, ClientOptions
from sqlalchemy.exc import SQLAlchemyError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_TEAMS_HTTP_BODY_BYTES = 64 * 1024
log = structlog.get_logger(__name__)


class _IngressGuard:
    """Guards `/api/messages` before SDK parsing or auth: 503 while disabled, 413 when oversize."""

    def __init__(self, app: ASGIApp, *, enabled: bool) -> None:
        self._app = app
        self._enabled = enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != "/api/messages":
            await self._app(scope, receive, send)
            return
        if not self._enabled:
            response = JSONResponse({"error": "Teams ingress disabled"}, status_code=503)
            await response(scope, receive, send)
            return
        messages: list[Message] = []
        size = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > MAX_TEAMS_HTTP_BODY_BYTES:
                response = JSONResponse({"error": "Request too large"}, status_code=413)
                await response(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        buffered = iter(messages)

        async def replay() -> Message:
            return next(buffered, {"type": "http.disconnect"})

        await self._app(scope, replay, send)


@dataclass(frozen=True)
class TeamsHttpService:
    """One Teams HTTP process: the SDK owns `/api/messages`, this owns lifecycle and health."""

    app: FastAPI
    teams_app: App
    http_adapter: FastAPIAdapter
    runtime: TeamsRuntime
    turns: TeamsApp
    _ready: asyncio.Event

    @property
    def ready(self) -> bool:
        """Whether SDK initialization completed inside the active lifespan."""
        return self._ready.is_set()


def create_teams_http_service(
    *,
    settings: TeamsSettings,
    runtime: TeamsRuntime,
    client: Client | ClientOptions | None = None,
) -> TeamsHttpService:
    """Build the FastAPI/Teams SDK composition without starting a server.

    The lifespan calls `App.initialize`, so construction has no side effects and
    readiness cannot open before the SDK installs its authenticated route.
    `client` is the SDK's outbound-HTTP seam (`AppOptions.client`); tests pass one
    to intercept Bot Framework calls.
    """
    ready = asyncio.Event()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await teams_app.initialize()
        # Spawned, not awaited: a slow Teams API or MA reconcile must not
        # delay readiness. Turns wait for the sweep on their own.
        turns.start()
        ready.set()
        try:
            yield
        finally:
            ready.clear()
            await turns.drain(timeout=15.0)
            await teams_app.stop()

    async def _healthz() -> dict[str, str]:
        return {"status": "live"}

    async def _readyz() -> JSONResponse:
        if ready.is_set():
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "not_ready"}, status_code=503)

    fastapi_app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    fastapi_app.add_middleware(_IngressGuard, enabled=settings.enabled)
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

    async def bot_token() -> str | None:
        token = await teams_app.token_provider.get_app_token()
        return str(token) if token is not None else None

    routines = RoutinesPanel(runtime)
    privacy = PrivacyPanel(runtime)
    billing = BillingPanel(runtime)
    setup = SetupPanel(runtime)
    commands: dict[str, CommandHandler] = {
        "new": new_command,
        "setup": setup.command,
        "routines": routines.command,
        "memory": show_memory,
        "privacy": privacy.command,
        "billing": billing.command,
    }
    commands["help"] = functools.partial(send_help, names=(*commands, "help"))
    turns = TeamsApp(runtime=runtime, sender=teams_app, commands=commands, bot_token=bot_token)

    async def handle_feedback(ctx: ActivityContext[MessageSubmitActionInvokeActivity]) -> None:
        try:
            await record_feedback(
                runtime.sessionmaker, ctx.activity, configured_tenant=settings.tenant_id
            )
        except SQLAlchemyError:
            log.exception("teams.feedback.failed")

    teams_app.on_message(turns.handle_message)
    teams_app.on_card_action_execute(card.CANCEL_VERB, turns.handle_cancel)
    teams_app.on_card_action_execute(routines_card.VERB, routines.on_action)
    teams_app.on_card_action_execute(privacy_card.VERB, privacy.on_action)
    teams_app.on_card_action_execute(billing_panel.VERB, billing.on_action)
    teams_app.on_dialog_open(routines_card.CREATE_DIALOG, routines.on_dialog_open)
    teams_app.on_dialog_submit(routines_card.CREATE_DIALOG, routines.on_dialog_submit)
    teams_app.on_card_action_execute(setup_card.VERB, setup.on_action)
    teams_app.on_dialog_open(setup_card.CREATE_DIALOG, setup.on_create_open)
    teams_app.on_dialog_submit(setup_card.CREATE_DIALOG, setup.on_create_submit)
    teams_app.on_dialog_open(setup_card.TOKEN_DIALOG, setup.on_token_open)
    teams_app.on_dialog_submit(setup_card.TOKEN_DIALOG, setup.on_token_submit)
    teams_app.on_dialog_open(CREDENTIAL_DIALOG, turns.credentials.on_dialog_open)
    teams_app.on_dialog_submit(credential_requests.SUBMIT, turns.credentials.on_dialog_submit)
    teams_app.on_message_submit_feedback(handle_feedback)
    teams_app.on_file_consent(turns.outputs.handle_consent)
    return TeamsHttpService(
        app=fastapi_app,
        teams_app=teams_app,
        http_adapter=http_adapter,
        runtime=runtime,
        turns=turns,
        _ready=ready,
    )
