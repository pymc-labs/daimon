"""REST-only Discord agent post transport."""

import asyncio
import io
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.mcp.tools.discord import _post_transport
from daimon.core.agent_identity import AgentIdentity


@pytest.fixture(autouse=True)
def clear_creation_cache() -> None:
    _post_transport._created_hooks.clear()  # pyright: ignore[reportPrivateUsage]
    _post_transport._creation_tasks.clear()  # pyright: ignore[reportPrivateUsage]
    _post_transport._create_unavailable_until.clear()  # pyright: ignore[reportPrivateUsage]
    _post_transport._deferred_channels.clear()  # pyright: ignore[reportPrivateUsage]


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


async def test_slow_creation_falls_back_then_uses_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock(spec=discord.Client)
    client.application_id = 77
    client.http = MagicMock()
    client.http.channel_webhooks = AsyncMock(return_value=[])
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 120
    channel.guild.me = MagicMock(spec=discord.Member)
    channel.permissions_for.return_value.manage_webhooks = True
    channel.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    hook = MagicMock(spec=discord.Webhook)
    hook.id = 121
    hook.token = "test"
    hook.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    release = asyncio.Event()

    async def create(*, name: str) -> MagicMock:
        await release.wait()
        return hook

    channel.create_webhook = AsyncMock(side_effect=create)
    monkeypatch.setattr(_post_transport, "_CREATE_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(discord.Webhook, "partial", MagicMock(return_value=hook))
    identity = AgentIdentity("Research", None, False)
    await asyncio.gather(
        *(
            _post_transport.send_agent_message(
                client, channel, identity, content="first", identity_enabled=True
            )
            for _ in range(3)
        )
    )
    channel.create_webhook.assert_awaited_once()
    assert channel.send.await_count == 3
    assert channel.send.call_args.kwargs["content"] == "-# Research\nfirst"
    release.set()
    await _post_transport._creation_tasks[channel.id]  # pyright: ignore[reportPrivateUsage]
    await _post_transport.send_agent_message(
        client, channel, identity, content="later", identity_enabled=True
    )
    hook.send.assert_awaited_once()


async def test_cached_hook_uses_each_calls_open_client_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_session = MagicMock(closed=False)
    second_session = MagicMock(closed=False)
    first_client = MagicMock(spec=discord.Client)
    first_client.application_id = 77
    first_client.session = first_session
    first_client.http = MagicMock()
    first_client.http.channel_webhooks = AsyncMock(return_value=[])
    second_client = MagicMock(spec=discord.Client)
    second_client.application_id = 77
    second_client._connection = MagicMock()  # pyright: ignore[reportPrivateUsage]
    second_client.http = MagicMock()
    second_client.http._HTTPClient__session = second_session  # pyright: ignore[reportPrivateUsage]
    first_channel = MagicMock(spec=discord.TextChannel)
    first_channel.id = 124
    first_channel.guild.me = MagicMock(spec=discord.Member)
    first_channel.permissions_for.return_value.manage_webhooks = True
    second_channel = MagicMock(spec=discord.TextChannel)
    second_channel.id = first_channel.id
    second_channel.send = AsyncMock()
    first_hook = MagicMock(spec=discord.Webhook)
    first_hook.id = 125
    first_hook.token = "test-token"

    async def send_first(**kwargs: object) -> MagicMock:
        if first_session.closed:
            raise RuntimeError("Session is closed")
        return MagicMock(spec=discord.Message)

    first_hook.send = AsyncMock(side_effect=send_first)
    first_channel.create_webhook = AsyncMock(return_value=first_hook)
    sessions: list[object] = []

    async def send_second(self: discord.Webhook, **kwargs: object) -> MagicMock:
        sessions.append(self.session)
        if self.session.closed:
            raise RuntimeError("Session is closed")
        return MagicMock(spec=discord.Message)

    monkeypatch.setattr(discord.Webhook, "send", send_second)
    identity = AgentIdentity("Research", None, False)
    await _post_transport.send_agent_message(
        first_client, first_channel, identity, content="first", identity_enabled=True
    )
    first_session.closed = True
    await _post_transport.send_agent_message(
        second_client, second_channel, identity, content="second", identity_enabled=True
    )
    assert sessions == [second_session]
    second_channel.send.assert_not_awaited()


async def test_creation_timeout_cools_down_after_client_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock(spec=discord.Client)
    client.application_id = 77
    client.http = MagicMock()
    client.http.channel_webhooks = AsyncMock(return_value=[])
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 126
    channel.guild.me = MagicMock(spec=discord.Member)
    channel.permissions_for.return_value.manage_webhooks = True
    channel.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    release = asyncio.Event()

    async def create(*, name: str) -> MagicMock:
        await release.wait()
        raise RuntimeError("Session is closed")

    channel.create_webhook = AsyncMock(side_effect=create)
    monkeypatch.setattr(_post_transport, "_CREATE_WAIT_SECONDS", 0.01)
    identity = AgentIdentity("Research", None, False)
    await _post_transport.send_agent_message(
        client, channel, identity, content="first", identity_enabled=True
    )
    assert _post_transport._create_unavailable_until[channel.id] > time.monotonic()  # pyright: ignore[reportPrivateUsage]
    release.set()
    with pytest.raises(RuntimeError, match="Session is closed"):
        await _post_transport._creation_tasks[channel.id]  # pyright: ignore[reportPrivateUsage]
    await asyncio.sleep(0)
    await _post_transport.send_agent_message(
        client, channel, identity, content="second", identity_enabled=True
    )
    channel.create_webhook.assert_awaited_once()
    assert channel.send.await_count == 2


async def test_create_429_respects_cooldown_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock(spec=discord.Client)
    client.application_id = 77
    client.http = MagicMock()
    client.http.channel_webhooks = AsyncMock(return_value=[])
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 122
    channel.guild.me = MagicMock(spec=discord.Member)
    channel.permissions_for.return_value.manage_webhooks = True
    channel.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    limited = discord.HTTPException(MagicMock(status=429), {"message": "rate limited"})
    limited.retry_after = 65.0  # pyright: ignore[reportAttributeAccessIssue]
    hook = MagicMock(spec=discord.Webhook)
    hook.id = 123
    hook.token = "test"
    hook.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
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
    monkeypatch.setattr(_post_transport, "_CREATE_WAIT_SECONDS", 0.01)
    identity = AgentIdentity("Research", None, False)
    await _post_transport.send_agent_message(
        client, channel, identity, content="first", identity_enabled=True
    )
    channel.send.assert_awaited_once_with(content="-# Research\nfirst", files=[])
    release.set()
    await _post_transport._creation_tasks[channel.id]  # pyright: ignore[reportPrivateUsage]
    await _post_transport.send_agent_message(
        client, channel, identity, content="second", identity_enabled=True
    )
    channel.create_webhook.assert_awaited_once()
    _post_transport._create_unavailable_until[channel.id] = 0  # pyright: ignore[reportPrivateUsage]
    await _post_transport.send_agent_message(
        client, channel, identity, content="later", identity_enabled=True
    )
    assert channel.create_webhook.await_count == 2
    hook.send.assert_awaited_once()


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
        identity_enabled=True,
    )
    assert sent is message
    assert hook.send.call_args.kwargs["wait"] is True
    assert hook.send.call_args.kwargs["username"] == "Research"
    assert hook.send.call_args.kwargs["avatar_url"] == "https://example.com/a.png"


