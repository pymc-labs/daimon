"""DiscordDriver -- the Discord half of the PlatformDriver Protocol.

Builds a DiscordRuntime + DaimonBot from an injected MARouter-backed fake
AsyncAnthropic and drives a turn via the REAL `DaimonBot.on_message` entry
point (D-02) -- the full on_message -> _orchestrate -> run_turn chain,
including Discord's own gates (mention detection, tenant liveness,
per-tenant concurrency). `create_session` and `build_context_xml` are
boundary-stubbed (Discord-API-only concerns unrelated to MA billing/turn
wiring -- mirrors `tests/integration/test_discord_turn_e2e.py`); `run_turn`
itself is never patched (D-01).

The posted-control half (`post_credential_card` / `click_private_input` /
`submit_private_input`) spans two processes the way production does. The post
runs the MCP server's own `_request_*_impl` against a transport-patched
`discord.http.HTTPClient`, so discord.py's real serializer produces the card
payload. The click and the submit run in the bot process: the real
`CredentialRequestButton` dispatch chain and the real modal `on_submit`, with
the interaction and the partial message the card is edited through faked at
the SDK boundary -- there is no Discord gateway to receive from.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import discord.http
from cryptography.fernet import Fernet
from daimon.adapters.discord.agent_setup.new_agent import NewAgentModal
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.commands.agent_setup import AgentSetupCog
from daimon.adapters.discord.credential_button import CredentialRequestButton
from daimon.adapters.discord.credential_modals import (
    EnvCredentialModal,
    EnvFileModal,
    McpCredentialModal,
)
from daimon.adapters.discord.runtime import DiscordRuntime, build_turn_deps
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
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    McpSettings,
    Settings,
    ThreadNamingSettings,
)
from daimon.core.credential_requests import CUSTOM_ID_PATTERN, CUSTOM_ID_PREFIX
from daimon.core.defaults.ma_index import find_agents_by_daimon_tag
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.posted_controls import CardKind
from daimon.core.purge import AccountPurgeResult
from daimon.core.purge import purge_account as core_purge_account
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.credential_requests import peek_credential_request
from daimon.core.stores.domain import CredentialRequestRow, Role
from daimon.core.stores.tenants import set_provision_status
from daimon.core.stores.turn_origins import create_origin
from daimon.testing import ma_session
from daimon.testing.ma import MARouter, build_fake_anthropic
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .cards import CapturedCard, read_discord_card, walk_components
from .protocol import PanelAction, parity_account_id
from .views import CapturedView, normalize_line, read_discord_modal, read_discord_view

_BALANCE_BLOCKED_TEXT = (
    "This server's daimon credit is depleted. An admin can top up with `/billing`."
)
_CAP_BLOCKED_TEXT = (
    "Monthly usage cap reached for this guild. "
    "An admin can adjust the cap with `/billing` (when available)."
)

#: The message id the faked REST POST hands back for a posted card. The row
#: records it, and every later edit and validity check is matched against it.
_POSTED_MESSAGE_ID = "9200000000000001"
_BOT_USER_ID = "1"
_VIEW_CHANNEL = 1 << 10
_SEND_MESSAGES = 1 << 11

#: The deployment settings the MCP modal demands before it will consume a
#: request. Both are fixed here: the scenarios are about the card, never
#: about an unconfigured deployment.
_MCP_PUBLIC_URL = "https://mcp.example.com/mcp"
_MCP_JWT_SECRET = "x" * 32

#: The install the agent-lifecycle tools act as. The fork and archive
#: scenarios name a tenant directly rather than opening a panel, so the
#: workspace and the caller are fixed here and only the tenant varies.
_LIFECYCLE_WORKSPACE_ID = "4200000000000001"
_LIFECYCLE_USER_ID = "4200000000000002"

#: The name of the channel every panel scenario opens in. Discord renders a
#: channel by name and Slack by id, so the drivers alias both to one word and
#: the two platforms' routing sentences become comparable.
_PANEL_CHANNEL_NAME = "here"
_ALIAS_CHANNEL = "here"
_ALIAS_USER = "you"

#: Which button each `PanelAction` is, by its label with emoji stripped. The
#: labels are the panel's own copy; a rename that broke a scenario would be
#: telling the truth about the screen having changed.
_PANEL_BUTTON_LABELS: dict[str, frozenset[str]] = {
    "who_answers_where": frozenset({"Who answers where"}),
    "next_page": frozenset({"Next"}),
    "prev_page": frozenset({"Previous"}),
    "new_agent": frozenset({"New agent"}),
    "back": frozenset({"Back"}),
    "expand_keys": frozenset({"Show more", "Show fewer"}),
    "expand_skills": frozenset({"Show more", "Show fewer"}),
    "expand_connections": frozenset({"Show more", "Show fewer"}),
}


def _guild_payload(guild_id: str) -> dict[str, Any]:
    return {
        "id": guild_id,
        "name": "parity-guild",
        "owner_id": "1",
        "afk_timeout": 0,
        "verification_level": 0,
        "default_message_notifications": 0,
        "explicit_content_filter": 0,
        "roles": [],
        "emojis": [],
        "features": [],
        "mfa_level": 0,
        "system_channel_flags": 0,
        "premium_tier": 0,
        "preferred_locale": "en-US",
        "nsfw_level": 0,
        "premium_progress_bar_enabled": False,
        "stickers": [],
        "region": "us-east",
    }


def _everyone_role_payload(guild_id: str) -> dict[str, Any]:
    return {
        "id": guild_id,
        "name": "@everyone",
        "permissions": str(_VIEW_CHANNEL | _SEND_MESSAGES),
        "position": 0,
        "color": 0,
        "hoist": False,
        "managed": False,
        "mentionable": False,
        "flags": 0,
    }


def _member_payload(user_id: str) -> dict[str, Any]:
    return {
        "user": {
            "id": user_id,
            "username": "parity-user",
            "discriminator": "0001",
            "global_name": "parity-user",
            "avatar": None,
            "bot": False,
            "flags": 0,
        },
        "roles": [],
        "joined_at": "2026-01-01T00:00:00+00:00",
        "deaf": False,
        "mute": False,
        "flags": 0,
    }


def _channel_payload(*, channel_id: str, guild_id: str) -> dict[str, Any]:
    return {
        "id": channel_id,
        "type": 0,
        "guild_id": guild_id,
        "name": "parity",
        "position": 0,
        "permission_overwrites": [],
        "nsfw": False,
        "rate_limit_per_user": 0,
        "parent_id": None,
    }


def _message_payload(*, channel_id: str) -> dict[str, Any]:
    return {
        "id": _POSTED_MESSAGE_ID,
        "channel_id": channel_id,
        "author": {
            "id": _BOT_USER_ID,
            "username": "daimon",
            "discriminator": "0001",
            "global_name": "daimon",
            "avatar": None,
            "bot": True,
            "flags": 0,
        },
        "content": "",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "edited_timestamp": None,
        "tts": False,
        "mention_everyone": False,
        "mentions": [],
        "mention_roles": [],
        "attachments": [],
        "embeds": [],
        "type": 0,
        "pinned": False,
        "flags": 0,
    }


@contextlib.contextmanager
def _patched_discord_rest(
    *, guild_id: str, channel_id: str, user_id: str, posted: list[dict[str, Any]]
) -> Iterator[None]:
    """Serve the REST calls one card post makes, at the transport level.

    discord.py's own Guild / Member / TextChannel / Message constructors run
    on these payloads and its own serializer builds the request body, so the
    captured `components` are exactly what Discord would have received.
    """

    async def _request(
        _self: discord.http.HTTPClient, route: discord.http.Route, **kwargs: Any
    ) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload(guild_id)
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role_payload(guild_id)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload(user_id)
        if route.path == "/channels/{channel_id}":
            return _channel_payload(channel_id=channel_id, guild_id=guild_id)
        if route.method == "POST" and route.path == "/channels/{channel_id}/messages":
            posted.append(cast(dict[str, Any], kwargs.get("json") or {}))
            return _message_payload(channel_id=channel_id)
        raise AssertionError(f"unexpected discord route {route.method} {route.path}")

    async def _static_login(_self: discord.http.HTTPClient, _token: str) -> dict[str, Any]:
        return {
            "id": _BOT_USER_ID,
            "username": "daimon",
            "discriminator": "0001",
            "avatar": None,
            "bot": True,
        }

    with (
        patch.object(discord.http.HTTPClient, "request", _request),
        patch.object(discord.http.HTTPClient, "static_login", _static_login),
    ):
        yield


@dataclass
class DiscordDriver:
    """Drives turns through `DaimonBot.on_message`, the real Discord entry point."""

    param_id: str = "discord"
    #: Every card this driver posted or edited, oldest first.
    _cards: list[CapturedCard] = field(default_factory=list[CapturedCard])
    #: One fake client for the whole lifecycle, so every `edit_posted_card`
    #: call -- whichever entry point made it -- lands on the same recorder.
    _client: MagicMock | None = None
    #: Every setup-panel screen this driver drew, oldest first.
    _views: list[CapturedView] = field(default_factory=list[CapturedView])
    #: The panel view currently on screen; a click runs its real callbacks.
    _panel_view: discord.ui.LayoutView | None = None
    #: The form the New agent button opened, waiting for its submission.
    _panel_modal: discord.ui.Modal | None = None
    #: The stand-in bot the cog and its callbacks read the runtime off.
    _panel_bot: MagicMock | None = None
    #: Platform ids rewritten to neutral names when a screen is read.
    _panel_aliases: dict[str, str] = field(default_factory=dict[str, str])
    #: The deployment fall-through the panel renders; scenarios set routing
    #: through the config store instead, so both platforms read one source.
    _panel_default: DeploymentDefault = field(default_factory=DeploymentDefault)

    def _make_runtime(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        *,
        billing_config: object | None = None,
    ) -> DiscordRuntime:
        settings = MagicMock()
        settings.mcp = McpSettings()
        settings.billing.markup = Decimal("1.0")
        settings.billing.signup_credit = Decimal("0")
        settings.crypto.keys = []
        settings.github.oauth_scopes = ()
        discord_settings = MagicMock()
        discord_settings.max_concurrent_turns_per_tenant = 100
        discord_settings.bot_display_name = "daimon"
        settings.discord = discord_settings
        settings.thread_naming = ThreadNamingSettings(enabled=False)
        anthropic = build_fake_anthropic(router.dispatch)
        resolver_cache = new_resolver_cache()
        deployment_default = DeploymentDefault(agent_name="test-agent", environment_name="test-env")
        return DiscordRuntime(
            settings=settings,
            anthropic=anthropic,
            sessionmaker=sessionmaker,
            notebook_rate_limiter=RateLimiter(max_requests=999),
            billing_config=billing_config,  # pyright: ignore[reportArgumentType]  # test-injected BillingConfig | None
            deployment_default=deployment_default,
            resolver_cache=resolver_cache,
            turn_deps=build_turn_deps(
                settings,
                anthropic,
                sessionmaker,
                deployment_default=deployment_default,
                resolver_cache=resolver_cache,
                billing_config=billing_config,  # pyright: ignore[reportArgumentType]  # test-injected BillingConfig | None
            ),
        )

    def _make_bot(self, runtime: DiscordRuntime) -> DaimonBot:
        intents = discord.Intents.default()
        intents.message_content = True
        bot = DaimonBot(runtime=runtime, intents=intents)
        bot._connection.user = MagicMock(spec=discord.ClientUser)  # pyright: ignore[reportPrivateUsage]
        bot._connection.user.id = 999  # pyright: ignore[reportPrivateUsage]
        bot._connection.user.mentioned_in = MagicMock(return_value=True)  # pyright: ignore[reportPrivateUsage]
        return bot

    def _make_message(
        self, *, workspace_id: str, channel_id: str, user_id: str, text: str
    ) -> discord.Message:
        message = MagicMock(spec=discord.Message)
        message.content = f"<@999> {text}"
        message.author = MagicMock()
        message.author.bot = False
        message.author.id = int(user_id)
        message.author.display_name = "parity-user"
        message.guild = MagicMock(spec=discord.Guild)
        message.guild.id = int(workspace_id)
        message.guild.owner_id = int(user_id)

        thread = MagicMock(spec=discord.Thread)
        thread.id = int(channel_id)
        thread.parent_id = int(channel_id) - 1

        # message_ref must carry a real .id so lifecycle._message_ref is
        # non-None; edit must be an AsyncMock since _edit_message awaits it.
        message_ref = MagicMock()
        message_ref.id = 42
        message_ref.edit = AsyncMock()
        thread.send = AsyncMock(return_value=message_ref)

        message.channel = thread
        message.add_reaction = AsyncMock()
        message.attachments = []
        message.mentions = [SimpleNamespace(id=999)]
        message.created_at = datetime(2026, 6, 14, tzinfo=UTC)
        return message

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
        runtime = self._make_runtime(sessionmaker, router, billing_config=billing_config)
        bot = self._make_bot(runtime)
        message = self._make_message(
            workspace_id=workspace_id, channel_id=channel_id, user_id=user_id, text=text
        )
        thread = cast(MagicMock, message.channel)

        with (
            patch("daimon.core.turn.prepare.create_session") as mock_create_session,
            patch("daimon.adapters.discord.bot.build_context_xml") as mock_build_context_xml,
        ):
            mock_create_session.return_value = ma_session(
                id="sess_parity_test",
                agent_id="ag_parity_test",
                model="claude-sonnet-4-6",
                environment_id="env_parity_test",
            )
            mock_build_context_xml.return_value = (f"<user_query>{text}</user_query>", [])
            await bot.on_message(message)

        message_ref = cast(MagicMock, thread.send.return_value)
        posted: list[str] = []
        for call in thread.send.call_args_list:
            if call.args and isinstance(call.args[0], str):
                posted.append(call.args[0])
        for call in message_ref.edit.call_args_list:
            content = call.kwargs.get("content")
            if isinstance(content, str):
                posted.append(content)
        return posted

    def expected_blocked_text(self, kind: Literal["balance", "cap"]) -> str:
        return _BALANCE_BLOCKED_TEXT if kind == "balance" else _CAP_BLOCKED_TEXT

    def _agent_tool_context(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        *,
        tenant_id: uuid.UUID,
        workspace_id: str,
        user_id: str,
    ) -> tuple[McpRuntime, AuthIdentity]:
        """The runtime and identity the agent lifecycle tools run under.

        Fork and archive are chat operations: the panel dropped both, so the
        only implementation either platform has left is the MCP tool, and the
        driver reaches it exactly as a turn would -- with this platform's own
        `AuthIdentity` and nothing else changed.
        """
        anthropic = build_fake_anthropic(router.dispatch)
        runtime = McpRuntime(
            session_factory=sessionmaker,
            client=anthropic,
            settings=Settings(
                database=DatabaseSettings(url="postgresql+asyncpg://parity/parity"),  # pyright: ignore[reportArgumentType]  # pydantic coerces the DSN string
                anthropic=AnthropicSettings(api_key=SecretStr("parity")),
            ),
            deployment_default=DeploymentDefault(),
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
        )
        auth = AuthIdentity(
            account_id=parity_account_id(tenant_id, user_id),
            tenant_id=tenant_id,
            role=Role.ADMIN,
            platform="discord",
            external_id=workspace_id,
            platform_user_id=user_id,
            is_admin=True,
        )
        return runtime, auth

    async def delete_agent(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        tenant_id: uuid.UUID,
        name: str,
    ) -> None:
        runtime, auth = self._agent_tool_context(
            sessionmaker,
            router,
            tenant_id=tenant_id,
            workspace_id=_LIFECYCLE_WORKSPACE_ID,
            user_id=_LIFECYCLE_USER_ID,
        )
        await _archive_agent_impl(
            runtime,
            auth,
            name=name,
            expected_ma_agent_id=await _pin_agent(runtime, tenant_id=tenant_id, name=name),
        )

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
        runtime, auth = self._agent_tool_context(
            sessionmaker,
            router,
            tenant_id=tenant_id,
            workspace_id=_LIFECYCLE_WORKSPACE_ID,
            user_id=_LIFECYCLE_USER_ID,
        )
        await _fork_agent_impl(
            runtime,
            auth,
            source_name=source_name,
            new_name=new_name,
            expected_ma_agent_id=await _pin_agent(runtime, tenant_id=tenant_id, name=source_name),
        )

    async def purge_account(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        router: MARouter,
        account_id: uuid.UUID,
    ) -> AccountPurgeResult:
        return await core_purge_account(
            sm=sessionmaker,
            account_id=account_id,
            anthropic=build_fake_anthropic(router.dispatch),
        )

    async def uninstall(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        workspace_id: str,
    ) -> None:
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=workspace_id)
        await set_provision_status(sessionmaker, tenant_id=tenant_id, archive=True)

    # -- posted-control lifecycle -------------------------------------------

    def _credential_runtime(
        self, sessionmaker: async_sessionmaker[AsyncSession], router: MARouter
    ) -> DiscordRuntime:
        """The bot-process runtime the click and the submit run against.

        Deliberately not `_make_runtime`: a credential submission needs the
        daimon-mcp settings and a real Fernet (the MCP form stores an
        agent-scoped copy of the token), and a turn needs neither.
        """
        settings = MagicMock()
        settings.mcp.public_url = _MCP_PUBLIC_URL
        settings.mcp.jwt_secret = SecretStr(_MCP_JWT_SECRET)
        settings.github.oauth_scopes = ()
        return DiscordRuntime(
            settings=settings,
            anthropic=build_fake_anthropic(router.dispatch),
            sessionmaker=sessionmaker,
            notebook_rate_limiter=RateLimiter(max_requests=999),
            billing_config=None,
            deployment_default=DeploymentDefault(),
            resolver_cache=new_resolver_cache(),
            turn_deps=MagicMock(  # pyright: ignore[reportArgumentType]  # no turn runs here
                fernet=build_multifernet((Fernet.generate_key().decode(),))
            ),
        )

    def _card_client(self, runtime: DiscordRuntime) -> MagicMock:
        """The stand-in `DaimonBot` every card edit is delivered through.

        `edit_posted_card` reaches the card as
        `client.get_partial_messageable(thread).get_partial_message(id)`, and a
        MagicMock returns the same child for every argument -- so one
        recorder catches every edit of the one card a scenario posts.
        """
        if self._client is None:
            client = MagicMock()
            message = client.get_partial_messageable.return_value.get_partial_message.return_value

            async def _record(**kwargs: Any) -> None:
                self._cards.append(read_discord_card(kwargs["view"].to_components()))

            message.edit = AsyncMock(side_effect=_record)
            self._client = client
        self._client.runtime = runtime
        return self._client

    def _card_interaction(
        self,
        *,
        runtime: DiscordRuntime,
        workspace_id: str,
        channel_id: str,
        user_id: str,
    ) -> MagicMock:
        """An interaction on the posted card, in the thread it was posted in.

        `is_credential_interaction_valid` re-checks the requester, the guild,
        the thread and its parent against the row on every click and every
        submit, so all four are wired from the same ids the mint used. The
        user is a guild admin: a replacement scenario has to reach its
        compare-and-set, not stop at the shared-agent gate in front of it.
        """
        interaction = MagicMock()
        interaction.client = self._card_client(runtime)
        interaction.guild_id = int(workspace_id)
        interaction.user = MagicMock(spec=discord.Member)
        interaction.user.id = int(user_id)
        interaction.user.guild_permissions.administrator = True
        interaction.user.guild_permissions.manage_guild = False
        interaction.channel_id = int(channel_id)
        interaction.channel = MagicMock(spec=discord.Thread)
        interaction.channel.parent_id = int(channel_id) - 1
        interaction.response.defer = AsyncMock()
        interaction.response.send_message = AsyncMock()
        interaction.response.send_modal = AsyncMock()
        interaction.response.is_done.return_value = False
        interaction.followup.send = AsyncMock()
        return interaction

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
        anthropic = build_fake_anthropic(router.dispatch)
        runtime = McpRuntime(
            session_factory=sessionmaker,
            client=anthropic,
            settings=Settings(
                database=DatabaseSettings(url="postgresql+asyncpg://parity/parity"),  # pyright: ignore[reportArgumentType]  # pydantic coerces the DSN string
                anthropic=AnthropicSettings(api_key=SecretStr("parity")),
                discord=DiscordSettings(bot_token=SecretStr("parity-bot-token")),
            ),
            deployment_default=DeploymentDefault(),
        )
        auth = AuthIdentity(
            account_id=parity_account_id(tenant_id, user_id),
            tenant_id=tenant_id,
            role=Role.ADMIN,
            platform="discord",
            external_id=workspace_id,
            platform_user_id=user_id,
            is_admin=True,
        )
        # The tools refuse to mint for an agent they cannot pin by MA id, and
        # take that id off the origin's configuration target when the caller
        # passes none -- so the origin names the agent this card is for.
        agents = await find_agents_by_daimon_tag(anthropic, tenant_id=tenant_id, name=agent_name)
        if not agents:
            raise AssertionError(f"the router serves no agent named {agent_name!r}")
        now = datetime.now(UTC)
        async with sessionmaker.begin() as session:
            origin = await create_origin(
                session,
                tenant_id=tenant_id,
                account_id=auth.account_id,
                platform="discord",
                parent_channel_id=str(int(channel_id) - 1),
                thread_id=channel_id,
                responder_ma_agent_id="ag_parity_responder",
                responder_name="Daimon",
                configuration_target_ma_agent_id=agents[0].id,
                configuration_target_name=agent_name,
                role=Role.ADMIN,
                expires_at=now + timedelta(minutes=30),
                now=now,
            )

        posted: list[dict[str, Any]] = []
        with _patched_discord_rest(
            guild_id=workspace_id, channel_id=channel_id, user_id=user_id, posted=posted
        ):
            if kind == "env":
                await _request_agent_key_impl(
                    runtime,
                    auth,
                    agent_name=agent_name,
                    key=target,
                    purpose="a parity scenario",
                    channel_id=channel_id,
                    pending_task=pending_task,
                    origin_context_id=str(origin.id),
                    expected_ma_agent_id=agents[0].id,
                )
            elif kind == "env_file":
                await _request_agent_key_impl(
                    runtime,
                    auth,
                    agent_name=agent_name,
                    key=None,
                    purpose="a parity scenario",
                    channel_id=channel_id,
                    pending_task=pending_task,
                    origin_context_id=str(origin.id),
                    expected_ma_agent_id=agents[0].id,
                )
            elif kind == "mcp":
                if mcp_server_url is None:
                    raise ValueError("kind='mcp' needs its server url")
                await _request_mcp_token_impl(
                    runtime,
                    auth,
                    agent_name=agent_name,
                    server_name=target,
                    url=mcp_server_url,
                    channel_id=channel_id,
                    pending_task=pending_task,
                    origin_context_id=str(origin.id),
                    expected_ma_agent_id=agents[0].id,
                )
            else:
                raise NotImplementedError(
                    f"kind={kind!r} is not wired into the parity drivers: the two repo "
                    "kinds submit against GitHub, which no scenario fakes yet"
                )

        if len(posted) != 1:
            raise AssertionError(f"expected exactly one posted card, got {len(posted)}")
        self._cards.append(read_discord_card(posted[0]["components"]))
        return _token_from_components(posted[0]["components"])

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
        runtime = self._credential_runtime(sessionmaker, router)
        interaction = self._card_interaction(
            runtime=runtime, workspace_id=workspace_id, channel_id=channel_id, user_id=user_id
        )
        interaction.type = discord.InteractionType.component
        interaction.message = MagicMock(spec=discord.Message)
        interaction.message.id = int(_POSTED_MESSAGE_ID)
        match = CUSTOM_ID_PATTERN.fullmatch(f"{CUSTOM_ID_PREFIX}{token}")
        if match is None:
            raise AssertionError(f"token {token!r} does not fit the dynamic-item template")
        button = await CredentialRequestButton.from_custom_id(
            interaction, MagicMock(spec=discord.ui.Item), match
        )
        if not await button.interaction_check(interaction):
            return str(interaction.response.send_message.call_args.args[0])
        await button.callback(interaction)
        return None

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
        runtime = self._credential_runtime(sessionmaker, router)
        async with sessionmaker() as session:
            row = await peek_credential_request(session, token=token)
        if row is None:
            raise AssertionError(f"no credential request for token {token!r}")
        modal = _build_modal(runtime, row, value=value, file_bytes=file_bytes)
        interaction = self._card_interaction(
            runtime=runtime, workspace_id=workspace_id, channel_id=channel_id, user_id=user_id
        )
        # Discord omits `message` on a modal submit; `is_credential_interaction_valid`
        # accepts that only for this interaction type, so both are set together.
        interaction.type = discord.InteractionType.modal_submit
        interaction.message = None
        await modal.on_submit(interaction)

    def captured_cards(self) -> list[CapturedCard]:
        return list(self._cards)

    def captured_card_states(self) -> list[str]:
        return [card.state for card in self._cards]

    # -- setup panel --------------------------------------------------------
    #
    # The panel is one ephemeral message that changes shape, so the driver
    # holds the live view between calls: a click runs the real callback on the
    # instance currently on screen, and whatever that callback edits the
    # message to becomes the next screen.

    def _panel_settings(self) -> Settings:
        """Real `Settings` for the panel runtime — the panel reads several.

        `load_agent_details` derives repo access from the two GitHub facts and
        filters the deployment's own MCP server out of the server list, and
        Details offers coding-tool access only where it could work, so the
        values are spelled here rather than left to a MagicMock.
        """
        return Settings.model_validate(
            {
                "database": {"url": "postgresql+asyncpg://parity/parity"},
                "anthropic": {"api_key": "parity"},
                "mcp": {"public_url": _MCP_PUBLIC_URL, "jwt_secret": _MCP_JWT_SECRET},
            }
        )

    def _panel_runtime(
        self, sessionmaker: async_sessionmaker[AsyncSession], router: MARouter
    ) -> DiscordRuntime:
        anthropic = build_fake_anthropic(router.dispatch)
        cache = new_resolver_cache()
        settings = self._panel_settings()
        return DiscordRuntime(
            settings=settings,
            anthropic=anthropic,
            sessionmaker=sessionmaker,
            notebook_rate_limiter=RateLimiter(max_requests=999),
            billing_config=None,
            deployment_default=self._panel_default,
            resolver_cache=cache,
            turn_deps=build_turn_deps(
                settings,
                anthropic,
                sessionmaker,
                deployment_default=self._panel_default,
                resolver_cache=cache,
                billing_config=None,
            ),
        )

    def _record_view(self, view: object) -> None:
        if isinstance(view, discord.ui.LayoutView):
            self._panel_view = view
            self._views.append(read_discord_view(view, aliases=self._panel_aliases))

    def _panel_interaction(
        self, *, workspace_id: str, channel_id: str, user_id: str, is_admin: bool
    ) -> MagicMock:
        """An interaction on the open panel, with every seam the panel uses.

        `guild` stays None: the routing screen resolves channel names off the
        guild cache when it has one, and a fake cache would put invented names
        in front of an assertion about what the panel actually knows.
        """
        interaction = MagicMock(spec=discord.Interaction)
        interaction.client = self._panel_bot
        interaction.guild = None
        interaction.guild_id = int(workspace_id)
        interaction.user = MagicMock(spec=discord.Member)
        interaction.user.id = int(user_id)
        interaction.user.display_name = "parity-user"
        interaction.user.guild_permissions.administrator = is_admin
        interaction.user.guild_permissions.manage_guild = False
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = int(channel_id)
        channel.name = _PANEL_CHANNEL_NAME
        interaction.channel = channel
        interaction.channel_id = int(channel_id)

        done = {"value": False}

        async def _defer(**_kwargs: Any) -> None:
            done["value"] = True

        async def _edit(**kwargs: Any) -> None:
            done["value"] = True
            self._record_view(kwargs.get("view"))

        async def _send_modal(modal: discord.ui.Modal) -> None:
            done["value"] = True
            self._panel_modal = modal
            self._views.append(read_discord_modal(modal, aliases=self._panel_aliases))

        interaction.response.defer = AsyncMock(side_effect=_defer)
        interaction.response.edit_message = AsyncMock(side_effect=_edit)
        interaction.response.send_message = AsyncMock()
        interaction.response.send_modal = AsyncMock(side_effect=_send_modal)
        interaction.response.is_done = MagicMock(side_effect=lambda: done["value"])
        interaction.edit_original_response = AsyncMock(side_effect=_edit)
        interaction.delete_original_response = AsyncMock()
        interaction.followup.send = AsyncMock()
        return interaction

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
        del tenant_id  # the cog derives it from the guild, as production does
        runtime = self._panel_runtime(sessionmaker, router)
        bot = MagicMock()
        bot.runtime = runtime
        self._panel_bot = bot
        self._panel_aliases = {
            _PANEL_CHANNEL_NAME: _ALIAS_CHANNEL,
            channel_id: _ALIAS_CHANNEL,
            user_id: _ALIAS_USER,
        }
        interaction = self._panel_interaction(
            workspace_id=workspace_id, channel_id=channel_id, user_id=user_id, is_admin=is_admin
        )
        cog = AgentSetupCog(bot)
        await cog.agent_setup.callback(cog, interaction)  # pyright: ignore[reportUnknownMemberType]
        return self._last_view()

    def _last_view(self) -> CapturedView:
        if not self._views:
            raise AssertionError("the panel drew nothing")
        return self._views[-1]

    def _panel_buttons(self) -> list[discord.ui.Button[Any]]:
        view = self._panel_view
        if view is None:
            raise AssertionError("open_setup_panel must run before a click")
        buttons: list[discord.ui.Button[Any]] = []
        for child in view.walk_children():
            if isinstance(child, discord.ui.Button):
                buttons.append(child)
            elif isinstance(child, discord.ui.Section) and isinstance(
                child.accessory, discord.ui.Button
            ):
                buttons.append(child.accessory)
        return buttons

    def _details_button(self, agent_name: str) -> discord.ui.Button[Any]:
        view = self._panel_view
        if view is None:
            raise AssertionError("open_setup_panel must run before a click")
        for child in view.walk_children():
            if not isinstance(child, discord.ui.Section):
                continue
            text = "\n".join(
                item.content for item in child.children if isinstance(item, discord.ui.TextDisplay)
            )
            accessory = child.accessory
            if agent_name in text and isinstance(accessory, discord.ui.Button):
                return accessory
        raise AssertionError(f"no roster row for {agent_name!r} on the screen")

    def _labelled_button(self, action: PanelAction) -> discord.ui.Button[Any]:
        wanted = _PANEL_BUTTON_LABELS[action]
        detail_heading = {
            "expand_keys": "Keys",
            "expand_skills": "Skills",
            "expand_connections": "Connections",
        }.get(action)
        if detail_heading is not None:
            view = self._panel_view
            if view is None:
                raise AssertionError("open_setup_panel must run before a click")
            for child in view.walk_children():
                if not isinstance(child, discord.ui.Section):
                    continue
                accessory = child.accessory
                text = normalize_line(
                    "\n".join(
                        item.content
                        for item in child.children
                        if isinstance(item, discord.ui.TextDisplay)
                    ),
                    aliases={},
                )
                if (
                    text.startswith(detail_heading)
                    and isinstance(accessory, discord.ui.Button)
                    and normalize_line(accessory.label or "", aliases={}) in wanted
                ):
                    return accessory
        for button in self._panel_buttons():
            if normalize_line(button.label or "", aliases={}) in wanted:
                return button
        raise AssertionError(f"no {action!r} control on the screen")

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
        del sessionmaker, router, tenant_id  # the open panel already holds both
        if action == "details":
            if agent_name is None:
                raise ValueError("action='details' needs the agent whose row was clicked")
            button = self._details_button(agent_name)
        else:
            button = self._labelled_button(action)
        interaction = self._panel_interaction(
            workspace_id=workspace_id,
            channel_id=channel_id,
            user_id=user_id,
            is_admin=self._panel_is_admin(),
        )
        await button.callback(interaction)  # pyright: ignore[reportUnknownMemberType]
        return self._last_view()

    def _panel_is_admin(self) -> bool:
        """The role the open panel was built with, so a click keeps it."""
        view = self._panel_view
        state = getattr(view, "state", None)
        return bool(getattr(state, "is_admin", False))

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
        del sessionmaker, router, tenant_id
        modal = self._panel_modal
        if not isinstance(modal, NewAgentModal):
            raise AssertionError("click_panel_action(action='new_agent') must run first")
        name_field = modal.name_label.component
        assert isinstance(name_field, discord.ui.TextInput), "the name field is a TextInput"
        name_field._value = name  # pyright: ignore[reportPrivateUsage]  # discord.py keeps the typed value private
        prompt_field = modal.prompt_label.component
        assert isinstance(prompt_field, discord.ui.TextInput), "the purpose field is a TextInput"
        prompt_field._value = purpose or ""  # pyright: ignore[reportPrivateUsage]
        model_field = modal.model_label.component
        assert isinstance(model_field, discord.ui.Select), "the model field is a Select"
        model_field._values = [model]  # pyright: ignore[reportPrivateUsage]
        interaction = self._panel_interaction(
            workspace_id=workspace_id,
            channel_id=channel_id,
            user_id=user_id,
            is_admin=self._panel_is_admin(),
        )
        await modal.on_submit(interaction)
        return self._last_view()

    def captured_views(self) -> list[CapturedView]:
        return list(self._views)


async def _pin_agent(runtime: McpRuntime, *, tenant_id: uuid.UUID, name: str) -> str:
    """The MA id the tool must be handed to act on `name`.

    `resolve_setup_agent` refuses a platform call that names an agent without
    pinning its identity, which is the whole point of the guard: a namesake
    recreated since the caller last looked must not be adopted silently. A
    real turn passes the id off the roster it just listed; this does the same
    read.
    """
    agents = await find_agents_by_daimon_tag(runtime.client, tenant_id=tenant_id, name=name)
    if not agents:
        raise AssertionError(f"the router serves no agent named {name!r}")
    return agents[0].id


def _token_from_components(components: object) -> str:
    """The request token the posted card's button carries."""
    for component in walk_components(components):
        custom_id = component.get("custom_id")
        if isinstance(custom_id, str) and custom_id.startswith(CUSTOM_ID_PREFIX):
            return custom_id.removeprefix(CUSTOM_ID_PREFIX)
    raise AssertionError("the posted card carries no credential button")


