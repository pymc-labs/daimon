"""The channel budget notice: who gets it, once per window, and that it never breaks a refusal."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from daimon.core import channel_budget_notice
from daimon.core.channel_admins import GroupMembers
from daimon.core.channel_budget_notice import (
    BudgetNotice,
    claim_budget_notice,
    notice_recipients,
    notify_budget_exhausted,
)
from daimon.core.config import DirectMessagePolicy
from daimon.core.stores import accounts, channel_admins, channel_budgets
from daimon.core.stores.domain import Platform, Role, TenantRow
from daimon.testing.factories import (
    make_account,
    make_channel_budget,
    make_ledger_entry,
    make_platform_principal,
    make_tenant,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

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
        session, platform=tenant.platform, external_id=user_id, tenant=tenant, account=account
    )
    if admin:
        await accounts.set_role(session, account.id, Role.ADMIN)
    if roles:
        await accounts.set_platform_role_ids(session, account.id, roles)


async def _spent_channel(
    session: AsyncSession, *, limit: str = "1", platform: Platform = "discord"
) -> TenantRow:
    tenant = await make_tenant(session, platform=platform)
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
        session, tenant_id=tenant.id, platform=tenant.platform, channel_id="chan-1", now=now
    )


async def _recipients(session: AsyncSession, notice: BudgetNotice | None) -> tuple[str, ...]:
    assert notice is not None, "the window was claimed"
    return await notice_recipients(async_sessionmaker(bind=session.bind), notice, None)


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
    assert await _recipients(db_session, notice) == ("u-never-spoke", "u-role"), (
        "granted users and members holding a granted role, never the server admins"
    )
    assert notice.workspace_id == tenant.external_id
    assert notice.text("<#chan-1>") == (
        "<#chan-1>'s budget is used up: $1.00 of $0.00 (monthly). "
        "New turns there are refused until the month ends or a server admin raises it."
    )
    assert await _claim(db_session, tenant) is None, "one notice per window"
    assert await _claim(db_session, tenant, now=datetime(2026, 8, 1, tzinfo=UTC)) is not None, (
        "a monthly budget's next month is a new window"
    )


async def test_a_channel_without_admins_tells_the_server_admins(db_session: AsyncSession) -> None:
    tenant = await _spent_channel(db_session)
    assert await _recipients(db_session, await _claim(db_session, tenant)) == ("u-admin",)


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

    async def landed(notice: BudgetNotice) -> int:
        sent.append(notice)
        return 1

    await notify_budget_exhausted(sessionmaker=db_session_factory, notifier=landed, **args)
    await notify_budget_exhausted(sessionmaker=db_session_factory, notifier=landed, **args)
    assert [n.recipient_ids for n in sent] == [("u-admin",)], (
        "no notifier leaves the window free; a delivered notice holds it"
    )


@pytest.mark.parametrize("cut_short", ["raises", "times_out"])
async def test_a_notice_cut_short_mid_send_keeps_the_window(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    cut_short: str,
) -> None:
    """Some DMs may have landed before the error, so a retry would send them again."""
    monkeypatch.setattr(channel_budget_notice, "NOTICE_TIMEOUT_S", 0.2)
    tenant = await _spent_channel(db_session)
    await db_session.commit()
    args = {"tenant_id": tenant.id, "platform": "discord", "channel_id": "chan-1", "now": _NOW}
    calls: list[BudgetNotice] = []

    async def partial(notice: BudgetNotice) -> int:
        calls.append(notice)
        if cut_short == "raises":
            raise RuntimeError("connection reset after the first DM")
        await asyncio.sleep(30)
        return 1

    await notify_budget_exhausted(sessionmaker=db_session_factory, notifier=partial, **args)
    await notify_budget_exhausted(sessionmaker=db_session_factory, notifier=partial, **args)

    assert len(calls) == 1, "the next refusal does not send the notice again"


async def test_a_notice_no_admin_received_frees_the_window(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _spent_channel(db_session)
    await db_session.commit()
    args = {"tenant_id": tenant.id, "platform": "discord", "channel_id": "chan-1", "now": _NOW}
    calls: list[BudgetNotice] = []

    async def none_landed(notice: BudgetNotice) -> int:
        calls.append(notice)
        return 0

    await notify_budget_exhausted(sessionmaker=db_session_factory, notifier=none_landed, **args)
    async with db_session_factory() as session:
        assert await _claim(session, tenant) is not None, (
            "a notice whose DMs all failed leaves the window unclaimed"
        )
    assert len(calls) == 1, "the notifier ran once"


def test_the_dm_policy_filters_recipients() -> None:
    notice = BudgetNotice(
        tenant_id=uuid.uuid4(),
        workspace_id="g",
        platform="discord",
        channel_id="c",
        recipient_ids=("a", "b"),
        budget_line="",
        monthly=False,
        budget_id=uuid.uuid4(),
        window_key="window",
    )
    allow_b = DirectMessagePolicy(mode="allowlist", recipient_ids=["b"])
    assert notice.allowed_recipients(allow_b) == ("b",)
    assert notice.allowed_recipients(DirectMessagePolicy(mode="disabled")) == ()
    assert "until a server admin raises it." in notice.text("c")


async def _slack_channel_with_group_admin(session: AsyncSession) -> TenantRow:
    tenant = await _spent_channel(session, platform="slack")
    await _member(session, tenant, "U_GROUP", roles=["S1"])
    await channel_admins.set_channel_admins(
        session,
        tenant_id=tenant.id,
        platform="slack",
        channel_id="chan-1",
        role_ids=["S1"],
        user_ids=[],
        actor_account_id=None,
    )
    await session.commit()
    return tenant


async def test_group_lookups_and_dms_run_after_the_claim_commits(
    db_session: AsyncSession, db_engine: AsyncEngine
) -> None:
    """A slow platform must never hold the budget row's lock or a pooled connection."""
    tenant = await _slack_channel_with_group_admin(db_session)
    pool = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    seen_keys: list[str | None] = []

    async def committed_key() -> str | None:
        async with pool() as other:
            budget = await channel_budgets.get_channel_budget(
                other, tenant_id=tenant.id, platform="slack", channel_id="chan-1"
            )
        assert budget is not None, "the channel has a budget"
        return budget.exhausted_notice_key

    async def lookup(group_id: str) -> frozenset[str]:
        seen_keys.append(await committed_key())
        return frozenset({"U_GROUP"})

    def group_members(platform: str, workspace_id: str) -> GroupMembers:
        return lookup

    sent: list[BudgetNotice] = []

    async def notifier(notice: BudgetNotice) -> int:
        seen_keys.append(await committed_key())
        sent.append(notice)
        return 1

    await notify_budget_exhausted(
        sessionmaker=pool,
        notifier=notifier,
        tenant_id=tenant.id,
        platform="slack",
        channel_id="chan-1",
        now=_NOW,
        group_members=group_members,
    )

    assert [n.recipient_ids for n in sent] == [("U_GROUP",)], "the live group admin is told"
    assert seen_keys == ["2026-07", "2026-07"], (
        "another connection already sees the claim during the lookup and the send"
    )


async def test_a_hanging_group_lookup_is_cut_off_by_the_notice_timeout(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(channel_budget_notice, "NOTICE_TIMEOUT_S", 0.2)
    tenant = await _slack_channel_with_group_admin(db_session)

    async def hanging(group_id: str) -> frozenset[str]:
        await asyncio.sleep(30)
        return frozenset()

    async def notifier(notice: BudgetNotice) -> int:
        raise AssertionError("no recipients were resolved, so nothing is sent")

    started = time.monotonic()
    await notify_budget_exhausted(
        sessionmaker=db_session_factory,
        notifier=notifier,
        tenant_id=tenant.id,
        platform="slack",
        channel_id="chan-1",
        now=_NOW,
        group_members=lambda platform, workspace_id: hanging,
    )

    assert time.monotonic() - started < 5, "the timeout bounds the lookups, not only the DMs"
    async with db_session_factory() as session:
        assert await _claim(session, tenant) is not None, (
            "nothing was sent, so the window is free for the next refusal"
        )
