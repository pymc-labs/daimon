"""REST-only Discord agent post transport."""

import io
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.mcp.tools.discord import _post_transport
from daimon.core.agent_identity import AgentIdentity


async def test_webhook_lookup_matches_application_id_not_bot_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock(spec=discord.Client)
    client.application_id = 77
    client.user.id = 10
    client.http = MagicMock()
    client._connection = MagicMock()  # pyright: ignore[reportPrivateUsage]
    client.http.channel_webhooks = AsyncMock(
        return_value=[{"id": "30", "application_id": "77", "channel_id": "20", "token": "test"}]
    )
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 20
    hook = MagicMock(spec=discord.Webhook)
    hook.id = 30
    hook.token = "test"
    monkeypatch.setattr(discord.Webhook, "from_state", MagicMock(return_value=hook))
    assert await _post_transport.own_webhook(client, channel, create=False) is hook


async def test_webhook_creation_requires_manage_webhooks() -> None:
    client = MagicMock(spec=discord.Client)
    client.application_id = 77
    channel = MagicMock(spec=discord.TextChannel)
    channel.guild.me = MagicMock(spec=discord.Member)
    channel.permissions_for.return_value.manage_webhooks = False
    channel.create_webhook = AsyncMock()
    assert await _post_transport.own_webhook(client, channel, create=True) is None
    channel.create_webhook.assert_not_awaited()


async def test_agent_send_uses_webhook_identity_and_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock(spec=discord.Client)
    client.application_id = 10
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
    client.application_id = 10
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


async def test_fallback_resplits_when_name_pushes_content_over_discord_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock(spec=discord.Client)
    channel = MagicMock(spec=discord.TextChannel)
    first = MagicMock(spec=discord.Message)
    second = MagicMock(spec=discord.Message)
    channel.send = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(_post_transport, "own_webhook", AsyncMock(return_value=None))
    extra: list[discord.Message] = []
    sent = await _post_transport.send_agent_message(
        client,
        channel,
        AgentIdentity("Research", None, False),
        content="x" * 1995,
        extra_messages=extra,
    )
    assert sent is first
    assert extra == [second]
    assert channel.send.await_count == 2
    assert all(len(call.kwargs["content"]) <= 2000 for call in channel.send.call_args_list)


async def test_webhook_edit_and_delete_use_webhook_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock(spec=discord.Client)
    client.application_id = 10
    channel = MagicMock(spec=discord.TextChannel)
    message = MagicMock(spec=discord.Message)
    message.id = 40
    message.webhook_id = 30
    message.application_id = 10
    message.edit = AsyncMock()
    message.delete = AsyncMock()
    hook = MagicMock(spec=discord.Webhook)
    hook.id = 30
    hook.edit_message = AsyncMock()
    hook.delete_message = AsyncMock()
    monkeypatch.setattr(_post_transport, "own_webhook", AsyncMock(return_value=hook))
    await _post_transport.edit_own_message(client, channel, message, content="updated")
    await _post_transport.delete_own_message(client, channel, message, known_webhooks={30: hook})
    hook.edit_message.assert_awaited_once_with(40, content="updated")
    hook.delete_message.assert_awaited_once_with(40)
    message.edit.assert_not_awaited()
    message.delete.assert_not_awaited()


async def test_deleted_webhook_edit_posts_update_as_new_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock(spec=discord.Client)
    client.application_id = 10
    channel = MagicMock(spec=discord.TextChannel)
    message = MagicMock(spec=discord.Message)
    message.webhook_id = 30
    message.application_id = 10
    message.author.name = "Research"
    new_message = MagicMock(spec=discord.Message)
    hook = MagicMock(spec=discord.Webhook)
    hook.id = 30
    hook.edit_message = AsyncMock(
        side_effect=discord.NotFound(
            MagicMock(status=404), {"code": 10015, "message": "Unknown Webhook"}
        )
    )
    monkeypatch.setattr(_post_transport, "own_webhook", AsyncMock(return_value=hook))
    send = AsyncMock(return_value=new_message)
    monkeypatch.setattr(_post_transport, "send_agent_message", send)
    result = await _post_transport.edit_own_message(client, channel, message, content="updated")
    assert result is new_message
    assert send.call_args.kwargs["content"] == "updated"


