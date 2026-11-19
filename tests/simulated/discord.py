"""Discord, driven at the gateway: real `DaimonBot`, real parser, recorded HTTP posts."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, cast

import discord
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.confirm_click import (
    _ClickAdapter,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.discord.tool_confirmation import confirmation_requester
from daimon.testing.discord_builders import discord_runtime
from daimon.testing.discord_ingress import IngressHTTP
from daimon.testing.discord_surface import OfflineDiscordGateway
from discord.http import HTTPClient
from discord.webhook.async_ import async_context

from .world import World

if TYPE_CHECKING:
    from discord.types.channel import TextChannel as ChannelPayload
    from discord.types.gateway import MessageCreateEvent
    from discord.types.guild import Guild as GuildPayload
    from discord.types.user import User as UserPayload

GUILD, CHANNEL, PERSON, BOT, DM = 4100, 4101, 4103, 4104, 4199


class _HTTP(IngressHTTP):
    # discord.py's Interaction reads the client's session; the click adapter never uses it.
    _HTTPClient__session = None
    proxy = proxy_auth = None

    async def get_member(self, guild_id: int, member_id: int) -> dict[str, Any]:
        return {
            "user": _user(member_id),
            "roles": [],
            "joined_at": "2026-11-01T00:00:00+00:00",
            "deaf": False,
            "mute": False,
            "flags": 0,
        }

    async def get_guild(self, guild_id: int, *, with_counts: bool = True) -> dict[str, Any]:
        return _guild()

    async def get_channel(self, channel_id: int) -> dict[str, Any]:
        thread = next((c for c in self.creations if c.thread_id == str(channel_id)), None)
        return json.loads(thread.response_json) if thread else _channel()

    async def close(self) -> None:
        """The bot's own HTTP session closes on shutdown; Discord keeps its state."""

    async def start_private_message(self, user_id: int) -> dict[str, Any]:
        self.surfaces.setdefault(
            DM, OfflineDiscordGateway(evidence_id="sim:dm", channel_id=DM, clock=self.clock)
        )
        self.history.setdefault(DM, [])
        return {"id": str(DM), "type": 1, "recipients": [_user(user_id)]}


class _Bot(DaimonBot):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.tasks: list[asyncio.Task[None]] = []
        self.errors: list[str] = []

    def _schedule_event(self, coro: Any, event_name: str, *args: Any, **kwargs: Any) -> Any:
        task = super()._schedule_event(coro, event_name, *args, **kwargs)  # pyright: ignore[reportUnknownMemberType]
        self.tasks.append(task)
        return task

    async def on_error(self, event_method: str, *args: object, **kwargs: object) -> None:
        self.errors.append(event_method)
        await super().on_error(event_method, *args, **kwargs)


