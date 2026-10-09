"""Discord agent post transport selection and routing."""

import asyncio
import io
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import structlog
from daimon.adapters.discord import post_transport
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.adapters.discord.post_transport import (
    DiscordPostTransport,
    _rate_limit_counter,
    _send_unavailable_until,
    _unavailable_until,
    _webhooks,
)


@pytest.fixture(autouse=True)
def clear_webhook_cache() -> None:
    _webhooks.clear()
    _unavailable_until.clear()
    _send_unavailable_until.clear()
    post_transport._creation_tasks.clear()  # pyright: ignore[reportPrivateUsage]
    post_transport._deferred_channels.clear()  # pyright: ignore[reportPrivateUsage]
    post_transport._warned_no_manage_webhooks.clear()  # pyright: ignore[reportPrivateUsage]


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
    client.runtime = SimpleNamespace(
        settings=SimpleNamespace(agent_identity=SimpleNamespace(enabled=True))
    )
    client.http.channel_webhooks = AsyncMock(return_value=[])
    parent = MagicMock(spec=discord.TextChannel)
    parent.id = 20
    parent.guild.id = 123
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


async def test_disabled_identity_posts_plain_bot_message_without_webhook() -> None:
    client, channel, hook = _world()
    transport = DiscordPostTransport(
        client,
        channel,
        name="Research",
        avatar_url="https://x/y",
        builtin=False,
        identity_enabled=False,
    )
    await transport.send(content="answer")
    channel.send.assert_awaited_once_with(content="answer")
    channel.create_webhook.assert_not_awaited()
    client.http.channel_webhooks.assert_not_awaited()
    hook.send.assert_not_awaited()
    assert not transport.fallback_used


async def test_bot_runtime_switch_covers_recovery_transports() -> None:
    client, channel, _ = _world()
    client.runtime.settings.agent_identity.enabled = False
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    await transport.send(content="answer")
    channel.send.assert_awaited_once_with(content="answer")
    channel.create_webhook.assert_not_awaited()


async def test_excluded_guild_posts_as_bot_and_other_guild_uses_webhook() -> None:
    client, channel, hook = _world()
    client.runtime.settings.agent_identity.excluded_discord_guild_ids = ["123"]
    excluded = DiscordPostTransport(
        client, channel, name="Research", avatar_url="https://x/y", builtin=False
    )
    await excluded.send(content="answer")
    channel.send.assert_awaited_once_with(content="answer")
    hook.send.assert_not_awaited()
    channel.guild.id = 456
    included = DiscordPostTransport(
        client, channel, name="Research", avatar_url="https://x/y", builtin=False
    )
    await included.send(content="answer")
    assert hook.send.call_args.kwargs["username"] == "Research"


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
    channel.send.assert_awaited_once_with(content="-# Research\nanswer")
    hook.send.assert_not_awaited()
    assert transport.fallback_used
    assert channel.id in _unavailable_until


async def test_missing_permission_is_logged_once_per_guild() -> None:
    client, channel, _ = _world(manage_webhooks=False)
    with structlog.testing.capture_logs() as logs:
        for content in ("one", "two"):
            _unavailable_until.clear()
            transport = DiscordPostTransport(
                client, channel, name="Research", avatar_url=None, builtin=False
            )
            await transport.send(content=content)
    warnings = [
        entry for entry in logs if entry["event"] == "discord.identity_fallback_no_manage_webhooks"
    ]
    assert warnings == [
        {
            "event": "discord.identity_fallback_no_manage_webhooks",
            "guild_id": 123,
            "log_level": "warning",
        }
    ]


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
    hook.send.assert_awaited_once()
    channel.send.assert_awaited_once_with(content="-# Writer\ntwo")


