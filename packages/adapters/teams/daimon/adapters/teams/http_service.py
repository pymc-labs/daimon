"""SDK-owned FastAPI ingress and health lifecycle for Microsoft Teams."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
import structlog
from daimon.adapters.teams import (
    billing_panel,
    card,
    credential_requests,
    privacy_card,
    routines_card,
    setup_card,
    thread_handoff,
    tool_confirmation,
    wizard,
)
from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.billing_panel import BillingPanel
from daimon.adapters.teams.channel_admin_groups import fetch_team_owner_ids
from daimon.adapters.teams.channel_files import ChannelFiles
from daimon.adapters.teams.channel_settings import ChannelSettingsDialog
from daimon.adapters.teams.channel_settings_card import CHANNEL_DIALOG
from daimon.adapters.teams.commands import CommandHandler
from daimon.adapters.teams.direct_chats import SdkDirectChats
from daimon.adapters.teams.externals import ExternalParticipants, MemberFacts
from daimon.adapters.teams.feedback import FEEDBACK_DIALOG, TeamsFeedback
from daimon.adapters.teams.help import send_help
from daimon.adapters.teams.here import show_here
from daimon.adapters.teams.installations import TeamInstalls
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS, TimedSender
from daimon.adapters.teams.memory import show_memory
from daimon.adapters.teams.privacy_panel import PrivacyPanel
from daimon.adapters.teams.routine_delivery import make_teams_routine_poster
from daimon.adapters.teams.routines_panel import RoutinesPanel
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.adapters.teams.setup_conversation import new_command
from daimon.adapters.teams.setup_panel import SetupPanel
from daimon.adapters.teams.site_grant import (
    CALLBACK_PATH,
    GrantedSite,
    GrantTarget,
    callback_route,
)
from daimon.adapters.teams.support import VERB as SUPPORT_VERB
from daimon.adapters.teams.support import SupportCommand
from daimon.adapters.teams.support import enabled as support_enabled
from daimon.adapters.teams.thread_handoff import TeamsThreadHandoff
from daimon.adapters.teams.thread_reader import ThreadReader
from daimon.adapters.teams.wizard import TeamsWizards
from daimon.core.config import TeamsSettings
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.posted_controls.teams_card import CREDENTIAL_DIALOG
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.domain import TeamsChannelSiteRow
from daimon.core.stores.teams_channel_sites import (
    get_teams_channel_site,
    upsert_teams_channel_site,
)
from daimon.core.teams_bot_framework import SERVICE_URL, retry_throttled
from daimon.core.teams_graph import GRAPH_SCOPE, GraphClient, TeamGroups
from daimon.core.teams_sharepoint import SharePoint
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from microsoft_teams.api.auth.cloud_environment import PUBLIC
from microsoft_teams.apps import App, FastAPIAdapter
from microsoft_teams.common import Client, ClientOptions
from microsoft_teams.common.http import MiddlewareContext, MiddlewareNext
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_TEAMS_HTTP_BODY_BYTES = 64 * 1024
log = structlog.get_logger(__name__)


class _RetryThrottled:
    """SDK HTTP middleware: every Bot Framework call gets one retry after a 429."""

    async def send(self, context: MiddlewareContext, next: MiddlewareNext) -> httpx.Response:
        return await retry_throttled(next)


def bot_client(client: Client | ClientOptions | None = None) -> Client:
    """The SDK's HTTP client, with the throttle retry after any middleware it already has."""
    built = client.clone() if isinstance(client, Client) else Client(client or ClientOptions())
    built.use(_RetryThrottled())
    return built


def _foreign_bearer(scope: Scope) -> bool:
    """A bearer token some issuer other than Bot Framework signed.

    The SDK also accepts Entra tokens (for Agent ID bots), checked against the
    token's own tenant and without the `serviceurl` claim. A bot never gets one.
    """
    header = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
    token = header.removeprefix("Bearer ")
    if not token:
        return False
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.InvalidTokenError:
        return False  # The SDK rejects it.
    return claims.get("iss") != PUBLIC.token_issuer


