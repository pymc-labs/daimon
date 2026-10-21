"""Each platform's real here command reads local agents under its own guard."""

from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.discord.commands.here import HereCog
from daimon.adapters.slack.here import handle_here_command
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.adapters.teams.commands import CommandContext
from daimon.adapters.teams.here import show_here
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.channel_backend import channel_ref, set_channel_backend
from daimon.core.config import DatabaseSettings, Settings
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.selfhosted_resources import SelfHostedAgents
from daimon.testing.factories import make_account, make_tenant
from mux.contracts.config import BackendConfig
from mux.contracts.ids import ModelRef, Scope
from mux.contracts.resources import AgentSpec
from pydantic import PostgresDsn
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class NoAnthropic:
    def __getattribute__(self, name: str) -> object:
        raise AssertionError(f"here accessed Anthropic.{name}")


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
async def test_here_command_resolves_local_card_without_anthropic(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    platform: Literal["discord", "slack", "teams"],
) -> None:
    tenant = await make_tenant(db_session, platform=platform, workspace_id="123")
    account = await make_account(db_session, tenant=tenant)
    await set_channel_backend(
        db_session,
        channel_ref(tenant.id, platform, "4200"),
        BackendConfig(backend="selfhosted", profile="selfhosted.loop", model="gpt-5"),
    )
    await db_session.commit()
    await SelfHostedAgents(db_session_factory).create(
        Scope(
            tenant_id=str(tenant.id),
            account_id=str(account.id),
            principal_id="test",
            authorization_id="test",
        ),
        AgentSpec(name="Local Researcher", model=ModelRef(provider="selfhosted", id="gpt-5")),
        key="agent",
    )
    runtime = SimpleNamespace(
        sessionmaker=db_session_factory,
        anthropic=cast(AsyncAnthropic, NoAnthropic()),
        deployment_default=DeploymentDefault(agent_name="Local Researcher"),
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            _env_file=None,  # type: ignore[call-arg]
        ),
    )
    if platform == "discord":
        interaction = MagicMock()
        interaction.guild_id = 123
        interaction.user = MagicMock(spec=discord.Member)
        interaction.user.id = 111
        interaction.user.guild_permissions.manage_guild = False
        interaction.user.guild_permissions.administrator = False
        interaction.guild.owner_id = 999
        channel = MagicMock(spec=discord.TextChannel)
        channel.id, channel.category_id = 4200, None
        channel.permissions_for.return_value = discord.Permissions.all()
        interaction.channel = channel
        interaction.guild.channels = [channel]
        interaction.guild.fetch_channel = AsyncMock(return_value=channel)
        interaction.client.runtime = runtime
        interaction.response.defer = AsyncMock()
        interaction.edit_original_response = AsyncMock()
        await HereCog.here.callback(HereCog(interaction.client), interaction)  # pyright: ignore[reportCallIssue]
        interaction.edit_original_response.assert_awaited_once()
        embed = interaction.edit_original_response.await_args.kwargs["embed"]
        assert embed.title == "Local Researcher answers here"
    elif platform == "slack":
        client = MagicMock()
        client.conversations_info = AsyncMock(return_value={"channel": {"is_member": True}})
        client.users_conversations = AsyncMock(
            return_value={"channels": [{"id": "4200", "is_private": False}]}
        )
        client.users_info = AsyncMock(return_value={"user": {}})
        client.chat_postEphemeral = AsyncMock()
        monkeypatch.setattr(
            "daimon.adapters.slack.here.resolve_web_client", AsyncMock(return_value=client)
        )
        monkeypatch.setattr(
            "daimon.adapters.slack.here.resolve_is_admin", AsyncMock(return_value=False)
        )
        await handle_here_command(
            cast(SlackRuntime, runtime), {"team_id": "123", "channel_id": "4200", "user_id": "111"}
        )
        client.chat_postEphemeral.assert_awaited_once()
        assert "Local Researcher answers here" in str(
            client.chat_postEphemeral.await_args.kwargs["attachments"]
        )
    else:
        inbound = TeamsInbound(
            kind="channel",
            entra_tenant_id="123",
            user_id="111",
            conversation_id="thread",
            channel_id="4200",
            activity_id="1",
            text="here",
            service_url=None,
        )
        send = AsyncMock()
        context = CommandContext(
            inbound=inbound,
            tenant_id=tenant.id,
            args="",
            is_admin=False,
            runtime=cast(TeamsRuntime, runtime),
            send=send,
        )
        await show_here(context)
        send.assert_awaited_once()
        assert send.await_args is not None
        assert "Local Researcher answers here" in str(send.await_args.args[0])
