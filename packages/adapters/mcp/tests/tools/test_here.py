"""The model-facing card cannot inherit a channel admin's private view."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools.discord._models import ChannelRow
from daimon.adapters.mcp.tools.here import (  # pyright: ignore[reportPrivateUsage]
    _require_thread_in_channel,
    _setter_display_name,
    _where_am_i_impl,
)
from daimon.core.stores.domain import Role
from fastmcp.exceptions import ToolError


def _auth(*, platform: str = "discord") -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.ADMIN,
        platform=platform,
        external_id="123456",
        platform_user_id="123",
        is_admin=True,
    )


def _runtime() -> MagicMock:
    runtime = MagicMock()
    runtime.settings.github.fallback_pat = None
    runtime.settings.github.app_id = None
    runtime.settings.github.app_private_key = None
    runtime.settings.mcp.public_url = None
    runtime.session_factory.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    runtime.session_factory.return_value.__aexit__ = AsyncMock(return_value=None)
    return runtime


async def test_admin_channel_turn_gets_member_card_and_unknown_bot_visibility() -> None:
    runtime = _runtime()
    caller = SimpleNamespace(inside_channel_id=None, home_place=lambda _id: None)
    rows = [
        ChannelRow(id="here", name="here", type="text", category_id="cat"),
        ChannelRow(id="sibling", name="sibling", type="text", category_id="cat"),
    ]
    with (
        patch(
            "daimon.adapters.mcp.tools.here.load_caller_view",
            new=AsyncMock(return_value=caller),
        ),
        patch(
            "daimon.adapters.mcp.tools.here._list_channels_impl", new=AsyncMock(return_value=rows)
        ),
        patch("daimon.adapters.mcp.tools.here.load_here_card", new=AsyncMock()) as load,
    ):
        await _where_am_i_impl(runtime, _auth(), "here", None)
    assert load.await_args.kwargs["is_admin"] is False
    assert load.await_args.kwargs["bot_can_view"] is None
    assert load.await_args.kwargs["category_id"] == "cat"
    assert "category_channels_bot_can_view" not in load.await_args.kwargs
    assert callable(load.await_args.kwargs["resolve_setter_display"])


async def test_slack_setter_uses_plain_display_name() -> None:
    client = MagicMock()
    client.users_info = AsyncMock(
        return_value={"user": {"profile": {"display_name": "Alex"}, "name": "fallback"}}
    )
    with patch(
        "daimon.adapters.mcp.tools.here.slack_web_client", new=AsyncMock(return_value=client)
    ):
        assert await _setter_display_name(_runtime(), _auth(platform="slack"), "U123") == "Alex"


async def test_discord_setter_uses_plain_member_name() -> None:
    member = SimpleNamespace(display_name="Alex")
    guild = MagicMock()
    guild.fetch_member = AsyncMock(return_value=member)
    client = MagicMock()
    client.fetch_guild = AsyncMock(return_value=guild)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=None)
    with (
        patch("daimon.adapters.mcp.tools.here._require_bot_token", return_value="token"),
        patch("daimon.adapters.mcp.tools.here.rest_client", return_value=context),
    ):
        assert await _setter_display_name(_runtime(), _auth(), "789") == "Alex"


async def test_discord_thread_must_belong_to_requested_channel() -> None:
    client = MagicMock()
    client.fetch_channel = AsyncMock(return_value=MagicMock(spec=discord.Thread, parent_id=999))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=None)
    with (
        patch("daimon.adapters.mcp.tools.here._require_bot_token", return_value="token"),
        patch("daimon.adapters.mcp.tools.here.rest_client", return_value=context),
        pytest.raises(ToolError, match="thread does not belong to this channel"),
    ):
        await _require_thread_in_channel(_runtime(), _auth(), "222", "333")


async def test_slack_thread_must_belong_to_requested_channel() -> None:
    client = MagicMock()
    client.conversations_replies = AsyncMock(return_value={"messages": [{"ts": "111.000001"}]})
    with (
        patch(
            "daimon.adapters.mcp.tools.here.slack_web_client", new=AsyncMock(return_value=client)
        ),
        pytest.raises(ToolError, match="thread does not belong to this channel"),
    ):
        await _require_thread_in_channel(_runtime(), _auth(platform="slack"), "C1", "222.000002")


async def test_tool_checks_thread_before_loading_card() -> None:
    caller = SimpleNamespace(inside_channel_id=None, home_place=lambda _id: None)
    with (
        patch(
            "daimon.adapters.mcp.tools.here.load_caller_view", new=AsyncMock(return_value=caller)
        ),
        patch(
            "daimon.adapters.mcp.tools.here._list_channels_impl",
            new=AsyncMock(return_value=[ChannelRow(id="here", name="here", type="text")]),
        ),
        patch(
            "daimon.adapters.mcp.tools.here._require_thread_in_channel",
            new=AsyncMock(side_effect=ToolError("thread does not belong to this channel")),
        ) as check,
        patch("daimon.adapters.mcp.tools.here.load_here_card", new=AsyncMock()) as load,
        pytest.raises(ToolError, match="thread does not belong to this channel"),
    ):
        await _where_am_i_impl(_runtime(), _auth(), "here", "thread")
    check.assert_awaited_once()
    load.assert_not_awaited()


@pytest.mark.parametrize(
    ("isolation", "rows", "message"),
    [
        ("home", (), "outside this conversation's home"),
        (None, (), "missing channel access"),
    ],
)
async def test_tool_refuses_home_boundary_and_missing_channel_access(
    isolation: str | None, rows: tuple[ChannelRow, ...], message: str
) -> None:
    runtime = _runtime()
    caller = SimpleNamespace(inside_channel_id=None, home_place=lambda _id: isolation)
    with (
        patch(
            "daimon.adapters.mcp.tools.here.load_caller_view",
            new=AsyncMock(return_value=caller),
        ),
        patch(
            "daimon.adapters.mcp.tools.here._list_channels_impl", new=AsyncMock(return_value=rows)
        ),
        patch("daimon.adapters.mcp.tools.here.load_here_card", new=AsyncMock()) as load,
        pytest.raises(ToolError, match=message),
    ):
        await _where_am_i_impl(runtime, _auth(), "here", None)
    load.assert_not_awaited()


async def test_teams_channel_turn_lists_its_channels_and_names_the_setter_from_storage() -> None:
    caller = SimpleNamespace(inside_channel_id=None, home_place=lambda _id: None)
    rows = [SimpleNamespace(id="19:ops@thread.tacv2"), SimpleNamespace(id="19:dev@thread.tacv2")]
    with (
        patch(
            "daimon.adapters.mcp.tools.here.load_caller_view", new=AsyncMock(return_value=caller)
        ),
        patch(
            "daimon.adapters.mcp.tools.here._teams_list_channels_impl",
            new=AsyncMock(return_value=rows),
        ),
        patch("daimon.adapters.mcp.tools.here.load_here_card", new=AsyncMock()) as load,
    ):
        await _where_am_i_impl(
            _runtime(),
            _auth(platform="teams"),
            "19:ops@thread.tacv2",
            "19:ops@thread.tacv2;messageid=1",
        )
    kwargs = load.await_args.kwargs
    assert kwargs["visible_channel_ids"] == {row.id for row in rows}, "the caller's channels"
    assert kwargs["thread_id"] == "19:ops@thread.tacv2;messageid=1", "the post's thread"
    assert kwargs["resolve_setter_display"] is None, "the card falls back to the stored name"


@pytest.mark.parametrize(
    "thread_id",
    ["19:dev@thread.tacv2;messageid=1", "19:ops@thread.tacv2", "a:chat;setup=abc"],
)
async def test_teams_thread_must_name_the_requested_channel(thread_id: str) -> None:
    with pytest.raises(ToolError, match="thread does not belong to this channel"):
        await _require_thread_in_channel(
            _runtime(), _auth(platform="teams"), "19:ops@thread.tacv2", thread_id
        )


async def test_a_teams_channel_the_caller_cannot_list_is_refused() -> None:
    caller = SimpleNamespace(inside_channel_id=None, home_place=lambda _id: None)
    with (
        patch(
            "daimon.adapters.mcp.tools.here.load_caller_view", new=AsyncMock(return_value=caller)
        ),
        patch(
            "daimon.adapters.mcp.tools.here._teams_list_channels_impl",
            new=AsyncMock(return_value=[]),
        ),
        patch("daimon.adapters.mcp.tools.here.load_here_card", new=AsyncMock()) as load,
        pytest.raises(ToolError, match="missing channel access"),
    ):
        await _where_am_i_impl(_runtime(), _auth(platform="teams"), "19:ops@thread.tacv2", None)
    load.assert_not_awaited()