async def test_pending_create_does_not_make_later_posts_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, channel, hook = _world()
    release = asyncio.Event()

    async def create(*, name: str) -> MagicMock:
        await release.wait()
        return hook

    channel.create_webhook = AsyncMock(side_effect=create)
    monkeypatch.setattr(post_transport, "_CREATE_WAIT_SECONDS", 0.2)
    first = DiscordPostTransport(client, channel, name="Research", avatar_url=None, builtin=False)
    later = DiscordPostTransport(client, channel, name="Writer", avatar_url=None, builtin=False)
    first_post = asyncio.create_task(first.send(content="first"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.wait_for(later.send(content="second"), 0.05)
    channel.send.assert_awaited_once_with(content="-# Writer\nsecond")
    release.set()
    await first_post
    channel.create_webhook.assert_awaited_once()


async def test_slow_webhook_create_falls_back_then_uses_created_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, channel, hook = _world()
    release = asyncio.Event()

    async def create(*, name: str) -> MagicMock:
        await release.wait()
        return hook

    channel.create_webhook = AsyncMock(side_effect=create)
    monkeypatch.setattr(post_transport, "_CREATE_WAIT_SECONDS", 0.01)
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    await transport.send(content="first")
    channel.send.assert_awaited_once_with(content="-# Research\nfirst")
    channel.create_webhook.assert_awaited_once()
    release.set()
    await post_transport._creation_tasks[channel.id]  # pyright: ignore[reportPrivateUsage]
    await transport.send(content="second")
    hook.send.assert_awaited_once()


async def test_concurrent_slow_first_posts_share_background_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, channel, hook = _world()
    release = asyncio.Event()

    async def create(*, name: str) -> MagicMock:
        await release.wait()
        return hook

    channel.create_webhook = AsyncMock(side_effect=create)
    monkeypatch.setattr(post_transport, "_CREATE_WAIT_SECONDS", 0.01)
    transports = [
        DiscordPostTransport(client, channel, name="Research", avatar_url=None, builtin=False)
        for _ in range(3)
    ]
    await asyncio.gather(*(transport.send(content="answer") for transport in transports))
    channel.create_webhook.assert_awaited_once()
    assert channel.send.await_count == 3
    release.set()
    await post_transport._creation_tasks[channel.id]  # pyright: ignore[reportPrivateUsage]


async def test_create_429_falls_back_within_budget_and_respects_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, channel, hook = _world()
    limited = discord.HTTPException(MagicMock(status=429), {"message": "rate limited"})
    limited.retry_after = 65.0  # pyright: ignore[reportAttributeAccessIssue]
    release = asyncio.Event()
    calls = 0

    async def create(*, name: str) -> MagicMock:
        nonlocal calls
        calls += 1
        if calls == 1:
            await release.wait()
            raise limited
        return hook

    channel.create_webhook = AsyncMock(side_effect=create)
    monkeypatch.setattr(post_transport, "_CREATE_WAIT_SECONDS", 0.01)
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    await transport.send(content="first")
    channel.send.assert_awaited_once_with(content="-# Research\nfirst")
    release.set()
    await post_transport._creation_tasks[channel.id]  # pyright: ignore[reportPrivateUsage]
    cooldown_transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    await cooldown_transport.send(content="during cooldown")
    assert channel.send.call_args.kwargs["content"] == "-# Research\nduring cooldown"
    channel.create_webhook.assert_awaited_once()
    assert _unavailable_until[channel.id] > post_transport.time.monotonic() + 60
    _unavailable_until[channel.id] = 0
    await transport.send(content="after cooldown")
    assert channel.create_webhook.await_count == 2
    hook.send.assert_awaited_once()


async def test_webhook_403_falls_back_with_name_and_caches_unavailability() -> None:
    client, channel, hook = _world()
    hook.send = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), {"message": "Missing Permissions"})
    )
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    await transport.send(content="answer")
    channel.send.assert_awaited_once_with(content="-# Research\nanswer")


async def test_exhausted_webhook_429_is_not_silently_reposted_by_bot() -> None:
    client, channel, hook = _world()
    hook.send = AsyncMock(
        side_effect=discord.HTTPException(
            MagicMock(status=429), {"message": "rate limited", "retry_after": 2.0}
        )
    )
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )

    with pytest.raises(discord.HTTPException) as exc_info:
        await transport.send(content="answer")

    assert exc_info.value.status == 429
    channel.send.assert_not_awaited()


async def test_bot_fallback_429_propagates() -> None:
    client, channel, _ = _world()
    channel.send = AsyncMock(
        side_effect=discord.HTTPException(
            MagicMock(status=429), {"message": "rate limited", "retry_after": 2.0}
        )
    )
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False, identity_enabled=False
    )

    with pytest.raises(discord.HTTPException) as exc_info:
        await transport.send(content="answer")

    assert exc_info.value.status == 429
    channel.send.assert_awaited_once_with(content="answer")


async def test_webhook_400_cooldown_only_applies_to_that_agent_identity() -> None:
    client, channel, hook = _world()
    hook.send = AsyncMock(
        side_effect=[
            discord.HTTPException(MagicMock(status=400), {"code": 50035, "message": "bad"}),
            MagicMock(spec=discord.Message),
        ]
    )
    research = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    writer = DiscordPostTransport(client, channel, name="Writer", avatar_url=None, builtin=False)
    await research.send(content="first")
    await writer.send(content="second")
    assert hook.send.await_count == 2
    assert channel.send.await_count == 1
    assert (channel.id, "Research", None) in _send_unavailable_until
    assert (channel.id, "Writer", None) not in _send_unavailable_until


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
    next_turn = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    await next_turn.send(content="two")
    assert channel.send.call_args.kwargs["content"] == "-# Research\ntwo"
    channel.create_webhook.assert_awaited_once()
    client.http.channel_webhooks.assert_awaited_once()


