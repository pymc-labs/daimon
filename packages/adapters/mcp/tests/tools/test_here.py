"""The model-facing card cannot inherit a channel admin's private view."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools.discord._models import ChannelRow
from daimon.adapters.mcp.tools.here import _where_am_i_impl  # pyright: ignore[reportPrivateUsage]
from daimon.core.stores.domain import Role
from fastmcp.exceptions import ToolError


def _auth() -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.ADMIN,
        platform="discord",
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
    caller = SimpleNamespace(inside_channel_id=None, isolated_place=lambda _id: None)
    rows = [
        ChannelRow(id="here", name="here", type="text", category_id="cat"),
        ChannelRow(id="sibling", name="sibling", type="text", category_id="cat"),
    ]
    with (
        patch(
            "daimon.adapters.mcp.tools.here.load_caller_isolation",
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
    assert "category_channels_bot_can_view" not in load.await_args.kwargs


@pytest.mark.parametrize(
    ("isolation", "rows", "message"),
    [
        ("isolated", (), "isolated channel's line"),
        (None, (), "missing channel access"),
    ],
)
async def test_tool_refuses_isolation_line_and_missing_channel_access(
    isolation: str | None, rows: tuple[ChannelRow, ...], message: str
) -> None:
    runtime = _runtime()
    caller = SimpleNamespace(inside_channel_id=None, isolated_place=lambda _id: isolation)
    with (
        patch(
            "daimon.adapters.mcp.tools.here.load_caller_isolation",
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
