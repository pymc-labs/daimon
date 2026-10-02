"""Tell a channel's admins, once per budget window, that its budget is used up.

When chat admission refuses a turn for the channel budget, the first refusal
of each window claims the budget's `exhausted_notice_key` and DMs the
channel's admins (its grant's users, plus members whose stored roles match a
granted role), or the server admins when it has none. The claim commits
before recipients are resolved or any DM is sent. Setting or raising the
budget clears the claim, and so does a notice nobody was sent, so a later
refusal tries again; one cut short mid-send keeps it. Delivery is the
adapter's `BudgetNotifier`, held to the tenant's DM policy; it runs in the
background under one timeout and never changes or delays the refusal. A
tenant opts out with the `budget_notices` setting.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final

import structlog
from daimon.core.channel_admins import GroupMembers, GroupMembersFor, channel_admin_user_ids
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

_PENDING: set[asyncio.Task[None]] = set()


def notice_key(budget: ChannelBudgetRow, *, now: datetime) -> str:
    """The window a notice covers: the UTC month for a monthly budget, else the whole budget."""
    return f"{now:%Y-%m}" if budget.window == "monthly" else "window"


async def claim_budget_notice(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    now: datetime,
) -> BudgetNotice | None:
    """Claim this window's notice; None when the budget is not exhausted or it went out.

    The notice has no recipients yet: they are resolved after the claim
    commits (`notice_recipients`), since a live group lookup may be slow.
    """
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
    return BudgetNotice(
        tenant_id=tenant_id,
        workspace_id=tenant.external_id,
        platform=platform,
        channel_id=channel_id,
        recipient_ids=(),
        budget_line=describe_budget(status),
        monthly=budget.window == "monthly",
        budget_id=budget.id,
        window_key=key,
    )


async def notice_recipients(
    sessionmaker: async_sessionmaker[AsyncSession],
    notice: BudgetNotice,
    members: GroupMembers | None,
) -> tuple[str, ...]:
    """The channel's admins, else the server admins; at most `MAX_RECIPIENTS`.

    `members` re-checks a recipient matched by a stored Slack group or Teams
    team, with no DB session open; without it such a match reaches nobody.
    """
    listed = await channel_admin_user_ids(
        sessionmaker,
        tenant_id=notice.tenant_id,
        platform=notice.platform,
        channel_id=notice.channel_id,
        limit=MAX_RECIPIENTS,
        members=members,
    )
    if listed:
        return tuple(listed)
    async with sessionmaker() as session:
        admins = await list_platform_user_ids(
            session,
            tenant_id=notice.tenant_id,
            platform=notice.platform,
            limit=MAX_RECIPIENTS,
            admins=True,
        )
    return tuple(admins)


async def _claim_and_send(
    sessionmaker: async_sessionmaker[AsyncSession],
    notifier: BudgetNotifier,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    now: datetime,
    group_members: GroupMembersFor | None,
) -> None:
    notice: BudgetNotice | None = None
    delivered = 0
    try:
        # One bound for everything: the claim, the live group lookups and the DMs.
        async with asyncio.timeout(NOTICE_TIMEOUT_S):
            # Committed before any platform call, so a slow lookup or DM never
            # holds the budget row's lock or a pooled connection.
            async with sessionmaker() as session, session.begin():
                notice = await claim_budget_notice(
                    session, tenant_id=tenant_id, platform=platform, channel_id=channel_id, now=now
                )
            if notice is None:
                return
            members = group_members(platform, notice.workspace_id) if group_members else None
            notice = replace(
                notice, recipient_ids=await notice_recipients(sessionmaker, notice, members)
            )
            if notice.recipient_ids:
                delivered = await notifier(notice)
    finally:
        # Otherwise the window stays claimed and no admin hears of it until
        # the budget is raised or reset.
        if notice is not None and not delivered:
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
    group_members: GroupMembersFor | None = None,
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
            group_members=group_members,
        )
    except Exception as exc:  # background notice: a failure is logged, never raised
        log.warning(
            "channel_budget.notice_failed", tenant_id=str(tenant_id), err_type=type(exc).__name__
        )


def spawn_budget_notice(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    notifier: BudgetNotifier | None,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str | None,
    now: datetime,
    group_members: GroupMembersFor | None = None,
) -> None:
    """Send the notice off the refusal path, so a slow DM API never holds the reply."""
    if notifier is None or channel_id is None:
        return
    task = asyncio.create_task(
        notify_budget_exhausted(
            sessionmaker=sessionmaker,
            notifier=notifier,
            tenant_id=tenant_id,
            platform=platform,
            channel_id=channel_id,
            now=now,
            group_members=group_members,
        ),
        name="channel_budget.notice",
    )
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


async def drain_budget_notices() -> None:
    """Runtime shutdown/test barrier, never awaited by a turn."""
    tasks = [task for task in _PENDING if task.get_loop() is asyncio.get_running_loop()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
