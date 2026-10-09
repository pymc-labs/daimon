"""REST-only Discord webhook posting for MCP agent tools."""

from __future__ import annotations

import asyncio
import io
import time
from collections.abc import Mapping
from typing import Any, cast

import discord
import structlog
from daimon.adapters.mcp.tools.discord._client import ensure_application_id
from daimon.core.agent_identity import AgentIdentity
from daimon.core.agent_post_identity import (
    DISCORD_AGENT_WEBHOOK_NAME,
    discord_username,
    fallback_name_prefix,
    is_our_discord_webhook,
    select_discord_webhook_id,
)

_locks: dict[int, asyncio.Lock] = {}
_lookup_unavailable_until: dict[int, float] = {}
_LOOKUP_UNAVAILABLE_SECONDS = 600
_CREATE_WAIT_SECONDS = 2.0
_creation_tasks: dict[int, asyncio.Task[discord.Webhook | None]] = {}
_created_hooks: dict[int, tuple[int, str]] = {}
_create_unavailable_until: dict[int, float] = {}
_deferred_channels: set[int] = set()
_log = structlog.get_logger()


def _finish_creation(channel_id: int, task: asyncio.Task[discord.Webhook | None]) -> None:
    if _creation_tasks.get(channel_id) is task:
        _creation_tasks.pop(channel_id)
    _deferred_channels.discard(channel_id)
    if not task.cancelled():
        exc = task.exception()
        if exc is not None:
            _log.warning(
                "discord.webhook_creation_failed",
                channel_id=channel_id,
                error_type=type(exc).__name__,
            )
        elif task.result() is not None:
            _create_unavailable_until.pop(channel_id, None)


def _retry_after(exc: discord.HTTPException) -> float:
    value: object = getattr(exc, "retry_after", None)
    if value is None:
        headers: object = getattr(exc.response, "headers", None)
        if isinstance(headers, Mapping):
            value = cast(Mapping[str, object], headers).get("Retry-After")
    try:
        return (
            max(0.0, float(value))
            if isinstance(value, (int, float, str))
            else _LOOKUP_UNAVAILABLE_SECONDS
        )
    except (TypeError, ValueError):
        return _LOOKUP_UNAVAILABLE_SECONDS


def _fresh_files(files: list[discord.File]) -> list[discord.File]:
    rebuilt: list[discord.File] = []
    for file in files:
        file.reset()
        data = file.fp.read()
        file.reset()
        rebuilt.append(
            discord.File(
                io.BytesIO(data),
                filename=file.filename,
                spoiler=file.spoiler,
                description=file.description,
            )
        )
    return rebuilt


async def own_webhooks(
    client: discord.Client, channel: discord.abc.GuildChannel | discord.Thread
) -> dict[int, discord.Webhook]:
    parent = channel.parent if isinstance(channel, discord.Thread) else channel
    if not isinstance(parent, (discord.TextChannel, discord.ForumChannel)):
        return {}
    application_id = await ensure_application_id(client)
    if application_id is None:
        return {}
    try:
        raw_hooks = await client.http.channel_webhooks(parent.id)
    except discord.HTTPException:
        return {}
    return {
        hook.id: hook
        for raw in raw_hooks
        if is_our_discord_webhook(
            application_id=_snowflake(raw.get("application_id")),
            channel_id=_snowflake(raw.get("channel_id")),
            our_application_id=application_id,
            target_channel_id=parent.id,
        )
        if raw.get("token")
        for hook in [
            discord.Webhook.from_state(data=raw, state=client._connection)  # pyright: ignore[reportPrivateUsage]
        ]
    }


def _snowflake(value: object) -> int | None:
    return int(value) if isinstance(value, (int, str)) else None


async def own_webhook(
    client: discord.Client,
    channel: discord.abc.GuildChannel | discord.Thread,
    *,
    create: bool,
    webhook_id: int | None = None,
) -> discord.Webhook | None:
    if not create:
        return await _resolve_own_webhook(client, channel, create=False, webhook_id=webhook_id)
    parent = channel.parent if isinstance(channel, discord.Thread) else channel
    if not isinstance(parent, (discord.TextChannel, discord.ForumChannel)):
        return None
    if isinstance(channel, discord.Thread) and channel.locked:
        return None
    if credentials := _created_hooks.get(parent.id):
        return discord.Webhook.partial(*credentials, client=client)
    if _create_unavailable_until.get(parent.id, 0) > time.monotonic():
        return None
    task = _creation_tasks.get(parent.id)
    started_here = task is None
    if task is None:
        task = asyncio.create_task(_resolve_own_webhook(client, channel, create=True))
        _creation_tasks[parent.id] = task
        task.add_done_callback(
            lambda done, channel_id=parent.id: _finish_creation(channel_id, done)
        )
    try:
        hook = await asyncio.wait_for(asyncio.shield(task), _CREATE_WAIT_SECONDS)
    except TimeoutError:
        _create_unavailable_until[parent.id] = time.monotonic() + _LOOKUP_UNAVAILABLE_SECONDS
        if parent.id not in _deferred_channels:
            _deferred_channels.add(parent.id)
            _log.info("discord.webhook.create_deferred", channel_id=parent.id)
        return None
    except Exception:
        return None
    if hook is not None and not started_here and hook.token is not None:
        return discord.Webhook.partial(hook.id, hook.token, client=client)
    return hook


