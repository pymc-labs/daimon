"""Where a watch may report is checked before it is saved: the owner's own DM only,
and never a new thread out of a private thread or a DM."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.mcp.tools import watches as tools
from daimon.adapters.mcp.tools.watches import DESTINATIONS, require_delivery_allowed
from daimon.core.config import DirectMessagePolicy
from fastmcp.exceptions import ToolError


def _runtime(policy: DirectMessagePolicy | None = None):
    @asynccontextmanager
    async def session():
        yield MagicMock()

    runtime = MagicMock()
    runtime.session_factory = session
    runtime.settings.direct_message_policies = {} if policy is None else {TENANT: policy}
    return runtime


TENANT = uuid.uuid4()
AUTH = SimpleNamespace(platform="discord", platform_user_id="555", tenant_id=TENANT)


def _origin(thread_id: str = "4242", parent_id: str = "4241"):
    return SimpleNamespace(thread_id=thread_id, parent_channel_id=parent_id)


@pytest.fixture
def discord_rest(monkeypatch):
    channels: dict[int, object] = {}

    @asynccontextmanager
    async def rest_client(_token):
        yield SimpleNamespace(fetch_channel=AsyncMock(side_effect=lambda cid: channels[cid]))

    monkeypatch.setattr(tools, "rest_client", rest_client)
    monkeypatch.setattr(tools, "_require_bot_token", lambda _runtime: "token")
    monkeypatch.setattr(tools, "dm_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(tools, "require_dm_recipient_allowed", AsyncMock())
    return channels


def _thread(kind: discord.ChannelType, parent_id: int = 4241) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.type = kind
    thread.parent_id = parent_id
    return thread


async def test_thread_delivery_needs_no_extra_check(discord_rest):
    await require_delivery_allowed(_runtime(), AUTH, _origin("dm:x", "dm:x"), "thread")  # type: ignore[arg-type]


@pytest.mark.parametrize("delivery", ["dm", "new_thread"])
async def test_a_watch_made_in_a_dm_reports_in_that_dm(discord_rest, delivery):
    with pytest.raises(ToolError, match="made in a DM"):
        await require_delivery_allowed(_runtime(), AUTH, _origin("dm:x", "dm:x"), delivery)  # type: ignore[arg-type]


async def test_other_platforms_refuse_dm_and_new_thread(discord_rest):
    slack = SimpleNamespace(platform="slack", platform_user_id="U1", tenant_id=TENANT)
    with pytest.raises(ToolError, match="only report in this thread"):
        await require_delivery_allowed(_runtime(), slack, _origin(), "dm")  # type: ignore[arg-type]


async def test_dm_delivery_needs_dms_on_and_the_owner_allowed(discord_rest, monkeypatch):
    await require_delivery_allowed(_runtime(), AUTH, _origin(), "dm")  # type: ignore[arg-type]
    tools.require_dm_recipient_allowed.assert_awaited_once()  # type: ignore[attr-defined]
    assert tools.require_dm_recipient_allowed.await_args.kwargs == {"recipient_id": "555"}  # type: ignore[attr-defined]
    blocked = _runtime(DirectMessagePolicy(mode="allowlist", recipient_ids=["777"]))
    with pytest.raises(ToolError, match="Ask an admin or an owner"):
        await require_delivery_allowed(blocked, AUTH, _origin(), "dm")  # type: ignore[arg-type]
    monkeypatch.setattr(tools, "dm_enabled", AsyncMock(return_value=False))
    with pytest.raises(ToolError, match="Ask an admin or an owner"):
        await require_delivery_allowed(_runtime(), AUTH, _origin(), "dm")  # type: ignore[arg-type]


async def test_new_thread_is_refused_from_a_private_thread(discord_rest):
    discord_rest[4241] = MagicMock(spec=discord.TextChannel)
    discord_rest[4242] = _thread(discord.ChannelType.private_thread)
    with pytest.raises(ToolError, match="private thread"):
        await require_delivery_allowed(_runtime(), AUTH, _origin(), "new_thread")  # type: ignore[arg-type]
    discord_rest[4242] = _thread(discord.ChannelType.public_thread)
    await require_delivery_allowed(_runtime(), AUTH, _origin(), "new_thread")  # type: ignore[arg-type]


async def test_new_thread_needs_a_text_channel_parent(discord_rest):
    discord_rest[4241] = MagicMock(spec=discord.ForumChannel)
    discord_rest[4242] = _thread(discord.ChannelType.public_thread)
    with pytest.raises(ToolError, match="cannot be started"):
        await require_delivery_allowed(_runtime(), AUTH, _origin(), "new_thread")  # type: ignore[arg-type]


def test_every_delivery_has_a_plain_destination():
    assert set(DESTINATIONS) == {"thread", "dm", "new_thread"}
    assert DESTINATIONS["dm"] == "owner's DM"
