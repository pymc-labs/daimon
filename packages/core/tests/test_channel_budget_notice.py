"""The channel budget notice: who gets it, once per window, and that it never breaks a refusal."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from daimon.core import channel_budget_notice
from daimon.core.channel_budget_notice import (
    BudgetNotice,
    claim_budget_notice,
    notice_recipients,
    notify_budget_exhausted,
)
from daimon.core.config import DirectMessagePolicy
from daimon.core.stores import accounts, channel_budgets
from daimon.core.stores.domain import Platform, Role, TenantRow
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


async def test_a_channel_without_admins_tells_the_server_admins(db_session: AsyncSession) -> None:
    tenant = await _spent_channel(db_session)
    assert await _recipients(db_session, await _claim(db_session, tenant)) == ("u-admin",)


async def test_live_server_admins_replace_the_stored_ones(db_session: AsyncSession) -> None:
    """Teams passes its admin team's owners: they are told, not the stored admin role."""
    tenant = await _spent_channel(db_session, limit="0")
    notice = await _claim(db_session, tenant)
    assert notice is not None, "the window was claimed"

    async def live() -> tuple[str, ...]:
        return ("owner-b", "owner-a")

    sessionmaker = async_sessionmaker(bind=db_session.bind)
    recipients = await notice_recipients(sessionmaker, notice, live)
    assert recipients == ("owner-a", "owner-b"), "the live admins, sorted, and not u-admin"


async def test_no_notice_while_the_budget_has_room(db_session: AsyncSession) -> None:
    tenant = await _spent_channel(db_session, limit="5")
    assert await _claim(db_session, tenant) is None


async def test_setting_the_budget_again_rearms_the_notice(db_session: AsyncSession) -> None:
    tenant = await _spent_channel(db_session)
    assert await _claim(db_session, tenant) is not None
    await make_channel_budget(db_session, tenant=tenant, limit_usd=Decimal("1"))
    assert await _claim(db_session, tenant) is not None, "a budget set again is a new window"


async def test_releasing_an_old_window_leaves_the_newer_claim(db_session: AsyncSession) -> None:
    tenant = await _spent_channel(db_session)
    august = datetime(2026, 8, 1, tzinfo=UTC)
    await make_ledger_entry(
        db_session, tenant=tenant, delta_usd=Decimal("-1"), channel_id="chan-1", occurred_at=august
    )
    old = await _claim(db_session, tenant)
    new = await _claim(db_session, tenant, now=august)
    assert old is not None and new is not None and new.window_key != old.window_key

    await channel_budgets.release_exhausted_notice(
        db_session, budget_id=old.budget_id, key=old.window_key
    )
    assert await _claim(db_session, tenant, now=august) is None, "the newer claim still stands"
    await channel_budgets.release_exhausted_notice(
        db_session, budget_id=new.budget_id, key=new.window_key
    )
    assert await _claim(db_session, tenant, now=august) is not None, "its own key frees it"


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


async def test_allowed_recipients_when_policy_or_grant_refuses_then_skipped() -> None:
    notice = BudgetNotice(
        tenant_id=uuid.uuid4(),
        workspace_id="g",
        platform="discord",
        channel_id="c",
        recipient_ids=("a", "b", "c"),
        limit_line="$5.00",
        monthly=False,
        budget_id=uuid.uuid4(),
        window_key="window",
    )

    async def everyone(_user_id: str) -> bool:
        return True

    async def not_c(user_id: str) -> bool:
        return user_id != "c"

    allow_bc = DirectMessagePolicy(mode="allowlist", recipient_ids=["b", "c"])
    assert await notice.allowed_recipients(allow_bc, everyone) == ("b", "c"), (
        "the env policy filters recipients"
    )
    assert await notice.allowed_recipients(allow_bc, not_c) == ("b",), (
        "the default agent's grant filters recipients too"
    )
    assert await notice.allowed_recipients(DirectMessagePolicy(mode="disabled"), everyone) == (), (
        "a disabled policy admits nobody"
    )
    assert notice.text("c") == "c has used its $5.00 budget.\n\nRaise the budget to resume now.", (
        "the notice names the channel and the limit"
    )
