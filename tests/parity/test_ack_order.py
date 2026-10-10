"""Interaction acknowledgement order for the GitHub connect entry points.

The uncovered set is a ratchet for the broader registered-view audit:
credential modals, billing, privacy, routing, skill and feedback controls are
not exercised here yet. Add a handler to the covered set before removing it.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import httpx
from anthropic import AsyncAnthropic
from daimon.adapters.discord.agent_setup.github_home import GitHubHomeView
from daimon.adapters.discord.agent_setup.github_new_repo import NewRepoCard, handle_dm_notice
from daimon.adapters.discord.agent_setup.roster_view import RosterView
from daimon.adapters.discord.github_connect_button import GitHubConnectButton
from daimon.adapters.slack.app import SlackApp
from daimon.adapters.slack.runtime import SlackRuntime
from pydantic import SecretStr
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

UNCOVERED_DISCORD_HANDLERS = frozenset(
    {
        "GitHubHomeView._on_waiting",
        "GitHubHomeView._on_choose",
        "GitHubHomeView._on_back",
        "GitHubHomeView._on_personal_link",
        "GitHubHomeView._on_unlink_prompt",
        "GitHubHomeView._on_manage",
        "GitHubUnlinkView._on_unlink",
        "NewRepoCard.back",
        "NewRepoCard.dismiss",
        "RosterView._on_details",
        "RosterView._on_new",
        "RosterView._on_routing",
        "credential_modals.*.on_submit",
        "billing_panel.*.callback",
        "privacy_panel.*.callback",
        "routing_view.*.callback",
        "feedback_button.*.callback",
        "wizard.*.callback",
    }
)


async def test_discord_github_connect_button_defers_before_member_fetch() -> None:
    order: list[str] = []
    button = GitHubConnectButton(requester_id="123", intent_id=uuid.uuid4())
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user.id = 123
    interaction.guild_id = 10
    interaction.channel_id = 20
    guild = MagicMock(spec=discord.Guild)
    interaction.guild = guild

    async def defer(**_kwargs: Any) -> None:
        order.append("defer")

    async def fetch_member(_id: int) -> None:
        order.append("member_http")
        raise discord.NotFound(MagicMock(status=404, reason="missing"), "missing")

    interaction.response.defer = AsyncMock(side_effect=defer)
    interaction.followup.send = AsyncMock()
    guild.fetch_member = AsyncMock(side_effect=fetch_member)
    await button._reveal(interaction)  # pyright: ignore[reportPrivateUsage]
    assert order == ["defer", "member_http"]


async def test_discord_setup_github_navigation_defers_before_db_load() -> None:
    order: list[str] = []
    view = MagicMock()
    view.state.guild_id = 10
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild_id = 10
    interaction.user.id = 123

    async def defer(**_kwargs: Any) -> None:
        order.append("defer")

    async def load_home(*_args: Any, **_kwargs: Any) -> object:
        order.append("db_load")
        return object()

    interaction.response.defer = AsyncMock(side_effect=defer)
    view.swap_to = AsyncMock()
    with (
        patch("daimon.adapters.discord.checks.is_guild_admin", return_value=True),
        patch("daimon.adapters.discord.agent_setup.github_home.load_home", load_home),
    ):
        await RosterView._on_connect_github(view, interaction)  # pyright: ignore[reportPrivateUsage]
    assert order == ["defer", "db_load"]


async def test_discord_github_home_connect_defers_before_db_transaction() -> None:
    order: list[str] = []
    view = MagicMock()
    view.state.guild_id = 10
    view.state.answering = None
    view.state.roster_agents = []
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild_id = 10

    async def defer(**_kwargs: Any) -> None:
        order.append("defer")

    async def db_enter() -> None:
        order.append("db_transaction")
        raise ValueError("stop after first slow call")

    interaction.response.defer = AsyncMock(side_effect=defer)
    interaction.followup.send = AsyncMock()
    view.runtime.sessionmaker.begin.return_value.__aenter__ = AsyncMock(side_effect=db_enter)
    with patch("daimon.adapters.discord.agent_setup.github_home.is_guild_admin", return_value=True):
        await GitHubHomeView._on_connect(view, interaction)  # pyright: ignore[reportPrivateUsage]
    assert order == ["defer", "db_transaction"]


async def test_discord_new_repo_connect_defers_before_db_transaction() -> None:
    order: list[str] = []
    card = MagicMock()
    card.group.tenant_id = uuid.uuid4()
    interaction = MagicMock(spec=discord.Interaction)

    async def defer(**_kwargs: Any) -> None:
        order.append("defer")

    async def db_enter() -> None:
        order.append("db_transaction")
        raise ValueError("stop after first slow call")

    interaction.response.defer = AsyncMock(side_effect=defer)
    interaction.followup.send = AsyncMock()
    card.runtime.sessionmaker.begin.return_value.__aenter__ = AsyncMock(side_effect=db_enter)
    await NewRepoCard.connect(card, interaction)
    assert order == ["defer", "db_transaction"]


async def test_discord_github_dm_notice_defers_before_db_transaction() -> None:
    order: list[str] = []
    runtime = MagicMock()
    interaction = MagicMock(spec=discord.Interaction)
    interaction.data = {"custom_id": f"github_notice:{uuid.uuid4()}:2026-10-10:connect"}

    async def defer(**_kwargs: Any) -> None:
        order.append("defer")

    async def db_enter() -> None:
        order.append("db_transaction")
        raise ValueError("stop after first slow call")

    interaction.response.defer = AsyncMock(side_effect=defer)
    runtime.sessionmaker.return_value.__aenter__ = AsyncMock(side_effect=db_enter)
    with pytest.raises(ValueError, match="stop after first slow call"):
        await handle_dm_notice(interaction, runtime)
    assert order == ["defer", "db_transaction"]


async def test_slack_github_connect_acks_before_handler_work() -> None:
    order: list[str] = []
    settings = MagicMock()
    settings.crypto.keys = (SecretStr("dummykey"),)
    settings.slack.max_concurrent_turns_per_tenant = 3
    runtime = SlackRuntime(
        settings=settings,
        anthropic=MagicMock(spec=AsyncAnthropic),
        sessionmaker=MagicMock(),
        billing_config=None,
        http_client=MagicMock(spec=httpx.AsyncClient),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]
    )
    app = SlackApp(runtime=runtime)
    client = MagicMock()

    async def ack(_response: SocketModeResponse) -> None:
        order.append("ack")

    async def handle(_runtime: SlackRuntime, _payload: dict[str, Any]) -> None:
        order.append("db_or_http")

    client.send_socket_mode_response = AsyncMock(side_effect=ack)
    request = SocketModeRequest(
        type="slash_commands",
        envelope_id="github-connect-ack",
        payload={"command": "/github", "text": "connect", "team_id": "T_TEST"},
    )
    with patch("daimon.adapters.slack.app.handle_github_command", handle):
        await app.on_request(client, request)
        await asyncio.gather(*app._bg_tasks)  # pyright: ignore[reportPrivateUsage]
    assert order == ["ack", "db_or_http"]
