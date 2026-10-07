"""Discord agent post transport selection and routing."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.adapters.discord.post_transport import (
    DiscordPostTransport,
    _rate_limit_counter,
    _unavailable_until,
    _webhooks,
)


@pytest.fixture(autouse=True)
def clear_webhook_cache() -> None:
    _webhooks.clear()
    _unavailable_until.clear()


def test_webhook_429s_are_counted_from_discord_library_logger() -> None:
    before = _rate_limit_counter.count
    logging.getLogger("discord.webhook.async_").warning(
        "Webhook ID %s is rate limited. Retrying in %.2f seconds.", 30, 1.0
    )
    assert _rate_limit_counter.count == before + 1


def _world(*, manage_webhooks: bool = True) -> tuple[MagicMock, MagicMock, MagicMock]:
    client = MagicMock()
    client.user.id = 10
    client.application_id = 10
    client.http.channel_webhooks = AsyncMock(return_value=[])
    parent = MagicMock(spec=discord.TextChannel)
    parent.id = 20
    parent.guild.me = MagicMock(spec=discord.Member)
    parent.permissions_for.return_value.manage_webhooks = manage_webhooks
    parent.send = AsyncMock()
    hook = MagicMock(spec=discord.Webhook)
    hook.id = 30
    hook.token = "in-memory-test-token"
    hook.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    parent.create_webhook = AsyncMock(return_value=hook)
    return client, parent, hook


async def test_agent_uses_channel_webhook_with_wait_and_identity() -> None:
    client, channel, hook = _world()
    view = MagicMock(spec=discord.ui.View)
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url="https://x/y", builtin=False
    )
    await transport.send(content="answer", view=view)
    hook.send.assert_awaited_once()
    assert hook.send.call_args.kwargs["wait"] is True
    assert hook.send.call_args.kwargs["username"] == "Research"
    assert hook.send.call_args.kwargs["avatar_url"] == "https://x/y"
    assert hook.send.call_args.kwargs["view"] is view
    channel.send.assert_not_awaited()


async def test_terminal_flush_without_card_omits_none_view() -> None:
    client, channel, hook = _world()
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    lifecycle = DiscordTurnLifecycle(
        send=transport.send,
        edit=transport.edit,
        agent_name="Research",
        model_id="claude-sonnet-4-6",
    )
    await lifecycle._flush_terminal()  # pyright: ignore[reportPrivateUsage]
    assert "view" not in hook.send.call_args.kwargs


async def test_missing_permission_falls_back_to_bot() -> None:
    client, channel, hook = _world(manage_webhooks=False)
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    await transport.send(content="answer")
    channel.send.assert_awaited_once_with(content="answer")
    hook.send.assert_not_awaited()
    assert transport.fallback_used
    assert channel.id in _unavailable_until


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


async def test_concurrent_turns_create_only_one_channel_webhook() -> None:
    client, channel, hook = _world()

    async def create(*, name: str) -> MagicMock:
        await asyncio.sleep(0)
        return hook

    channel.create_webhook = AsyncMock(side_effect=create)
    first = DiscordPostTransport(client, channel, name="Research", avatar_url=None, builtin=False)
    second = DiscordPostTransport(client, channel, name="Writer", avatar_url=None, builtin=False)
    await asyncio.gather(first.send(content="one"), second.send(content="two"))
    channel.create_webhook.assert_awaited_once_with(name="Daimon agents")
    assert hook.send.await_count == 2


async def test_webhook_403_falls_back_with_name_and_caches_unavailability() -> None:
    client, channel, hook = _world()
    hook.send = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), {"message": "Missing Permissions"})
    )
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    await transport.send(content="answer")
    channel.send.assert_awaited_once_with(content="**Research** answer")


async def test_webhook_limit_is_cached_for_ten_minutes() -> None:
    client, channel, _ = _world()
    channel.create_webhook = AsyncMock(
        side_effect=discord.HTTPException(
            MagicMock(status=400), {"code": 30007, "message": "Maximum webhooks reached"}
        )
    )
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    await transport.send(content="one")
    await transport.send(content="two")
    channel.create_webhook.assert_awaited_once()
    client.http.channel_webhooks.assert_awaited_once()


async def test_thread_pool_selects_by_thread_id_and_edits_by_message_webhook_id() -> None:
    client, parent, _ = _world()
    hooks = [MagicMock(spec=discord.Webhook) for _ in range(3)]
    for offset, hook in enumerate(hooks):
        hook.id = 30 + offset
        hook.token = "in-memory-test-token"
        hook.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
        hook.edit_message = AsyncMock(return_value=MagicMock(spec=discord.Message))
    parent.create_webhook = AsyncMock(side_effect=hooks)
    thread = MagicMock(spec=discord.Thread)
    thread.id = 23
    thread.parent = parent
    thread.locked = False
    transport = DiscordPostTransport(
        client, thread, name="Research", avatar_url=None, builtin=False
    )
    for _ in range(3):
        await transport.send(content="answer")
    assert parent.create_webhook.await_count == 3
    assert hooks[2].send.await_count == 3
    message = MagicMock(spec=discord.Message)
    message.id = 40
    message.webhook_id = hooks[1].id
    message.application_id = client.application_id
    await transport.edit(message, content="updated")
    hooks[1].edit_message.assert_awaited_once_with(40, content="updated", thread=thread)


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
    client.user.id = 99  # the bot user ID is not the application ID
    message = MagicMock(spec=discord.Message)
    message.id = 40
    message.webhook_id = hook.id
    message.application_id = client.application_id
    message.edit = AsyncMock()
    message.delete = AsyncMock()
    hook.edit_message = AsyncMock(return_value=message)
    hook.delete_message = AsyncMock()
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    await transport._webhook()  # pyright: ignore[reportPrivateUsage]
    channel.create_webhook.reset_mock()
    channel.permissions_for.return_value.manage_webhooks = False
    await transport.edit(message, content="updated")
    await transport.delete(message)
    hook.edit_message.assert_awaited_once_with(40, content="updated")
    hook.delete_message.assert_awaited_once_with(40)
    channel.create_webhook.assert_not_awaited()
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
    message.application_id = client.application_id
    hook.edit_message = AsyncMock(
        side_effect=discord.NotFound(
            MagicMock(status=404), {"code": 10015, "message": "Unknown Webhook"}
        )
    )
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    await transport._webhook()  # pyright: ignore[reportPrivateUsage]
    replacement = await transport.edit(message, content="updated", attachments=[])
    assert replacement is hook.send.return_value
    assert hook.send.call_args.kwargs["content"] == "updated"
