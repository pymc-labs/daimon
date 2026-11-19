"""Support changes: admin only, compared against what the caller read, audited."""

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
from daimon.core.stores.accounts import set_role
from daimon.core.stores.channel_changes import WORKSPACE_ID, list_changes
from daimon.core.stores.domain import Role
from daimon.core.stores.teams_installations import record_teams_installation
from daimon.core.stores.tenants import get_tenant
from daimon.core.support import (
    RequestChoice,
    SupportError,
    SupportPlace,
    parse_limit_spec,
    parse_menu,
)
from daimon.core.support_admin import settle_outcome_text
from daimon.core.support_changes import (
    CHANNEL_SETTING,
    LIMIT_SETTING,
    MENU_SETTING,
    MENU_USE_SETTING,
    NOT_IN_WORKSPACE,
    SETTLE_SETTING,
    ChannelPick,
    clear_limit,
    may_set_support,
    read_menu_use,
    remove_menu,
    set_limit,
    set_menu,
    set_menu_use,
    set_support_channel,
    settle,
    settle_from_post,
    stale_text,
)
from daimon.core.support_routing import in_channel
from daimon.testing.factories import make_platform_principal, make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

D = Decimal
MENU = parse_menu(
    {
        "name": "Support",
        "options": [
            {
                "key": "review",
                "label": "Review",
                "price": {"kind": "range", "min": "1", "max": "3"},
            },
            {"key": "quick", "label": "Quick", "price": {"kind": "fixed", "amount": "2"}},
        ],
    }
)
LIMIT = parse_limit_spec(scope="channel", scope_id="C1", shared="5")

type Factory = async_sessionmaker[AsyncSession]


def _actor(tenant_id: uuid.UUID, subject: Subject | None = None) -> ChannelActor:
    return ChannelActor(
        tenant_id=tenant_id,
        account_id=None,
        subject=subject or Subject(is_admin=True, platform_user_id="UADMIN"),
        surface="web",
    )


TEAM = "T_OWN"


async def _tenant(db_session: AsyncSession) -> uuid.UUID:
    tenant = await make_tenant(db_session, platform="slack", workspace_id=TEAM)
    await db_session.commit()
    return tenant.id


async def _menu(factory: Factory, actor: ChannelActor) -> None:
    await set_menu(factory, actor, platform="slack", key="support", spec=MENU)
    await set_menu_use(
        factory, actor, platform="slack", scope="workspace", scope_id="", key="support"
    )


async def _pending(
    factory: Factory,
    tenant_id: uuid.UUID,
    message: str = "m1",
    *,
    option: int = 0,
    post: bool = True,
    charged: bool = False,
) -> uuid.UUID:
    """A request in C1, posted to the support channel S1 (`post` False: never delivered).

    `charged` makes it one charged at submit, as a fixed price was before Accept."""
    async with factory.begin() as session:
        outcome = await ledger.submit_request(
            session,
            tenant_id=tenant_id,
            account_id=None,
            platform="slack",
            platform_user_id="U1",
            channel_id="C1",
            message_id=message,
            ma_session_id=None,
            place=SupportPlace(channel_id="C1", agent_id=None),
            choice=RequestChoice.of(MENU.options[option]),
            default_allowance=20,
        )
        assert outcome.status == "recorded" and outcome.row, "a recorded request"
        if charged:
            await session.execute(
                text(
                    "UPDATE support_escalations SET status = 'settled', charge = hold WHERE id = :id"
                ),
                {"id": outcome.row.id},
            )
        if post:
            await ledger.mark_delivered(
                session, escalation_id=outcome.row.id, support_channel_id="S1"
            )
    return outcome.row.id


async def _from_post(
    factory: Factory,
    request_id: uuid.UUID,
    charge: Decimal | None,
    *,
    by: str = "Staff",
    account: uuid.UUID | None = None,
    channel: str = "S1",
) -> ledger.SettleOutcome:
    return await settle_from_post(
        factory,
        escalation_id=request_id,
        charge=charge,
        settled_by=by,
        settled_by_account_id=account,
        default_allowance=20,
        channel_id=channel,
        parent_id=None,
        default_channel_id="SDEFAULT",
    )


