"""Discord agent post transport selection and routing."""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.post_transport import DiscordPostTransport, _webhooks


@pytest.fixture(autouse=True)
def clear_webhook_cache() -> None:
    _webhooks.clear()


def _world(*, manage_webhooks: bool = True) -> tuple[MagicMock, MagicMock, MagicMock]:
    client = MagicMock()
    client.user.id = 10
    client.http.channel_webhooks = AsyncMock(return_value=[])
    parent = MagicMock(spec=discord.TextChannel)
    parent.id = 20
    parent.guild.me = MagicMock(spec=discord.Member)
    parent.permissions_for.return_value.manage_webhooks = manage_webhooks
    parent.send = AsyncMock()
    hook = MagicMock(spec=discord.Webhook)
    hook.id = 30
    hook.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    parent.create_webhook = AsyncMock(return_value=hook)
    return client, parent, hook


async def test_agent_uses_channel_webhook_with_wait_and_identity() -> None:
    client, channel, hook = _world()
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url="https://x/y", builtin=False
    )
    await transport.send(content="answer")
    hook.send.assert_awaited_once()
    assert hook.send.call_args.kwargs["wait"] is True
    assert hook.send.call_args.kwargs["username"] == "Research"
    assert hook.send.call_args.kwargs["avatar_url"] == "https://x/y"
    channel.send.assert_not_awaited()


async def test_missing_permission_falls_back_to_bot() -> None:
    client, channel, hook = _world(manage_webhooks=False)
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    await transport.send(content="answer")
    channel.send.assert_awaited_once_with(content="answer")
    hook.send.assert_not_awaited()
    assert transport.fallback_used


async def test_thread_post_targets_parent_webhook_and_thread() -> None:
    client, parent, hook = _world()
    thread = MagicMock(spec=discord.Thread)
    thread.id = 21
    thread.parent = parent
    thread.locked = False
    thread.send = AsyncMock()
    transport = DiscordPostTransport(
        client, thread, name="Research", avatar_url=None, builtin=False
    )
    await transport.send(content="answer")
    parent.create_webhook.assert_awaited_once()
    assert hook.send.call_args.kwargs["thread"] is thread
    assert hook.send.call_args.kwargs["wait"] is True


async def test_locked_thread_uses_bot_fallback() -> None:
    client, parent, hook = _world()
    thread = MagicMock(spec=discord.Thread)
    thread.parent = parent
    thread.locked = True
    thread.send = AsyncMock()
    transport = DiscordPostTransport(
        client, thread, name="Research", avatar_url=None, builtin=False
    )
    await transport.send(content="answer")
    thread.send.assert_awaited_once_with(content="answer")
    hook.send.assert_not_awaited()


async def test_builtin_uses_bot_without_webhook_lookup() -> None:
    client, channel, _ = _world()
    transport = DiscordPostTransport(client, channel, name="Daimon", avatar_url=None, builtin=True)
    await transport.send(content="answer")
    client.http.channel_webhooks.assert_not_awaited()
    assert not transport.fallback_used


async def test_webhook_message_edits_and_deletes_through_webhook() -> None:
    client, channel, hook = _world()
    message = MagicMock(spec=discord.Message)
    message.id = 40
    message.webhook_id = hook.id
    message.edit = AsyncMock()
    message.delete = AsyncMock()
    hook.edit_message = AsyncMock(return_value=message)
    hook.delete_message = AsyncMock()
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    await transport.edit(message, content="updated")
    await transport.delete(message)
    hook.edit_message.assert_awaited_once_with(40, content="updated")
    hook.delete_message.assert_awaited_once_with(40)
    message.edit.assert_not_awaited()
    message.delete.assert_not_awaited()


async def test_bot_message_edits_and_deletes_through_bot() -> None:
    client, channel, hook = _world()
    message = MagicMock(spec=discord.Message)
    message.webhook_id = None
    message.edit = AsyncMock()
    message.delete = AsyncMock()
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    await transport.edit(message, content="updated")
    await transport.delete(message)
    message.edit.assert_awaited_once_with(content="updated")
    message.delete.assert_awaited_once()
    hook.send.assert_not_awaited()


async def test_deleted_webhook_edit_posts_replacement() -> None:
    client, channel, hook = _world()
    message = MagicMock(spec=discord.Message)
    message.id = 40
    message.webhook_id = hook.id
    hook.edit_message = AsyncMock(
        side_effect=discord.NotFound(
            MagicMock(status=404), {"code": 10015, "message": "Unknown Webhook"}
        )
    )
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    replacement = await transport.edit(message, content="updated", attachments=[])
    assert replacement is hook.send.return_value
    assert hook.send.call_args.kwargs["content"] == "updated"
