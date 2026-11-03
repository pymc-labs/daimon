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
    provider_actions,
    routines_card,
    setup_card,
    thread_handoff,
    tool_confirmation,
    wizard,
)
from daimon.adapters.teams.activity_feed import ActivityFeed, ChannelHomes
from daimon.adapters.teams.app import DRAIN_GRACE_S, TeamsApp
from daimon.adapters.teams.billing_panel import BillingPanel
from daimon.adapters.teams.channel_files import ChannelFiles
from daimon.adapters.teams.channel_settings import ChannelSettingsDialog
from daimon.adapters.teams.channel_settings_card import CHANNEL_DIALOG
from daimon.adapters.teams.channel_subscriptions import (
    LIFECYCLE_PATH,
    NOTIFICATIONS_PATH,
    ChannelSubscriptions,
    notification_route,
)
from daimon.adapters.teams.commands import CommandHandler
from daimon.adapters.teams.deployment_admins import demoting_owner_reads
from daimon.adapters.teams.direct_chats import SdkDirectChats
from daimon.adapters.teams.feedback import FEEDBACK_DIALOG, TeamsFeedback
from daimon.adapters.teams.help import send_help
from daimon.adapters.teams.here import HereApprovals, show_here
from daimon.adapters.teams.identity import GraphSource
from daimon.adapters.teams.installations import TeamInstalls
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS, TimedSender
from daimon.adapters.teams.memory import show_memory
from daimon.adapters.teams.privacy_panel import PrivacyPanel
from daimon.adapters.teams.reactions import ReactionTarget
from daimon.adapters.teams.restart_catch_up import RestartCatchUp
from daimon.adapters.teams.routine_delivery import make_teams_routine_poster
from daimon.adapters.teams.routines_panel import RoutinesPanel
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.adapters.teams.setup_conversation import new_command
from daimon.adapters.teams.setup_panel import SetupPanel
from daimon.adapters.teams.support import VERB as SUPPORT_VERB
from daimon.adapters.teams.support import SupportCommand
from daimon.adapters.teams.support import enabled as support_enabled
from daimon.adapters.teams.team_owners import fetch_team_owner_ids
from daimon.adapters.teams.thread_handoff import TeamsThreadHandoff
from daimon.adapters.teams.thread_reader import ThreadReader
from daimon.adapters.teams.wizard import TeamsWizards
from daimon.core.config import TeamsSettings
from daimon.core.logging_setup import mark_shutting_down
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.posted_controls.teams_card import CREDENTIAL_DIALOG
from daimon.core.stores.domain import TeamsChannelSubscriptionRow, TeamsInstallationRow
from daimon.core.stores.teams_channel_subscriptions import get_teams_channel_subscription
from daimon.core.stores.teams_installations import list_teams_installations
from daimon.core.teams_bot_framework import SERVICE_URL, retry_throttled
from daimon.core.teams_graph import GRAPH_SCOPE, GraphClient, GraphMessage, TeamGroups
from daimon.core.teams_sharepoint import SharePoint
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from microsoft_teams.api import UninstalledActivity
from microsoft_teams.api.auth.cloud_environment import PUBLIC
from microsoft_teams.apps import ActivityContext, App, FastAPIAdapter
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
        # The socket binds right after startup: from here Bot Framework's activities are its own.
        turns.begin_serving()
        try:
            yield
        finally:
            mark_shutting_down()
            ready.clear()
            await turns.drain(timeout=DRAIN_GRACE_S)
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
        # Standard channels only: the settings picker leaves private and shared ones out.
        standard = (c for c in channels if c.id and c.type in (None, "standard"))
        return {channel.id: channel.name for channel in standard if channel.id}

    graph, groups = GraphClient(runtime.http_client, graph_token), TeamGroups(team_group)
    owner_reads = demoting_owner_reads(
        runtime,
        # A runtime that brings its own owner read (tests) keeps it, still demoting.
        runtime.team_owners or functools.partial(fetch_team_owner_ids, graph),
        # `spawn` is bound below, before any read: the drain waits for a demotion.
        in_background=lambda coro: spawn(coro, name="teams.admin_demotion"),
    )
    runtime = dataclasses.replace(runtime, team_owners=owner_reads)

    async def channel_type(team_id: str, channel_id: str) -> str | None:
        api = teams_app.api.from_service_url(SERVICE_URL)
        channels = await api.teams.get_conversations(team_id)
        return next((channel.type for channel in channels if channel.id == channel_id), None)

    teams_tenant = derive_tenant_uuid(platform="teams", workspace_id=settings.tenant_id)

    installs = TeamInstalls(
        runtime.sessionmaker,
        groups,
        tenant_id=teams_tenant,
        entra_tenant_id=settings.tenant_id,
        alert_url=runtime.settings.ops.alert_webhook_url,
    )

    files = ChannelFiles(SharePoint(graph), groups)
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
    setup = SetupPanel(runtime, team_channels=billing_panel.sdk_team_channels(teams_app))
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
    subscriptions = None
    if runtime.settings.private_channel_messages.enabled and settings.public_url is not None:

        async def deliver(
            message: GraphMessage, channel: TeamsChannelSubscriptionRow, *, mentions: bool = False
        ) -> None:
            await turns.handle_graph_message(message, channel, mentions=mentions)  # Built below.

        subscriptions = ChannelSubscriptions(
            sessionmaker=runtime.sessionmaker,
            graph=graph,
            groups=groups,
            channel_type=channel_type,
            deliver=deliver,
            spawn=spawn,
            tenant_id=teams_tenant,
            public_url=str(settings.public_url),
        )
    if support is not None:
        commands["support"] = support.command

    async def react(target: ReactionTarget, reaction: str, *, add: bool) -> None:
        # The SDK's own client: the bot token, and only the activity's service URL.
        api = teams_app.api.from_service_url(target.service_url or SERVICE_URL).conversations
        change = api.add_reaction if add else api.delete_reaction
        await change(target.conversation_id, target.activity_id, reaction)

    async def installed_teams() -> list[TeamsInstallationRow]:
        async with runtime.sessionmaker() as session:
            return await list_teams_installations(session, tenant_id=teams_tenant)

    async def stored_group(channel_id: str) -> str | None:
        # A private or shared channel may be missing from the bot's channel list:
        # its message subscription records the team.
        async with runtime.sessionmaker() as session:
            watched = await get_teams_channel_subscription(
                session, tenant_id=teams_tenant, channel_id=channel_id
            )
        return None if watched is None else watched.group_id

    homes = ChannelHomes(
        installed_teams, billing_panel.sdk_team_channels(teams_app), stored_group=stored_group
    )
    feed = ActivityFeed(graph, homes, entra_tenant_id=settings.tenant_id, spawn=spawn)

    async def catch_up_messages(source: GraphSource, messages: list[GraphMessage]) -> None:
        await turns.catch_up_graph_messages(source, messages)  # Built below.

    catch_up = RestartCatchUp(
        sessionmaker=runtime.sessionmaker,
        tenant_id=teams_tenant,
        graph=graph,
        list_teams=installed_teams,
        standard_channels=channel_names,
        deliver=catch_up_messages,
        serving_from=lambda: turns.serving_from(),  # Built below.
        bot_app_id=settings.client_id,
        should_stop=lambda: turns.draining,
        service_url=SERVICE_URL,
    )
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
        subscriptions=subscriptions,
        catch_up=catch_up,
        react=react,
        feed=feed,
        routine_poster=make_teams_routine_poster(
            runtime.sessionmaker,
            direct,
            tenant_id=installs.tenant_id,
            dm_policies=runtime.settings.direct_message_policies,
            feed=feed,
        ),
    )

    feedback = TeamsFeedback(runtime, direct, spawn=spawn)
    teams_app.on_message(turns.handle_message)
    teams_app.on_install_add(installs.on_install)
    if subscriptions is not None:
        watched = subscriptions

        async def uninstalled(ctx: ActivityContext[UninstalledActivity]) -> None:
            await watched.on_uninstall(ctx, ours=installs.is_ours(ctx.activity))

        # First, so the installs handler (which ends the chain) still runs after it.
        teams_app.on_install_remove(uninstalled)
        teams_app.on_conversation_update(watched.on_conversation_update)
        for path, lifecycle in ((NOTIFICATIONS_PATH, False), (LIFECYCLE_PATH, True)):
            route = notification_route(
                watched, lifecycle=lifecycle, draining=lambda: turns.draining
            )
            fastapi_app.add_api_route(path, route, methods=["POST"])
    teams_app.on_install_remove(installs.on_uninstall)
    teams_app.on_card_action_execute(card.CANCEL_VERB, turns.handle_cancel)
    teams_app.on_card_action_execute(routines_card.VERB, routines.on_action)
    teams_app.on_card_action_execute(privacy_card.VERB, privacy.on_action)
    teams_app.on_card_action_execute("here_approvals", HereApprovals(runtime).on_action)
    teams_app.on_card_action_execute(billing_panel.VERB, billing.on_action)
    teams_app.on_card_action_execute(tool_confirmation.VERB, turns.confirmations.on_action)
    teams_app.on_card_action_execute(provider_actions.VERB, turns.provider_actions.on_action)
    handover = TeamsThreadHandoff(
        runtime,
        dispatch=turns._dispatch_inbound,  # pyright: ignore[reportPrivateUsage]  # verified message ingress
        reader=reader,
    )
    teams_app.on_card_action_execute(thread_handoff.VERB, handover.on_action)
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
    teams_app.on_dialog_open(card.VOTE_UP_DIALOG, feedback.on_up)
    teams_app.on_dialog_open(card.VOTE_DOWN_DIALOG, feedback.on_down)
    teams_app.on_message_fetch_task(feedback.on_fetch)
    teams_app.on_dialog_submit(FEEDBACK_DIALOG, feedback.on_submit)
    teams_app.on_message_submit_feedback(feedback.on_builtin)
    teams_app.on_file_consent(turns.outputs.handle_consent)
    return TeamsHttpService(
        app=fastapi_app,
        teams_app=teams_app,
        http_adapter=http_adapter,
        runtime=runtime,
        turns=turns,
        _ready=ready,
    )