async def test_exhausted_webhook_429_propagates_without_bot_repost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock(spec=discord.Client)
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock()
    hook = MagicMock(spec=discord.Webhook)
    hook.send = AsyncMock(
        side_effect=discord.HTTPException(
            MagicMock(status=429), {"message": "rate limited", "retry_after": 2.0}
        )
    )
    monkeypatch.setattr(_post_transport, "own_webhook", AsyncMock(return_value=hook))

    with pytest.raises(discord.HTTPException) as exc_info:
        await _post_transport.send_agent_message(
            client,
            channel,
            AgentIdentity("Research", None, False),
            content="answer",
            identity_enabled=True,
        )

    assert exc_info.value.status == 429
    channel.send.assert_not_awaited()


async def test_disabled_agent_send_uses_plain_bot_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock(spec=discord.Client)
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    lookup = AsyncMock()
    monkeypatch.setattr(_post_transport, "own_webhook", lookup)
    await _post_transport.send_agent_message(
        client,
        channel,
        AgentIdentity("Research", "https://example.com/a.png", False),
        content="answer",
        identity_enabled=False,
    )
    channel.send.assert_awaited_once_with(content="answer", files=[])
    lookup.assert_not_awaited()


async def test_disabled_webhook_edit_replacement_uses_plain_bot_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock(spec=discord.Client)
    client.application_id = 10
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    message = MagicMock(spec=discord.Message)
    message.webhook_id = 30
    message.application_id = 10
    message.author.name = "Research"
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(_post_transport, "ensure_application_id", AsyncMock())
    monkeypatch.setattr(_post_transport, "own_webhook", lookup)

    await _post_transport.edit_own_message(
        client, channel, message, content="replacement", identity_enabled=False
    )

    channel.send.assert_awaited_once_with(content="replacement", files=[])
    lookup.assert_awaited_once()


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
        identity_enabled=True,
    )
    channel.send.assert_awaited_once_with(content="-# Research\nanswer", files=[])


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
        identity_enabled=True,
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
        client,
        channel,
        AgentIdentity("Research", None, False),
        content="answer",
        files=[file],
        identity_enabled=True,
    )
    sent_files = channel.send.call_args.kwargs["files"]
    assert sent_files[0].fp.read() == b"payload"


async def test_a_403_lookup_backs_off_for_sixty_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    monkeypatch.setattr(_post_transport, "time", SimpleNamespace(monotonic=lambda: now[0]))
    client = MagicMock(spec=discord.Client)
    client.application_id = 10
    client.http = MagicMock()
    client.http.channel_webhooks = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), {"message": "Missing Permissions"})
    )
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 92
    _post_transport._lookup_unavailable_until.clear()  # pyright: ignore[reportPrivateUsage]

    async def lookup() -> None:
        await _post_transport.own_webhook(client, channel, create=False, webhook_id=30)

    with pytest.raises(discord.ClientException, match="webhook lookup failed"):
        await lookup()
    now[0] += 59
    with pytest.raises(discord.ClientException, match="webhook lookup unavailable"):
        await lookup()
    now[0] += 2
    with pytest.raises(discord.ClientException, match="webhook lookup failed"):
        await lookup()
    assert client.http.channel_webhooks.await_count == 2, "the back-off ends after 60 s"