class Discord:
    """One guild with one channel; `say` posts a mention and waits for the turn."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.http = _HTTP(evidence_id="sim", parent_channel_id=CHANNEL)
        self._next_id = 5000
        self._boot()

    def _boot(self) -> None:
        world = self.world
        runtime = discord_runtime(
            settings=world.settings,
            anthropic=world.anthropic,
            sessionmaker=world.sessions,
            deployment_default=world.deps.deployment_default,
            resolver_cache=world.deps.resolver_cache,
            turn_deps=world.deps,
        )
        self.bot = _Bot(
            runtime=runtime, intents=discord.Intents.none(), http_client=cast(HTTPClient, self.http)
        )

    async def install(self) -> None:
        """The guild-join path: provision the tenant, then seed its defaults."""
        guild = await self._connect()
        tenant_id = await self.bot._provision_joined_guild(guild)  # pyright: ignore[reportPrivateUsage]
        assert tenant_id is not None, "the guild join provisioned no tenant"
        self.tenant_id = tenant_id
        ok = await self.bot._seed_tenant_defaults(  # pyright: ignore[reportPrivateUsage]
            tenant_id=tenant_id, guild=guild, was_ready=False
        )
        assert ok is True, f"seeding the fresh tenant's defaults failed: {self.bot.errors}"
        self._idle = asyncio.all_tasks()

    async def restart(self) -> None:
        """The bot process restarts; Discord, its threads and posts, stay as they were."""
        await self.bot.close()
        await self.world.restart()
        self._boot()
        await self._connect()
        self._idle = asyncio.all_tasks()

    async def _connect(self) -> discord.Guild:
        """The gateway's READY: the bot user, its guild and the channel."""
        await self.bot._async_setup_hook()  # pyright: ignore[reportPrivateUsage]
        state = self.bot._connection  # pyright: ignore[reportPrivateUsage]
        state.user = discord.ClientUser(state=state, data=cast("UserPayload", _user(BOT, bot=True)))
        guild = discord.Guild(
            state=state,
            data=cast("GuildPayload", _guild()),
        )
        channel = discord.TextChannel(
            state=state,
            guild=guild,
            data=cast("ChannelPayload", _channel()),
        )
        state._add_guild(guild)  # pyright: ignore[reportPrivateUsage]
        guild._add_channel(channel)  # pyright: ignore[reportPrivateUsage]
        for made in self.http.creations:  # GUILD_CREATE lists the guild's active threads
            data = json.loads(made.response_json)
            guild._add_thread(discord.Thread(guild=guild, state=state, data=data))  # pyright: ignore[reportPrivateUsage]
        self.http.connection_state, self.http.guild_id, self.http.bot_user_id = state, GUILD, BOT
        return guild

    async def say(self, text: str, *, thread: int | None = None) -> list[dict[str, Any]]:
        """Mention the bot in the channel (or a thread); return the posts the turn made."""
        self._next_id += 1
        channel = thread or CHANNEL
        payload = {
            "id": str(self._next_id),
            "guild_id": str(GUILD),
            "channel_id": str(channel),
            "type": 0,
            "content": f"<@{BOT}> {text}",
            "attachments": [],
            "embeds": [],
            "components": [],
            "author": _user(PERSON),
            "mentions": [_user(BOT, bot=True)],
            "mention_roles": [],
            "mention_everyone": False,
            "pinned": False,
            "timestamp": "2026-11-19T12:00:00+00:00",
            "edited_timestamp": None,
            "member": {
                "roles": [],
                "joined_at": "2026-11-01T00:00:00+00:00",
                "deaf": False,
                "mute": False,
                "flags": 0,
            },
        }
        start = len(self.http.posts)
        self.http.history.setdefault(channel, []).append(json.loads(json.dumps(payload)))
        self.bot._connection.parse_message_create(cast("MessageCreateEvent", payload))  # pyright: ignore[reportPrivateUsage]
        await self.settle()
        return self.texts(start)

    def thread_of_last_reply(self) -> int:
        """The thread the bot's latest post went to."""
        return int(self.http.posts[-1].message.channel_id)

    def texts(self, start: int = 0) -> list[str]:
        """What the person sees, post by post: text, card words and button labels."""
        return [_text(p) for p in self.http.posts[start:]]

    async def press(self, label: str) -> list[str]:
        """Click a live Confirm card's button through the gateway's interaction parser."""
        state = self.bot._connection  # pyright: ignore[reportPrivateUsage]
        found = self._card(label)
        assert found is not None, f"no live card with a {label!r} button: {self.texts()}"
        message_id, custom_id = found
        channel_id = next(
            int(p.message.channel_id)
            for p in self.http.posts
            if p.message.message_id == str(message_id)
        )
        message = await self.http.get_message(channel_id, message_id)
        token = f"sim-{message_id}"
        payload = {
            "id": str(discord.utils.time_snowflake(discord.utils.utcnow())),
            "application_id": str(BOT),
            "type": discord.InteractionType.component.value,
            "token": token,
            "version": 1,
            "guild_id": str(GUILD),
            "channel_id": str(channel_id),
            "channel": await self.http.get_channel(channel_id),
            "member": {**await self.http.get_member(GUILD, PERSON), "permissions": "0"},
            "message": message,
            "data": {"custom_id": custom_id, "component_type": discord.ComponentType.button.value},
            "app_permissions": "0",
            "locale": "en-US",
            "guild_locale": "en-US",
            "attachment_size_limit": 10 * 1024 * 1024,
            "entitlements": [],
            "authorizing_integration_owners": {"0": str(GUILD)},
            "context": 0,
        }
        # The production click path's reply transport: card edits through the bot's HTTP.
        adapter = _ClickAdapter(
            http=self.http, channel_id=channel_id, message_id=message_id, token=token
        )
        start, reset = len(self.http.posts), async_context.set(adapter)
        try:
            state.parsers["INTERACTION_CREATE"](payload)
        finally:
            async_context.reset(reset)
        await self.settle()
        return self.texts(start)

    def _card(self, label: str) -> tuple[int, str] | None:
        views = self.bot._connection._view_store._views  # pyright: ignore[reportPrivateUsage]
        for message_id, items in views.items():
            for (_, custom_id), item in items.items():
                if getattr(item, "label", None) == label and confirmation_requester(item.view):
                    return message_id, custom_id
        return None

    async def settle(self) -> None:
        """Wait until all the work a message started is done, or it waits on a card."""
        for _ in range(200):
            await asyncio.sleep(0.05)
            busy = [t for t in asyncio.all_tasks() - self._idle if t is not asyncio.current_task()]
            if (
                not [t for t in busy if not t.done()]
                or self._card("Approve")
                or self._card("Confirm")
            ):
                return
        raise AssertionError(
            f"still busy after 10s: {[t.get_name() for t in busy]} {self.texts()[-3:]}"
        )


def _channel() -> dict[str, Any]:
    return {"id": str(CHANNEL), "type": 0, "name": "general", "position": 0, "guild_id": str(GUILD)}


def _text(post: Any) -> str:
    message = post.message
    return "\n".join([message.content, *message.embed_texts, *message.component_labels])


def _user(user_id: int, *, bot: bool = False) -> dict[str, Any]:
    return {
        "id": str(user_id),
        "username": "Daimon" if bot else "Person",
        "discriminator": "0000",
        "avatar": None,
        "bot": bot,
    }


def _guild() -> dict[str, Any]:
    return {
        "id": str(GUILD),
        "name": "Sim",
        "owner_id": str(PERSON),
        "roles": [],
        "emojis": [],
        "features": [],
    }
