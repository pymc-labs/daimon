"""Agent-authored Discord posts through application-owned channel webhooks."""

from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import Any, cast

import structlog
from daimon.core.agent_post_identity import (
    DISCORD_AGENT_WEBHOOK_NAME,
    discord_username,
    fallback_name_prefix,
    is_our_discord_webhook,
    select_discord_webhook_id,
)

import discord

_POOL_SIZE = 3
_UNAVAILABLE_SECONDS = 600
_locks: dict[int, asyncio.Lock] = {}
_webhooks: dict[int, dict[int, discord.Webhook]] = {}
_unavailable_until: dict[int, float] = {}
_send_unavailable_until: dict[tuple[int, str, str | None], float] = {}
_log = structlog.get_logger()


class _WebhookRateLimitCounter(logging.Filter):
    count = 0

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING and "is rate limited" in record.getMessage():
            self.count += 1
            _log.warning("discord.webhook.rate_limited", count=self.count)
        return True


_rate_limit_counter = _WebhookRateLimitCounter()
logging.getLogger("discord.webhook.async_").addFilter(_rate_limit_counter)


def _snowflake(value: object) -> int | None:
    return int(value) if isinstance(value, (int, str)) else None


def _cooldown(exc: discord.HTTPException) -> bool:
    return exc.code == 30007 or exc.status == 403


def known_webhook_ids() -> frozenset[int]:
    return frozenset(hook_id for pool in _webhooks.values() for hook_id in pool)


def _snapshot_files(kwargs: dict[str, Any]) -> dict[str, list[tuple[bytes, str, bool, str | None]]]:
    snapshots: dict[str, list[tuple[bytes, str, bool, str | None]]] = {}
    for key in ("file", "files"):
        value = kwargs.get(key)
        files = [value] if isinstance(value, discord.File) else value
        if not isinstance(files, list):
            continue
        saved: list[tuple[bytes, str, bool, str | None]] = []
        for file in cast(list[object], files):
            if not isinstance(file, discord.File):
                continue
            file.reset()
            saved.append((file.fp.read(), file.filename, file.spoiler, file.description))
            file.reset()
        if saved:
            snapshots[key] = saved
    return snapshots


def _retry_kwargs(
    kwargs: dict[str, Any], snapshots: dict[str, list[tuple[bytes, str, bool, str | None]]]
) -> dict[str, Any]:
    copied = dict(kwargs)
    for key, files in snapshots.items():
        rebuilt = [
            discord.File(io.BytesIO(data), filename=name, spoiler=spoiler, description=description)
            for data, name, spoiler, description in files
        ]
        copied[key] = rebuilt[0] if key == "file" else rebuilt
    return copied


