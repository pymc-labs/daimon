"""What a non-production bot does with traffic from outside its allowed guilds.

`DaimonBot.dispatch` drops every event from a guild missing from
``command_guild_ids``. A DM names no guild, so it is let through only when its
author is a member of an allowed guild; anything unknown fails closed. A
mention in, or an install to, a guild that is not allowed gets one plain line
and is never processed.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Iterable

import structlog

import discord

log = structlog.get_logger(__name__)

#: How long one channel stays quiet after it was told the bot isn't enabled.
NOTICE_COOLDOWN_S = 3600.0
#: How many channels one process remembers having told.
NOTICE_LIMIT = 10_000


def guild_not_enabled_text(contact: str) -> str:
    return f"Daimon isn't enabled in this server yet. Ask {contact} to enable it."


def dm_not_enabled_text(contact: str) -> str:
    return f"Daimon isn't enabled for you yet. Ask {contact} to enable it."


class NoticeLimiter:
    """At most one notice per key per cooldown; in memory, oldest key forgotten first."""

    def __init__(
        self,
        *,
        cooldown_s: float = NOTICE_COOLDOWN_S,
        limit: int = NOTICE_LIMIT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cooldown_s = cooldown_s
        self._limit = limit
        self._clock = clock
        self._last: OrderedDict[int, float] = OrderedDict()

    def allow(self, key: int) -> bool:
        """True when `key` may be told now; remembers it."""
        now = self._clock()
        last = self._last.get(key)
        if last is not None and now - last < self._cooldown_s:
            return False
        self._last[key] = now
        self._last.move_to_end(key)
        if len(self._last) > self._limit:
            self._last.popitem(last=False)
        return True

    def forget(self, key: int) -> None:
        """Let `key` be told again, e.g. after the notice could not be sent."""
        self._last.pop(key, None)


async def is_member_of_any(guilds: Iterable[discord.Guild], user_id: int) -> bool | None:
    """Whether the user is in any of `guilds`: True, False, or None when unknown.

    The member cache is checked first; a miss is confirmed with a live lookup,
    since the bot runs without the members intent. Not-found is a definite no;
    any other Discord failure leaves the answer unknown.
    """
    unknown = False
    for guild in guilds:
        if guild.get_member(user_id) is not None:
            return True
        try:
            await guild.fetch_member(user_id)
        except discord.NotFound:
            continue
        except discord.HTTPException as exc:
            log.warning(
                "discord.staging_gate.member_lookup_failed",
                guild_id=str(guild.id),
                error_type=type(exc).__name__,
            )
            unknown = True
            continue
        return True
    return None if unknown else False