def _build_modal(
    runtime: DiscordRuntime,
    row: CredentialRequestRow,
    *,
    value: str,
    file_bytes: bytes | None,
) -> discord.ui.Modal:
    """The modal this request's click opens, with its one input filled in.

    Rebuilt from the row rather than kept from `click_private_input`: each
    driver method owns its own runtime, and the modal holds the one it was
    built with. The mapping below is the same `kind` -> modal dispatch
    `CredentialRequestButton.callback` makes.
    """
    if row.kind == "env":
        env_modal = EnvCredentialModal(runtime=runtime, request_row=row)
        env_modal.value_input._value = value  # pyright: ignore[reportPrivateUsage]
        return env_modal
    if row.kind == "mcp":
        mcp_modal = McpCredentialModal(runtime=runtime, request_row=row)
        mcp_modal.token_input._value = value  # pyright: ignore[reportPrivateUsage]
        return mcp_modal
    if row.kind == "env_file":
        if file_bytes is None:
            raise ValueError("the .env form submits an upload, not a typed value")
        file_modal = EnvFileModal(runtime=runtime, request_row=row)
        attachment = MagicMock(spec=discord.Attachment)
        attachment.size = len(file_bytes)
        attachment.read = AsyncMock(return_value=file_bytes)
        file_modal.file_input._values = [attachment]  # pyright: ignore[reportPrivateUsage]
        return file_modal
    raise NotImplementedError(f"kind={row.kind!r} has no parity submit path")
