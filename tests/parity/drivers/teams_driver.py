"""TeamsDriver -- the Teams half of the PlatformDriver Protocol.

Every call starts the real Teams HTTP service over a `TeamsRuntime` and POSTs
Bot Framework activities to `/api/messages`. Outbound Bot Framework calls land
in `_TeamsApiFake` (after the adapter's `tests/conftest.py`, not importable
here); the credential card is posted by the MCP tool through `TeamsBotClient`.

Ids: the workspace is the Entra tenant and a user its object id (both UUIDs);
a channel is `19:…@thread.tacv2` and a thread `…;messageid=<root>`. A channel
id without a root starts a new thread, the way a new post does.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from cryptography.fernet import Fernet
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.agents import (
    _archive_agent_impl,  # pyright: ignore[reportPrivateUsage]
    _fork_agent_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.credential_requests import (
    _request_agent_key_impl,  # pyright: ignore[reportPrivateUsage]
    _request_mcp_token_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient
from daimon.adapters.teams.credential_requests import SUBMIT
from daimon.adapters.teams.http_service import create_teams_http_service
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings, TeamsSettings
from daimon.core.continuity.dispatch import dispatch_pending_continuations
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.posted_controls import CardKind
from daimon.core.posted_controls.teams_card import CREDENTIAL_DIALOG
from daimon.core.purge import AccountPurgeResult
from daimon.core.purge import purge_account as core_purge_account
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.stores.turn_origins import create_origin
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.core.turn.deps import build_turn_deps
from daimon.testing import ma_session
from daimon.testing.asgi import asgi_lifespan
from daimon.testing.ma import MARouter, build_fake_anthropic
from microsoft_teams.apps.token_manager import TokenManager
from microsoft_teams.common import Client, ClientOptions
from microsoft_teams.common.http.client import MiddlewareContext
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .cards import CapturedCard, read_teams_card, walk_components
from .protocol import PanelAction, parity_account_id, pin_agent
from .views import CapturedView, normalize_line, read_teams_view

_CLIENT_ID = "parity-bot"
_BOT_ID = f"28:{_CLIENT_ID}"
_CARD_TYPE = "application/vnd.microsoft.card.adaptive"
_TURN_DEFAULT = DeploymentDefault(agent_name="test-agent", environment_name="test-env")

#: The root post a card's origin thread hangs off, and the id Teams hands back
#: for the posted card.
_ROOT_MESSAGE_ID = "1700000000100"
_POSTED_MESSAGE_ID = "1700000000200"

#: The install the agent-lifecycle tools act as (see `SlackDriver`).
_LIFECYCLE_TENANT = str(uuid.UUID(int=0xA11CE))
_LIFECYCLE_USER = str(uuid.UUID(int=0xB0B))

_BALANCE_BLOCKED_TEXT = (
    "This organisation's credit is depleted. An admin can top up with `billing` in a 1:1 "
    "chat with me."
)
_CAP_BLOCKED_TEXT = "Monthly usage cap reached for this organisation. Ask an admin to adjust it."

#: The label each `PanelAction` wears on the Teams card, once emoji are stripped.
#: The Details lists have no Show more on Teams (test_teams_deliberate_gaps.py).
_PANEL_LABELS: dict[str, str] = {
    "details": "Details",
    "who_answers_where": "Who answers where",
    "next_page": "Next",
    "prev_page": "Previous",
    "new_agent": "New agent",
    "back": "Back",
}


@dataclass
class _TeamsApiFake:
    """SDK middleware answering every Bot Framework call: POST mints `m-<n>`, PUT echoes."""

    #: (method, body, activity id) of every post and edit, in order.
    activities: list[tuple[str, dict[str, Any], str]] = field(
        default_factory=list[tuple[str, dict[str, Any], str]]
    )

    async def send(
        self, context: MiddlewareContext, next: Callable[[], Awaitable[httpx.Response]]
    ) -> httpx.Response:
        del next
        request = httpx.Request(context.method, context.url)
        path = httpx.URL(context.url).path
        if "/activities" not in path:
            return httpx.Response(200, json={}, request=request)
        update = re.search(r"/activities/([^/]+)$", path)
        activity_id = update.group(1) if update else f"m-{len(self.activities) + 1}"
        self.activities.append((context.method, cast(dict[str, Any], context.json), activity_id))
        return httpx.Response(200, json={"id": activity_id}, request=request)


def _cards_in(body: dict[str, Any]) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = body.get("attachments") or []
    return [a["content"] for a in attachments if a.get("contentType") == _CARD_TYPE]


def _actions(card: object) -> list[dict[str, Any]]:
    """Every action on a card, in render order."""
    return [c for c in walk_components(card) if str(c.get("type", "")).startswith("Action.")]


async def _exchange(
    runtime: TeamsRuntime, activity: dict[str, Any]
) -> tuple[dict[str, Any], _TeamsApiFake]:
    """POST one activity to the started service and drain the work it spawned.

    Ingress auth, MSAL, boot provisioning and timers are stubbed.
    """
    assert runtime.settings.teams is not None
    fake = _TeamsApiFake()
    client = Client(ClientOptions())
    client.use(fake)
    with (
        patch.dict(os.environ, {"DANGEROUSLY_ALLOW_UNAUTHENTICATED_REQUESTS": "true"}),
        patch.object(TokenManager, "get_bot_token", AsyncMock(return_value="parity-bot-token")),
        patch("daimon.adapters.teams.app.provision_configured_tenant", new_callable=AsyncMock),
        patch("daimon.adapters.teams.app.run_wake_poller", new_callable=AsyncMock),
    ):
        service = create_teams_http_service(
            settings=runtime.settings.teams, runtime=runtime, client=client
        )
        async with asgi_lifespan(service.app):
            await service.turns.start()
            transport = httpx.ASGITransport(app=service.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://parity") as http:
                response = await http.post("/api/messages", json=activity)
            await service.turns.drain(30.0)
    assert response.status_code == 200, f"{response.status_code}: {response.text}"
    return (response.json() if response.content else {}), fake


def _activity(
    *, tenant: str, user: str, conversation: str, text: str | None = None
) -> dict[str, Any]:
    """A message (or, with `text=None`, an invoke's shell) from `user` in `conversation`."""
    channel = conversation.startswith("19:")
    channel_data: dict[str, Any] = {"tenant": {"id": tenant}}
    if channel:
        channel_data |= {
            "channel": {"id": conversation.split(";", 1)[0]},
            "team": {"id": "19:parity-team@thread.tacv2"},
        }
    activity: dict[str, Any] = {
        "type": "message",
        "id": str(uuid.uuid4().int)[:13],
        "channelId": "msteams",
        "serviceUrl": "https://smba.trafficmanager.net/parity",
        "from": {"id": f"29:{user}", "aadObjectId": user},
        "recipient": {"id": _BOT_ID, "name": "daimon"},
        "conversation": {
            "id": conversation,
            "conversationType": "channel" if channel else "personal",
            "tenantId": tenant,
        },
        "channelData": channel_data,
    }
    if text is not None and channel:
        mention = "<at>daimon</at>"
        activity["text"] = f"{mention} {text}"
        activity["entities"] = [
            {"type": "mention", "text": mention, "mentioned": {"id": _BOT_ID, "name": "daimon"}}
        ]
    elif text is not None:
        activity["text"] = text
    return activity


def _invoke(name: str, value: dict[str, Any], *, reply_to: str, **where: str) -> dict[str, Any]:
    return _activity(**where) | {
        "type": "invoke",
        "name": name,
        "value": value,
        "replyToId": reply_to,
    }


@contextmanager
def unsuperseded_continuations() -> Iterator[None]:
    """Let a queued continuation run after the turn that triggers it.

    Teams stamps a chat's latest message on arrival, so the triggering turn would
    supersede it; Discord's scenarios stub its history read the same way.
    """

    async def _dispatch(*args: Any, **kwargs: Any) -> None:
        latest = AsyncMock(return_value=None)
        await dispatch_pending_continuations(*args, **kwargs | {"latest_user_message_at": latest})

    with patch("daimon.adapters.teams.app.dispatch_pending_continuations", _dispatch):
        yield


def _thread_of(channel_id: str) -> str:
    """The thread a card scenario's origin names: a root post in the channel."""
    return f"{channel_id};messageid={_ROOT_MESSAGE_ID}"


@dataclass
class TeamsDriver:
    """Drives Teams through `/api/messages`, the real Teams entry point."""

    param_id: str = "teams"
    _cards: list[CapturedCard] = field(default_factory=list[CapturedCard])
    _fernet_key: str = field(default_factory=lambda: Fernet.generate_key().decode())
    _views: list[CapturedView] = field(default_factory=list[CapturedView])
    #: The live panel: the card on screen, its message id, the open form.
    _panel_card: dict[str, Any] = field(default_factory=dict[str, Any])
    _panel_message_id: str = ""
    _panel_form: dict[str, Any] = field(default_factory=dict[str, Any])
    #: Teams reads a caller's role off `DAIMON_TEAMS__ADMIN_USER_IDS`.
    _panel_admins: tuple[str, ...] = ()
    _panel_aliases: dict[str, str] = field(default_factory=dict[str, str])

    def _runtime(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        *,
        entra_tenant: str,
        admins: tuple[str, ...] = (),
        billing_config: object | None = None,
        turn: bool = False,
    ) -> TeamsRuntime:
        """Real turn deps over a MagicMock `Settings`.

        A `turn` runs as the other drivers' do: no daimon-mcp, `test-agent` by default.
        """
        settings = MagicMock()
        settings.teams = TeamsSettings(
            client_id=_CLIENT_ID,
            client_secret=SecretStr("parity-secret"),
            tenant_id=entra_tenant,
            enabled=True,
            admin_user_ids=admins,
        )
        settings.crypto.keys = (SecretStr(self._fernet_key),)
        settings.mcp.public_url = None if turn else "https://mcp.example.com/mcp"
        settings.mcp.jwt_secret = None if turn else SecretStr("x" * 32)
        settings.mcp.app_root_url = None
        settings.github.fallback_pat = None
        settings.github.app_id = None
        settings.github.app_private_key = None
        settings.github.oauth_scopes = ()
        settings.defaults_root = MagicMock()
        settings.billing.markup = Decimal("1.0")
        settings.billing.signup_credit = Decimal("0")
        settings.tool_safety = OPEN_TOOL_SAFETY
        anthropic = build_fake_anthropic(router.dispatch)
        default = _TURN_DEFAULT if turn else DeploymentDefault()
        resolver_cache = new_resolver_cache()
        return TeamsRuntime(
            settings=settings,
            anthropic=anthropic,
            sessionmaker=sessionmaker,
            billing_config=billing_config,  # pyright: ignore[reportArgumentType]  # test-injected BillingConfig | None
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(404))
            ),
            resolver_cache=resolver_cache,
            turn_deps=build_turn_deps(
                settings,
                anthropic,
                sessionmaker,
                deployment_default=default,
                resolver_cache=resolver_cache,
                billing_config=billing_config,  # pyright: ignore[reportArgumentType]
            ),
            deployment_default=default,
        )

    async def dispatch_turn(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        tenant_id: uuid.UUID,
        workspace_id: str,
        channel_id: str,
        user_id: str,
        text: str,
        billing_config: object | None = None,
    ) -> list[str]:
        del tenant_id  # derived from the Entra tenant, as production does
        runtime = self._runtime(
            sessionmaker,
            router,
            entra_tenant=workspace_id,
            billing_config=billing_config,
            turn=True,
        )
        activity = _activity(tenant=workspace_id, user=user_id, conversation=channel_id, text=text)
        session = ma_session(
            id="sess_parity_test",
            agent_id="ag_parity_test",
            model="claude-sonnet-4-6",
            environment_id="env_parity_test",
        )
        with patch("daimon.core.turn.prepare.create_session", return_value=session):
            _response, fake = await _exchange(runtime, activity)
        return [
            body["text"] for _m, body, _id in fake.activities if isinstance(body.get("text"), str)
        ]

    def expected_blocked_text(self, kind: Literal["balance", "cap"]) -> str:
        return _BALANCE_BLOCKED_TEXT if kind == "balance" else _CAP_BLOCKED_TEXT

    def _mcp_runtime(
        self,
        router: MARouter,
        sessionmaker: async_sessionmaker[AsyncSession],
        teams_client: TeamsBotClient | None = None,
    ) -> McpRuntime:
        return McpRuntime(
            session_factory=sessionmaker,
            client=build_fake_anthropic(router.dispatch),
            settings=Settings(
                database=DatabaseSettings(url="postgresql+asyncpg://parity/parity"),  # pyright: ignore[reportArgumentType]  # pydantic coerces the DSN string
                anthropic=AnthropicSettings(api_key=SecretStr("parity")),
            ),
            deployment_default=DeploymentDefault(),
            fernet=build_multifernet((self._fernet_key,)),
            teams_client=teams_client,
        )

    def _auth(self, tenant_id: uuid.UUID, *, workspace_id: str, user_id: str) -> AuthIdentity:
        return AuthIdentity(
            account_id=parity_account_id(tenant_id, user_id),
            tenant_id=tenant_id,
            role=Role.ADMIN,
            platform="teams",
            external_id=workspace_id,
            platform_user_id=user_id,
            is_admin=True,
        )

    async def delete_agent(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        tenant_id: uuid.UUID,
        name: str,
    ) -> None:
        runtime = self._mcp_runtime(router, sessionmaker)
        auth = self._auth(tenant_id, workspace_id=_LIFECYCLE_TENANT, user_id=_LIFECYCLE_USER)
        pinned = await pin_agent(runtime.client, tenant_id=tenant_id, name=name)
        await _archive_agent_impl(runtime, auth, name=name, expected_ma_agent_id=pinned)

    async def fork_agent(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        tenant_id: uuid.UUID,
        source_name: str,
        new_name: str,
        account_id: uuid.UUID,
    ) -> None:
        del account_id  # the tool stamps the install's own account, not a caller's
        runtime = self._mcp_runtime(router, sessionmaker)
        auth = self._auth(tenant_id, workspace_id=_LIFECYCLE_TENANT, user_id=_LIFECYCLE_USER)
        pinned = await pin_agent(runtime.client, tenant_id=tenant_id, name=source_name)
        await _fork_agent_impl(
            runtime, auth, source_name=source_name, new_name=new_name, expected_ma_agent_id=pinned
        )

    async def purge_account(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        account_id: uuid.UUID,
    ) -> AccountPurgeResult:
        return await core_purge_account(
            sm=sessionmaker, account_id=account_id, anthropic=build_fake_anthropic(router.dispatch)
        )

    async def uninstall(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        workspace_id: str,
    ) -> None:
        raise NotImplementedError("removing the Teams app archives nothing on purpose")

    # -- posted-control lifecycle -------------------------------------------

    async def post_credential_card(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        tenant_id: uuid.UUID,
        workspace_id: str,
        channel_id: str,
        user_id: str,
        kind: CardKind,
        target: str,
        agent_name: str,
        mcp_server_url: str | None = None,
        branch: str | None = None,
        pending_task: str | None = None,
    ) -> str:
        posted: list[dict[str, Any]] = []

        def bot_framework(request: httpx.Request) -> httpx.Response:
            if request.url.host == "login.microsoftonline.com":
                return httpx.Response(200, json={"access_token": "parity", "expires_in": 3600})
            if "/members/" in request.url.path:
                return httpx.Response(200, json={"aadObjectId": user_id})
            if request.method == "POST":
                posted.append(json.loads(request.content))
            return httpx.Response(200, json={"id": _POSTED_MESSAGE_ID})

        client = TeamsBotClient(
            httpx.AsyncClient(transport=httpx.MockTransport(bot_framework)),
            client_id=_CLIENT_ID,
            client_secret="parity-secret",
            tenant_id=workspace_id,
        )
        runtime = self._mcp_runtime(router, sessionmaker, teams_client=client)
        auth = self._auth(tenant_id, workspace_id=workspace_id, user_id=user_id)
        agent_id = await pin_agent(runtime.client, tenant_id=tenant_id, name=agent_name)
        now = datetime.now(UTC)
        async with sessionmaker.begin() as session:
            origin = await create_origin(
                session,
                tenant_id=tenant_id,
                account_id=auth.account_id,
                platform="teams",
                parent_channel_id=channel_id,
                thread_id=_thread_of(channel_id),
                responder_ma_agent_id="ag_parity_responder",
                responder_name="Daimon",
                configuration_target_ma_agent_id=agent_id,
                configuration_target_name=agent_name,
                role=Role.ADMIN,
                expires_at=now + timedelta(minutes=30),
                now=now,
            )
        common: dict[str, Any] = {
            "agent_name": agent_name,
            "channel_id": channel_id,
            "pending_task": pending_task,
            "origin_context_id": str(origin.id),
            "expected_ma_agent_id": agent_id,
        }
        if kind in ("env", "env_file"):
            key = target if kind == "env" else None
            await _request_agent_key_impl(
                runtime, auth, key=key, purpose="a parity scenario", **common
            )
        elif kind == "mcp":
            if mcp_server_url is None:
                raise ValueError("kind='mcp' needs its server url")
            await _request_mcp_token_impl(
                runtime, auth, server_name=target, url=mcp_server_url, **common
            )
        else:
            raise NotImplementedError(f"kind={kind!r} is not wired into the parity drivers")
        if len(posted) != 1:
            raise AssertionError(f"expected exactly one posted card, got {len(posted)}")
        [card] = _cards_in(posted[0])
        self._cards.append(read_teams_card(card))
        for action in _actions(card):
            token = cast(dict[str, Any], action.get("data") or {}).get("token")
            if isinstance(token, str):
                return token
        raise AssertionError("the posted card carries no credential button")

    async def click_private_input(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        tenant_id: uuid.UUID,
        workspace_id: str,
        channel_id: str,
        user_id: str,
        token: str,
    ) -> str | None:
        data = {"msteams": {"type": "task/fetch"}, "dialog_id": CREDENTIAL_DIALOG, "token": token}
        task = await self._card_invoke(
            sessionmaker, router, "task/fetch", data, workspace_id, channel_id, user_id
        )
        return None if task.get("type") == "continue" else str(task.get("value"))

    async def submit_private_input(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        tenant_id: uuid.UUID,
        workspace_id: str,
        channel_id: str,
        user_id: str,
        token: str,
        value: str = "",
        file_bytes: bytes | None = None,
    ) -> None:
        if file_bytes is not None:
            raise NotImplementedError("a Teams dialog takes no file upload")
        data = {"action": SUBMIT, "token": token, "secret": value}
        await self._card_invoke(
            sessionmaker, router, "task/submit", data, workspace_id, channel_id, user_id
        )

    async def _card_invoke(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        name: str,
        data: dict[str, Any],
        workspace_id: str,
        channel_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """One click or submit on the posted card, from its thread; the dialog response."""
        # The requester is an admin, so a replacement reaches its compare-and-set.
        runtime = self._runtime(sessionmaker, router, entra_tenant=workspace_id, admins=(user_id,))
        where = {"tenant": workspace_id, "user": user_id, "conversation": _thread_of(channel_id)}
        invoke = _invoke(name, {"data": data}, reply_to=_POSTED_MESSAGE_ID, **where)
        response, fake = await _exchange(runtime, invoke)
        for _method, body, _id in fake.activities:
            self._cards.extend(read_teams_card(card) for card in _cards_in(body))
        return cast(dict[str, Any], response.get("task") or {})

    def captured_cards(self) -> list[CapturedCard]:
        return list(self._cards)

    def captured_card_states(self) -> list[str]:
        return [card.state for card in self._cards]

    # -- setup panel --------------------------------------------------------
    #
    # `setup` in the 1:1 chat posts the roster as a card; a panel button is an
    # `adaptiveCard/action` invoke answered with the card that replaces it, and
    # New agent opens a dialog whose submit edits the card into Details.

    def _show(self, card: dict[str, Any]) -> CapturedView:
        self._panel_card = card
        view = read_teams_view(card, aliases=self._panel_aliases)
        self._views.append(view)
        return view

    async def _panel_invoke(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        name: str,
        value: dict[str, Any],
        *,
        workspace_id: str,
        channel_id: str,
        user_id: str,
    ) -> tuple[dict[str, Any], _TeamsApiFake]:
        runtime = self._runtime(
            sessionmaker, router, entra_tenant=workspace_id, admins=self._panel_admins
        )
        where = {"tenant": workspace_id, "user": user_id, "conversation": channel_id}
        return await _exchange(
            runtime, _invoke(name, value, reply_to=self._panel_message_id, **where)
        )

    async def open_setup_panel(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        tenant_id: uuid.UUID,
        workspace_id: str,
        channel_id: str,
        user_id: str,
        is_admin: bool,
    ) -> CapturedView:
        del tenant_id
        self._panel_admins = (user_id,) if is_admin else ()
        self._panel_aliases = {channel_id: "here", user_id: "you"}
        runtime = self._runtime(
            sessionmaker, router, entra_tenant=workspace_id, admins=self._panel_admins
        )
        message = _activity(
            tenant=workspace_id, user=user_id, conversation=channel_id, text="setup"
        )
        _response, fake = await _exchange(runtime, message)
        posted = [(i, c) for m, body, i in fake.activities if m == "POST" for c in _cards_in(body)]
        if not posted:
            raise AssertionError("setup posted no panel card")
        self._panel_message_id, card = posted[-1]
        return self._show(card)

    async def click_panel_action(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        tenant_id: uuid.UUID,
        workspace_id: str,
        channel_id: str,
        user_id: str,
        action: PanelAction,
        agent_name: str | None = None,
    ) -> CapturedView:
        del tenant_id
        if action.startswith("expand_"):
            listed = action.removeprefix("expand_")
            button = next(
                a for a in _actions(self._panel_card) if a.get("data", {}).get("list") == listed
            )
        elif (label := _PANEL_LABELS.get(action)) is not None:
            button = _find_button(self._panel_card, label, agent_name)
        else:
            raise NotImplementedError(f"the Teams panel has no {action!r} control")
        data = cast(dict[str, Any], button.get("data") or {})
        where = {"workspace_id": workspace_id, "channel_id": channel_id, "user_id": user_id}
        if button.get("type") == "Action.Execute":
            value = {"action": {"type": "Action.Execute", "verb": button["verb"], "data": data}}
            response, _fake = await self._panel_invoke(
                sessionmaker, router, "adaptiveCard/action", value | {"trigger": "manual"}, **where
            )
            if response.get("type") != _CARD_TYPE:
                raise AssertionError(f"the click did not redraw the panel: {response}")
            return self._show(cast(dict[str, Any], response["value"]))
        response, _fake = await self._panel_invoke(
            sessionmaker, router, "task/fetch", {"data": data}, **where
        )
        info = cast(dict[str, Any], response["task"]["value"])
        self._panel_form = cast(dict[str, Any], info["card"]["content"])
        form = read_teams_view(self._panel_form, aliases=self._panel_aliases, title=info["title"])
        self._views.append(form)
        return form

    async def submit_new_agent(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        tenant_id: uuid.UUID,
        workspace_id: str,
        channel_id: str,
        user_id: str,
        name: str,
        purpose: str | None,
        model: str,
    ) -> CapturedView:
        del tenant_id
        if not self._panel_form:
            raise AssertionError("click_panel_action(action='new_agent') must run first")
        [create] = cast(list[dict[str, Any]], self._panel_form.get("actions") or [])
        data = cast(dict[str, Any], create.get("data") or {})
        data |= {"name": name, "purpose": purpose or "", "model": model}
        response, fake = await self._panel_invoke(
            sessionmaker,
            router,
            "task/submit",
            {"data": data},
            workspace_id=workspace_id,
            channel_id=channel_id,
            user_id=user_id,
        )
        edits = [card for m, body, _id in fake.activities if m == "PUT" for card in _cards_in(body)]
        if not edits:
            raise AssertionError(f"the form did not land on Details: {response}")
        return self._show(edits[-1])

    def captured_views(self) -> list[CapturedView]:
        return list(self._views)


def _find_button(card: dict[str, Any], label: str, agent_name: str | None) -> dict[str, Any]:
    """The button a reader would press: by label, and for Details by the row's agent."""
    for action in _actions(card):
        if normalize_line(str(action.get("title") or ""), aliases={}) != label:
            continue
        data = cast(dict[str, Any], action.get("data") or {})
        if agent_name is None or data.get("agent") == agent_name:
            return action
    raise AssertionError(f"the panel on screen has no {label!r} button for {agent_name!r}")