async def test_cached_edit_does_not_wait_for_pending_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, channel, hook = _world()
    release = asyncio.Event()

    async def create(*, name: str) -> MagicMock:
        await release.wait()
        return hook

    channel.create_webhook = AsyncMock(side_effect=create)
    monkeypatch.setattr(post_transport, "_CREATE_WAIT_SECONDS", 0.2)
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    post = asyncio.create_task(transport.send(content="first"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    known = MagicMock(spec=discord.Webhook)
    known.id = 31
    known.edit_message = AsyncMock(return_value=MagicMock(spec=discord.Message))
    _webhooks[channel.id] = {known.id: known}
    message = MagicMock(spec=discord.Message)
    message.webhook_id = known.id
    message.application_id = client.application_id
    message.id = 40
    await asyncio.wait_for(transport.edit(message, content="updated"), 0.05)
    known.edit_message.assert_awaited_once_with(40, content="updated")
    release.set()
    await post


async def test_one_hook_per_channel_and_edits_by_existing_message_webhook_id() -> None:
    client, parent, _ = _world()
    hooks = [MagicMock(spec=discord.Webhook) for _ in range(3)]
    for offset, hook in enumerate(hooks):
        hook.id = 30 + offset
        hook.token = "in-memory-test-token"
        hook.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
        hook.edit_message = AsyncMock(return_value=MagicMock(spec=discord.Message))
    parent.create_webhook = AsyncMock(return_value=hooks[0])
    thread = MagicMock(spec=discord.Thread)
    thread.id = 23
    thread.parent = parent
    thread.locked = False
    transport = DiscordPostTransport(
        client, thread, name="Research", avatar_url=None, builtin=False
    )
    for _ in range(3):
        await transport.send(content="answer")
    assert post_transport._POOL_SIZE == 1  # pyright: ignore[reportPrivateUsage]
    assert parent.create_webhook.await_count == 1
    assert hooks[0].send.await_count == 3
    message = MagicMock(spec=discord.Message)
    message.id = 40
    message.webhook_id = hooks[1].id
    message.application_id = client.application_id
    _webhooks[parent.id][hooks[1].id] = hooks[1]
    await transport.edit(message, content="updated")
    hooks[1].edit_message.assert_awaited_once_with(40, content="updated", thread=thread)


async def test_same_parent_threads_can_select_one_webhook() -> None:
    client, parent, _ = _world()
    hooks = [MagicMock(spec=discord.Webhook) for _ in range(3)]
    for offset, hook in enumerate(hooks):
        hook.id = 30 + offset
        hook.token = "in-memory-test-token"
        hook.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    _webhooks[parent.id] = {hook.id: hook for hook in hooks}

    for thread_id in (21, 24, 27):
        thread = MagicMock(spec=discord.Thread)
        thread.id = thread_id
        thread.parent = parent
        thread.locked = False
        transport = DiscordPostTransport(
            client, thread, name="Research", avatar_url=None, builtin=False
        )
        await transport.send(content="answer")

    assert hooks[0].send.await_count == 3
    hooks[1].send.assert_not_awaited()
    hooks[2].send.assert_not_awaited()


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
    thread.send.assert_awaited_once_with(content="-# Research\nanswer")
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


@pytest.mark.parametrize("excluded", [False, True])
async def test_existing_webhook_message_is_edited_and_deleted_with_identity_off_or_excluded(
    excluded: bool,
) -> None:
    client, channel, hook = _world()
    if excluded:
        client.runtime.settings.agent_identity.excluded_discord_guild_ids = ["123"]
    else:
        client.runtime.settings.agent_identity.enabled = False
    message = MagicMock(spec=discord.Message)
    message.id = 40
    message.webhook_id = hook.id
    message.application_id = client.application_id
    hook.edit_message = AsyncMock(return_value=message)
    hook.delete_message = AsyncMock()
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    _webhooks[channel.id] = {hook.id: hook}

    await transport.edit(message, content="updated")
    controls = MagicMock(spec=discord.ui.View)
    await transport.edit(message, view=controls)
    await transport.delete(message)

    assert hook.edit_message.await_args_list[0].args == (40,)
    assert hook.edit_message.await_args_list[0].kwargs == {"content": "updated"}
    assert hook.edit_message.await_args_list[1].kwargs == {"view": controls}
    hook.delete_message.assert_awaited_once_with(40)
    channel.send.assert_not_awaited()
    channel.create_webhook.assert_not_awaited()


async def test_bot_message_edits_and_deletes_through_bot() -> None:
    client, channel, hook = _world()
    message = MagicMock(spec=discord.Message)
    message.webhook_id = None
    edited = MagicMock(spec=discord.Message)
    message.edit = AsyncMock(return_value=edited)
    message.delete = AsyncMock()
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    assert await transport.edit(message, content="updated") is edited, (
        "the edited message comes back, so a turn can read Discord's edit time"
    )
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


async def test_recovery_edit_does_not_replace_unknown_webhook_card() -> None:
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

    with pytest.raises(discord.ClientException, match="no longer exists"):
        await transport.edit(message, content="updated", _allow_replacement=False)

    hook.send.assert_not_awaited()
    channel.send.assert_not_awaited()


async def test_missing_webhook_token_posts_edit_as_replacement() -> None:
    client, channel, hook = _world()
    message = MagicMock(spec=discord.Message)
    message.id = 40
    message.webhook_id = hook.id
    message.application_id = client.application_id
    message.author.name = "Research"
    channel.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    channel.permissions_for.return_value.manage_webhooks = False
    client.http.channel_webhooks = AsyncMock(return_value=[])
    replacement = await transport.edit(message, content="updated")
    assert replacement is channel.send.return_value
    channel.send.assert_awaited_once_with(content="-# Research\nupdated")
    hook.edit_message.assert_not_awaited()


async def test_recovery_edit_does_not_replace_card_without_webhook_token() -> None:
    client, channel, hook = _world()
    message = MagicMock(spec=discord.Message)
    message.id = 40
    message.webhook_id = hook.id
    message.application_id = client.application_id
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    channel.permissions_for.return_value.manage_webhooks = False
    client.http.channel_webhooks = AsyncMock(return_value=[])

    with pytest.raises(discord.ClientException, match="token unavailable"):
        await transport.edit(message, content="updated", _allow_replacement=False)

    channel.send.assert_not_awaited()
    hook.send.assert_not_awaited()


async def test_webhook_edit_403_lookup_does_not_post_replacement() -> None:
    client, channel, _ = _world()
    client.http.channel_webhooks = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), {"message": "Missing Permissions"})
    )
    message = MagicMock(spec=discord.Message)
    message.id = 40
    message.webhook_id = 30
    message.application_id = client.application_id
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    with pytest.raises(discord.ClientException, match="webhook lookup failed"):
        await transport.edit(message, content="updated")
    with pytest.raises(discord.ClientException, match="webhook lookup unavailable"):
        await transport.edit(message, content="updated again")
    client.http.channel_webhooks.assert_awaited_once_with(channel.id)
    channel.send.assert_not_awaited()


