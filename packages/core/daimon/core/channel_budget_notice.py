"""Tell people about a channel budget, once per budget window.

When chat admission refuses a turn for the channel budget, the first refusal
of each window claims the budget notice and DMs the server admins. The claim
commits before any DM is sent. A tenant opts out with `budget_notices`.

An alert-only budget never refuses; instead admission claims an alert for each
full multiple of the limit (once each per window) and posts it to the budget's
alert channel, else the workspace's support destination.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final

import structlog
from daimon.core.channel_budget import get_channel_budget_status
from daimon.core.config import DirectMessagePolicy, Settings
from daimon.core.native_admins import ServerAdminIds
from daimon.core.stores.accounts import list_platform_user_ids
from daimon.core.stores.channel_budgets import (
    claim_budget_alert,
    claim_exhausted_notice,
    release_budget_alert,
    release_exhausted_notice,
)
from daimon.core.stores.domain import ChannelBudgetRow, TenantRow
from daimon.core.stores.platform_names import get_channel_names
from daimon.core.stores.tenants import get_tenant
from daimon.core.support_routing import SupportDestination, support_destination
from daimon.core.windows import window_label
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

MAX_RECIPIENTS: Final = 10
NOTICE_TIMEOUT_S: Final = 15.0

RecipientCheck = Callable[[str], Awaitable[bool]]
"""Whether a DM grant admits this recipient (`dm_access.sender_admits`)."""


@dataclass(frozen=True)
class BudgetNotice:
    tenant_id: uuid.UUID
    workspace_id: str
    """The tenant's platform id: a Discord guild, a Slack team, a Teams tenant."""
    platform: str
    channel_id: str
    recipient_ids: tuple[str, ...]
    limit_line: str
    """The budget's limit in dollars, e.g. `$5.00`."""
    monthly: bool
    budget_id: uuid.UUID
    window_key: str
    """The `notice_key` claimed for this notice, released when no DM lands."""
    channel_name: str | None = None
    """The channel's name as last seen; None when never seen."""

    @property
    def channel_label(self) -> str:
        """The channel as a reader sees it where the platform cannot mention it."""
        return f"#{self.channel_name}" if self.channel_name else "This channel"

    async def allowed_recipients(
        self, policy: DirectMessagePolicy, admits: RecipientCheck
    ) -> tuple[str, ...]:
        """Recipients the env policy and `admits` (the default agent's DM grant) both allow."""
        return tuple([r for r in self.recipient_ids if policy.allows(r) and await admits(r)])

    def text(self, channel_ref: str) -> str:
        """Two lines with a blank line between; only a monthly budget offers the wait."""
        if self.monthly:
            return (
                f"{channel_ref} has used its {self.limit_line} monthly budget.\n\n"
                "Raise the budget to resume now, or wait until next month."
            )
        return (
            f"{channel_ref} has used its {self.limit_line} budget.\n\n"
            "Raise the budget to resume now."
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
    names = await get_channel_names(
        session, tenant_id=tenant_id, platform=platform, channel_ids=(channel_id,)
    )
    return BudgetNotice(
        tenant_id=tenant_id,
        workspace_id=tenant.external_id,
        platform=platform,
        channel_id=channel_id,
        recipient_ids=(),
        limit_line=f"${budget.limit_usd:,.2f}",
        monthly=budget.window == "monthly",
        budget_id=budget.id,
        window_key=key,
        channel_name=names.get(channel_id),
    )


async def notice_recipients(
    sessionmaker: async_sessionmaker[AsyncSession],
    notice: BudgetNotice,
    server_admins: ServerAdminIds | None = None,
) -> tuple[str, ...]:
    """The server admins; at most `MAX_RECIPIENTS`."""
    if server_admins is not None:
        return tuple(sorted(await server_admins())[:MAX_RECIPIENTS])
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
    server_admins: ServerAdminIds | None,
) -> None:
    notice: BudgetNotice | None = None
    sending = returned = False
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
            notice = replace(
                notice,
                recipient_ids=await notice_recipients(sessionmaker, notice, server_admins),
            )
            if notice.recipient_ids:
                sending = True
                delivered = await notifier(notice)
                returned = True
    finally:
        # Freed when nobody was sent anything, so a later refusal tries again.
        # Kept when the notifier timed out or raised: some DMs may have landed,
        # and freeing it would send them again on every refusal.
        if notice is not None and not delivered and (returned or not sending):
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
    server_admins: ServerAdminIds | None = None,
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
            server_admins=server_admins,
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
    server_admins: ServerAdminIds | None = None,
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
            server_admins=server_admins,
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


@dataclass(frozen=True)
class BudgetAlert:
    """One alert-only budget's alert: where it posts, and what it says."""

    tenant_id: uuid.UUID
    workspace_id: str
    """The tenant's platform id: a Discord guild, a Slack team, a Teams tenant."""
    platform: str
    channel_id: str
    """The budgeted channel."""
    destination_id: str
    """The channel the alert posts to."""
    slack_team_id: str | None
    """The Slack team the destination lives in; None on other platforms."""
    spent_line: str
    limit_line: str
    window_text: str
    level: int
    """Which full multiple of the limit this alert is for: 1 at the limit, 2 at twice it."""
    budget_id: uuid.UUID
    window_key: str
    prior_key: str | None
    prior_level: int
    """The claim before this one, restored when the post does not land."""
    channel_name: str | None = None

    @property
    def channel_label(self) -> str:
        """The budgeted channel where the platform cannot mention it."""
        return f"#{self.channel_name}" if self.channel_name else self.channel_id

    def text(self, channel_ref: str) -> str:
        reached = "reached" if self.level == 1 else f"reached {self.level}x"
        return (
            f"{channel_ref} has {reached} its {self.limit_line} budget ({self.window_text}): "
            f"{self.spent_line} spent.\n\n"
            "This budget only alerts; replies continue."
        )


