"""One channel's overview, filtered by what each viewer can already learn elsewhere."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.channel_environments import save_scope_environment
from daimon.core.channel_overview import (
    ChannelAdminSet,
    ChannelViewer,
    OverviewFact,
    build_channel_overview,
    load_channel_overview,
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.channel_budgets import set_channel_budget
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

ROOM = "C_ROOM"
OTHER = "C_OTHER"
POLICY = TenantAccessPolicy(
    sealed_channel_ids=(ROOM,),
    isolated_channel_ids=(ROOM,),
    protected_channel_ids=(ROOM,),
    agent_channel_pins={"room-agent": (ROOM,)},
)
EVERYTHING: frozenset[OverviewFact] = frozenset(
    {"isolation", "seal", "protection", "budget", "environment", "admins"}
)


@pytest.mark.parametrize(
    ("viewer", "channel_id", "shown"),
    [
        (ChannelViewer(reads_access_policy=True), ROOM, EVERYTHING),
        (
            ChannelViewer(is_server_admin=True),
            ROOM,
            frozenset({"isolation", "budget", "environment", "admins"}),
        ),
        (
            ChannelViewer(sees_channel=True, inside_channel_id=ROOM),
            ROOM,
            frozenset({"budget", "environment"}),
        ),
        (ChannelViewer(sees_channel=True), ROOM, frozenset({"budget"})),
        (ChannelViewer(sees_channel=True, inside_channel_id=ROOM), OTHER, frozenset({"budget"})),
        (ChannelViewer(), OTHER, frozenset({"environment"})),
        (ChannelViewer(inside_channel_id=ROOM), OTHER, frozenset[OverviewFact]()),
    ],
    ids=[
        "operator sees everything",
        "server admin sees all but seal and protection",
        "member inside sees the budget and environment",
        "member outside sees only the budget",
        "member inside sees no environment outside",
        "a viewer the channel isn't shown to sees its environment from its side",
        "nothing across the line",
    ],
)
def test_each_viewer_sees_only_what_it_can_already_learn(
    viewer: ChannelViewer, channel_id: str, shown: frozenset[OverviewFact]
) -> None:
    overview = build_channel_overview(
        channel_id,
        viewer=viewer,
        policy=POLICY,
        environment_name="gpu",
        budget=None,
        admins=None,
    )
    assert overview.shown == shown, "the shown facts follow the viewer"
    hidden = EVERYTHING - shown
    values = {
        "isolation": overview.isolated,
        "seal": overview.sealed,
        "protection": overview.protected,
        "environment": overview.environment_name,
        "admins": overview.admins,
    }
    assert all(values[fact] is None for fact in hidden if fact in values), (
        "an omitted fact is None, never a false value"
    )


def test_shown_facts_carry_their_values() -> None:
    overview = build_channel_overview(
        ROOM,
        viewer=ChannelViewer(reads_access_policy=True),
        policy=POLICY,
        environment_name=None,
        budget=None,
        admins=None,
    )
    assert (overview.isolated, overview.sealed, overview.protected) == (True, True, True), (
        "all three hold"
    )
    assert overview.admins == ChannelAdminSet(), "shown with nobody is an empty set"
    assert overview.budget is None and "budget" in overview.shown, "shown: no budget"
    outside = build_channel_overview(
        OTHER,
        viewer=ChannelViewer(reads_access_policy=True),
        policy=POLICY,
        environment_name=None,
        budget=None,
        admins=None,
    )
    assert (outside.isolated, outside.sealed, outside.protected) == (False, False, False), (
        "a shown fact that does not hold is false"
    )


async def test_load_reads_one_channel_and_skips_what_the_viewer_cannot_see(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await set_access_policy(db_session, tenant_id=tenant.id, policy=POLICY)
    await save_scope_environment(
        db_session,
        tenant_id=tenant.id,
        channel_id=ROOM,
        environment_name="gpu",
        actor_account_id=None,
    )
    await set_channel_budget(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        channel_id=ROOM,
        limit_usd=Decimal("50"),
        window="monthly",
        starts_at=None,
        ends_at=None,
        set_by_account_id=None,
    )
    await set_channel_admins(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        channel_id=ROOM,
        role_ids=["R1"],
        user_ids=["U1"],
        actor_account_id=None,
    )

    async def load(viewer: ChannelViewer):
        return await load_channel_overview(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            channel_id=ROOM,
            viewer=viewer,
            default=DeploymentDefault(agent_name="daimon", environment_name="default"),
            now=datetime.now(UTC),
        )

    admin = await load(ChannelViewer(is_server_admin=True))
    assert admin.isolated is True and admin.environment_name == "gpu", "admin sees the room"
    assert admin.budget is not None and admin.budget.budget.limit_usd == Decimal("50"), "budget"
    assert admin.admins == ChannelAdminSet(("R1",), ("U1",)), "the grant's ids are shown"
    assert admin.sealed is None and admin.protected is None, "the seal is the operator's"

    member = await load(ChannelViewer(sees_channel=True))
    assert member.budget is not None, "anyone shown the channel reads its budget"
    assert (member.isolated, member.admins, member.environment_name) == (None, None, None), (
        "outside the room a member learns nothing else of it"
    )
