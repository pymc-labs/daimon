"""Tell a channel's admins, once per budget window, that its budget is used up.

When chat admission refuses a turn for the channel budget, the first refusal
of each window claims the budget's `exhausted_notice_key` and DMs the
channel's admins (its grant's users, plus members whose stored roles match a
granted role), or the server admins when it has none. Setting or raising the
budget clears the claim, and so does a notice no admin received, so a later
refusal tries again. Delivery is the adapter's `BudgetNotifier`, held to the
tenant's DM policy; it runs under a timeout and never changes the refusal. A
tenant opts out with the `budget_notices` setting.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import structlog
from daimon.core.channel_admins import channel_admin_user_ids
from daimon.core.channel_budget import describe_budget, get_channel_budget_status
from daimon.core.config import DirectMessagePolicy
from daimon.core.stores.accounts import list_platform_user_ids
from daimon.core.stores.channel_budgets import claim_exhausted_notice, release_exhausted_notice
from daimon.core.stores.domain import ChannelBudgetRow
from daimon.core.stores.tenants import get_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

MAX_RECIPIENTS: Final = 10
NOTICE_TIMEOUT_S: Final = 15.0


@dataclass(frozen=True)
class BudgetNotice:
    tenant_id: uuid.UUID
    workspace_id: str
    """The tenant's platform id: a Discord guild, a Slack team, a Teams tenant."""
    platform: str
    channel_id: str
    recipient_ids: tuple[str, ...]
    budget_line: str
    monthly: bool
    budget_id: uuid.UUID
    window_key: str
    """The `notice_key` claimed for this notice, released when no DM lands."""

    def allowed_recipients(self, policy: DirectMessagePolicy) -> tuple[str, ...]:
        return tuple(r for r in self.recipient_ids if policy.allows(r))

    def text(self, channel_ref: str) -> str:
        until = (
            "the month ends or a server admin raises it"
            if self.monthly
            else "a server admin raises it"
        )
        return (
            f"{channel_ref}'s budget is used up: {self.budget_line}. "
            f"New turns there are refused until {until}."
        )


BudgetNotifier = Callable[[BudgetNotice], Awaitable[int]]
"""Sends the notice and returns how many DMs landed."""


def notice_key(budget: ChannelBudgetRow, *, now: datetime) -> str:
    """The window a notice covers: the UTC month for a monthly budget, else the whole budget."""
    return f"{now:%Y-%m}" if budget.window == "monthly" else "window"


async def _recipients(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, channel_id: str
) -> list[str]:
    listed = await channel_admin_user_ids(
        session, tenant_id=tenant_id, platform=platform, channel_id=channel_id, limit=MAX_RECIPIENTS
    )
    if listed:
        return listed
    return await list_platform_user_ids(
        session, tenant_id=tenant_id, platform=platform, limit=MAX_RECIPIENTS, admins=True
    )


async def claim_budget_notice(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, channel_id: str, now: datetime
) -> BudgetNotice | None:
    """The notice to send, or None when the budget is not exhausted or this window's went out."""
    status = await get_channel_budget_status(
        session, tenant_id=tenant_id, platform=platform, channel_id=channel_id, now=now
    )
    if status is None or not status.is_exceeded:
        return None
    budget = status.budget
    key = notice_key(budget, now=now)
    if not await claim_exhausted_notice(session, budget_id=budget.id, key=key):
        return None
    tenant = await get_tenant(session, tenant_id)
    if tenant is None:
        return None
    recipients = await _recipients(
        session, tenant_id=tenant_id, platform=platform, channel_id=channel_id
    )
    return BudgetNotice(
        tenant_id=tenant_id,
        workspace_id=tenant.external_id,
        platform=platform,
        channel_id=channel_id,
        recipient_ids=tuple(recipients),
        budget_line=describe_budget(status),
        monthly=budget.window == "monthly",
        budget_id=budget.id,
        window_key=key,
    )


async def _claim_and_send(
    sessionmaker: async_sessionmaker[AsyncSession],
    notifier: BudgetNotifier,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    now: datetime,
) -> None:
    async with sessionmaker() as session, session.begin():
        notice = await claim_budget_notice(
            session, tenant_id=tenant_id, platform=platform, channel_id=channel_id, now=now
        )
    if notice is None:
        return
    delivered = 0
    try:
        if notice.recipient_ids:
            async with asyncio.timeout(NOTICE_TIMEOUT_S):
                delivered = await notifier(notice)
    finally:
        if not delivered:
            # Otherwise the window stays claimed and no admin hears of it until
            # the budget is raised or reset.
            async with sessionmaker() as session, session.begin():
                await release_exhausted_notice(
                    session, budget_id=notice.budget_id, key=notice.window_key
                )
    if delivered:
        log.info("channel_budget.notice_sent", tenant_id=str(tenant_id), recipients=delivered)


async def notify_budget_exhausted(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    notifier: BudgetNotifier | None,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str | None,
    now: datetime,
) -> None:
    """Claim and send this window's notice; every failure is logged, never raised."""
    if notifier is None or channel_id is None:
        return
    try:
        await _claim_and_send(
            sessionmaker,
            notifier,
            tenant_id=tenant_id,
            platform=platform,
            channel_id=channel_id,
            now=now,
        )
    except Exception as exc:  # a notice must never turn a refusal into a crash
        log.warning(
            "channel_budget.notice_failed", tenant_id=str(tenant_id), err_type=type(exc).__name__
        )