async def test_listed_webhook_without_token_does_not_post_replacement() -> None:
    client, channel, _ = _world()
    client.http.channel_webhooks = AsyncMock(
        return_value=[{"id": "30", "application_id": "10", "channel_id": "20", "token": None}]
    )
    message = MagicMock(spec=discord.Message)
    message.id = 40
    message.webhook_id = 30
    message.application_id = client.application_id
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    with pytest.raises(discord.ClientException):
        await transport.edit(message, content="updated")
    channel.send.assert_not_awaited()


async def test_loaded_webhook_id_proves_ownership_without_application_id() -> None:
    client, channel, hook = _world()
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    await transport._webhook()  # pyright: ignore[reportPrivateUsage]
    message = MagicMock(spec=discord.Message)
    message.webhook_id = hook.id
    message.application_id = None
    assert await transport.owns_message(message)


async def test_failed_webhook_upload_rebuilds_file_for_bot_fallback() -> None:
    client, channel, hook = _world()
    original = discord.File(io.BytesIO(b"payload"), filename="a.txt")

    async def fail_upload(**kwargs: object) -> None:
        files = kwargs["files"]
        assert isinstance(files, list)
        files[0].fp.read()
        raise discord.HTTPException(MagicMock(status=400), {"code": 50035, "message": "bad"})

    hook.send = AsyncMock(side_effect=fail_upload)
    channel.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    await transport.send(content="answer", files=[original])
    files = channel.send.call_args.kwargs["files"]
    assert files[0].fp.read() == b"payload"
    await transport.send(content="overflow")
    assert channel.send.call_args.kwargs["content"] == "overflow"
    assert hook.send.await_count == 1


