"""Independent reader families refuse reserved channels before content/row access."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import discord
import pytest
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.config import RecruitmentSettings
from daimon.core.here_card import HereCard


def policy():
    return RecruitmentSettings(tenants={"101": {"channel_ids": ["2"], "recruiter_user_ids": ["7"]}})


def channel():
    value = MagicMock(spec=discord.Thread)
    value.id, value.parent_id, value.guild.id = 20, 2, 101
    return value


def interaction():
    value = MagicMock(spec=discord.Interaction)
    value.channel, value.guild_id = channel(), 101
    value.client = SimpleNamespace(
        runtime=SimpleNamespace(settings=SimpleNamespace(recruitment=policy()))
    )
    value.response.is_done.return_value = False
    value.response.send_message = AsyncMock()
    value.followup.send = AsyncMock()
    return value


async def test_channel_rule_label_cannot_name_an_agent_after_reserved_channel(monkeypatch):
    from contextlib import asynccontextmanager

    from daimon.adapters.mcp.tools import channel_rules

    @asynccontextmanager
    async def rest(token):
        yield SimpleNamespace(fetch_channel=AsyncMock(return_value=channel()))

    monkeypatch.setattr(channel_rules, "rest_client", rest)
    monkeypatch.setattr(channel_rules, "_require_bot_token", lambda _: "fake")
    auth = SimpleNamespace(platform="discord", external_id="101")
    runtime = SimpleNamespace(settings=SimpleNamespace(recruitment=policy()))
    assert await channel_rules._channel_label(runtime, auth, "20") is None


async def test_cli_channel_listing_and_label_filter_reserved_metadata(monkeypatch):
    from contextlib import asynccontextmanager
    from dataclasses import dataclass
    from io import StringIO

    from daimon.adapters.cli.commands import channels as module
    from rich.console import Console

    @dataclass
    class Summary:
        channels: list

    @asynccontextmanager
    async def session():
        yield MagicMock()

    runtime = SimpleNamespace(
        settings=SimpleNamespace(
            recruitment=policy(),
            discord=SimpleNamespace(bot_token=SimpleNamespace(get_secret_value=lambda: "fake")),
        ),
        sessionmaker=session,
        deployment_default=MagicMock(),
    )
    monkeypatch.setattr(module, "_existing_tenant_id", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(
        module,
        "load_tenant_summary",
        AsyncMock(return_value=Summary([SimpleNamespace(channel_id="2")])),
    )

    async def resolve(*args, **kwargs):
        return {
            "id": "20",
            "guild_id": "101",
            "type": 11,
            "parent_id": "2",
            "name": "private candidate",
        }

    monkeypatch.setattr(module, "_fetch_discord_channel", resolve)
    console = Console(file=StringIO())
    await module.channels_list(
        rt=runtime, console=console, platform="discord", workspace_id="101", as_json=True
    )
    assert '"channels": []' in console.file.getvalue()
    assert (
        await module._channel_label(
            runtime,
            platform="discord",
            workspace_id="101",
            channel_id="20",
            discord_transport=None,
            slack_transport=None,
        )
        is None
    )


@pytest.mark.parametrize("unresolved", [False, True])
async def test_background_controls_and_support_refuse_before_fetch_or_send(unresolved):
    from daimon.adapters.discord.posted_controls.edit import edit_posted_card
    from daimon.adapters.discord.support_escalation import post_to_support_channel

    state = MagicMock()
    state._get_guild.return_value = None
    source = (
        discord.PartialMessageable(state=state, id=20, guild_id=101) if unresolved else channel()
    )
    bot = SimpleNamespace(
        runtime=SimpleNamespace(settings=SimpleNamespace(recruitment=policy())),
        get_channel=lambda _: source,
        get_partial_messageable=MagicMock(side_effect=AssertionError("private card fetch")),
    )
    row = MagicMock()
    row.origin_thread_id, row.posted_message_id = "20", "30"
    await edit_posted_card(bot, row=row, state="received")
    assert not await post_to_support_channel(bot, channel_id="20", body="synthetic")
    bot.get_partial_messageable.assert_not_called()
    state.http.send_message.assert_not_called()


async def test_waiting_panel_omits_reserved_request_labels(monkeypatch):
    from contextlib import asynccontextmanager

    from daimon.adapters.discord.agent_setup import github_waiting as module

    @asynccontextmanager
    async def session():
        yield MagicMock()

    row = SimpleNamespace(thread_id="20", parent_channel_id="2")
    monkeypatch.setattr(module, "list_waiting", AsyncMock(return_value=[row]))
    monkeypatch.setattr(module, "list_asker_requests", AsyncMock(return_value=[row]))
    monkeypatch.setattr(module, "list_authorized_repos", AsyncMock(return_value=[]))
    factory = MagicMock()
    monkeypatch.setattr(module, "GitHubWaitingView", factory)
    runtime = SimpleNamespace(settings=SimpleNamespace(recruitment=policy()), sessionmaker=session)
    state = SimpleNamespace(guild_id=101, account_id=uuid4(), can_manage_github_agents=False)
    await module.load_waiting_view(state, runtime=runtime, user_id=7)
    assert factory.call_args.kwargs["requests"] == ()
    assert factory.call_args.kwargs["own_requests"] == ()
    assert factory.call_args.kwargs["thread_labels"] == {}


async def test_here_from_ordinary_channel_filters_reserved_sibling_names(monkeypatch):
    from contextlib import asynccontextmanager

    from daimon.adapters.discord.commands import here as module

    @asynccontextmanager
    async def session():
        yield MagicMock()

    tap = interaction()
    ordinary = MagicMock(spec=discord.TextChannel)
    private = MagicMock(spec=discord.TextChannel)
    ordinary.id, private.id = 3, 2
    ordinary.name, private.name = "general", "private candidate"
    ordinary.category_id = private.category_id = 5
    permissions = SimpleNamespace(view_channel=True, read_message_history=True)
    ordinary.permissions_for.return_value = private.permissions_for.return_value = permissions
    tap.guild = MagicMock(spec=discord.Guild)
    tap.guild.channels = [ordinary, private]
    tap.guild.fetch_channel = AsyncMock(return_value=ordinary)
    tap.user = MagicMock(spec=discord.Member)
    tap.channel = ordinary
    tap.response.defer, tap.edit_original_response = AsyncMock(), AsyncMock()
    runtime = tap.client.runtime
    runtime.sessionmaker, runtime.anthropic, runtime.deployment_default = (
        session,
        MagicMock(),
        MagicMock(),
    )
    runtime.settings.github_app = SimpleNamespace(app_id=None, private_key=None)
    runtime.settings.mcp = SimpleNamespace(public_url=None)
    monkeypatch.setattr(module, "resolve_tenant_for_interaction", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(module, "find_platform_principal", AsyncMock(return_value=None))
    monkeypatch.setattr(module, "is_guild_admin", lambda _: False)
    monkeypatch.setattr(module, "load_access_policy", AsyncMock(return_value=TenantAccessPolicy()))
    card = AsyncMock(
        return_value=HereCard(
            agent_name=None,
            tier=None,
            channel_rule=ChannelRule(),
            effective_readers="any",
            effective_writers="any",
            who_may_answer="",
            reads_kept_inside=False,
            text="",
        )
    )
    monkeypatch.setattr(module, "load_here_card", card)
    monkeypatch.setattr(module, "build_here_embed", lambda _: discord.Embed())
    await module.HereCog.here.callback.__wrapped__(MagicMock(), tap)
    assert card.await_args.kwargs["visible_channel_ids"] == {"3"}
    assert card.await_args.kwargs["category_channels_bot_can_view"] == []


@pytest.mark.parametrize("target", ["111111111111111112", "111111111111111113"])
async def test_rule_copy_checks_target_metadata_and_keeps_agent_name_semantics(target, monkeypatch):
    from contextlib import asynccontextmanager

    from daimon.adapters.mcp.tools import channel_rules as module
    from fastmcp.exceptions import ToolError

    source = MagicMock(spec=discord.TextChannel)
    source.id, source.guild.id, source.name = int(target), 101, "general"
    client = SimpleNamespace(fetch_channel=AsyncMock(return_value=source))

    @asynccontextmanager
    async def rest(token):
        yield client

    monkeypatch.setattr(module, "rest_client", rest)
    monkeypatch.setattr(module, "_require_bot_token", lambda _: "fake")
    monkeypatch.setattr(module, "require_scope", lambda *args: None)
    monkeypatch.setattr(module, "mcp_subject", lambda *args, **kwargs: MagicMock())
    write = AsyncMock(side_effect=LookupError("authorized core reached"))
    monkeypatch.setattr(module, "set_channel_rule", write)
    runtime = SimpleNamespace(
        settings=SimpleNamespace(
            recruitment=RecruitmentSettings(
                tenants={
                    "101": {"channel_ids": ["111111111111111112"], "recruiter_user_ids": ["7"]}
                }
            ),
            mcp=SimpleNamespace(public_url=None),
        ),
        client=MagicMock(),
        session_factory=MagicMock(),
        deployment_default=MagicMock(),
    )
    auth = SimpleNamespace(
        platform="discord", external_id="101", tenant_id=uuid4(), account_id=uuid4(), is_admin=True
    )
    with pytest.raises(
        ToolError if target.endswith("2") else LookupError,
        match="Source unavailable|authorized core reached",
    ):
        await module._set_channel_rule_impl(
            runtime, auth, channel_id=target, readers="inside", copy_from="daimon"
        )
    if target.endswith("2"):
        write.assert_not_awaited()
    else:
        assert write.await_args.kwargs["copy_from"] == "daimon"
        assert all(call.args == (int(target),) for call in client.fetch_channel.await_args_list)
