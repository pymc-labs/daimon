"""The channel budget notice: who gets it, once per window, and that it never breaks a refusal."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from daimon.core import channel_budget_notice
from daimon.core.channel_budget_notice import (
    AlertOutcome,
    BudgetAlert,
    BudgetNotice,
    claim_budget_alert_post,
    claim_budget_notice,
    notice_recipients,
    notify_budget_alert,
    notify_budget_exhausted,
    poll_budget_alerts_once,
)
from daimon.core.config import DirectMessagePolicy
from daimon.core.stores import accounts, channel_budgets
from daimon.core.stores.domain import Platform, Role, TenantRow
from daimon.testing.factories import (
    budget_alert,
    make_account,
    make_channel_budget,
    make_ledger_entry,
    make_platform_principal,
    make_tenant,
)
from daimon.testing.settings_mock import bare_settings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

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


async def _alert_budget(
    session: AsyncSession, *, spent: str, channel: str | None = "chan-9"
) -> TenantRow:
    tenant = await make_tenant(session)
    await make_ledger_entry(
        session, tenant=tenant, delta_usd=-Decimal(spent), channel_id="chan-1", occurred_at=_NOW
    )
    await make_channel_budget(
        session,
        tenant=tenant,
        limit_usd=Decimal("5"),
        on_limit="alert",
        alert_channel_id=channel,
    )
    await session.commit()
    return tenant


async def _alert(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantRow,
    sent: list[BudgetAlert],
    *,
    outcome: AlertOutcome = "sent",
) -> None:
    async def alerter(alert: BudgetAlert) -> AlertOutcome:
        sent.append(alert)
        return outcome

    await notify_budget_alert(
        sessionmaker=factory,
        alerter=alerter,
        tenant_id=tenant.id,
        platform=tenant.platform,
        channel_id="chan-1",
        now=_NOW,
        settings=bare_settings(),
    )


async def test_an_alert_posts_once_per_multiple_of_the_limit(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _alert_budget(db_session, spent="5.50")
    sent: list[BudgetAlert] = []
    await _alert(db_session_factory, tenant, sent)
    await _alert(db_session_factory, tenant, sent)
    assert [(a.level, a.destination_id) for a in sent] == [(1, "chan-9")], (
        "the first multiple alerts once, in the budget's alert channel"
    )
    text = sent[0].text("#ops")
    assert "$5.50" in text and "$5.00" in text and "monthly" in text and "#ops" in text, (
        "the alert names the channel, the spend, the limit and the window"
    )

    await make_ledger_entry(
        db_session, tenant=tenant, delta_usd=Decimal("-5"), channel_id="chan-1", occurred_at=_NOW
    )
    await db_session.commit()
    await _alert(db_session_factory, tenant, sent)
    assert [a.level for a in sent] == [1, 2], "the next full multiple alerts again"

    await _alert(db_session_factory, tenant, sent)
    next_month = _NOW.replace(month=8)
    async with db_session_factory() as session:
        again = await claim_budget_alert_post(
            session,
            tenant_id=tenant.id,
            platform=tenant.platform,
            channel_id="chan-1",
            now=next_month,
            settings=bare_settings(),
        )
    assert again is None, "a new month has spent nothing, so nothing is due"
    assert [a.level for a in sent] == [1, 2], "a repeat at the same level posts nothing"


async def test_an_alert_that_fails_is_retried_a_refused_one_is_not_and_a_stop_budget_never_alerts(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _alert_budget(db_session, spent="6")
    sent: list[BudgetAlert] = []
    await _alert(db_session_factory, tenant, sent, outcome="failed")
    await _alert(db_session_factory, tenant, sent)
    assert [a.level for a in sent] == [1, 1], "a failed post frees the claim for the next poll"

    refused = await _alert_budget(db_session, spent="6")
    gone: list[BudgetAlert] = []
    await _alert(db_session_factory, refused, gone, outcome="refused")
    await _alert(db_session_factory, refused, gone)
    assert len(gone) == 1, "a channel Daimon cannot post in keeps the claim: no retry loop"

    stopper = await make_tenant(db_session)
    await make_ledger_entry(
        db_session, tenant=stopper, delta_usd=Decimal("-9"), channel_id="chan-1", occurred_at=_NOW
    )
    await make_channel_budget(db_session, tenant=stopper, limit_usd=Decimal("5"))
    await db_session.commit()
    await _alert(db_session_factory, stopper, sent)
    assert len(sent) == 2, "a stop budget posts no alert"


async def test_the_poller_alerts_whatever_path_spent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _alert_budget(db_session, spent="6")
    sent: list[BudgetAlert] = []

    async def alerter(alert: BudgetAlert) -> AlertOutcome:
        sent.append(alert)
        return "sent"

    for _ in range(2):
        await poll_budget_alerts_once(
            db_session_factory,
            platform=tenant.platform,
            alerter=alerter,
            settings=bare_settings(),
            now=_NOW,
        )
    mine = [a.level for a in sent if a.tenant_id == tenant.id]
    assert mine == [1], "the ledger's spend alerts once, with no turn running"


def test_an_alert_in_another_workspace_names_the_channel_and_its_workspace() -> None:
    alert = budget_alert()
    assert alert.channel_ref("<#C1>", "Ops") == "<#C1>", "its own workspace gets a mention"
    foreign = replace(alert, own=False)
    assert foreign.channel_ref("<#C1>", "Ops") == "#general in Ops", "elsewhere, label and name"
    assert foreign.channel_ref("<#C1>") == "#general", "no name read: the label alone"


async def test_an_alert_without_a_destination_claims_nothing(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _alert_budget(db_session, spent="6", channel=None)
    sent: list[BudgetAlert] = []
    with capture_logs() as logs:
        await _alert(db_session_factory, tenant, sent)
    assert sent == [], "no alert channel and no support channel leaves nowhere to post"
    assert [e["event"] for e in logs if e["log_level"] == "warning"] == [
        "channel_budget.alert_destination_missing"
    ], "the poller says why it stays silent"
    async with db_session_factory() as session:
        status = await channel_budgets.get_channel_budget(
            session, tenant_id=tenant.id, platform=tenant.platform, channel_id="chan-1"
        )
    assert status is not None and status.alert_level == 0, "nothing was claimed"