class DiscordPostTransport:
    def __init__(
        self,
        client: discord.Client,
        channel: discord.abc.Messageable,
        *,
        name: str,
        avatar_url: str | None,
        builtin: bool,
        identity_enabled: bool | None = None,
    ) -> None:
        self.client = client
        self.channel = channel
        self.name = discord_username(name)
        self.avatar_url = avatar_url
        self.builtin = builtin
        if identity_enabled is None:
            runtime = getattr(client, "runtime", None)
            settings = getattr(runtime, "settings", None)
            configured = getattr(getattr(settings, "agent_identity", None), "enabled", None)
            identity_enabled = configured if isinstance(configured, bool) else False
        self.identity_enabled = identity_enabled
        self.fallback_used = False
        self._fallback_prefix_applied = False

    def _destination(
        self,
    ) -> tuple[discord.TextChannel | discord.ForumChannel, discord.Thread | None] | None:
        channel = self.channel
        thread = channel if isinstance(channel, discord.Thread) else None
        parent = thread.parent if thread is not None else channel
        if isinstance(parent, (discord.TextChannel, discord.ForumChannel)):
            return parent, thread
        return None

    def _ours(self, message: discord.Message) -> bool:
        application_id = self.client.application_id
        destination = self._destination()
        pool = _webhooks.get(destination[0].id, {}) if destination is not None else {}
        return isinstance(getattr(message, "webhook_id", None), int) and (
            (
                application_id is not None
                and getattr(message, "application_id", None) == application_id
            )
            or message.webhook_id in pool
        )

    def _send_cooldown_key(self, channel_id: int) -> tuple[int, str, str | None]:
        return channel_id, self.name, self.avatar_url

    async def _webhook(
        self, *, create: bool = True, webhook_id: int | None = None
    ) -> discord.Webhook | None:
        if create and (self.builtin or not self.identity_enabled):
            return None
        destination = self._destination()
        if destination is None:
            return None
        parent, thread = destination
        if create and thread is not None and thread.locked:
            return None
        if (
            create
            and _send_unavailable_until.get(self._send_cooldown_key(parent.id), 0)
            > time.monotonic()
        ):
            return None
        application_id = self.client.application_id
        if application_id is None:
            if webhook_id is not None:
                raise discord.ClientException("application identity unavailable")
            return None
        lock = _locks.setdefault(parent.id, asyncio.Lock())
        async with lock:
            pool = _webhooks.setdefault(parent.id, {})
            target_index = thread.id % _POOL_SIZE if thread is not None else 0
            if webhook_id is not None and webhook_id in pool:
                return pool[webhook_id]
            if create and len(pool) > target_index:
                return self._pick(pool)
            if create and pool and _unavailable_until.get(parent.id, 0) > time.monotonic():
                return self._pick(pool)
            if create and _unavailable_until.get(parent.id, 0) > time.monotonic():
                return None
            if webhook_id is not None and _unavailable_until.get(parent.id, 0) > time.monotonic():
                raise discord.ClientException("webhook lookup unavailable")
            if create:
                member = parent.guild.me
                if member is None or not parent.permissions_for(member).manage_webhooks:  # pyright: ignore[reportUnnecessaryComparison]
                    _unavailable_until[parent.id] = time.monotonic() + _UNAVAILABLE_SECONDS
                    return self._pick(pool)
            if webhook_id is None and not create and pool:
                return self._pick(pool)
            try:
                raw_hooks = await self.client.http.channel_webhooks(parent.id)
                target_listed = False
                for raw in raw_hooks:
                    if is_our_discord_webhook(
                        application_id=_snowflake(raw.get("application_id")),
                        channel_id=_snowflake(raw.get("channel_id")),
                        our_application_id=application_id,
                        target_channel_id=parent.id,
                    ):
                        if webhook_id is not None and _snowflake(raw.get("id")) == webhook_id:
                            target_listed = True
                        if not raw.get("token"):
                            continue
                        hook = discord.Webhook.from_state(  # pyright: ignore[reportPrivateUsage]
                            data=raw,
                            state=self.client._connection,  # pyright: ignore[reportPrivateUsage]
                        )
                        pool[hook.id] = hook
                if webhook_id is not None:
                    if target_listed and webhook_id not in pool:
                        raise discord.ClientException("own webhook token unavailable")
                    return pool.get(webhook_id)
                while create and len(pool) <= target_index and len(pool) < _POOL_SIZE:
                    try:
                        hook = await parent.create_webhook(name=DISCORD_AGENT_WEBHOOK_NAME)
                    except discord.HTTPException as exc:
                        if _cooldown(exc):
                            _unavailable_until[parent.id] = time.monotonic() + _UNAVAILABLE_SECONDS
                        return self._pick(pool)
                    except Exception as exc:
                        _log.warning(
                            "discord.webhook_creation_failed", error_type=type(exc).__name__
                        )
                        return self._pick(pool)
                    if hook.token is not None:
                        pool[hook.id] = hook
                    else:
                        return self._pick(pool)
                return self._pick(pool)
            except discord.HTTPException as exc:
                if _cooldown(exc):
                    _unavailable_until[parent.id] = time.monotonic() + _UNAVAILABLE_SECONDS
                if webhook_id is not None:
                    raise discord.ClientException("webhook lookup failed") from exc
                return pool.get(webhook_id) if webhook_id is not None else self._pick(pool)
            except Exception as exc:
                _log.warning("discord.webhook_lookup_failed", error_type=type(exc).__name__)
                if webhook_id is not None:
                    raise discord.ClientException("webhook lookup failed") from exc
                return pool.get(webhook_id) if webhook_id is not None else self._pick(pool)

    def _pick(self, pool: dict[int, discord.Webhook]) -> discord.Webhook | None:
        selected = select_discord_webhook_id(
            sorted(pool)[:_POOL_SIZE],
            self.channel.id if isinstance(self.channel, discord.Thread) else None,
        )
        return pool[selected] if selected is not None else None

    async def owns_message(self, message: discord.Message) -> bool:
        return self._ours(message)

    async def send(self, *args: Any, **kwargs: Any) -> discord.Message:  # noqa: ANN401
        kwargs = dict(kwargs)
        prefix_if_fallback = bool(kwargs.pop("_prefix_if_fallback", False))
        if kwargs.get("view") is None:
            kwargs.pop("view", None)
        webhook_rejected = False
        retry_files = _snapshot_files(kwargs)
        hook = await self._webhook()
        if hook is not None:
            destination = self._destination()
            assert destination is not None
            _, thread = destination
            send_kwargs = dict(kwargs)
            if thread is not None:
                send_kwargs["thread"] = thread
            try:
                sent = await hook.send(  # pyright: ignore[reportCallIssue]
                    *args, **send_kwargs, username=self.name, avatar_url=self.avatar_url, wait=True
                )
                assert sent is not None
                return cast(discord.Message, sent)
            except discord.HTTPException as exc:
                if exc.code == 50083 and thread is not None:
                    await thread.edit(archived=False)
                    try:
                        sent = await hook.send(  # pyright: ignore[reportCallIssue]
                            *args,
                            **_retry_kwargs(send_kwargs, retry_files),
                            username=self.name,
                            avatar_url=self.avatar_url,
                            wait=True,
                        )
                        assert sent is not None
                        return cast(discord.Message, sent)
                    except discord.HTTPException as retry_exc:
                        exc = retry_exc
                if exc.status == 429:
                    raise exc  # discord.py normally retries these itself
                if exc.code == 10015:
                    _webhooks.get(destination[0].id, {}).pop(hook.id, None)
                elif exc.status == 400:
                    _send_unavailable_until[self._send_cooldown_key(destination[0].id)] = (
                        time.monotonic() + _UNAVAILABLE_SECONDS
                    )
                webhook_rejected = True
        self.fallback_used = self.identity_enabled and not self.builtin
        content = kwargs.get("content") if isinstance(kwargs.get("content"), str) else None
        if content is None and args and isinstance(args[0], str):
            content = args[0]
        already_prefixed = isinstance(content, str) and content.startswith(
            fallback_name_prefix(self.name, "")
        )
        should_prefix = (
            (webhook_rejected or prefix_if_fallback)
            and self.identity_enabled
            and not self.builtin
            and not self._fallback_prefix_applied
            and not already_prefixed
        )
        if already_prefixed or should_prefix:
            self._fallback_prefix_applied = True
        if should_prefix and isinstance(kwargs.get("content"), str):
            kwargs["content"] = fallback_name_prefix(self.name, kwargs["content"])
        elif should_prefix and args and isinstance(args[0], str):
            args = (fallback_name_prefix(self.name, args[0]), *args[1:])
        try:
            return await self.channel.send(*args, **_retry_kwargs(kwargs, retry_files))
        except discord.HTTPException as exc:
            if exc.code != 50083 or not isinstance(self.channel, discord.Thread):
                raise
            await self.channel.edit(archived=False)
            return await self.channel.send(*args, **_retry_kwargs(kwargs, retry_files))

    async def edit(self, message: discord.Message, **kwargs: Any) -> discord.Message | None:  # noqa: ANN401
        # Boot recovery must keep the original intent when its pending card
        # cannot be edited; a replacement message does not clear that card.
        allow_replacement = kwargs.pop("_allow_replacement", True)
        if isinstance(kwargs.get("content"), str) and kwargs["content"].startswith(
            fallback_name_prefix(self.name, "")
        ):
            self._fallback_prefix_applied = True
        if self._ours(message):
            if self._destination() is None:
                raise discord.ClientException("webhook channel unavailable")
            hook = await self._webhook(create=False, webhook_id=message.webhook_id)
            if hook is None:
                if not allow_replacement:
                    raise discord.ClientException("own webhook token unavailable")
                return await self.send(_prefix_if_fallback=True, **_replacement_send_kwargs(kwargs))
            destination = self._destination()
            assert destination is not None
            _, thread = destination
            try:
                edit_kwargs = dict(kwargs)
                if thread is not None:
                    edit_kwargs["thread"] = thread
                return cast(discord.Message, await hook.edit_message(message.id, **edit_kwargs))  # pyright: ignore[reportCallIssue]
            except discord.NotFound as exc:
                if exc.code != 10015:
                    raise
                _webhooks.get(destination[0].id, {}).pop(hook.id, None)
                if not allow_replacement:
                    raise discord.ClientException("own webhook no longer exists") from exc
                return await self.send(_prefix_if_fallback=True, **_replacement_send_kwargs(kwargs))
        await message.edit(**kwargs)
        return None

    async def delete(self, message: discord.Message) -> None:
        if self._ours(message):
            if self._destination() is None:
                raise discord.ClientException("webhook channel unavailable")
            hook = await self._webhook(create=False, webhook_id=message.webhook_id)
            if hook is None:
                raise discord.ClientException("own webhook token unavailable")
            destination = self._destination()
            assert destination is not None
            _, thread = destination
            if thread is not None:
                await hook.delete_message(message.id, thread=thread)
            else:
                await hook.delete_message(message.id)
            return
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