async def _resolve_own_webhook(
    client: discord.Client,
    channel: discord.abc.GuildChannel | discord.Thread,
    *,
    create: bool,
    webhook_id: int | None = None,
) -> discord.Webhook | None:
    parent = channel.parent if isinstance(channel, discord.Thread) else channel
    if not isinstance(parent, (discord.TextChannel, discord.ForumChannel)):
        if webhook_id is not None:
            raise discord.ClientException("webhook channel unavailable")
        return None
    if isinstance(channel, discord.Thread) and channel.locked and create:
        return None
    application_id = await ensure_application_id(client)
    if application_id is None:
        if webhook_id is not None:
            raise discord.ClientException("application identity unavailable")
        return None
    if webhook_id is not None and _lookup_unavailable_until.get(parent.id, 0) > time.monotonic():
        raise discord.ClientException("webhook lookup unavailable")
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
                    our_application_id=application_id,
                    target_channel_id=parent.id,
                )
                and raw.get("token")
            ]
            target_listed = webhook_id is not None and any(
                _snowflake(raw.get("id")) == webhook_id
                and is_our_discord_webhook(
                    application_id=_snowflake(raw.get("application_id")),
                    channel_id=_snowflake(raw.get("channel_id")),
                    our_application_id=application_id,
                    target_channel_id=parent.id,
                )
                for raw in raw_hooks
            )
            hook = (
                next((item for item in hooks if item.id == webhook_id), None)
                if webhook_id
                else None
            )
            if hook is None and webhook_id is None and hooks:
                chosen = select_discord_webhook_id(
                    (item.id for item in hooks),
                    channel.id if isinstance(channel, discord.Thread) else None,
                )
                hook = next(item for item in hooks if item.id == chosen)
            if hook is None and create:
                hook = await parent.create_webhook(name=DISCORD_AGENT_WEBHOOK_NAME)
                if hook.token is not None:
                    _log.info("discord.webhook.created", channel_id=parent.id, webhook_id=hook.id)
            if hook is None and target_listed:
                raise discord.ClientException("own webhook token unavailable")
            if create and hook is not None and hook.token is not None:
                _created_hooks[parent.id] = (hook.id, hook.token)
            return hook if hook is not None and hook.token is not None else None
        except discord.HTTPException as exc:
            if create and exc.status == 429:
                retry_after = _retry_after(exc)
                _create_unavailable_until[parent.id] = time.monotonic() + retry_after
                _log.warning(
                    "discord.webhook.create_rate_limited",
                    channel_id=parent.id,
                    retry_after=retry_after,
                )
            if exc.status == 403:
                _lookup_unavailable_until[parent.id] = (
                    time.monotonic() + _LOOKUP_UNAVAILABLE_SECONDS
                )
                if create:
                    _create_unavailable_until[parent.id] = (
                        time.monotonic() + _LOOKUP_UNAVAILABLE_SECONDS
                    )
            if webhook_id is not None:
                raise discord.ClientException("webhook lookup failed") from exc
            return None


async def own_webhook_ids(
    client: discord.Client, channel: discord.abc.GuildChannel | discord.Thread
) -> frozenset[int]:
    return frozenset(await own_webhooks(client, channel))


async def send_agent_message(
    client: discord.Client,
    channel: discord.abc.GuildChannel | discord.Thread,
    identity: AgentIdentity,
    *,
    content: str,
    files: list[discord.File] | None = None,
    extra_messages: list[discord.Message] | None = None,
    identity_enabled: bool = False,
) -> discord.Message:
    fallback_files = _fresh_files(files or [])
    hook = (
        None
        if identity.builtin or not identity_enabled
        else await own_webhook(client, channel, create=True)
    )
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
            if exc.code == 10015:
                parent = channel.parent if isinstance(channel, discord.Thread) else channel
                if parent is not None:
                    _created_hooks.pop(parent.id, None)
    if not isinstance(channel, discord.abc.Messageable):
        raise TypeError("channel does not support messages")
    fallback_content = (
        content
        if identity.builtin or not identity_enabled
        else fallback_name_prefix(identity.name, content)
    )
    chunks = [fallback_content[i : i + 2000] for i in range(0, len(fallback_content), 2000)] or [""]
    sent = await channel.send(content=chunks[0], files=fallback_files)
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
    identity_enabled: bool = False,
    **kwargs: Any,  # noqa: ANN401
) -> discord.Message | None:
    await ensure_application_id(client)
    hook = (
        await own_webhook(client, channel, create=False, webhook_id=message.webhook_id)
        if isinstance(message.webhook_id, int)
        else None
    )
    ours = isinstance(message.webhook_id, int) and (
        (client.application_id is not None and message.application_id == client.application_id)
        or hook is not None
    )
    if ours:
        if isinstance(channel, discord.Thread):
            kwargs["thread"] = channel
        if hook is not None:
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
            identity_enabled=identity_enabled,
        )
    await message.edit(**kwargs)
    return None


async def delete_own_message(
    client: discord.Client,
    channel: discord.abc.GuildChannel | discord.Thread,
    message: discord.Message,
    *,
    known_webhooks: dict[int, discord.Webhook] | None = None,
) -> None:
    await ensure_application_id(client)
    hooks = known_webhooks
    webhook_id = message.webhook_id
    if hooks is None and webhook_id is not None:
        hooks = await own_webhooks(client, channel)
    hooks = hooks or {}
    ours = isinstance(webhook_id, int) and (
        (client.application_id is not None and message.application_id == client.application_id)
        or webhook_id in hooks
    )
    if ours:
        assert isinstance(webhook_id, int)
        hook = hooks.get(webhook_id)
        if hook is None:
            raise discord.ClientException("own webhook token unavailable")
        if isinstance(channel, discord.Thread):
            await hook.delete_message(message.id, thread=channel)
        else:
            await hook.delete_message(message.id)
    else:
        await message.delete()