async def test_missing_webhook_token_posts_update_as_new_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock(spec=discord.Client)
    client.application_id = 10
    channel = MagicMock(spec=discord.TextChannel)
    message = MagicMock(spec=discord.Message)
    message.webhook_id = 30
    message.application_id = 10
    message.author.name = "Research"
    new_message = MagicMock(spec=discord.Message)
    monkeypatch.setattr(_post_transport, "own_webhook", AsyncMock(return_value=None))
    send = AsyncMock(return_value=new_message)
    monkeypatch.setattr(_post_transport, "send_agent_message", send)
    result = await _post_transport.edit_own_message(client, channel, message, content="updated")
    assert result is new_message
    assert send.call_args.kwargs["content"] == "updated"


async def test_webhook_edit_403_lookup_does_not_post_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock(spec=discord.Client)
    client.application_id = 10
    client.http = MagicMock()
    client.http.channel_webhooks = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), {"message": "Missing Permissions"})
    )
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 91
    message = MagicMock(spec=discord.Message)
    message.id = 40
    message.webhook_id = 30
    message.application_id = 10
    message.author.name = "Research"
    send = AsyncMock()
    monkeypatch.setattr(_post_transport, "send_agent_message", send)
    _post_transport._lookup_unavailable_until.clear()  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(discord.ClientException, match="webhook lookup failed"):
        await _post_transport.edit_own_message(client, channel, message, content="updated")
    with pytest.raises(discord.ClientException, match="webhook lookup unavailable"):
        await _post_transport.edit_own_message(client, channel, message, content="updated again")
    client.http.channel_webhooks.assert_awaited_once_with(channel.id)
    send.assert_not_awaited()


async def test_listed_webhook_without_token_does_not_post_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock(spec=discord.Client)
    client.application_id = 10
    client.http = MagicMock()
    client.http.channel_webhooks = AsyncMock(
        return_value=[{"id": "30", "application_id": "10", "channel_id": "92", "token": None}]
    )
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 92
    message = MagicMock(spec=discord.Message)
    message.id = 40
    message.webhook_id = 30
    message.application_id = 10
    send = AsyncMock()
    monkeypatch.setattr(_post_transport, "send_agent_message", send)
    with pytest.raises(discord.ClientException, match="own webhook token unavailable"):
        await _post_transport.edit_own_message(client, channel, message, content="updated")
    send.assert_not_awaited()


async def test_mcp_thread_uses_same_sorted_webhook_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock(spec=discord.Client)
    client.application_id = 10
    client.http = MagicMock()
    client._connection = MagicMock()  # pyright: ignore[reportPrivateUsage]
    client.http.channel_webhooks = AsyncMock(
        return_value=[
            {"id": str(i), "application_id": "10", "channel_id": "20", "token": "test"}
            for i in (32, 30, 31)
        ]
    )
    parent = MagicMock(spec=discord.TextChannel)
    parent.id = 20
    thread = MagicMock(spec=discord.Thread)
    thread.id = 22
    thread.parent = parent
    hooks = {i: MagicMock(spec=discord.Webhook) for i in (30, 31, 32)}
    for i, hook in hooks.items():
        hook.id = i
        hook.token = "test"
    monkeypatch.setattr(
        discord.Webhook,
        "from_state",
        MagicMock(side_effect=lambda *, data, state: hooks[int(data["id"])]),
    )
    assert await _post_transport.own_webhook(client, thread, create=False) is hooks[31]


async def test_mcp_failed_webhook_upload_rebuilds_file_for_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock(spec=discord.Client)
    channel = MagicMock(spec=discord.TextChannel)
    hook = MagicMock(spec=discord.Webhook)

    async def fail_upload(**kwargs: object) -> None:
        files = kwargs["files"]
        assert isinstance(files, list)
        files[0].fp.read()
        raise discord.HTTPException(MagicMock(status=400), {"code": 50035, "message": "bad"})

    hook.send = AsyncMock(side_effect=fail_upload)
    monkeypatch.setattr(_post_transport, "own_webhook", AsyncMock(return_value=hook))
    channel.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    file = discord.File(io.BytesIO(b"payload"), filename="a.txt")
    await _post_transport.send_agent_message(
        client, channel, AgentIdentity("Research", None, False), content="answer", files=[file]
    )
    sent_files = channel.send.call_args.kwargs["files"]
    assert sent_files[0].fp.read() == b"payload"
