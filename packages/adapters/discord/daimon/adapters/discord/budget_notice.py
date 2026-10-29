"""Delivers the channel budget notice (`daimon.core.channel_budget_notice`) by member DM."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import timedelta
from typing import cast

import structlog
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.channel_budget_notice import BudgetNotice, BudgetNotifier
from daimon.core.config import DirectMessagePolicy
from daimon.core.stores.watches import NoticeAttempt, SentNotice
from daimon.core.turn.deps import TurnDeps

import discord

log = structlog.get_logger()

OpenDm = Callable[[int, int], Awaitable[discord.abc.Messageable]]


def discord_budget_notifier(
    runtime: DiscordRuntime, open_dm: OpenDm, is_closed: Callable[[], bool]
) -> BudgetNotifier:
    """DM each recipient the DM policy allows who is still a human member of the guild.

    Once the client is closed nothing more can be sent, so the rest count as
    undelivered rather than raising: with none delivered the window is freed.
    """

    async def send(notice: BudgetNotice) -> int:
        policies = runtime.settings.direct_message_policies
        text = notice.text(f"<#{notice.channel_id}>")
        delivered = 0
        for user_id in notice.allowed_recipients(
            policies.get(notice.tenant_id, DirectMessagePolicy())
        ):
            if is_closed():
                log.info("channel_budget.notice_undelivered", err_type="ClientClosed")
                break
            try:
                landed = await send_member_notice(
                    runtime,
                    open_dm,
                    is_closed,
                    tenant_id=notice.tenant_id,
                    workspace_id=notice.workspace_id,
                    recipient_id=user_id,
                    text=text,
                )
                delivered += int(landed)
            except (discord.HTTPException, LookupError, ValueError) as exc:
                log.info("channel_budget.notice_undelivered", err_type=type(exc).__name__)
            except RuntimeError as exc:
                # aiohttp's "Session is closed": the client closed mid-notice, nothing went out.
                if not is_closed():
                    raise
                log.info("channel_budget.notice_undelivered", err_type=type(exc).__name__)
                break
        return delivered

    return send


def with_budget_notifier(
    runtime: DiscordRuntime,
    open_dm: OpenDm,
    is_closed: Callable[[], bool],
    client: discord.Client | None = None,
) -> DiscordRuntime:
    """`runtime` whose turns send the notice; a stand-in test runtime comes back unchanged.

    With `client`, a recipient matched by a stored role must still hold it.
    """
    real = cast(object, runtime)
    deps = cast(object, runtime.turn_deps)
    if not isinstance(real, DiscordRuntime) or not isinstance(deps, TurnDeps):
        return runtime
    return replace(
        real,
        turn_deps=replace(
            deps,
            budget_notifier=discord_budget_notifier(real, open_dm, is_closed),
        ),
    )


async def send_member_notice(
    runtime: DiscordRuntime,
    open_dm: OpenDm,
    is_closed: Callable[[], bool],
    *,
    tenant_id: uuid.UUID,
    workspace_id: str,
    recipient_id: str,
    text: str,
) -> bool:
    """Existing private member-notice transport, also used for watch migration DMs."""
    policy = runtime.settings.direct_message_policies.get(tenant_id, DirectMessagePolicy())
    if is_closed() or not policy.allows(recipient_id):
        return False
    try:
        dm = await open_dm(int(workspace_id), int(recipient_id))
        await dm.send(text, allowed_mentions=discord.AllowedMentions.none())
        return True
    except (discord.HTTPException, LookupError, ValueError):
        return False


# Discord's clock and ours can disagree; look a little before the earlier attempt.
_HISTORY_SKEW = timedelta(minutes=1)


async def send_watch_notice(
    runtime: DiscordRuntime,
    open_dm: OpenDm,
    is_closed: Callable[[], bool],
    *,
    bot_user_id: int | None,
    tenant_id: uuid.UUID,
    workspace_id: str,
    recipient_id: str,
    text: str,
    attempt: NoticeAttempt,
) -> SentNotice | None:
    """The member-notice transport for watch owner notices, sent at most once per notice.

    The nonce makes Discord return the first message for a concurrent resend
    within its dedup window. When an earlier attempt never recorded its
    outcome, the DM is read first and a matching message from the bot counts
    as sent. A failed read sends nothing, so the claim retries later.
    """
    policy = runtime.settings.direct_message_policies.get(tenant_id, DirectMessagePolicy())
    if is_closed() or not policy.allows(recipient_id):
        return None
    try:
        dm = await open_dm(int(workspace_id), int(recipient_id))
        if attempt.since is not None:
            if bot_user_id is None:
                return None
            async for earlier in dm.history(after=attempt.since - _HISTORY_SKEW, limit=50):
                if earlier.author.id == bot_user_id and earlier.content == text:
                    return SentNotice(str(earlier.channel.id), str(earlier.id))
        message = await dm.send(
            text, allowed_mentions=discord.AllowedMentions.none(), nonce=attempt.nonce
        )
        return SentNotice(str(message.channel.id), str(message.id))
    except (discord.HTTPException, LookupError, ValueError):
        return None
