"""The admin read of support plans and the settle wording every admin surface shares."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from daimon.core.authz import Subject
from daimon.core.channel_changes import ChannelActor, Expected
from daimon.core.stores import support_escalation as ledger
from daimon.core.support_plan_admin import (
    NO_REQUEST,
    get_plan_usage,
    list_plan_usage,
    parse_plan_value,
    settle_outcome_text,
)
from daimon.core.support_plan_changes import set_allowance, set_catalog, set_plan, settle
from daimon.core.support_plans import (
    CLIENT_NOT_NOTIFIED,
    RequestChoice,
    SupportPlace,
    SupportPlanError,
    parse_catalog,
    parse_plan_spec,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

D = Decimal
NOW = datetime(2026, 11, 17, tzinfo=UTC)
MENU = parse_catalog(
    {
        "name": "Support",
        "options": [
            {"key": "review", "label": "Review", "price": {"kind": "range", "min": "1", "max": "3"}}
        ],
    }
)

type Factory = async_sessionmaker[AsyncSession]


def _actor(tenant_id: uuid.UUID) -> ChannelActor:
    return ChannelActor(
        tenant_id=tenant_id, account_id=None, subject=Subject(is_admin=True), surface="cli"
    )


async def _setup(db_session: AsyncSession, factory: Factory) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = await make_tenant(db_session, platform="slack")
    await db_session.commit()
    actor = _actor(tenant.id)
    menu = await set_catalog(factory, actor, platform="slack", key="support", spec=MENU)
    spec = parse_plan_spec(scope="channel", scope_id="C1", catalog="support", pool="5")
    await set_plan(factory, actor, platform="slack", spec=spec)
    await set_plan(
        factory,
        actor,
        platform="slack",
        spec=parse_plan_spec(
            scope="workspace",
            per_person="2",
            window="fixed",
            starts_at="2026-10-01",
            ends_at="2026-11-01",
        ),
    )
    await set_allowance(
        factory,
        actor,
        platform="slack",
        platform_user_id="U1",
        credits=D(4),
        scope_id="C1",
        scope="channel",
    )
    async with factory.begin() as session:
        outcome = await ledger.submit_request(
            session,
            tenant_id=tenant.id,
            account_id=None,
            platform="slack",
            platform_user_id="U1",
            channel_id="C1",
            message_id="m1",
            ma_session_id=None,
            place=SupportPlace(channel_id="C1", agent_id=None),
            choice=RequestChoice("review", menu.revision),
            default_allowance=20,
        )
    assert outcome.row is not None, "a pending request"
    return tenant.id, outcome.row.id


async def test_list_plan_usage_when_plans_exist_then_workspace_first_with_pool_used(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id, _ = await _setup(db_session, db_session_factory)
    plans = await list_plan_usage(db_session, tenant_id=tenant_id, now=NOW)
    assert [p.plan.scope for p in plans] == ["workspace", "channel"], "workspace first"
    workspace, channel = plans
    assert workspace.state == "ended", "the fixed window ended on 1 Nov"
    assert workspace.summary == "Support ended on 1 Nov.", "an ended plan says so"
    assert channel.pool_used == D(1), "the pending range request holds its minimum"
    assert channel.summary == "1 of 5 credits used", "the pool in words"
    assert channel.value.catalog == "support", "the menu key, not its id"
    assert [a.platform_user_id for a in channel.allowances] == ["U1"], "own cap listed"


async def test_get_plan_usage_when_missing_then_none(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id, _ = await _setup(db_session, db_session_factory)
    found = await get_plan_usage(
        db_session, tenant_id=tenant_id, scope="channel", scope_id="C1", now=NOW
    )
    missing = await get_plan_usage(
        db_session, tenant_id=tenant_id, scope="agent", scope_id="agent_x", now=NOW
    )
    assert found is not None and found.plan.pool == D(5), "the channel plan"
    assert missing is None, "no agent plan"


async def test_parse_plan_value_when_round_tripped_then_expected_matches(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id, _ = await _setup(db_session, db_session_factory)
    plans = await list_plan_usage(db_session, tenant_id=tenant_id, now=NOW)
    for usage in plans:
        assert parse_plan_value(usage.value.as_json()) == usage.value, "as_json round-trips"


@pytest.mark.parametrize(
    "data",
    [
        {"scope": "workspace", "window": "weekly"},
        {"scope": "nowhere", "window": "total"},
        {"scope": "workspace", "window": "total", "pool": 1.5},
        {"scope": "workspace", "window": "total", "ends_at": "soon"},
    ],
)
def test_parse_plan_value_when_malformed_then_refused(data: dict[str, object]) -> None:
    with pytest.raises(SupportPlanError):
        parse_plan_value(data)


async def test_settle_outcome_text_when_each_outcome_then_worded_for_the_settler(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id, request_id = await _setup(db_session, db_session_factory)
    actor = _actor(tenant_id)
    over = await settle(
        db_session_factory, actor, escalation_id=request_id, charge=D(9), default_allowance=20
    )
    assert settle_outcome_text(over) == "Enter an amount from 1 to 3.", "the range"
    done = await settle(
        db_session_factory, actor, escalation_id=request_id, charge=D(2), default_allowance=20
    )
    assert settle_outcome_text(done) == f"Charged 2 credits. {CLIENT_NOT_NOTIFIED}", "charged"
    again = await settle(
        db_session_factory, actor, escalation_id=request_id, charge=D(3), default_allowance=20
    )
    assert settle_outcome_text(again) == "Already charged 2 credits.", "already settled"
    fixed = await settle(
        db_session_factory,
        actor,
        escalation_id=request_id,
        charge=None,
        default_allowance=20,
        expected=Expected(D(2)),
    )
    assert settle_outcome_text(fixed).startswith("Declined."), "a correction to a decline"
    missing = await settle(
        db_session_factory, actor, escalation_id=uuid.uuid4(), charge=D(1), default_allowance=20
    )
    assert settle_outcome_text(missing) == NO_REQUEST, "no such request"
