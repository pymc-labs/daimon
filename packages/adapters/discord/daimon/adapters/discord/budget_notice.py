"""Delivers the channel budget notice (`daimon.core.channel_budget_notice`) by member DM."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import cast

import structlog
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.channel_budget_notice import BudgetNotice, BudgetNotifier
from daimon.core.config import DirectMessagePolicy
from daimon.core.turn.deps import TurnDeps

import discord

log = structlog.get_logger()

OpenDm = Callable[[int, int], Awaitable[discord.abc.Messageable]]


def discord_budget_notifier(runtime: DiscordRuntime, open_dm: OpenDm) -> BudgetNotifier:
    """DM each recipient the DM policy allows who is still a human member of the guild."""

    async def send(notice: BudgetNotice) -> int:
        policies = runtime.settings.direct_message_policies
        text = notice.text(f"<#{notice.channel_id}>")
        delivered = 0
        for user_id in notice.allowed_recipients(
            policies.get(notice.tenant_id, DirectMessagePolicy())
        ):
            try:
                dm = await open_dm(int(notice.workspace_id), int(user_id))
                await dm.send(text, allowed_mentions=discord.AllowedMentions.none())
                delivered += 1
            except (discord.HTTPException, LookupError, ValueError) as exc:
                log.info("channel_budget.notice_undelivered", err_type=type(exc).__name__)
        return delivered

    return send


def with_budget_notifier(runtime: DiscordRuntime, open_dm: OpenDm) -> DiscordRuntime:
    """`runtime` whose turns send the notice; a stand-in test runtime comes back unchanged."""
    real = cast(object, runtime)
    deps = cast(object, runtime.turn_deps)
    if not isinstance(real, DiscordRuntime) or not isinstance(deps, TurnDeps):
        return runtime
    return replace(
        real, turn_deps=replace(deps, budget_notifier=discord_budget_notifier(real, open_dm))
    )
