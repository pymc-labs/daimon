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
    webhook_id: int | None = None,
) -> discord.Webhook | None:
    parent = channel.parent if isinstance(channel, discord.Thread) else channel
    if not isinstance(parent, (discord.TextChannel, discord.ForumChannel)):
        return None
    if isinstance(channel, discord.Thread) and channel.locked and create:
        return None
    if client.application_id is None:
        return None
    if create:
        member = parent.guild.me
        if member is None and client.user is not None:  # pyright: ignore[reportUnnecessaryComparison]
            try:
                member = await parent.guild.fetch_member(client.user.id)
            except discord.HTTPException:
                return None
        if member is None or not parent.permissions_for(member).manage_webhooks:  # pyright: ignore[reportUnnecessaryComparison]
            return None
    lock = _locks.setdefault(parent.id, asyncio.Lock())
    async with lock:
        try:
            raw_hooks = await client.http.channel_webhooks(parent.id)
            hooks = [
                discord.Webhook.from_state(data=raw, state=client._connection)  # pyright: ignore[reportPrivateUsage]
                for raw in raw_hooks
                if is_our_discord_webhook(
                    application_id=_snowflake(raw.get("application_id")),
                    channel_id=_snowflake(raw.get("channel_id")),
                    our_application_id=client.application_id,
                    target_channel_id=parent.id,
                )
                and raw.get("token")
            ]
            hook = (
                next((item for item in hooks if item.id == webhook_id), None)
                if webhook_id
                else None
            )
            if hook is None and webhook_id is None and hooks:
                hook = hooks[0]
            if hook is None and create:
                hook = await parent.create_webhook(name=DISCORD_AGENT_WEBHOOK_NAME)
            return hook if hook is not None and hook.token is not None else None
        except discord.HTTPException:
            return None


async def own_webhook_ids(
    client: discord.Client, channel: discord.abc.GuildChannel | discord.Thread
) -> frozenset[int]:
    parent = channel.parent if isinstance(channel, discord.Thread) else channel
    if not isinstance(parent, (discord.TextChannel, discord.ForumChannel)):
        return frozenset()
    if client.application_id is None:
        return frozenset()
    try:
        raw_hooks = await client.http.channel_webhooks(parent.id)
    except discord.HTTPException:
        return frozenset()
    return frozenset(
        int(raw["id"])
        for raw in raw_hooks
        if is_our_discord_webhook(
            application_id=_snowflake(raw.get("application_id")),
            channel_id=_snowflake(raw.get("channel_id")),
            our_application_id=client.application_id,
            target_channel_id=parent.id,
        )
    )


async def send_agent_message(
    client: discord.Client,
    channel: discord.abc.GuildChannel | discord.Thread,
    identity: AgentIdentity,
    *,
    content: str,
    files: list[discord.File] | None = None,
    extra_messages: list[discord.Message] | None = None,
) -> discord.Message:
    hook = None if identity.builtin else await own_webhook(client, channel, create=True)
    if hook is not None:
        kwargs: dict[str, Any] = {}
        if isinstance(channel, discord.Thread):
            kwargs["thread"] = channel
        try:
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
        except discord.HTTPException as exc:
            if exc.status == 429:
                raise
    if not isinstance(channel, discord.abc.Messageable):
        raise TypeError("channel does not support messages")
    fallback_content = content if identity.builtin else fallback_name_prefix(identity.name, content)
    chunks = [fallback_content[i : i + 2000] for i in range(0, len(fallback_content), 2000)] or [""]
    sent = await channel.send(content=chunks[0], files=files or [])
    for chunk in chunks[1:]:
        additional = await channel.send(content=chunk, files=[])
        if extra_messages is not None:
            extra_messages.append(additional)
    return sent


async def edit_own_message(
    client: discord.Client,
    channel: discord.abc.GuildChannel | discord.Thread,
    message: discord.Message,
    extra_messages: list[discord.Message] | None = None,
    **kwargs: Any,  # noqa: ANN401
) -> discord.Message | None:
    ours = (
        isinstance(message.webhook_id, int)
        and client.application_id is not None
        and message.application_id == client.application_id
    )
    if ours:
        hook = await own_webhook(client, channel, create=False, webhook_id=message.webhook_id)
        if hook is None:
            raise RuntimeError("own webhook token unavailable")
        if isinstance(channel, discord.Thread):
            kwargs["thread"] = channel
        try:
            await hook.edit_message(message.id, **kwargs)  # pyright: ignore[reportCallIssue]
            return None
        except discord.NotFound as exc:
            if exc.code != 10015:  # Unknown Webhook
                raise
    if ours:
        content = kwargs.get("content")
        if not isinstance(content, str):
            raise ValueError("deleted webhook message needs replacement content")
        return await send_agent_message(
            client,
            channel,
            AgentIdentity(name=message.author.name, avatar_url=None, builtin=False),
            content=content,
            extra_messages=extra_messages,
        )
    await message.edit(**kwargs)
    return None


async def delete_own_message(
    client: discord.Client,
    channel: discord.abc.GuildChannel | discord.Thread,
    message: discord.Message,
) -> None:
    ours = (
        isinstance(message.webhook_id, int)
        and client.application_id is not None
        and message.application_id == client.application_id
    )
    if ours:
        hook = await own_webhook(client, channel, create=False, webhook_id=message.webhook_id)
        if hook is None:
            raise RuntimeError("own webhook token unavailable")
        if isinstance(channel, discord.Thread):
            await hook.delete_message(message.id, thread=channel)
        else:
            await hook.delete_message(message.id)
    else:
        await message.delete()
