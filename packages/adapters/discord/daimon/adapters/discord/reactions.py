"""The status reactions on the message a turn answers: working, done, merged.

With the `status_reactions` feature on, the asker's message shows ⏳ while the turn
runs and ✅ once it ends, however it ended, and a message queued behind a running
turn shows 📨. The status comes first, then the model and effort emoji, then any
other reaction of the bot's.
"""

from __future__ import annotations

import asyncio
import contextlib
import weakref

import structlog
from daimon.core.model_reactions import MODEL_MARKS

import discord

log = structlog.get_logger()

WORKING = "⏳"
DONE = "✅"
QUEUED = "📨"
STATUS_MARKS = frozenset({WORKING, DONE, QUEUED})
# Two arrangements of one message interleave their removes and adds; one at a time.
_ARRANGING: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()


def own_user(message: discord.Message) -> discord.abc.Snowflake | None:
    """The bot as it appears where `message` is, to take its own reactions off."""
    if message.guild is not None:
        return message.guild.me
    channel = message.channel
    return channel.me if isinstance(channel, discord.DMChannel) else None


async def arrange_reactions(
    message: discord.Message,
    *,
    status: str | None = None,
    model: tuple[str, ...] = (),
) -> bool:
    """Show `status`, then the model emoji, then the bot's other reactions, in that order.

    `status` None keeps the status shown, and `model` empty keeps the model emoji shown.
    Discord lists reactions in the order they were added, so every reaction from the
    first one out of place on is taken off and added again, one arrangement per message
    at a time. Best effort: False when it did not finish.
    """
    me = own_user(message)
    if me is None:
        return False
    lock = _ARRANGING.setdefault(message.id, asyncio.Lock())
    async with lock:
        try:
            fetched = await message.fetch()
            shown = tuple(str(reaction.emoji) for reaction in fetched.reactions if reaction.me)
            statuses = (
                (status,) if status is not None else tuple(e for e in shown if e in STATUS_MARKS)
            )
            models = model or tuple(e for e in shown if e in MODEL_MARKS)
            rest = tuple(e for e in shown if e not in STATUS_MARKS and e not in MODEL_MARKS)
            target = (*statuses, *models, *rest)
            kept = 0
            while kept < min(len(shown), len(target)) and shown[kept] == target[kept]:
                kept += 1
            for emoji in shown[kept:]:
                await fetched.remove_reaction(emoji, me)
            for emoji in target[kept:]:
                await fetched.add_reaction(emoji)
        except Exception as exc:  # cosmetic boundary: a reaction never fails the turn
            log.warning("turn.reactions_arrange_failed", error_type=type(exc).__name__)
            return False
    return True


async def remove_own_reaction(message: discord.Message, emoji: str) -> None:
    """Take the bot's `emoji` off `message`, in a server or a DM. Best effort."""
    me = own_user(message)
    if me is None:
        return
    with contextlib.suppress(discord.HTTPException):
        await message.remove_reaction(emoji, me)