BudgetAlerter = Callable[[BudgetAlert], Awaitable[bool]]
"""Posts the alert and says whether it landed."""


async def _alert_destination(
    session: AsyncSession, budget: ChannelBudgetRow, tenant: TenantRow, settings: Settings
) -> SupportDestination | None:
    """The budget's own alert channel, else where the workspace's support requests go."""
    if budget.alert_channel_id is not None:
        return SupportDestination(
            platform=budget.platform,
            channel_id=budget.alert_channel_id,
            own=True,
            slack_team_id=tenant.external_id if budget.platform == "slack" else None,
        )
    return await support_destination(
        session, tenant_id=tenant.id, platform=budget.platform, settings=settings
    )


async def claim_budget_alert_post(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    now: datetime,
    settings: Settings,
) -> BudgetAlert | None:
    """Claim the alert for the multiple of the limit now spent; None when there is none
    to send: no alert-only budget, below its limit, already alerted, or nowhere to post."""
    status = await get_channel_budget_status(
        session, tenant_id=tenant_id, platform=platform, channel_id=channel_id, now=now
    )
    level = 0 if status is None else status.alert_level
    if status is None or level == 0:
        return None
    budget = status.budget
    key = notice_key(budget, now=now)
    prior_level = budget.alert_level if budget.alert_window_key == key else 0
    if prior_level >= level:
        return None
    tenant = await get_tenant(session, tenant_id)
    if tenant is None:
        return None
    destination = await _alert_destination(session, budget, tenant, settings)
    if destination is None:
        return None
    if not await claim_budget_alert(session, budget_id=budget.id, key=key, level=level):
        return None
    names = await get_channel_names(
        session, tenant_id=tenant_id, platform=platform, channel_ids=(channel_id,)
    )
    return BudgetAlert(
        tenant_id=tenant_id,
        workspace_id=tenant.external_id,
        platform=platform,
        channel_id=channel_id,
        destination_id=destination.channel_id,
        slack_team_id=destination.slack_team_id
        or (tenant.external_id if platform == "slack" else None),
        spent_line=f"${status.spent_usd:,.2f}",
        limit_line=f"${budget.limit_usd:,.2f}",
        window_text=window_label(budget),
        level=level,
        budget_id=budget.id,
        window_key=key,
        prior_key=budget.alert_window_key,
        prior_level=budget.alert_level,
        channel_name=names.get(channel_id),
    )


async def _claim_and_post_alert(
    sessionmaker: async_sessionmaker[AsyncSession],
    alerter: BudgetAlerter,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    now: datetime,
    settings: Settings,
) -> None:
    alert: BudgetAlert | None = None
    landed = False
    try:
        async with asyncio.timeout(NOTICE_TIMEOUT_S):
            # Committed before the post, so a slow platform call holds no lock.
            async with sessionmaker() as session, session.begin():
                alert = await claim_budget_alert_post(
                    session,
                    tenant_id=tenant_id,
                    platform=platform,
                    channel_id=channel_id,
                    now=now,
                    settings=settings,
                )
            if alert is None:
                return
            landed = await alerter(alert)
            if landed:
                log.info("channel_budget.alert_sent", tenant_id=str(tenant_id), level=alert.level)
    finally:
        # Freed when nothing posted, so the next turn tries again.
        if alert is not None and not landed:
            async with sessionmaker() as session, session.begin():
                await release_budget_alert(
                    session,
                    budget_id=alert.budget_id,
                    key=alert.window_key,
                    level=alert.level,
                    prior_key=alert.prior_key,
                    prior_level=alert.prior_level,
                )


async def notify_budget_alert(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    alerter: BudgetAlerter,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    now: datetime,
    settings: Settings,
) -> None:
    """Claim and post this channel's due alert; every failure is logged, never raised."""
    try:
        await _claim_and_post_alert(
            sessionmaker,
            alerter,
            tenant_id=tenant_id,
            platform=platform,
            channel_id=channel_id,
            now=now,
            settings=settings,
        )
    except Exception as exc:  # background alert: a failure is logged, never raised
        log.warning(
            "channel_budget.alert_failed", tenant_id=str(tenant_id), err_type=type(exc).__name__
        )


def spawn_budget_alert(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    alerter: BudgetAlerter | None,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str | None,
    now: datetime,
    settings: Settings | None,
) -> None:
    """Check for a due alert off the turn's path: an alert never delays or fails a turn."""
    if alerter is None or channel_id is None or settings is None:
        return
    task = asyncio.create_task(
        notify_budget_alert(
            sessionmaker=sessionmaker,
            alerter=alerter,
            tenant_id=tenant_id,
            platform=platform,
            channel_id=channel_id,
            now=now,
            settings=settings,
        ),
        name="channel_budget.alert",
    )
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
