"""REST-only Discord webhook posting for MCP agent tools."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import discord
from daimon.core.agent_identity import AgentIdentity
from daimon.core.agent_post_identity import (
    DISCORD_AGENT_WEBHOOK_NAME,
    discord_username,
    fallback_name_prefix,
    is_our_discord_webhook,
)

_locks: dict[int, asyncio.Lock] = {}


def _snowflake(value: object) -> int | None:
    return int(value) if isinstance(value, (int, str)) else None


async def own_webhook(
    client: discord.Client,
    channel: discord.abc.GuildChannel | discord.Thread,
    *,
    create: bool,
) -> discord.Webhook | None:
    parent = channel.parent if isinstance(channel, discord.Thread) else channel
    if not isinstance(parent, (discord.TextChannel, discord.ForumChannel)):
        return None
    if isinstance(channel, discord.Thread) and channel.locked and create:
        return None
    if client.user is None:
        return None
    lock = _locks.setdefault(parent.id, asyncio.Lock())
    async with lock:
        try:
            raw_hooks = await client.http.channel_webhooks(parent.id)
            hook = next(
                (
                    discord.Webhook.from_state(data=raw, state=client._connection)  # pyright: ignore[reportPrivateUsage]
                    for raw in raw_hooks
                    if is_our_discord_webhook(
                        application_id=_snowflake(raw.get("application_id")),
                        channel_id=_snowflake(raw.get("channel_id")),
                        our_application_id=client.user.id,
                        target_channel_id=parent.id,
                    )
                    and raw.get("token")
                ),
                None,
            )
            if hook is None and create:
                hook = await parent.create_webhook(name=DISCORD_AGENT_WEBHOOK_NAME)
            return hook if hook is not None and hook.token is not None else None
        except discord.HTTPException:
            return None


async def send_agent_message(
    client: discord.Client,
    channel: discord.abc.GuildChannel | discord.Thread,
    identity: AgentIdentity,
    *,
    content: str,
    files: list[discord.File] | None = None,
) -> discord.Message:
    hook = None if identity.builtin else await own_webhook(client, channel, create=True)
    if hook is not None:
        kwargs: dict[str, Any] = {}
        if isinstance(channel, discord.Thread):
            kwargs["thread"] = channel
        sent = await hook.send(  # pyright: ignore[reportCallIssue]
            content=content,
            files=files or [],
            username=discord_username(identity.name),
            avatar_url=identity.avatar_url,
            wait=True,
            **kwargs,
        )
        assert sent is not None
        return cast(discord.Message, sent)
    if not isinstance(channel, discord.abc.Messageable):
        raise TypeError("channel does not support messages")
    fallback_content = content if identity.builtin else fallback_name_prefix(identity.name, content)
    return await channel.send(content=fallback_content, files=files or [])


async def edit_own_message(
    client: discord.Client,
    channel: discord.abc.GuildChannel | discord.Thread,
    message: discord.Message,
    **kwargs: Any,  # noqa: ANN401
) -> discord.Message | None:
    hook = await own_webhook(client, channel, create=False)
    if hook is not None and message.webhook_id == hook.id:
        if isinstance(channel, discord.Thread):
            kwargs["thread"] = channel
        try:
            await hook.edit_message(message.id, **kwargs)  # pyright: ignore[reportCallIssue]
            return None
        except discord.NotFound as exc:
            if exc.code != 10015:  # Unknown Webhook
                raise
    if message.webhook_id is not None:
        content = kwargs.get("content")
        if not isinstance(content, str):
            raise ValueError("deleted webhook message needs replacement content")
        return await send_agent_message(
            client,
            channel,
            AgentIdentity(name=message.author.name, avatar_url=None, builtin=False),
            content=content,
        )
    await message.edit(**kwargs)
    return None


async def delete_own_message(
    client: discord.Client,
    channel: discord.abc.GuildChannel | discord.Thread,
    message: discord.Message,
) -> None:
    hook = await own_webhook(client, channel, create=False)
    if hook is not None and message.webhook_id == hook.id:
        if isinstance(channel, discord.Thread):
            await hook.delete_message(message.id, thread=channel)
        else:
            await hook.delete_message(message.id)
    else:
        await message.delete()