class _IngressGuard:
    """Guards `/api/messages` before SDK parsing or auth.

    503 while disabled, 401 for a non-Bot-Framework token, 413 when oversize.
    """

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
        if _foreign_bearer(scope):
            await JSONResponse({"error": "Unauthorized"}, status_code=401)(scope, receive, send)
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
        client=bot_client(client),
        # Pinned so stray SERVICE_URL or CLOUD env vars cannot split this process
        # from the MCP server, which posts to the same hosts.
        service_url=SERVICE_URL,
        cloud=PUBLIC,
        # JWT validation stays on by default: the SDK reads its own
        # DANGEROUSLY_ALLOW_UNAUTHENTICATED_REQUESTS env var when the option
        # is left unset, which is the supported way to exercise real ingress
        # in local development and tests.
    )

    async def bot_token() -> str | None:
        token = await teams_app.token_provider.get_app_token()
        return str(token) if token is not None else None

    async def graph_token() -> str | None:
        # MSAL caches app tokens per scope, so this is a network call about hourly.
        token = await teams_app.token_provider.get_app_token(GRAPH_SCOPE, settings.tenant_id)
        return str(token) if token is not None else None

    async def team_group(team_id: str) -> str | None:
        try:
            details = await teams_app.api.from_service_url(SERVICE_URL).teams.get_by_id(team_id)
        except TEAMS_SEND_ERRORS as err:
            log.warning("teams.team_lookup.failed", error=type(err).__name__)
            return None
        return details.aad_group_id

    async def channel_names(team_id: str) -> dict[str, str | None]:
        try:
            teams = teams_app.api.from_service_url(SERVICE_URL).teams
            channels = await teams.get_conversations(team_id)
        except TEAMS_SEND_ERRORS as err:
            log.warning("teams.channel_lookup.failed", error=type(err).__name__)
            return {}
        # Private and shared channels keep files in a site of their own: none here.
        standard = (c for c in channels if c.id and c.type in (None, "standard"))
        return {channel.id: channel.name for channel in standard if channel.id}

    graph, groups = GraphClient(runtime.http_client, graph_token), TeamGroups(team_group)
    runtime = dataclasses.replace(
        runtime, team_owners=functools.partial(fetch_team_owner_ids, graph)
    )

    async def roster_member(conversation_id: str, aad_object_id: str) -> MemberFacts | None:
        conversations = teams_app.api.from_service_url(SERVICE_URL).conversations
        try:
            member = await conversations.get_member_by_id(conversation_id, aad_object_id)
        except httpx.HTTPStatusError as err:
            if err.response.status_code in (403, 404):
                return None
            raise
        if (member.aad_object_id or "").lower() != aad_object_id.lower():
            return None
        guest = None if member.user_role is None else member.user_role.lower() == "guest"
        return MemberFacts(tenant_id=member.tenant_id, is_guest=guest)

    async def channel_type(team_id: str, channel_id: str) -> str | None:
        api = teams_app.api.from_service_url(SERVICE_URL)
        channels = await api.teams.get_conversations(team_id)
        return next((channel.type for channel in channels if channel.id == channel_id), None)

    async def channel_members(
        team_id: str | None, team_group_id: str | None, channel_id: str
    ) -> dict[str, MemberFacts]:
        group = await groups.group_id(team_id, known=team_group_id)
        return {
            member.user_id.lower(): MemberFacts(member.tenant_id, "guest" in member.roles)
            for member in await graph.list_channel_members(group, channel_id)
            if member.user_id
        }

    teams_tenant = derive_tenant_uuid(platform="teams", workspace_id=settings.tenant_id)

    async def member_guests() -> frozenset[str]:
        async with runtime.sessionmaker() as session:
            policy = await load_access_policy(session, tenant_id=teams_tenant)
        return frozenset(policy.member_guest_ids)

    runtime = dataclasses.replace(
        runtime,
        externals=ExternalParticipants(
            tenant_id=settings.tenant_id,
            roster_member=roster_member,
            channel_type=channel_type,
            channel_members=channel_members,
            member_guests=member_guests,
            restrict_guests=settings.restrict_guests,
            restrict_external=settings.restrict_external_participants,
        ),
    )
    installs = TeamInstalls(
        runtime.sessionmaker,
        groups,
        tenant_id=teams_tenant,
        entra_tenant_id=settings.tenant_id,
        alert_url=runtime.settings.ops.alert_webhook_url,
    )

    async def stored_site(channel_id: str) -> TeamsChannelSiteRow | None:
        async with runtime.sessionmaker() as session:
            return await get_teams_channel_site(
                session, tenant_id=teams_tenant, channel_id=channel_id
            )

    files = ChannelFiles(
        SharePoint(graph, runtime.http_client), groups, channel_names, stored_site=stored_site
    )
    reader = ThreadReader(graph, groups, bot_app_id=settings.client_id, files=files)
    routines = RoutinesPanel(runtime)

    def spawn(coro: Coroutine[Any, Any, None], *, name: str) -> asyncio.Task[None]:
        return turns.spawn(coro, name=name)  # Built below; its drain waits for the task.

    privacy = PrivacyPanel(runtime, spawn=spawn)
    billing = BillingPanel(
        runtime,
        roster_name=billing_panel.sdk_roster_name(teams_app),
        team_channels=billing_panel.sdk_team_channels(teams_app),
    )
    setup = SetupPanel(runtime)
    channel_settings = ChannelSettingsDialog(runtime, channel_names=channel_names)
    commands: dict[str, CommandHandler] = {
        "new": new_command,
        "setup": setup.command,
        "here": show_here,
        "routines": routines.command,
        "memory": show_memory,
        "privacy": privacy.command,
        "billing": billing.command,
    }
    direct = SdkDirectChats(teams_app, TimedSender(teams_app), entra_tenant_id=settings.tenant_id)
    support = (
        SupportCommand(runtime, direct, spawn=spawn) if support_enabled(runtime.settings) else None
    )
    if support is not None:
        commands["support"] = support.command
    commands["help"] = functools.partial(send_help, names=(*commands, "help"))
    turns = TeamsApp(
        runtime=runtime,
        sender=teams_app,
        commands=commands,
        bot_token=bot_token,
        reader=reader,
        channel_files=files,
        installs=installs,
        direct=direct,
        ask_human=support is not None,
        routine_poster=make_teams_routine_poster(
            runtime.sessionmaker,
            direct,
            tenant_id=installs.tenant_id,
            dm_policies=runtime.settings.direct_message_policies,
        ),
    )

    feedback = TeamsFeedback(runtime, direct, spawn=spawn)
    teams_app.on_message(turns.handle_message)
    teams_app.on_install_add(installs.on_install)
    teams_app.on_install_remove(installs.on_uninstall)
    teams_app.on_card_action_execute(card.CANCEL_VERB, turns.handle_cancel)
    teams_app.on_card_action_execute(routines_card.VERB, routines.on_action)
    teams_app.on_card_action_execute(privacy_card.VERB, privacy.on_action)
    teams_app.on_card_action_execute(billing_panel.VERB, billing.on_action)
    teams_app.on_card_action_execute(tool_confirmation.VERB, turns.confirmations.on_action)
    teams_app.on_card_action_execute(thread_handoff.VERB, TeamsThreadHandoff(runtime).on_action)
    teams_app.on_dialog_open(routines_card.CREATE_DIALOG, routines.on_dialog_open)
    teams_app.on_dialog_submit(routines_card.CREATE_DIALOG, routines.on_dialog_submit)
    teams_app.on_card_action_execute(setup_card.VERB, setup.on_action)
    wizards = TeamsWizards(
        runtime, start_turn=turns.start_wizard_turn, draining=lambda: turns.draining
    )
    teams_app.on_card_action_execute(wizard.VERB, wizards.on_action)
    if support is not None:
        teams_app.on_card_action_execute(SUPPORT_VERB, support.on_action)
        teams_app.on_dialog_open(card.ASK_HUMAN_DIALOG, support.on_ask_open)
        teams_app.on_dialog_submit(card.ASK_HUMAN_DIALOG, support.on_ask_submit)
    teams_app.on_dialog_open(setup_card.CREATE_DIALOG, setup.on_create_open)
    teams_app.on_dialog_submit(setup_card.CREATE_DIALOG, setup.on_create_submit)
    teams_app.on_dialog_open(setup_card.TOKEN_DIALOG, setup.on_token_open)
    teams_app.on_dialog_submit(setup_card.TOKEN_DIALOG, setup.on_token_submit)
    teams_app.on_dialog_open(setup_card.OPERATOR_DIALOG, setup.on_operator_open)
    teams_app.on_dialog_submit(setup_card.OPERATOR_DIALOG, setup.on_operator_submit)
    teams_app.on_dialog_open(setup_card.SKILL_DIALOG, setup.on_skill_open)
    teams_app.on_dialog_submit(setup_card.SKILL_DIALOG, setup.on_skill_submit)
    teams_app.on_dialog_open(CHANNEL_DIALOG, channel_settings.on_open)
    teams_app.on_dialog_submit(CHANNEL_DIALOG, channel_settings.on_submit)
    teams_app.on_dialog_open(CREDENTIAL_DIALOG, turns.credentials.on_dialog_open)
    teams_app.on_dialog_submit(credential_requests.SUBMIT, turns.credentials.on_dialog_submit)
    teams_app.on_message_fetch_task(feedback.on_fetch)
    teams_app.on_dialog_submit(FEEDBACK_DIALOG, feedback.on_submit)
    teams_app.on_message_submit_feedback(feedback.on_builtin)
    teams_app.on_file_consent(turns.outputs.handle_consent)
    if settings.public_url is not None:

        async def files_granted(target: GrantTarget, site: GrantedSite) -> None:
            async with runtime.sessionmaker.begin() as session:
                await upsert_teams_channel_site(
                    session,
                    tenant_id=teams_tenant,
                    channel_id=target.channel_id,
                    group_id=target.group_id,
                    site_id=site.site_id,
                    drive_id=site.folder.drive_id,
                    folder_id=site.folder.item_id,
                )
            files.forget(target.group_id)

        grant = callback_route(settings, runtime.http_client, on_granted=files_granted)
        fastapi_app.add_api_route(CALLBACK_PATH, grant, methods=["GET"])
    return TeamsHttpService(
        app=fastapi_app,
        teams_app=teams_app,
        http_adapter=http_adapter,
        runtime=runtime,
        turns=turns,
        _ready=ready,
    )
