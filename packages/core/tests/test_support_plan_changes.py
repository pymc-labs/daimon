"""Support plan changes: admin only, compared against what the caller read, audited."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Place, Subject, authorize
from daimon.core.channel_changes import ChannelActor, ChannelChangeRefused, Expected
from daimon.core.errors import StaleChannelValue
from daimon.core.stores import support_escalation as ledger
from daimon.core.stores.channel_changes import WORKSPACE_ID, list_changes
from daimon.core.support_plan_changes import (
    ALLOWANCE_SETTING,
    SETTLE_SETTING,
    clear_allowance,
    clear_plan,
    is_support_channel,
    may_set_support_plan,
    read_plan,
    remove_catalog,
    set_allowance,
    set_catalog,
    set_plan,
    settle,
    settle_from_post,
)
from daimon.core.support_plans import RequestChoice, SupportPlace, parse_catalog, parse_plan_spec
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

D = Decimal
MENU = parse_catalog(
    {
        "name": "Support",
        "options": [
            {"key": "review", "label": "Review", "price": {"kind": "range", "min": "1", "max": "3"}}
        ],
    }
)
PLAN = parse_plan_spec(scope="channel", scope_id="C1", catalog="support", pool="5")

type Factory = async_sessionmaker[AsyncSession]


def _actor(tenant_id: uuid.UUID, subject: Subject | None = None) -> ChannelActor:
    return ChannelActor(
        tenant_id=tenant_id,
        account_id=None,
        subject=subject or Subject(is_admin=True, platform_user_id="UADMIN"),
        surface="web",
    )


async def _tenant(db_session: AsyncSession) -> uuid.UUID:
    tenant = await make_tenant(db_session, platform="slack")
    await db_session.commit()
    return tenant.id


async def _pending(factory: Factory, tenant_id: uuid.UUID, revision: int) -> uuid.UUID:
    async with factory.begin() as session:
        outcome = await ledger.submit_request(
            session,
            tenant_id=tenant_id,
            account_id=None,
            platform="slack",
            platform_user_id="U1",
            channel_id="C1",
            message_id="m1",
            ma_session_id=None,
            place=SupportPlace(channel_id="C1", agent_id=None),
            choice=RequestChoice("review", revision),
            default_allowance=20,
        )
    assert outcome.status == "recorded" and outcome.row, "a pending request"
    return outcome.row.id


def test_may_set_support_plan_when_any_subject_then_matches_channel_budgets() -> None:
    # Spec: server admin only, as SET_CHANNEL_BUDGET is (an admin's agent key included).
    subjects = (
        Subject(),
        Subject(is_admin=True),
        Subject(is_admin=True, via_agent_key=True),
        Subject(is_admin=True, client=True),
    )
    for subject in subjects:
        budget = authorize(
            TenantAccessPolicy(), subject=subject, action=Action.SET_CHANNEL_BUDGET, place=Place()
        )
        assert may_set_support_plan(subject) == budget, f"{subject} differs from budgets"
    assert may_set_support_plan(Subject(is_admin=True)), "a native admin may"
    assert not may_set_support_plan(Subject()), "a member may not"


def test_is_support_channel_when_post_or_its_thread_then_true() -> None:
    assert is_support_channel(configured="S1", channel_id="S1", parent_id=None), "the channel"
    assert is_support_channel(configured="S1", channel_id="T1", parent_id="S1"), "a thread in it"
    assert not is_support_channel(configured="S1", channel_id="C1", parent_id=None), "elsewhere"
    assert not is_support_channel(configured=None, channel_id="S1", parent_id=None), "unset"


async def test_changes_when_not_an_admin_then_forbidden_and_unrecorded(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id = await _tenant(db_session)
    member = _actor(tenant_id, Subject(platform_user_id="U1"))
    attempts = (
        set_plan(db_session_factory, member, platform="slack", spec=PLAN),
        set_catalog(db_session_factory, member, platform="slack", key="support", spec=MENU),
        set_allowance(
            db_session_factory, member, platform="slack", platform_user_id="U1", credits=D(9)
        ),
        settle(
            db_session_factory,
            member,
            escalation_id=uuid.uuid4(),
            charge=D(1),
            default_allowance=20,
        ),
    )
    for attempt in attempts:
        with pytest.raises(ChannelChangeRefused) as refused:
            await attempt
        assert refused.value.kind == "forbidden", "only an admin changes support plans"
    changes = await list_changes(db_session, tenant_id=tenant_id, channel_id=WORKSPACE_ID)
    assert changes == [], "a refusal records nothing"


async def test_set_plan_when_expected_is_stale_then_refuses_and_records_once(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id = await _tenant(db_session)
    actor = _actor(tenant_id)
    with pytest.raises(ChannelChangeRefused) as missing:
        await set_plan(db_session_factory, actor, platform="slack", spec=PLAN)
    assert missing.value.kind == "not_found", "the plan names a menu that does not exist"
    await set_catalog(
        db_session_factory,
        actor,
        platform="slack",
        key="support",
        spec=MENU,
        expected=Expected(None),
    )
    await set_plan(db_session_factory, actor, platform="slack", spec=PLAN, expected=Expected(None))
    with pytest.raises(StaleChannelValue) as stale:
        await set_plan(
            db_session_factory, actor, platform="slack", spec=PLAN, expected=Expected(None)
        )
    assert isinstance(stale.value.current, dict), "the stale error carries the plan now"
    current = await read_plan(db_session, tenant_id=tenant_id, scope="channel", scope_id="C1")
    assert current and current.catalog == "support" and current.pool == 5, "the plan as read"
    cleared = await clear_plan(
        db_session_factory,
        actor,
        platform="slack",
        scope="channel",
        scope_id="C1",
        expected=Expected(current),
    )
    assert cleared, "cleared against what was read"
    changes = await list_changes(db_session, tenant_id=tenant_id, channel_id="C1")
    assert [(c.before is None, c.after is None, c.surface) for c in changes] == [
        (False, True, "web"),
        (True, False, "web"),
    ], "a channel plan is recorded on its channel; the stale attempt left no row"


async def test_remove_catalog_when_a_plan_uses_it_then_refused_until_cleared(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id = await _tenant(db_session)
    actor = _actor(tenant_id)
    menu = await set_catalog(db_session_factory, actor, platform="slack", key="support", spec=MENU)
    with pytest.raises(StaleChannelValue):
        await set_catalog(
            db_session_factory,
            actor,
            platform="slack",
            key="support",
            spec=MENU,
            expected=Expected(menu.revision - 1),
        )
    await set_plan(db_session_factory, actor, platform="slack", spec=PLAN)
    with pytest.raises(ChannelChangeRefused) as in_use:
        await remove_catalog(db_session_factory, actor, platform="slack", key="support")
    assert in_use.value.kind == "invalid", "a menu in use stays"
    await clear_plan(db_session_factory, actor, platform="slack", scope="channel", scope_id="C1")
    removed = await remove_catalog(
        db_session_factory, actor, platform="slack", key="support", expected=Expected(menu.revision)
    )
    again = await remove_catalog(db_session_factory, actor, platform="slack", key="support")
    assert (removed, again) == (True, False), "removed once, then there is none"


async def test_set_allowance_when_recorded_then_the_audit_names_no_person(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id = await _tenant(db_session)
    actor = _actor(tenant_id)
    with pytest.raises(ChannelChangeRefused) as no_plan:
        await set_allowance(
            db_session_factory,
            actor,
            platform="slack",
            platform_user_id="U_SECRET",
            credits=D(3),
            scope="workspace",
        )
    assert no_plan.value.kind == "not_found", "an allowance needs its plan"
    await set_allowance(
        db_session_factory,
        actor,
        platform="slack",
        platform_user_id="U_SECRET",
        credits=D(3),
        expected=Expected(None),
    )
    with pytest.raises(StaleChannelValue):
        await clear_allowance(
            db_session_factory,
            actor,
            platform="slack",
            platform_user_id="U_SECRET",
            expected=Expected(D(2)),
        )
    first = await clear_allowance(
        db_session_factory, actor, platform="slack", platform_user_id="U_SECRET"
    )
    second = await clear_allowance(
        db_session_factory, actor, platform="slack", platform_user_id="U_SECRET"
    )
    assert (first, second) == (True, False), "cleared once"
    changes = await list_changes(db_session, tenant_id=tenant_id, channel_id=WORKSPACE_ID)
    allowances = [c for c in changes if c.setting == ALLOWANCE_SETTING]
    assert [(c.before, c.after) for c in allowances] == [
        ({"plan": "default", "credits": "3"}, None),
        (None, {"plan": "default", "credits": "3"}),
    ], "set then cleared, on the default plan"
    dumped = json.dumps([[c.before, c.after] for c in changes])
    assert "U_SECRET" not in dumped, "channel_changes never names a person"


async def test_settle_when_admin_then_charges_and_audits_the_amount_only(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id = await _tenant(db_session)
    actor = _actor(tenant_id)
    menu = await set_catalog(db_session_factory, actor, platform="slack", key="support", spec=MENU)
    await set_plan(db_session_factory, actor, platform="slack", spec=PLAN)
    request_id = await _pending(db_session_factory, tenant_id, menu.revision)
    done = await settle(
        db_session_factory,
        actor,
        escalation_id=request_id,
        charge=D(2),
        default_allowance=20,
        expected=Expected(None),
    )
    assert done.status == "settled" and done.row, "settled"
    assert done.row.settled_by == "UADMIN", "the settler is the admin"
    with pytest.raises(StaleChannelValue):
        await settle(
            db_session_factory,
            actor,
            escalation_id=request_id,
            charge=D(3),
            default_allowance=20,
            expected=Expected(None),
        )
    refund = await settle(
        db_session_factory,
        actor,
        escalation_id=request_id,
        charge=D(0),
        default_allowance=20,
        expected=Expected(D(2)),
    )
    assert refund.status == "settled" and refund.row and refund.row.charge == 0, "refunded"
    changes = await list_changes(db_session, tenant_id=tenant_id, channel_id="C1")
    settles = [c for c in changes if c.setting == SETTLE_SETTING]
    assert [c.after for c in settles] == [
        {"request": str(request_id), "status": "settled", "charge": "0"},
        {"request": str(request_id), "status": "settled", "charge": "2"},
    ], "each settle records the charge, not the person or their words"


async def test_settle_from_post_when_staff_click_then_recorded_as_the_panel(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id = await _tenant(db_session)
    actor = _actor(tenant_id)
    menu = await set_catalog(db_session_factory, actor, platform="slack", key="support", spec=MENU)
    await set_plan(db_session_factory, actor, platform="slack", spec=PLAN)
    request_id = await _pending(db_session_factory, tenant_id, menu.revision)
    declined = await settle_from_post(
        db_session_factory,
        escalation_id=request_id,
        charge=None,
        settled_by="Staff",
        settled_by_account_id=None,
        default_allowance=20,
    )
    assert declined.status == "declined", "staff decline from the post"
    again = await settle_from_post(
        db_session_factory,
        escalation_id=request_id,
        charge=D(1),
        settled_by="Other",
        settled_by_account_id=None,
        default_allowance=20,
    )
    assert again.status == "already_settled" and again.row, "the second click sees it done"
    assert again.row.settled_by == "Staff", "the first settler stands"
    changes = await list_changes(db_session, tenant_id=tenant_id, channel_id="C1")
    surfaces = [c.surface for c in changes if c.setting == SETTLE_SETTING]
    assert surfaces == ["panel"], "one record, from the support post"