def test_may_set_support_when_any_subject_then_matches_channel_budgets() -> None:
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
        assert may_set_support(subject) == budget, f"{subject} differs from budgets"
    assert may_set_support(Subject(is_admin=True)), "a native admin may"
    assert not may_set_support(Subject()), "a member may not"


def test_in_channel_when_post_or_its_thread_then_true() -> None:
    assert in_channel(configured="S1", channel_id="S1", parent_id=None), "the channel"
    assert in_channel(configured="S1", channel_id="T1", parent_id="S1"), "a thread in it"
    assert not in_channel(configured="S1", channel_id="C1", parent_id=None), "elsewhere"
    assert not in_channel(configured=None, channel_id="S1", parent_id=None), "unset"


async def test_changes_when_not_an_admin_then_forbidden_and_unrecorded(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id = await _tenant(db_session)
    member = _actor(tenant_id, Subject(platform_user_id="U1"))
    f = db_session_factory
    attempts = (
        set_limit(f, member, platform="slack", spec=LIMIT),
        clear_limit(f, member, platform="slack", scope="channel", scope_id="C1"),
        set_menu(f, member, platform="slack", key="support", spec=MENU),
        remove_menu(f, member, platform="slack", key="support"),
        set_menu_use(f, member, platform="slack", scope="workspace", scope_id="", key=None),
        set_support_channel(
            f, member, pick=ChannelPick("slack", "S9", slack_team_id=TEAM), own_allowed=True
        ),
        settle(f, member, escalation_id=uuid.uuid4(), charge=D(1), default_allowance=20),
    )
    for attempt in attempts:
        with pytest.raises(ChannelChangeRefused) as refused:
            await attempt
        assert refused.value.kind == "forbidden", "only an admin changes support"
    changes = await list_changes(db_session, tenant_id=tenant_id, channel_id=WORKSPACE_ID)
    assert changes == [], "a refusal records nothing"


async def test_set_limit_when_expected_is_stale_then_refuses_and_records_once(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id = await _tenant(db_session)
    actor = _actor(tenant_id)
    row = await set_limit(
        db_session_factory, actor, platform="slack", spec=LIMIT, expected=Expected(None)
    )
    with pytest.raises(StaleChannelValue) as stale:
        await set_limit(
            db_session_factory, actor, platform="slack", spec=LIMIT, expected=Expected(None)
        )
    assert stale_text(stale.value).endswith(f"Now: revision {row.revision}."), "the limit now"
    with pytest.raises(StaleChannelValue):
        await clear_limit(
            db_session_factory,
            actor,
            platform="slack",
            scope="channel",
            scope_id="C1",
            expected=Expected(row.revision - 1),
        )
    cleared = await clear_limit(
        db_session_factory,
        actor,
        platform="slack",
        scope="channel",
        scope_id="C1",
        expected=Expected(row.revision),
    )
    assert cleared, "cleared against what was read"
    changes = await list_changes(db_session, tenant_id=tenant_id, channel_id="C1")
    assert [(c.before is None, c.after is None, c.surface) for c in changes] == [
        (False, True, "web"),
        (True, False, "web"),
    ], "a channel limit is recorded on its channel; the stale attempts left no row"


async def test_set_limit_when_a_persons_own_then_the_audit_names_no_person(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id = await _tenant(db_session)
    actor = _actor(tenant_id)
    spec = parse_limit_spec(scope="workspace", platform_user_id="USECRET", each_person="3")
    await set_limit(db_session_factory, actor, platform="slack", spec=spec, expected=Expected(None))

    async def clear() -> bool:
        return await clear_limit(
            db_session_factory,
            actor,
            platform="slack",
            scope="workspace",
            scope_id="",
            platform_user_id="USECRET",
        )

    assert (await clear(), await clear()) == (True, False), "cleared once"
    changes = await list_changes(db_session, tenant_id=tenant_id, channel_id=WORKSPACE_ID)
    limits = [c for c in changes if c.setting == LIMIT_SETTING]
    assert [c.after is None for c in limits] == [True, False], "set then cleared"
    assert isinstance(limits[1].after, dict) and limits[1].after["person"] is True
    dumped = json.dumps([[c.before, c.after] for c in changes])
    assert "USECRET" not in dumped, "channel_changes never names a person"


async def test_remove_menu_when_a_place_uses_it_then_refused_until_dropped(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id = await _tenant(db_session)
    actor = _actor(tenant_id)
    menu = await set_menu(db_session_factory, actor, platform="slack", key="support", spec=MENU)
    with pytest.raises(StaleChannelValue):
        await set_menu(
            db_session_factory,
            actor,
            platform="slack",
            key="support",
            spec=MENU,
            expected=Expected(menu.revision - 1),
        )
    await set_menu_use(
        db_session_factory, actor, platform="slack", scope="channel", scope_id="C1", key="support"
    )
    with pytest.raises(ChannelChangeRefused) as in_use:
        await remove_menu(db_session_factory, actor, platform="slack", key="support")
    assert in_use.value.kind == "invalid", "a menu in use stays"
    old = await set_menu_use(
        db_session_factory, actor, platform="slack", scope="channel", scope_id="C1", key=None
    )
    assert old == "support", "returns the place's old pick"
    await remove_menu(
        db_session_factory, actor, platform="slack", key="support", expected=Expected(menu.revision)
    )
    with pytest.raises(ChannelChangeRefused, match="key. There are no menus yet") as gone:
        await remove_menu(db_session_factory, actor, platform="slack", key="support")
    assert gone.value.kind == "not_found", "removing a menu twice is refused"
    changes = await list_changes(db_session, tenant_id=tenant_id, channel_id="C1")
    assert [(c.setting, c.before, c.after) for c in changes] == [
        (MENU_USE_SETTING, {"menu": "support"}, None),
        (MENU_USE_SETTING, None, {"menu": "support"}),
    ], "a place's menu is recorded on its channel"


async def test_set_menu_use_when_unknown_or_stale_then_refused(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id = await _tenant(db_session)
    actor = _actor(tenant_id)
    for key in ("support", "calls"):
        await set_menu(db_session_factory, actor, platform="slack", key=key, spec=MENU)
    with pytest.raises(ChannelChangeRefused, match="Menus: calls, support.$"):
        await set_menu_use(
            db_session_factory, actor, platform="slack", scope="agent", scope_id="a1", key="suport"
        )
    await set_menu_use(
        db_session_factory,
        actor,
        platform="slack",
        scope="agent",
        scope_id="a1",
        key="calls",
        expected=Expected(None),
    )
    with pytest.raises(StaleChannelValue) as stale:
        await set_menu_use(
            db_session_factory,
            actor,
            platform="slack",
            scope="agent",
            scope_id="a1",
            key="support",
            expected=Expected(None),
        )
    assert stale_text(stale.value).endswith("Now: menu calls."), "what the place offers now"
    picked = await read_menu_use(db_session, tenant_id=tenant_id, scope="agent", scope_id="a1")
    assert picked == "calls", "the stale attempt changed nothing"


@pytest.mark.parametrize(
    ("setting", "current", "now"),
    [
        (MENU_SETTING, {"key": "support", "revision": 3, "options": []}, "revision 3"),
        (MENU_SETTING, None, "no menu"),
        (MENU_USE_SETTING, None, "no menu"),
        (LIMIT_SETTING, {"scope": "workspace", "revision": 7}, "revision 7"),
        (LIMIT_SETTING, None, "no limit"),
        (CHANNEL_SETTING, None, "the default channel"),
        (SETTLE_SETTING, {"request": "r", "status": "settled", "charge": "1"}, "settled, 1 credit"),
        (SETTLE_SETTING, {"request": "r", "status": "declined", "charge": None}, "declined"),
    ],
)
def test_stale_text_when_each_setting_then_says_what_is_there_in_brief(
    setting: str, current: object, now: str
) -> None:
    assert stale_text(StaleChannelValue(setting, current)).endswith(f" Now: {now}."), setting


async def test_set_support_channel_when_outside_the_workspace_then_refused_and_unrecorded(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id = await _tenant(db_session)
    await record_teams_installation(
        db_session, tenant_id=tenant_id, team_id="TM_OWN", group_id="g", name=None
    )
    await db_session.commit()
    actor = _actor(tenant_id)
    foreign = (
        ChannelPick("slack", "S9", slack_team_id="T_OTHER"),
        ChannelPick("slack", "S9"),
        ChannelPick("discord", "S9", discord_guild_id=TEAM),
        ChannelPick("teams", "S9", teams_team_id="TM_OTHER", teams_service_url="u"),
        ChannelPick("teams", "S9", teams_service_url="u"),
        ChannelPick("web", "S9"),
    )
    for pick in foreign:
        with pytest.raises(ChannelChangeRefused) as refused:
            await set_support_channel(db_session_factory, actor, pick=pick, own_allowed=True)
        assert refused.value.kind == "invalid", f"{pick} is not this workspace's"
        assert str(refused.value) == NOT_IN_WORKSPACE
    assert await list_changes(db_session, tenant_id=tenant_id, channel_id=WORKSPACE_ID) == []
    own = ChannelPick("teams", "S9", teams_team_id="TM_OWN", teams_service_url="u")
    # The tenant is Slack, so a Teams pick still needs its install but not the tenant's platform.
    row = await set_support_channel(db_session_factory, actor, pick=own, own_allowed=True)
    assert row and row.teams_team_id == "TM_OWN", "an installed team is this workspace's"


async def test_set_menu_when_channel_then_checked_like_the_request_channel(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id = await _tenant(db_session)
    actor = _actor(tenant_id)
    own = ChannelPick("slack", "S9", slack_team_id=TEAM)
    with pytest.raises(ChannelChangeRefused) as locked:
        await set_menu(db_session_factory, actor, platform="slack", key="m", spec=MENU, channel=own)
    assert locked.value.kind == "forbidden", "a deployment that keeps its channel refuses"
    foreign = ChannelPick("slack", "S9", slack_team_id="T_OTHER")
    with pytest.raises(ChannelChangeRefused) as refused:
        await set_menu(
            db_session_factory,
            actor,
            platform="slack",
            key="m",
            spec=MENU,
            channel=foreign,
            own_allowed=True,
        )
    assert str(refused.value) == NOT_IN_WORKSPACE, "another workspace's channel is refused"
    row = await set_menu(
        db_session_factory,
        actor,
        platform="slack",
        key="m",
        spec=MENU,
        channel=own,
        own_allowed=True,
    )
    assert row.channel and (row.channel.id, row.channel.team_id) == ("S9", TEAM), "stored"
    assert row.spec.channel == row.channel, "the menu as shown carries it"


async def test_set_support_channel_when_discord_then_guild_must_be_the_tenants(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="G1")
    await db_session.commit()
    actor = _actor(tenant.id)
    with pytest.raises(ChannelChangeRefused):
        await set_support_channel(
            db_session_factory,
            actor,
            pick=ChannelPick("discord", "C9", discord_guild_id="G2"),
            own_allowed=True,
        )
    row = await set_support_channel(
        db_session_factory,
        actor,
        pick=ChannelPick("discord", "C9", discord_guild_id="G1"),
        own_allowed=True,
    )
    assert row and row.channel_id == "C9"


async def test_set_support_channel_when_set_and_dropped_then_audited_on_the_workspace(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id = await _tenant(db_session)
    actor = _actor(tenant_id)
    pick = ChannelPick("slack", "S9", slack_team_id=TEAM)
    with pytest.raises(ChannelChangeRefused) as locked:
        await set_support_channel(db_session_factory, actor, pick=pick, own_allowed=False)
    assert locked.value.kind == "forbidden", "the operator keeps the default channel"
    row = await set_support_channel(
        db_session_factory, actor, pick=pick, own_allowed=True, expected=Expected(None)
    )
    assert row and row.channel_id == "S9"
    with pytest.raises(StaleChannelValue):
        await set_support_channel(
            db_session_factory, actor, pick=None, own_allowed=True, expected=Expected(None)
        )
    dropped = await set_support_channel(
        db_session_factory, actor, pick=None, own_allowed=True, expected=Expected(row.revision)
    )
    assert dropped is None, "the default applies again"
    changes = await list_changes(db_session, tenant_id=tenant_id, channel_id=WORKSPACE_ID)
    assert [(c.setting, c.before is None, c.after is None) for c in changes] == [
        (CHANNEL_SETTING, False, True),
        (CHANNEL_SETTING, True, False),
    ]


async def test_settle_when_admin_then_charges_and_audits_the_amount_only(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id = await _tenant(db_session)
    actor = _actor(tenant_id)
    await _menu(db_session_factory, actor)
    await set_limit(db_session_factory, actor, platform="slack", spec=LIMIT)
    request_id = await _pending(db_session_factory, tenant_id)
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


async def test_settle_when_charged_at_submit_then_keep_or_refund_and_audit(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id = await _tenant(db_session)
    actor = _actor(tenant_id)
    await _menu(db_session_factory, actor)
    await set_limit(db_session_factory, actor, platform="slack", spec=LIMIT)
    kept_id = await _pending(
        db_session_factory, tenant_id, "m1", option=1, post=False, charged=True
    )
    refunded_id = await _pending(
        db_session_factory, tenant_id, "m2", option=1, post=False, charged=True
    )
    async with db_session_factory() as session:
        lost = await ledger.list_requests(session, tenant_id=tenant_id, undelivered=True)
        assert {r.id for r in lost} == {kept_id, refunded_id}, "charged at submit, not seen"
        assert all(r.status == "settled" and r.charge == D(2) for r in lost), "charged 2 each"

    other = await settle(
        db_session_factory,
        actor,
        escalation_id=kept_id,
        charge=D(1),
        default_allowance=20,
        expected=Expected(None),
    )
    assert other.status == "invalid", "another amount is refused"
    assert "keep" in settle_outcome_text(other).lower(), "and says to keep or decline"
    kept = await settle(
        db_session_factory,
        actor,
        escalation_id=kept_id,
        charge=D(2),
        default_allowance=20,
        expected=Expected(None),
    )
    assert kept.status == "settled" and kept.row, "kept"
    assert (kept.row.charge, kept.row.settled_by) == (D(2), "UADMIN"), "same charge, admin recorded"
    refund = await settle(
        db_session_factory,
        actor,
        escalation_id=refunded_id,
        charge=None,
        default_allowance=20,
        expected=Expected(None),
    )
    assert refund.status == "declined" and refund.row, "declined"
    assert (refund.row.charge, refund.row.settled_by) == (D(0), "UADMIN"), "refunded"
    async with db_session_factory() as session:
        lost = await ledger.list_requests(session, tenant_id=tenant_id, undelivered=True)
    assert lost == [], "both left the list"
    for request_id in (kept_id, refunded_id):
        with pytest.raises(StaleChannelValue):
            await settle(
                db_session_factory,
                actor,
                escalation_id=request_id,
                charge=None,
                default_allowance=20,
                expected=Expected(None),
            )
    changes = await list_changes(db_session, tenant_id=tenant_id, channel_id="C1")
    after = [(c.before, c.after) for c in changes if c.setting == SETTLE_SETTING]
    row = {"request": str(kept_id), "charge": "2"}
    gone = {"request": str(refunded_id), "charge": "2"}
    assert after == [
        ({**gone, "status": "charged"}, {**gone, "status": "declined", "charge": "0"}),
        ({**row, "status": "charged"}, {**row, "status": "settled"}),
    ], "each settle is audited from charged (unhandled) to what it left"


async def test_settle_when_a_person_already_handled_it_then_refused_as_stale(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id = await _tenant(db_session)
    actor = _actor(tenant_id)
    await _menu(db_session_factory, actor)
    pending_id = await _pending(db_session_factory, tenant_id, "m1", post=False)
    charged_id = await _pending(db_session_factory, tenant_id, "m2", option=1, post=False)
    first = await settle(
        db_session_factory, actor, escalation_id=pending_id, charge=None, default_allowance=20
    )
    second = await settle(
        db_session_factory, actor, escalation_id=charged_id, charge=D(2), default_allowance=20
    )
    assert (first.status, second.status) == ("declined", "settled"), "an admin handled both"
    async with db_session_factory() as session:
        lost = await ledger.list_requests(session, tenant_id=tenant_id, undelivered=True)
    assert lost == [], "a person handled both"
    for request_id in (pending_id, charged_id):
        with pytest.raises(StaleChannelValue):
            await settle(
                db_session_factory,
                actor,
                escalation_id=request_id,
                charge=None,
                default_allowance=20,
                expected=Expected(None),
            )
        again = await settle(
            db_session_factory, actor, escalation_id=request_id, charge=None, default_allowance=20
        )
        assert again.status == "already_settled", "the CLI and MCP path says so too"


async def test_settle_from_post_when_staff_click_then_recorded_as_the_panel(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id = await _tenant(db_session)
    actor = _actor(tenant_id)
    await _menu(db_session_factory, actor)
    await set_limit(db_session_factory, actor, platform="slack", spec=LIMIT)
    request_id = await _pending(db_session_factory, tenant_id)
    elsewhere = await _from_post(db_session_factory, request_id, D(1), channel="SDEFAULT")
    assert elsewhere.status == "not_here", "the channel it was posted to, not the default now"
    declined = await _from_post(db_session_factory, request_id, None)
    assert declined.status == "declined", "staff decline from the post"
    again = await _from_post(db_session_factory, request_id, D(1), by="Other")
    assert again.status == "already_settled" and again.row, "the second click sees it done"
    assert again.row.settled_by == "Staff", "the first settler stands"
    changes = await list_changes(db_session, tenant_id=tenant_id, channel_id="C1")
    surfaces = [c.surface for c in changes if c.setting == SETTLE_SETTING]
    assert surfaces == ["panel"], "one record, from the support post"


async def test_settle_from_post_when_the_requester_clicks_then_only_an_admin_settles(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id = await _tenant(db_session)
    actor = _actor(tenant_id)
    await _menu(db_session_factory, actor)
    await set_limit(db_session_factory, actor, platform="slack", spec=LIMIT)
    request_id = await _pending(db_session_factory, tenant_id)
    for accept, charge in ((True, None), (False, None), (False, D(1))):
        own = await settle_from_post(
            db_session_factory,
            escalation_id=request_id,
            charge=charge,
            settled_by="U1",
            settled_by_account_id=None,
            default_allowance=20,
            channel_id="S1",
            parent_id=None,
            default_channel_id="SDEFAULT",
            accept=accept,
        )
        assert own.status == "own_request", "the requester never settles their own request"
    tenant = await get_tenant(db_session, tenant_id)
    requester = await make_platform_principal(
        db_session, platform="slack", external_id="U1", tenant=tenant
    )
    await set_role(db_session, requester.account_id, Role.ADMIN)
    await db_session.commit()
    settled = await _from_post(db_session_factory, request_id, None, by="U1")
    assert settled.status == "declined", "an admin may settle their own request"


async def test_settle_from_post_when_settler_is_another_tenant_then_audit_names_no_account(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id = await _tenant(db_session)
    actor = _actor(tenant_id)
    await _menu(db_session_factory, actor)
    await set_limit(db_session_factory, actor, platform="slack", spec=LIMIT)
    first = await _pending(db_session_factory, tenant_id, "m1")
    second = await _pending(db_session_factory, tenant_id, "m2")
    home = await make_tenant(db_session, platform="slack")
    foreign = await make_platform_principal(
        db_session, platform="slack", external_id="USTAFF", tenant=home
    )
    own = await get_tenant(db_session, tenant_id)
    local = await make_platform_principal(
        db_session, platform="slack", external_id="ULOCAL", tenant=own
    )
    await db_session.commit()
    for request_id, user, account in (
        (first, "USTAFF", foreign.account_id),
        (second, "ULOCAL", local.account_id),
    ):
        await _from_post(db_session_factory, request_id, D(1), by=user, account=account)
    rows = [await ledger.get_request(db_session, escalation_id=i) for i in (first, second)]
    assert [r and r.settled_by_account_id for r in rows] == [
        foreign.account_id,
        local.account_id,
    ], "the request keeps both, and the purge clears them there"
    changes = await list_changes(db_session, tenant_id=tenant_id, channel_id="C1")
    actors = {
        c.after["request"]: c.actor_account_id
        for c in changes
        if c.setting == SETTLE_SETTING and isinstance(c.after, dict)
    }
    assert actors == {str(first): None, str(second): local.account_id}, (
        "an account of another tenant never enters this tenant's audit"
    )


async def test_clear_limit_when_requests_pending_then_declined_and_audited(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id = await _tenant(db_session)
    actor = _actor(tenant_id)
    await _menu(db_session_factory, actor)
    await set_limit(db_session_factory, actor, platform="slack", spec=LIMIT)
    request_id = await _pending(db_session_factory, tenant_id)
    await clear_limit(db_session_factory, actor, platform="slack", scope="channel", scope_id="C1")
    row = await ledger.get_request(db_session, escalation_id=request_id)
    assert row and (row.status, row.charge, row.limit_id) == ("declined", 0, None), (
        "a request left pending would settle with no limit to bound it"
    )
    assert row.settled_by == "UADMIN", "the admin who cleared the limit declined it"
    late = await _from_post(db_session_factory, request_id, D(3))
    assert late.status == "already_settled", "a click on the old post finds it declined"
    changes = await list_changes(db_session, tenant_id=tenant_id, channel_id="C1")
    settles = [(c.before, c.after) for c in changes if c.setting == SETTLE_SETTING]
    assert settles == [
        (
            {"request": str(request_id), "status": "pending", "charge": None},
            {"request": str(request_id), "status": "declined", "charge": "0"},
        )
    ], "the decline is audited like a settle"


async def test_menu_keys_when_removed_or_picked_then_parsed_like_every_other_key(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    actor = _actor(await _tenant(db_session))
    await set_menu(db_session_factory, actor, platform="slack", key="support", spec=MENU)
    await set_menu_use(
        db_session_factory,
        actor,
        platform="slack",
        scope="workspace",
        scope_id="",
        key=" support ",
    )
    assert await read_menu_use(
        db_session, tenant_id=actor.tenant_id, scope="workspace", scope_id=""
    )
    with pytest.raises(SupportError, match="menu key"):
        await remove_menu(db_session_factory, actor, platform="slack", key="Bad Key")
    with pytest.raises(SupportError, match="menu key"):
        await set_menu_use(
            db_session_factory, actor, platform="slack", scope="workspace", scope_id="", key="A B"
        )
    await set_menu_use(
        db_session_factory, actor, platform="slack", scope="workspace", scope_id="", key=None
    )
    await remove_menu(db_session_factory, actor, platform="slack", key=" support ")
