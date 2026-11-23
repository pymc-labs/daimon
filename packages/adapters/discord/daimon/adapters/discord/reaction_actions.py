"""Reaction actions on Discord: mark a message after a tool call, confirm it with a reaction.

The mark is a hook, not a model tool. After a turn ends well, `mark_trigger_message`
adds the binding's mark emoji to the message that started the turn when the agent's
call of the binding's tool finished without error (`daimon.core.reaction_actions`).

`ReactionActionCog` reads the confirm emoji. A person adds it to a message that
carries the bot's own mark emoji, and the bot asks the MCP service to call the
binding's confirm tool for that person. The MCP service holds the server's key;
this process sends only a 15-second, single-use credential (`daimon.core.reaction_credential`).
No model turn runs. On success the bot swaps the mark for the confirm emoji. An
error, or a result that needs attention, puts the warning emoji on the message and
sends the person a private card. Nothing is posted in the channel. Removing the
confirm emoji does nothing.

Both paths are best effort: every Discord write is swallowed and logged, like the
other reactions. A broad catch at each listener entry is the one outer boundary,
because discord.py drops what a listener raises. With the feature off, or no binding
for the server, nothing runs past a lookup in the settings.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast

import httpx
import structlog
from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.core.features.snapshot import FeaturePlace, place_feature
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.reaction_actions import (
    CALL_PATH,
    NO_TEXT,
    Outcome,
    ReactionAction,
    bindings_for,
    confirm_binding,
    marking_actions,
    message_url,
)
from daimon.core.reaction_credential import ReactionCall, mint

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from anthropic.types.beta import BetaManagedAgentsAgent
    from daimon.adapters.discord.bot import DaimonBot
    from daimon.core.turn.state import TurnState

log = structlog.get_logger()

#: The call waits for the tool; the MCP service allows it a minute.
_CALL_TIMEOUT_S: Final = 75.0
_FAILED_TEXT: Final = "The action could not be reached."
#: Printed in the card under the tool's own line.
_RETRY_ADD: Final = "Check the message, then add the emoji again."
_RETRY_REMOVE: Final = "Check the message, then remove your emoji and add it again."

#: How long, and for how many messages, a success is remembered if the mark stays.
_DONE_SECONDS: Final = 600.0
_DONE_MAX: Final = 512

#: What a call needs: the credential's claims, and the outcome it brings back.
Caller = Callable[[ReactionCall], Awaitable[Outcome]]

_active: ReactionActionCog | None = None


def _workspace(guild_id: int | str) -> str:
    return f"discord:{guild_id}"


def _emoji(spec: str) -> discord.PartialEmoji:
    name, _, ident = spec.partition(":")
    return discord.PartialEmoji(name=name, id=int(ident))


async def _enabled(cog: ReactionActionCog, guild_id: int) -> bool:
    runtime = cog.bot.runtime
    place = FeaturePlace.of(
        derive_tenant_uuid(platform="discord", workspace_id=str(guild_id)), "discord", None
    )
    return (
        await place_feature(runtime.sessionmaker, runtime.settings, place, "reaction_actions")
    ).on


async def mark_trigger_message(
    message: discord.Message | None, state: TurnState, agent: BetaManagedAgentsAgent | None
) -> None:
    """Add each matching binding's mark emoji to the turn's trigger message. Never raises.

    `agent` is the agent that ran the turn. Without it nothing is marked: a tool block
    names its server only by a label the agent chose."""
    cog = _active
    if cog is None or message is None or message.guild is None or agent is None:
        return
    try:
        actions = bindings_for(cog.actions, _workspace(message.guild.id))
        public_url = cog.bot.runtime.settings.mcp.public_url
        marking = (
            marking_actions(
                state.content,
                actions,
                agent_name=agent.name,
                server_urls={server.name: server.url for server in agent.mcp_servers},
                public_url=None if public_url is None else str(public_url),
            )
            if actions
            else ()
        )
        if marking and await _enabled(cog, message.guild.id):
            for action in marking:
                await message.add_reaction(_emoji(action.mark_emoji))
    except Exception:  # cosmetic boundary: the answer is already delivered
        log.warning("reaction_action.mark_failed", exc_info=True)


class ReactionActionCog(commands.Cog):
    """Listens for the confirm emoji and runs the binding's confirm tool for the reactor."""

    def __init__(self, bot: DaimonBot, *, caller: Caller | None = None) -> None:
        self.bot = bot
        self.actions: tuple[ReactionAction, ...] = tuple(
            bot.runtime.settings.mcp_identity.reaction_actions
        )
        self._caller = caller or self._post
        self._locks: dict[int, tuple[asyncio.Lock, int]] = {}
        self._done: OrderedDict[tuple[int, int], float] = OrderedDict()

    async def cog_load(self) -> None:
        global _active
        _active = self if self.actions else None

    async def cog_unload(self) -> None:
        global _active
        if _active is self:
            _active = None

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if not self.actions:
            return
        try:
            await self._confirm(payload)
        except Exception:  # outer boundary: discord.py drops what a listener raises
            log.warning("reaction_action.handler_failed", exc_info=True)

    async def _confirm(self, payload: discord.RawReactionActionEvent) -> None:
        # Config-only gates first: no I/O until the reaction can be ours.
        if payload.guild_id is None or payload.emoji.id is None:
            return
        me = self.bot.user
        if me is None or payload.user_id == me.id:
            return
        if payload.member is not None and payload.member.bot:
            return
        action = confirm_binding(
            self.actions, workspace=_workspace(payload.guild_id), emoji_id_=payload.emoji.id
        )
        if action is None or not await _enabled(self, payload.guild_id):
            return
        # One call at a time per message. A later reaction waits, then reads the message
        # afresh: after a success the mark is gone and it makes no call; after a failure
        # the mark is still there and that person's call runs.
        lock, waiting = self._locks.get(payload.message_id, (asyncio.Lock(), 0))
        self._locks[payload.message_id] = (lock, waiting + 1)
        try:
            async with lock:
                if self._was_done(payload.message_id, action):
                    return
                message = await self._marked_message(payload, action)
                if message is None:
                    return
                outcome = await self._caller(self._call(payload, action, message))
                if outcome.status == "ok":
                    self._remember(payload.message_id, action)
                await self._settle(payload, action, message, outcome)
        finally:
            lock, waiting = self._locks[payload.message_id]
            if waiting <= 1:
                del self._locks[payload.message_id]
            else:
                self._locks[payload.message_id] = (lock, waiting - 1)

    def _was_done(self, message_id: int, action: ReactionAction) -> bool:
        """Whether this message's confirm already succeeded, even if the mark stayed."""
        at = self._done.get((message_id, action.mark_emoji_id))
        return at is not None and time.monotonic() - at < _DONE_SECONDS

    def _remember(self, message_id: int, action: ReactionAction) -> None:
        """Keep a success in this process, for a bounded time and count. The Discord
        write that removes the mark can fail, and the mark alone must not allow a repeat."""
        now = time.monotonic()
        self._done[(message_id, action.mark_emoji_id)] = now
        while self._done and (
            len(self._done) > _DONE_MAX or now - next(iter(self._done.values())) >= _DONE_SECONDS
        ):
            self._done.popitem(last=False)

    async def _marked_message(
        self, payload: discord.RawReactionActionEvent, action: ReactionAction
    ) -> discord.Message | None:
        """The message, only if it carries the bot's own mark emoji; else None."""
        try:
            channel = self.bot.get_channel(payload.channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(payload.channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                return None
            message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            log.info("reaction_action.message_unverifiable", message_id=str(payload.message_id))
            return None
        if any(
            isinstance(r.emoji, (discord.Emoji, discord.PartialEmoji))
            and r.emoji.id == action.mark_emoji_id
            and r.me
            for r in message.reactions
        ):
            return message
        return None

    def _call(
        self,
        payload: discord.RawReactionActionEvent,
        action: ReactionAction,
        message: discord.Message,
    ) -> ReactionCall:
        channel = message.channel
        parent = channel.parent_id if isinstance(channel, discord.Thread) else None
        return ReactionCall(
            workspace=action.workspace,
            server=action.server,
            tool=action.confirm_tool,
            agent=action.agent,
            user=str(payload.user_id),
            guild=str(payload.guild_id),
            channel=str(payload.channel_id),
            message=str(payload.message_id),
            parent=None if parent is None else str(parent),
        )

    async def _post(self, call: ReactionCall) -> Outcome:
        settings = self.bot.runtime.settings.mcp
        root, secret = settings.app_root_url, settings.jwt_secret
        if root is None or secret is None:
            return Outcome("error", _FAILED_TEXT)
        token = mint(call, secret=secret.get_secret_value().encode(), now=datetime.now(UTC))
        try:
            async with httpx.AsyncClient(timeout=_CALL_TIMEOUT_S) as http:
                response = await http.post(
                    root + CALL_PATH, headers={"authorization": f"Bearer {token}"}, json={}
                )
            body = cast("dict[str, object]", response.json())
        except (httpx.HTTPError, ValueError):
            log.warning("reaction_action.call_failed", exc_info=True)
            return Outcome("error", _FAILED_TEXT)
        status, text = body.get("status"), body.get("message")
        if response.status_code != 200 or status not in ("ok", "attention", "error"):
            log.warning("reaction_action.call_refused", status=response.status_code)
            return Outcome("error", _FAILED_TEXT)
        return Outcome(
            cast("str", status),  # pyright: ignore[reportArgumentType]
            text if isinstance(text, str) else "",
        )

    async def _settle(
        self,
        payload: discord.RawReactionActionEvent,
        action: ReactionAction,
        message: discord.Message,
        outcome: Outcome,
    ) -> None:
        guild = message.guild
        me: discord.abc.Snowflake | None = guild.me if guild is not None else self.bot.user
        if me is None:
            return
        warning = action.warning_emoji
        if outcome.status == "ok":
            try:
                await message.remove_reaction(_emoji(action.mark_emoji), me)
                await message.add_reaction(_emoji(action.confirm_emoji))
                if any(str(r.emoji) == warning and r.me for r in message.reactions):
                    await message.remove_reaction(warning, me)
            except discord.HTTPException:
                log.warning("reaction_action.reaction_failed", exc_info=True)
            return
        # Take the person's emoji back so they can add it again; this needs a permission
        # the bot may lack, and the card says which it is.
        removed = await _try(
            message.remove_reaction(_emoji(action.confirm_emoji), discord.Object(payload.user_id))
        )
        warned = await _try(message.add_reaction(warning))
        notified = await self._notify(payload.user_id, outcome, removed=removed)
        if not warned and not notified:
            log.warning(
                "reaction_action.failure_unshown",
                message_url=message_url(
                    str(payload.guild_id), str(payload.channel_id), str(payload.message_id)
                ),
            )

    async def _notify(self, user_id: int, outcome: Outcome, *, removed: bool) -> bool:
        retry = _RETRY_ADD if removed else _RETRY_REMOVE
        text = f"{outcome.text or NO_TEXT}\n{retry}"
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(embed=build_notice_embed(text, neutral=True))
        except discord.HTTPException:
            log.info("reaction_action.notice_failed", exc_info=True)
            return False
        return True


async def _try(write: Awaitable[object]) -> bool:
    """Run one Discord write; False when it fails. Best effort, logged."""
    try:
        await write
    except discord.HTTPException:
        log.info("reaction_action.write_failed", exc_info=True)
        return False
    return True
