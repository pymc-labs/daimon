"""The channel-target resolver shared by the budget, environment, admin and isolation tools."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import _channel_target as tool_module
from daimon.adapters.mcp.tools._channel_target import (
    ChannelTarget,
    parse_channel_target,
    resolve_channel,
)
from daimon.core.stores.domain import Role
from fastmcp.exceptions import ToolError

TEAMS_CHANNEL = "19:c@thread.tacv2"


@pytest.mark.parametrize(
    ("platform", "raw", "channel"),
    [
        ("slack", " C1:1717.5 ", "C1"),
        ("teams", f"{TEAMS_CHANNEL};messageid=17", TEAMS_CHANNEL),
        ("teams", TEAMS_CHANNEL, TEAMS_CHANNEL),
        ("discord", "123", "123"),
    ],
)
def test_parse_splits_a_thread_id_into_its_channel(platform: str, raw: str, channel: str) -> None:
    assert parse_channel_target(platform, raw) == ChannelTarget(channel), (
        "a thread id names its channel; a Teams channel id keeps its own colon"
    )


@pytest.mark.parametrize(
    ("platform", "raw", "message"),
    [
        ("discord", " ", "channel_id is empty"),
        ("slack", ":1717.5", "channel_id is empty"),
        ("teams", ";messageid=17", "channel_id is empty"),
    ],
)
def test_parse_refuses_an_empty_id(platform: str, raw: str, message: str) -> None:
    with pytest.raises(ToolError, match=message):
        parse_channel_target(platform, raw)


async def test_a_discord_id_that_is_not_a_snowflake_is_refused_before_any_lookup() -> None:
    runtime = MagicMock()
    with pytest.raises(ToolError, match="not a Discord channel id"):
        await resolve_channel(runtime, _auth("discord"), "general", lenient=True)
    assert runtime.mock_calls == [], "nothing is looked up"


def _auth(platform: str) -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        platform=platform,
        external_id="T1",
        platform_user_id="U1",
    )


async def test_a_discord_thread_resolves_to_its_parent_and_keeps_the_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def visible(runtime: McpRuntime, auth: AuthIdentity, channel_id: str) -> str:
        if channel_id == "404":
            raise ToolError("not visible")
        return "222" if channel_id == "999" else channel_id

    monkeypatch.setattr(tool_module, "resolve_visible_channel", visible)
    auth = _auth("discord")

    assert await resolve_channel(MagicMock(), auth, "999") == ChannelTarget("222", "999"), (
        "a thread names its parent, and the thread is kept for its own seal"
    )
    assert await resolve_channel(MagicMock(), auth, "222") == ChannelTarget("222")
    with pytest.raises(ToolError, match="not visible"):
        await resolve_channel(MagicMock(), auth, "404")
    assert await resolve_channel(MagicMock(), auth, "404", lenient=True) == ChannelTarget("404"), (
        "a lenient lookup takes a refused id as given"
    )


async def test_a_lenient_slack_or_teams_id_is_split_without_a_lookup() -> None:
    runtime = MagicMock()
    slack = await resolve_channel(runtime, _auth("slack"), "C1:1717.5", lenient=True)
    teams = await resolve_channel(
        runtime, _auth("teams"), f"{TEAMS_CHANNEL};messageid=17", lenient=True
    )
    assert (slack, teams) == (ChannelTarget("C1"), ChannelTarget(TEAMS_CHANNEL)), (
        "clearing needs only the channel"
    )
    assert runtime.mock_calls == [], "no platform call is made"


async def test_other_platforms_are_refused() -> None:
    with pytest.raises(ToolError, match="only for Discord, Slack and Teams"):
        await resolve_channel(MagicMock(), _auth("cli"), "c1")


@pytest.mark.parametrize(("is_private", "caller_in_channel"), [(False, False), (True, True)])
async def test_slack_channel_resolves_when_the_caller_can_see_it(
    monkeypatch: pytest.MonkeyPatch, is_private: bool, caller_in_channel: bool
) -> None:
    client = _slack_client(monkeypatch, is_private=is_private, caller_in_channel=caller_in_channel)
    resolved = await resolve_channel(MagicMock(), _auth("slack"), "C1:1717.5")
    assert resolved == ChannelTarget("C1"), "a thread resolves to its channel"
    client.conversations_info.assert_awaited_once_with(channel="C1")


async def test_slack_channel_hidden_from_the_caller_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _slack_client(monkeypatch, is_private=True, caller_in_channel=False)
    with pytest.raises(ToolError, match="missing channel access"):
        await resolve_channel(MagicMock(), _auth("slack"), "C1")


def _slack_client(
    monkeypatch: pytest.MonkeyPatch, *, is_private: bool, caller_in_channel: bool
) -> MagicMock:
    client = MagicMock()
    client.conversations_info = AsyncMock(
        return_value={"channel": {"id": "C1", "is_member": True, "is_private": is_private}}
    )
    client.users_info = AsyncMock(return_value={"user": {"id": "U1"}})
    client.conversations_members = AsyncMock(
        return_value={"members": ["U1"] if caller_in_channel else ["U2"]}
    )

    async def fake_client(runtime: object, *, team_id: str) -> MagicMock:
        assert team_id == "T1", "resolved in the caller's own workspace"
        return client

    monkeypatch.setattr(tool_module, "slack_web_client", fake_client)
    return client


@pytest.mark.parametrize("member", [True, False])
async def test_a_teams_thread_resolves_to_its_channel_for_a_member_only(
    monkeypatch: pytest.MonkeyPatch, member: bool
) -> None:
    channel = TEAMS_CHANNEL
    client = MagicMock()
    client.is_member = AsyncMock(return_value=member)
    located: list[str] = []

    async def locate(runtime: McpRuntime, auth: AuthIdentity, _client: object, cid: str) -> object:
        located.append(cid)
        return SimpleNamespace(channel_id=cid)

    monkeypatch.setattr(tool_module, "require_client", lambda runtime, auth: (client, "u-1"))
    monkeypatch.setattr(tool_module, "locate_channel", locate)
    resolve = resolve_channel(MagicMock(), _auth("teams"), f"{channel};messageid=17")
    if member:
        assert await resolve == ChannelTarget(channel), "a member resolves the thread's channel"
    else:
        with pytest.raises(ToolError, match="not a member"):
            await resolve
    assert located == [channel], "a thread id is looked up as its channel"
    client.is_member.assert_awaited_once_with(channel, "u-1")