async def test_archived_thread_webhook_retry_rebuilds_file() -> None:
    client, parent, hook = _world()
    thread = MagicMock(spec=discord.Thread)
    thread.id = 21
    thread.parent = parent
    thread.locked = False
    thread.edit = AsyncMock()
    sent = MagicMock(spec=discord.Message)
    received: list[bytes] = []

    async def send(**kwargs: object) -> discord.Message:
        files = kwargs["files"]
        assert isinstance(files, list)
        received.append(files[0].fp.read())
        if len(received) == 1:
            raise discord.HTTPException(
                MagicMock(status=400), {"code": 50083, "message": "thread archived"}
            )
        return sent

    hook.send = AsyncMock(side_effect=send)
    transport = DiscordPostTransport(
        client, thread, name="Research", avatar_url=None, builtin=False
    )
    result = await transport.send(
        content="answer", files=[discord.File(io.BytesIO(b"payload"), filename="a.txt")]
    )
    assert result is sent
    assert received == [b"payload", b"payload"]
    thread.edit.assert_awaited_once_with(archived=False)


async def test_lifecycle_prefix_is_not_duplicated_after_webhook_rejection() -> None:
    client, channel, hook = _world()
    hook.send = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=400), {"code": 50035, "message": "bad"})
    )
    transport = DiscordPostTransport(
        client, channel, name="Research", avatar_url=None, builtin=False
    )
    await transport.send(content="-# Research\nfirst")
    assert channel.send.call_args.kwargs["content"] == "-# Research\nfirst"
    await transport.send(content="second")
    assert channel.send.call_args.kwargs["content"] == "second"


async def test_missing_permission_backs_off_for_sixty_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    monkeypatch.setattr(post_transport, "time", SimpleNamespace(monotonic=lambda: now[0]))
    client, channel, _ = _world(manage_webhooks=False)

    async def send() -> None:
        transport = DiscordPostTransport(
            client, channel, name="Research", avatar_url=None, builtin=False
        )
        await transport.send(content="answer")

    await send()
    assert channel.permissions_for.call_count == 1
    now[0] += 59
    await send()
    assert channel.permissions_for.call_count == 1, "the back-off holds inside 60 s"
    now[0] += 2
    await send()
    assert channel.permissions_for.call_count == 2, "and the next answer checks again after it"


def test_gaining_manage_webhooks_clears_that_guilds_back_off() -> None:
    allowed = MagicMock(spec=discord.TextChannel)
    allowed.permissions_for.return_value.manage_webhooks = True
    denied = MagicMock(spec=discord.TextChannel)
    denied.permissions_for.return_value.manage_webhooks = False
    guild = MagicMock(spec=discord.Guild)
    guild.id = 123
    guild.get_channel.side_effect = {20: allowed, 21: denied}.get
    _unavailable_until.update({20: 9e9, 21: 9e9, 99: 9e9})

    post_transport.clear_webhook_backoff(guild)

    assert set(_unavailable_until) == {21, 99}, (
        "only this guild's channels the bot can now manage leave the back-off"
    )


async def test_permission_events_clear_the_back_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from daimon.adapters.discord import bot as bot_module

    cleared: list[object] = []
    monkeypatch.setattr(bot_module, "clear_webhook_backoff", cleared.append)
    bot = MagicMock()
    bot.user.id = 10
    guild = MagicMock(spec=discord.Guild)

    before_role = MagicMock(permissions=discord.Permissions.none(), guild=guild)
    after_role = MagicMock(permissions=discord.Permissions(manage_webhooks=True), guild=guild)
    await bot_module.DaimonBot.on_guild_role_update(bot, before_role, after_role)
    await bot_module.DaimonBot.on_guild_role_update(bot, after_role, after_role)

    def member(user_id: int, roles: list[MagicMock]) -> MagicMock:
        found = MagicMock(spec=discord.Member)
        found.id = user_id
        found.roles = roles
        found.guild = guild
        return found

    before_me = member(10, [])
    await bot_module.DaimonBot.on_member_update(bot, before_me, member(10, [MagicMock()]))
    await bot_module.DaimonBot.on_member_update(bot, before_me, member(11, [MagicMock()]))

    before_channel = MagicMock(overwrites={}, guild=guild)
    after_channel = MagicMock(overwrites={"bot": "allow"}, guild=guild)
    await bot_module.DaimonBot.on_guild_channel_update(bot, before_channel, after_channel)
    await bot_module.DaimonBot.on_guild_channel_update(bot, after_channel, after_channel)

    assert cleared == [guild, guild, guild], (
        "a role, bot-member or overwrite change re-checks the back-off; no change, no re-check"
    )
