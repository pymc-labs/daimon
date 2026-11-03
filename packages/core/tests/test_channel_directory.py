"""The channel directory: every table that names a channel, one row each."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from daimon.core import channel_directory
from daimon.core._models import SharedChannel, ThreadParticipationScope
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.channel_directory import (
    WEB_ROW_ID,
    Sourced,
    load_channel_directory,
    load_channel_row,
)
from daimon.core.platform_names import ChannelSighting
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.scope_cascade import CascadeMode, CascadeScope
from daimon.core.stores._scope_modes import set_scope_mode
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.channel_budgets import set_channel_budget
from daimon.core.stores.domain import TenantRow
from daimon.core.stores.platform_names import upsert_channel_names
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.tenant_ledger import insert_entry
from daimon.testing.factories import make_tenant, make_usage_event
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT = DeploymentDefault(agent_name="daimon")
NOW = datetime(2026, 5, 20, 12, tzinfo=UTC)
MARKUP = Decimal(2)


async def _spend(
    session: AsyncSession, tenant_id: uuid.UUID, channel_id: str, usd: str, at: datetime
) -> None:
    await insert_entry(
        session,
        tenant_id=tenant_id,
        delta_usd=-Decimal(usd),
        reason="turn_debit",
        idempotency_key=f"t:{uuid.uuid4()}",
        channel_id=channel_id,
        occurred_at=at,
    )


async def _usage(
    session: AsyncSession, tenant: TenantRow, channel_id: str, raw_usd: str, at: datetime
) -> None:
    """A usage row costing `raw_usd` before markup (opus input is $5 per million tokens)."""
    sid = f"sess_{uuid.uuid4()}"
    await make_usage_event(
        session,
        tenant=tenant,
        managed_session_id=sid,
        model="claude-opus-4-7",
        input_tokens=int(Decimal(raw_usd) * 200_000),
        output_tokens=0,
        channel_id=channel_id,
    )
    await session.execute(
        text("update usage_events set occurred_at = :at where managed_session_id = :s"),
        {"at": at, "s": sid},
    )


async def test_the_directory_unions_every_channel_source(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await make_tenant(db_session)
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="c1"),
        tenant_id=tenant.id,
        agent_name="local",
        mode="agent",
    )
    await set_channel_budget(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        channel_id="c2",
        limit_usd=Decimal("10"),
        window="monthly",
        starts_at=None,
        ends_at=None,
        set_by_account_id=None,
    )
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(
            channel_rules={
                "c3": ChannelRule(readers="inside"),
                "C9:1700000000.000100": ChannelRule(readers="inside"),
            },
            agent_rules={"homebody": AgentRule(runs_in=("c4", "web"))},
        ),
    )
    db_session.add(
        SharedChannel(tenant_id=tenant.id, platform="discord", channel_id="c5", source="platform")
    )
    await upsert_channel_names(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        names={"c6": ChannelSighting("general", kind="text", parent_id="k1", parent_name="Team")},
    )
    await set_scope_mode(
        db_session,
        ThreadParticipationScope,
        tenant_id=tenant.id,
        platform="discord",
        scope=CascadeScope.CHANNEL,
        scope_id="c7",
        mode=CascadeMode.ON,
    )
    await _spend(db_session, tenant.id, "c2", "1.5", NOW - timedelta(days=2))
    await _usage(db_session, tenant, "c2", "0.75", NOW - timedelta(days=2))
    await _usage(db_session, tenant, "c8", "1", NOW - timedelta(days=25))
    await _usage(db_session, tenant, "old", "3", NOW - timedelta(days=60))
    await _usage(db_session, tenant, "web:person-1", "0.125", NOW - timedelta(hours=1))
    await _usage(db_session, tenant, "web:person-2", "0.25", NOW - timedelta(days=1))
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="dm1"),
        tenant_id=tenant.id,
        agent_name="local",
        mode="agent",
    )
    await db_session.commit()

    async def dms(*_: object, **__: object) -> set[str]:
        return {"dm1"}

    monkeypatch.setattr(channel_directory, "list_dm_channel_ids", dms)
    directory = await load_channel_directory(
        db_session, tenant_id=tenant.id, default=DEFAULT, now=NOW, markup=MARKUP
    )
    rows = {row.channel_id: row for row in directory.rows}

    assert directory.rows[0].channel_id == WEB_ROW_ID, "web chat is one row, first"
    assert set(rows) == {WEB_ROW_ID, "c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8"}, (
        "every source lists its channel; DMs, threads, old spend and web places do not"
    )
    assert rows["c1"].agent == Sourced("local", "channel")
    assert rows["c2"].agent == Sourced("daimon", "deployment"), "the cascade's source is named"
    assert rows["c3"].tags == ("kept_inside",) and rows["c5"].tags == ("shared",)
    assert rows["c4"].kept_agents == ("homebody",)
    name = rows["c6"].name
    assert name is not None and (name.name, name.parent_name) == ("general", "Team")
    budget = rows["c2"].budget
    assert budget is not None and budget.spent_usd == Decimal("1.5"), (
        "a budget counts ledger debits, as the gate does"
    )
    assert rows["c2"].spend.recent_usd == Decimal("1.5"), "spend is usage priced with markup"
    assert rows["c8"].spend.recent_usd == Decimal("2") and rows["c8"].spend.month_usd == 0, (
        "25 days ago is last month but inside the last 30 days"
    )
    web = rows[WEB_ROW_ID].spend
    assert (web.month_usd, web.last_active_at) == (Decimal("0.75"), NOW - timedelta(hours=1)), (
        "every person's web chat adds up in the one row"
    )

    one = await load_channel_row(
        db_session, tenant_id=tenant.id, channel_id="c99", default=DEFAULT, now=NOW, markup=MARKUP
    )
    assert one is not None and one.agent == Sourced("daimon", "deployment"), (
        "an unlisted channel still reads"
    )
    assert (
        await load_channel_row(
            db_session,
            tenant_id=tenant.id,
            channel_id="dm1",
            default=DEFAULT,
            now=NOW,
            markup=MARKUP,
        )
        is None
    ), "a DM's channel is not a channel"
