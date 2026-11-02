"""Spend allowance store: rows, tenant scoping and the billed-dollar window sum."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from daimon.core.stores import spend_allowances, usage_events
from daimon.testing.factories import make_account, make_tenant
from mux.testing.drivers.anthropic.ma_models import ma_model_usage
from sqlalchemy.ext.asyncio import AsyncSession

START = datetime(2026, 1, 1, tzinfo=UTC)


async def test_create_list_and_delete_are_scoped_to_the_tenant(db_session: AsyncSession) -> None:
    mine = await make_tenant(db_session, workspace_id="w1")
    theirs = await make_tenant(db_session, workspace_id="w2")
    account = await make_account(db_session, tenant=mine)
    created = await spend_allowances.create_allowance(
        db_session,
        tenant_id=mine.id,
        subject_kind="group",
        subject_id="everyone",
        agent_id=None,
        amount_usd=Decimal("12.50"),
        window="month",
        starts_at=START,
        created_by_account_id=account.id,
    )
    assert created.amount_usd == Decimal("12.50"), "the row model carries the amount"
    assert created.created_by_account_id == account.id, "who set it is recorded"
    assert [
        r.id for r in await spend_allowances.list_for_tenant(db_session, tenant_id=mine.id)
    ] == [created.id], "the tenant lists its allowance"
    assert await spend_allowances.list_for_tenant(db_session, tenant_id=theirs.id) == [], (
        "another tenant sees none"
    )
    assert not await spend_allowances.delete_allowance(
        db_session, tenant_id=theirs.id, allowance_id=created.id
    ), "another tenant cannot delete it"
    assert await spend_allowances.delete_allowance(
        db_session, tenant_id=mine.id, allowance_id=created.id
    ), "the owner deletes it"
    assert not await spend_allowances.delete_allowance(
        db_session, tenant_id=mine.id, allowance_id=created.id
    ), "deleting twice reports it was already gone"


async def test_create_rejects_an_unknown_window(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    with pytest.raises(Exception, match="ck_spend_allowances_window"):
        await spend_allowances.create_allowance(
            db_session,
            tenant_id=tenant.id,
            subject_kind="person",
            subject_id="u1",
            agent_id=None,
            amount_usd=Decimal(1),
            window="week",  # type: ignore[arg-type]  # the check constraint is under test
            starts_at=START,
        )


async def test_used_since_is_billed_dollars_for_the_person_and_agent(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    for n, (user, agent) in enumerate([("u1", "a1"), ("u1", "a2"), ("u2", "a1")]):
        await usage_events.record(
            db_session,
            tenant_id=tenant.id,
            platform_user_id=user,
            managed_session_id=f"s{n}",
            model="claude-opus-4-7",
            model_usage=ma_model_usage(input_tokens=1_000_000, output_tokens=0),
            event_id=f"e{n}",
            agent_id=agent,
        )
    since = datetime.now(UTC) - timedelta(days=1)
    kwargs = {"tenant_id": tenant.id, "platform_user_ids": ["u1"], "since": since}
    markup = Decimal("1.2")
    on_a1 = await spend_allowances.used_since(db_session, agent_id="a1", markup=markup, **kwargs)
    on_all = await spend_allowances.used_since(db_session, agent_id=None, markup=markup, **kwargs)
    assert on_a1 == Decimal("6.000000"), "one $5 call at 1.2x markup is $6 billed"
    assert on_all == Decimal("12.000000"), "no agent counts the person's whole spend"
    later = await spend_allowances.used_since(
        db_session,
        agent_id=None,
        markup=markup,
        **{**kwargs, "since": datetime.now(UTC) + timedelta(hours=1)},
    )
    assert later == Decimal(0), "spend before the window start does not count"


async def test_used_since_counts_every_id_the_person_spends_under(
    db_session: AsyncSession,
) -> None:
    """A person who spends under a chat id and a web id has both counted together."""
    tenant = await make_tenant(db_session)
    for n, user in enumerate(["chat-id", "web-id", "someone-else"]):
        await usage_events.record(
            db_session,
            tenant_id=tenant.id,
            platform_user_id=user,
            managed_session_id=f"s{n}",
            model="claude-opus-4-7",
            model_usage=ma_model_usage(input_tokens=1_000_000, output_tokens=0),
            event_id=f"e{n}",
        )
    used = await spend_allowances.used_since(
        db_session,
        tenant_id=tenant.id,
        platform_user_ids=["chat-id", "web-id"],
        agent_id=None,
        since=datetime.now(UTC) - timedelta(days=1),
        markup=Decimal(1),
    )
    assert used == Decimal("10.000000"), "two $5 calls under the person's two ids add up"
