"""Agent-authored Discord posts through an application-owned channel webhook."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from daimon.core.agent_post_identity import (
    DISCORD_AGENT_WEBHOOK_NAME,
    discord_username,
    is_our_discord_webhook,
)

import discord

_locks: dict[int, asyncio.Lock] = {}
_webhooks: dict[int, discord.Webhook] = {}


def _snowflake(value: object) -> int | None:
    return int(value) if isinstance(value, (int, str)) else None


class DiscordPostTransport:
    def __init__(
        self,
        client: discord.Client,
        channel: discord.abc.Messageable,
        *,
        name: str,
        avatar_url: str | None,
        builtin: bool,
    ) -> None:
        self.client = client
        self.channel = channel
        self.name = discord_username(name)
        self.avatar_url = avatar_url
        self.builtin = builtin
        self.fallback_used = False

    def _destination(
        self,
    ) -> tuple[discord.TextChannel | discord.ForumChannel, discord.Thread | None] | None:
        channel = self.channel
        thread = channel if isinstance(channel, discord.Thread) else None
        parent = thread.parent if thread is not None else channel
        if isinstance(parent, (discord.TextChannel, discord.ForumChannel)):
            return parent, thread
        return None

    async def _webhook(self, *, create: bool = True) -> discord.Webhook | None:
        if self.builtin:
            return None
        destination = self._destination()
        if destination is None:
            return None
        parent, thread = destination
        if thread is not None and thread.locked:
            return None
        member = parent.guild.me
        if member is None or not parent.permissions_for(member).manage_webhooks:  # pyright: ignore[reportUnnecessaryComparison]
            return None
        if self.client.user is None:
            return None
        lock = _locks.setdefault(parent.id, asyncio.Lock())
        async with lock:
            cached = _webhooks.get(parent.id)
            if cached is not None:
                return cached
            try:
                raw_hooks = await self.client.http.channel_webhooks(parent.id)
                hook = next(
                    (
                        discord.Webhook.from_state(data=raw, state=self.client._connection)  # pyright: ignore[reportPrivateUsage]
                        for raw in raw_hooks
                        if is_our_discord_webhook(
                            application_id=_snowflake(raw.get("application_id")),
                            channel_id=_snowflake(raw.get("channel_id")),
                            our_application_id=self.client.user.id,
                            target_channel_id=parent.id,
                        )
                        and raw.get("token")
                    ),
                    None,
                )
                if hook is None and create:
                    hook = await parent.create_webhook(name=DISCORD_AGENT_WEBHOOK_NAME)
                if hook is not None and hook.token is not None:
                    _webhooks[parent.id] = hook
                    return hook
                return None
            except (discord.HTTPException, discord.Forbidden):
                return None

    async def owns_message(self, message: discord.Message) -> bool:
        hook = await self._webhook(create=False)
        webhook_id = getattr(message, "webhook_id", None)
        return hook is not None and isinstance(webhook_id, int) and webhook_id == hook.id

    async def send(self, *args: Any, **kwargs: Any) -> discord.Message:  # noqa: ANN401
        hook = await self._webhook()
        if hook is not None:
            destination = self._destination()
            assert destination is not None
            _, thread = destination
            send_kwargs: dict[str, Any] = dict(kwargs)
            if thread is not None:
                send_kwargs["thread"] = thread
            try:
                sent = await hook.send(  # pyright: ignore[reportCallIssue]
                    *args,
                    **send_kwargs,
                    username=self.name,
                    avatar_url=self.avatar_url,
                    wait=True,
                )
                assert sent is not None
                return cast(discord.Message, sent)
            except discord.HTTPException as exc:
                if exc.code == 50083 and thread is not None:
                    await thread.edit(archived=False)
                    sent = await hook.send(  # pyright: ignore[reportCallIssue]
                        *args,
                        **send_kwargs,
                        username=self.name,
                        avatar_url=self.avatar_url,
                        wait=True,
                    )
                    assert sent is not None
                    return cast(discord.Message, sent)
                if not isinstance(exc, discord.NotFound):
                    raise
                if exc.code != 10015:  # Unknown Webhook
                    raise
                _webhooks.pop(destination[0].id, None)
        self.fallback_used = not self.builtin
        try:
            return await self.channel.send(*args, **kwargs)
        except discord.HTTPException as exc:
            if exc.code != 50083 or not isinstance(self.channel, discord.Thread):
                raise
            await self.channel.edit(archived=False)
            return await self.channel.send(*args, **kwargs)

    async def edit(self, message: discord.Message, **kwargs: Any) -> discord.Message | None:  # noqa: ANN401
        hook = await self._webhook()
        webhook_id = getattr(message, "webhook_id", None)
        if hook is not None and isinstance(webhook_id, int) and webhook_id == hook.id:
            destination = self._destination()
            assert destination is not None
            _, thread = destination
            try:
                edit_kwargs: dict[str, Any] = dict(kwargs)
                if thread is not None:
                    edit_kwargs["thread"] = thread
                return cast(discord.Message, await hook.edit_message(message.id, **edit_kwargs))  # pyright: ignore[reportCallIssue]
            except discord.NotFound as exc:
                if exc.code != 10015:  # Unknown Webhook
                    raise
                _webhooks.pop(destination[0].id, None)
                # A deleted webhook cannot edit its old messages. Surface the
                # update as a new post instead of losing the turn's answer.
                return await self.send(**_replacement_send_kwargs(kwargs))
        if isinstance(webhook_id, int):
            return await self.send(**_replacement_send_kwargs(kwargs))
        await message.edit(**kwargs)
        return None

    async def delete(self, message: discord.Message) -> None:
        hook = await self._webhook()
        webhook_id = getattr(message, "webhook_id", None)
        if hook is not None and isinstance(webhook_id, int) and webhook_id == hook.id:
            destination = self._destination()
            assert destination is not None
            _, thread = destination
            if thread is not None:
                await hook.delete_message(message.id, thread=thread)
            else:
                await hook.delete_message(message.id)
        else:
            await message.delete()


def _replacement_send_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    send_kwargs = dict(kwargs)
    attachments = send_kwargs.pop("attachments", None)
    if attachments:
        send_kwargs["files"] = [a for a in attachments if isinstance(a, discord.File)]
    if send_kwargs.get("view") is None:
        send_kwargs.pop("view", None)
    if send_kwargs.get("embed") is None:
        send_kwargs.pop("embed", None)
    return send_kwargs
