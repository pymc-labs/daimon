"""Tell the server admins, once per budget window, that its budget is used up.

When chat admission refuses a turn for the channel budget, the first refusal
of each window claims the budget notice and DMs the server admins. The claim
commits before any DM is sent. A tenant opts out with `budget_notices`.
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
from daimon.core.config import DirectMessagePolicy
from daimon.core.native_admins import ServerAdminIds
from daimon.core.stores.accounts import list_platform_user_ids
from daimon.core.stores.channel_budgets import claim_exhausted_notice, release_exhausted_notice
from daimon.core.stores.domain import ChannelBudgetRow
from daimon.core.stores.platform_names import get_channel_names
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

    def allowed_recipients(self, policy: DirectMessagePolicy) -> tuple[str, ...]:
        return tuple(r for r in self.recipient_ids if policy.allows(r))

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
