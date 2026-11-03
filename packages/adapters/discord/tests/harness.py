"""Shared Discord-adapter test harness: the bot builder every bot test
used to copy verbatim.

Runtime builders stay per-file on purpose: each pins the settings
attributes its tests read, and a superset would change truthiness branches.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import discord
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.config import TurnQueueSettings
from daimon.core.membership import CachedMembershipResolver, Membership
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn_queue import TurnQueue
from daimon.testing.turn_fakes import deps_for


def make_bot(runtime: DiscordRuntime) -> DaimonBot:
    """A `DaimonBot` on `runtime` whose gateway user is a stub that is
    mentioned in every message, so `on_message` reaches orchestration."""
    intents = discord.Intents.default()
    intents.message_content = True
    if not isinstance(runtime, DiscordRuntime):
        runtime = DiscordRuntime(
            settings=runtime.settings,
            anthropic=runtime.anthropic,
            sessionmaker=runtime.sessionmaker,
            notebook_rate_limiter=runtime.notebook_rate_limiter,
            billing_config=runtime.billing_config,
            deployment_default=runtime.deployment_default,
            resolver_cache=runtime.resolver_cache,
            turn_deps=deps_for(runtime.anthropic, runtime.sessionmaker),
        )
    if not isinstance(runtime.turn_deps, TurnDeps):
        runtime = replace(runtime, turn_deps=deps_for(runtime.anthropic, runtime.sessionmaker))
    bot = DaimonBot(runtime=runtime, intents=intents)

    async def membership(tenant_id, guild_id, user_id):  # type: ignore[no-untyped-def]
        # Ordinary bot tests isolate orchestration from platform lookups;
        # resolver/event tests exercise revocation and errors explicitly.
        return Membership(is_member=True)

    bot.membership_resolver = CachedMembershipResolver(membership)
    bot.runtime = replace(
        bot.runtime,
        turn_deps=replace(bot.runtime.turn_deps, membership_resolver=bot.membership_resolver),
    )
    if not isinstance(runtime.settings.turn_queue, TurnQueueSettings):
        # Mocked settings: the default queue, no process-wide cap. Tests that
        # need a cap or a bound set it on `bot.turn_queue`.
        bot.turn_queue = TurnQueue.from_settings(TurnQueueSettings(), platform="discord")
    bot._connection.user = MagicMock(spec=discord.ClientUser)  # pyright: ignore[reportPrivateUsage]
    bot._connection.user.id = 999  # pyright: ignore[reportPrivateUsage]
    bot._connection.user.mentioned_in = MagicMock(return_value=True)  # pyright: ignore[reportPrivateUsage]
    return bot
