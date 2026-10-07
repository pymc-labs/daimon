"""REST-only Discord agent post transport."""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.mcp.tools.discord import _post_transport
from daimon.core.agent_identity import AgentIdentity


async def test_agent_send_uses_webhook_identity_and_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock(spec=discord.Client)
    channel = MagicMock(spec=discord.TextChannel)
    message = MagicMock(spec=discord.Message)
    hook = MagicMock(spec=discord.Webhook)
    hook.send = AsyncMock(return_value=message)
    monkeypatch.setattr(_post_transport, "own_webhook", AsyncMock(return_value=hook))
    sent = await _post_transport.send_agent_message(
        client,
        channel,
        AgentIdentity("Research", "https://example.com/a.png", False),
        content="answer",
    )
    assert sent is message
    assert hook.send.call_args.kwargs["wait"] is True
    assert hook.send.call_args.kwargs["username"] == "Research"
    assert hook.send.call_args.kwargs["avatar_url"] == "https://example.com/a.png"


async def test_missing_webhook_uses_one_name_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock(spec=discord.Client)
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    monkeypatch.setattr(_post_transport, "own_webhook", AsyncMock(return_value=None))
    await _post_transport.send_agent_message(
        client,
        channel,
        AgentIdentity("Research", None, False),
        content="answer",
    )
    channel.send.assert_awaited_once_with(content="**Research** answer", files=[])


async def test_webhook_edit_and_delete_use_webhook_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock(spec=discord.Client)
    channel = MagicMock(spec=discord.TextChannel)
    message = MagicMock(spec=discord.Message)
    message.id = 40
    message.webhook_id = 30
    message.edit = AsyncMock()
    message.delete = AsyncMock()
    hook = MagicMock(spec=discord.Webhook)
    hook.id = 30
    hook.edit_message = AsyncMock()
    hook.delete_message = AsyncMock()
    monkeypatch.setattr(_post_transport, "own_webhook", AsyncMock(return_value=hook))
    await _post_transport.edit_own_message(client, channel, message, content="updated")
    await _post_transport.delete_own_message(client, channel, message)
    hook.edit_message.assert_awaited_once_with(40, content="updated")
    hook.delete_message.assert_awaited_once_with(40)
    message.edit.assert_not_awaited()
    message.delete.assert_not_awaited()


async def test_deleted_webhook_edit_posts_update_as_new_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock(spec=discord.Client)
    channel = MagicMock(spec=discord.TextChannel)
    message = MagicMock(spec=discord.Message)
    message.webhook_id = 30
    message.author.name = "Research"
    new_message = MagicMock(spec=discord.Message)
    monkeypatch.setattr(_post_transport, "own_webhook", AsyncMock(return_value=None))
    send = AsyncMock(return_value=new_message)
    monkeypatch.setattr(_post_transport, "send_agent_message", send)
    result = await _post_transport.edit_own_message(client, channel, message, content="updated")
    assert result is new_message
    assert send.call_args.kwargs["content"] == "updated"
