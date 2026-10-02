"""The channel budget notice: who gets it, once per window, and that it never breaks a refusal."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from daimon.core.channel_budget_notice import (
    BudgetNotice,
    claim_budget_notice,
    notify_budget_exhausted,
)
from daimon.core.config import DirectMessagePolicy
from daimon.core.stores import accounts, channel_admins, channel_budgets
from daimon.core.stores.domain import Role, TenantRow
from daimon.testing.factories import (
    make_account,
    make_channel_budget,
    make_ledger_entry,
    make_platform_principal,
    make_tenant,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_NOW = datetime(2026, 7, 15, 12, tzinfo=UTC)


async def _member(
    session: AsyncSession,
    tenant: TenantRow,
    user_id: str,
    *,
    admin: bool = False,
    roles: Sequence[str] = (),
) -> None:
    account = await make_account(session, tenant=tenant)
    await make_platform_principal(
        session, platform="discord", external_id=user_id, tenant=tenant, account=account
    )
    if admin:
        await accounts.set_role(session, account.id, Role.ADMIN)
    if roles:
        await accounts.set_platform_role_ids(session, account.id, roles)


async def _spent_channel(session: AsyncSession, *, limit: str = "1") -> TenantRow:
    tenant = await make_tenant(session)
    await make_ledger_entry(
        session, tenant=tenant, delta_usd=Decimal("-1"), channel_id="chan-1", occurred_at=_NOW
    )
    await make_channel_budget(session, tenant=tenant, limit_usd=Decimal(limit))
    await _member(session, tenant, "u-admin", admin=True)
    await _member(session, tenant, "u-role", roles=["r-1"])
    await _member(session, tenant, "u-plain", roles=["r-2"])
    return tenant


async def _claim(session: AsyncSession, tenant: TenantRow, *, now: datetime = _NOW):
    return await claim_budget_notice(
        session, tenant_id=tenant.id, platform="discord", channel_id="chan-1", now=now
    )


async def test_the_notice_goes_to_the_channel_admins_once_per_window(
    db_session: AsyncSession,
) -> None:
    tenant = await _spent_channel(db_session, limit="0")
    await channel_admins.set_channel_admins(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        channel_id="chan-1",
        role_ids=["r-1"],
        user_ids=["u-never-spoke"],
        actor_account_id=None,
    )

    notice = await _claim(db_session, tenant)
    assert notice is not None
    assert notice.recipient_ids == ("u-never-spoke", "u-role"), (
        "granted users and members holding a granted role, never the server admins"
    )
    assert notice.workspace_id == tenant.external_id
    assert notice.text("<#chan-1>") == (
        "<#chan-1>'s budget is used up: $1.00 of $0.00 (monthly). "
        "New turns there are refused until the month ends or an admin raises it."
    )
    assert await _claim(db_session, tenant) is None, "one notice per window"
    assert await _claim(db_session, tenant, now=datetime(2026, 8, 1, tzinfo=UTC)) is not None, (
        "a monthly budget's next month is a new window"
    )


async def test_a_channel_without_admins_tells_the_server_admins(db_session: AsyncSession) -> None:
    tenant = await _spent_channel(db_session)
    notice = await _claim(db_session, tenant)
    assert notice is not None
    assert notice.recipient_ids == ("u-admin",)


async def test_no_notice_while_the_budget_has_room(db_session: AsyncSession) -> None:
    tenant = await _spent_channel(db_session, limit="5")
    assert await _claim(db_session, tenant) is None


async def test_setting_or_raising_the_budget_rearms_the_notice(db_session: AsyncSession) -> None:
    tenant = await _spent_channel(db_session)
    assert await _claim(db_session, tenant) is not None
    await make_channel_budget(db_session, tenant=tenant, limit_usd=Decimal("1"))
    assert await _claim(db_session, tenant) is not None, "a budget set again is a new window"
    await channel_budgets.raise_channel_budget(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        channel_id="chan-1",
        amount_usd=Decimal("0"),
    )
    assert await _claim(db_session, tenant) is not None, "so is a raised one"


async def test_a_failing_notifier_never_raises_and_no_notifier_claims_nothing(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _spent_channel(db_session)
    await db_session.commit()
    args = {"tenant_id": tenant.id, "platform": "discord", "channel_id": "chan-1", "now": _NOW}

    await notify_budget_exhausted(sessionmaker=db_session_factory, notifier=None, **args)
    sent: list[BudgetNotice] = []

    async def failing(notice: BudgetNotice) -> None:
        sent.append(notice)
        raise RuntimeError("platform down")

    await notify_budget_exhausted(sessionmaker=db_session_factory, notifier=failing, **args)
    await notify_budget_exhausted(sessionmaker=db_session_factory, notifier=failing, **args)
    assert [n.recipient_ids for n in sent] == [("u-admin",)], (
        "the first notifier call is this window's, and its failure is swallowed"
    )


def test_the_dm_policy_filters_recipients() -> None:
    notice = BudgetNotice(
        tenant_id=uuid.uuid4(),
        workspace_id="g",
        platform="discord",
        channel_id="c",
        recipient_ids=("a", "b"),
        budget_line="",
        monthly=False,
    )
    allow_b = DirectMessagePolicy(mode="allowlist", recipient_ids=["b"])
    assert notice.allowed_recipients(allow_b) == ("b",)
    assert notice.allowed_recipients(DirectMessagePolicy(mode="disabled")) == ()
    assert "until an admin raises it." in notice.text("c")
