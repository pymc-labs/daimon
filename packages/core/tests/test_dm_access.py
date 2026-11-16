"""DM access I/O: grants, locks and picks decide a person's DM at start and at delivery."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import AgentRef, Place, Surface, build_subject
from daimon.core.direct_messages import (
    DirectMessageAccessRevoked,
    DirectMessageMembershipUnknown,
    DirectMessageReply,
    require_dm_delivery,
    start_cold_dm,
)
from daimon.core.dm_access import DmAgent, decide_dm, permits_dm
from daimon.core.dm_routing import DmChoose, DmRefused, DmStart
from daimon.core.features.service import FeatureActor, change_feature
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.membership import CachedMembershipResolver, Membership
from daimon.core.scope import ResolvedConfig
from daimon.core.stores import dm_access as store
from daimon.core.stores.direct_messages import set_dm_enabled
from daimon.core.turn.admission import Admission, AdmissionGrant
from daimon.core.turn.errors import AdmissionDenied
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.turn_fakes import deps_for

NOW = datetime(2026, 1, 1, tzinfo=UTC)
POLICY = TenantAccessPolicy()
ADA = build_subject(is_admin=False, platform_user_id="999", verified_membership=True)
BLIND = build_subject(is_admin=False, platform_user_id="999")


def dm_agent(name: str, tenant_id: uuid.UUID) -> DmAgent:
    record = ma_agent(id=f"ag_{name}", name=name, tenant_id=tenant_id)
    return DmAgent(name, record.id, AgentRef.of(name))


async def seed(factory, *names: str):
    async with factory.begin() as session:
        tenant = await make_tenant(session, workspace_id="700000101")
        account = await make_account(session, tenant=tenant)
        await set_dm_enabled(session, tenant_id=tenant.id, enabled=True)
    return tenant, account, [dm_agent(n, tenant.id) for n in names]


async def decide(factory, tenant, agents, subject=ADA, account_id=None):
    async with factory() as session:
        return await decide_dm(
            session,
            None,
            POLICY,
            tenant_id=tenant.id,
            platform="discord",
            agents=agents,
            subject=subject,
            account_id=account_id,
        )


async def test_decide_dm_with_dms_off_refuses(db_session_factory):
    """A workspace with DMs off reaches no agent, however its grants read."""
    tenant, _, agents = await seed(db_session_factory, "a")
    async with db_session_factory.begin() as session:
        await set_dm_enabled(session, tenant_id=tenant.id, enabled=False)
    decision = await decide(db_session_factory, tenant, agents)
    assert decision.route == DmRefused("no_agent"), "DMs off admit nobody"


async def test_decide_dm_with_one_open_agent_starts_it(db_session_factory):
    """A sole agent that admits the person is reached at once."""
    tenant, _, agents = await seed(db_session_factory, "a")
    decision = await decide(db_session_factory, tenant, agents)
    assert decision.route == DmStart("a", "only"), "one agent in play starts directly"


async def test_decide_dm_with_agent_opted_out_drops_it(db_session_factory):
    """An agent that turned the feature off at its own tier is not offered."""
    tenant, _, agents = await seed(db_session_factory, "a", "b")
    place = FeaturePlace(tenant.id, "discord", agent_id=agents[0].ma_agent_id)

    async def _admin(_: object) -> bool:
        return True

    await change_feature(
        db_session_factory,
        None,
        FeatureActor(tenant.id, None, "web", None, _admin),
        key="direct_messages",
        tier="agent",
        value="off",
        place=place,
    )
    decision = await decide(db_session_factory, tenant, agents)
    assert decision.route == DmStart("b", "only"), "the opted-out agent is skipped"


async def test_decide_dm_with_listed_grant_hides_agent_from_others(db_session_factory):
    """A listed agent admits only its people; the rest see the other agents."""
    tenant, _, agents = await seed(db_session_factory, "a", "b")
    async with db_session_factory.begin() as session:
        await store.set_grant(
            session, tenant_id=tenant.id, agent_name="a", who="listed", user_ids=["1"],
            group_ids=[], set_by=None, now=NOW,
        )  # fmt: skip
    decision = await decide(db_session_factory, tenant, agents)
    assert decision.route == DmStart("b", "only"), "999 is not on a's list"
    listed = await decide(db_session_factory, tenant, agents, build_subject(
        is_admin=False, platform_user_id="1", verified_membership=True
    ))  # fmt: skip
    assert isinstance(listed.route, DmChoose), "the listed person chooses between both"


async def test_decide_dm_with_a_lock_ignores_the_pick(db_session_factory):
    """A locked rule names the agent; a remembered pick for another is ignored."""
    tenant, account, agents = await seed(db_session_factory, "a", "b")
    async with db_session_factory.begin() as session:
        await store.add_rule(
            session, tenant_id=tenant.id, subject="everyone", subject_id=None,
            agent_name="a", locked=True, set_by=None, now=NOW,
        )  # fmt: skip
        await store.set_pick(
            session, tenant_id=tenant.id, account_id=account.id, agent_name="b", now=NOW
        )
    decision = await decide(db_session_factory, tenant, agents, account_id=account.id)
    assert decision.route == DmStart("a", "locked"), "the lock wins over the pick"
    assert not decision.may_switch_to("b"), "a locked person cannot switch"


async def test_decide_dm_with_a_pick_starts_the_picked_agent(db_session_factory):
    """Without a lock the saved pick decides among several agents."""
    tenant, account, agents = await seed(db_session_factory, "a", "b")
    async with db_session_factory.begin() as session:
        await store.set_pick(
            session, tenant_id=tenant.id, account_id=account.id, agent_name="b", now=NOW
        )
    decision = await decide(db_session_factory, tenant, agents, account_id=account.id)
    assert decision.route == DmStart("b", "pick"), "the pick is honoured"


async def test_decide_dm_with_unknown_groups_fails_closed(db_session_factory):
    """A group-based grant cannot be checked without membership: no agent is chosen."""
    tenant, _, agents = await seed(db_session_factory, "a")
    async with db_session_factory.begin() as session:
        await store.set_grant(
            session, tenant_id=tenant.id, agent_name="a", who="listed", user_ids=[],
            group_ids=["eng"], set_by=None, now=NOW,
        )  # fmt: skip
    decision = await decide(db_session_factory, tenant, agents, BLIND)
    assert type(decision.route).__name__ == "DmUnchecked", "unknown groups never admit"


async def test_permits_dm_is_false_under_a_lock_for_another_agent(db_session_factory):
    """A lock naming one agent refuses every other, even if its own grant is open."""
    tenant, _, agents = await seed(db_session_factory, "a", "b")
    async with db_session_factory.begin() as session:
        await store.add_rule(
            session, tenant_id=tenant.id, subject="user", subject_id="999",
            agent_name="a", locked=True, set_by=None, now=NOW,
        )  # fmt: skip

    async def ask(agent):
        async with db_session_factory() as session:
            return await permits_dm(
                session, None, POLICY, tenant_id=tenant.id, platform="discord",
                agent=agent, subject=ADA,
            )  # fmt: skip

    assert await ask(agents[0]), "the locked agent is permitted"
    assert not await ask(agents[1]), "another agent is refused under the lock"


async def cold(factory, agent_name="private", *, with_binding=True):
    tenant, account, _ = await seed(factory)
    admission = Admission(
        account_id=account.id,
        agent=ma_agent(id=f"ag_{agent_name}", name=agent_name, tenant_id=tenant.id),
        environment=ma_environment(id="env_1", name="default", tenant_id=tenant.id),
        config=ResolvedConfig(agent_name=agent_name, environment_name="default"),
    )
    deps = replace(
        deps_for(None, factory),
        membership_resolver=CachedMembershipResolver(
            AsyncMock(return_value=Membership(is_member=True, role_ids=("eng",)))
        ),
    )
    return tenant, account, admission, deps


async def test_start_cold_dm_under_a_grant_refusal_is_denied(db_session_factory):
    """Cold start rechecks the agent's grant at write time."""
    tenant, _, admission, deps = await cold(db_session_factory)
    async with db_session_factory.begin() as session:
        await store.set_grant(
            session, tenant_id=tenant.id, agent_name="private", who="listed", user_ids=["1"],
            group_ids=[], set_by=None, now=NOW,
        )  # fmt: skip
    with pytest.raises(AdmissionDenied):
        await start_cold_dm(
            deps, admission, tenant_id=tenant.id, workspace_id=tenant.external_id,
            route_key="555", external_user_id="999",
        )  # fmt: skip


async def test_start_cold_dm_with_remember_pick_saves_the_agent(db_session_factory):
    """A person's dropdown choice is stored for their next conversation."""
    tenant, account, admission, deps = await cold(db_session_factory)
    await start_cold_dm(
        deps, admission, tenant_id=tenant.id, workspace_id=tenant.external_id,
        route_key="555", external_user_id="999", remember_pick=True,
    )  # fmt: skip
    async with db_session_factory() as session:
        saved = await store.get_pick(session, tenant_id=tenant.id, account_id=account.id)
    assert saved is not None and saved.agent_name == "private", "the pick is remembered"


async def reply_for(deps, admission, tenant):
    row = await start_cold_dm(
        deps, admission, tenant_id=tenant.id, workspace_id=tenant.external_id,
        route_key="555", external_user_id="999",
    )  # fmt: skip
    grant = AdmissionGrant(
        tenant_id=tenant.id,
        subject=build_subject(
            is_admin=False, platform_user_id="999", role_ids=("eng",), verified_membership=True
        ),
        surface=Surface.DM,
        turn_place=Place(),
        agent=AgentRef.of("private"),
        run_place=Place(),
        channel_id=row.channel_id,
        thread_id=row.scope_id,
        is_dm=True,
        cold_dm=row.is_cold,
        human_invocation=True,
    )
    return DirectMessageReply(text="hi", conversation=row, grant=grant)


async def test_require_dm_delivery_with_open_grant_passes(db_session_factory):
    """An agent that still admits the person delivers."""
    tenant, _, admission, deps = await cold(db_session_factory)
    await require_dm_delivery(deps, await reply_for(deps, admission, tenant))


@pytest.mark.parametrize(
    ("kind", "grant_users", "grant_groups"),
    [("listed", ["1"], []), ("excluded", ["999"], [])],
)
async def test_require_dm_delivery_after_grant_change_raises(
    db_session_factory, kind, grant_users, grant_groups
):
    """Removing the person from the agent's list stops output at once."""
    tenant, _, admission, deps = await cold(db_session_factory)
    reply = await reply_for(deps, admission, tenant)
    async with db_session_factory.begin() as session:
        await store.set_grant(
            session, tenant_id=tenant.id, agent_name="private", who=kind, user_ids=grant_users,
            group_ids=grant_groups, set_by=None, now=NOW,
        )  # fmt: skip
    with pytest.raises(DirectMessageAccessRevoked, match="Private agent access revoked"):
        await require_dm_delivery(deps, reply)


async def test_require_dm_delivery_after_a_lock_for_another_agent_raises(db_session_factory):
    """A lock created after the chat began moves the person off this agent."""
    tenant, _, admission, deps = await cold(db_session_factory)
    reply = await reply_for(deps, admission, tenant)
    async with db_session_factory.begin() as session:
        await store.add_rule(
            session, tenant_id=tenant.id, subject="everyone", subject_id=None,
            agent_name="other", locked=True, set_by=None, now=NOW,
        )  # fmt: skip
    with pytest.raises(DirectMessageAccessRevoked):
        await require_dm_delivery(deps, reply)


async def test_require_dm_delivery_with_unknown_membership_keeps_proved_revocation(
    db_session_factory,
):
    """With membership unknown the admitted roles decide: a refusal there is revocation."""
    tenant, _, admission, deps = await cold(db_session_factory)
    reply = await reply_for(deps, admission, tenant)
    deps = replace(
        deps,
        membership_resolver=CachedMembershipResolver(AsyncMock(return_value=Membership())),
    )
    with pytest.raises(DirectMessageMembershipUnknown):
        await require_dm_delivery(deps, reply)
    async with db_session_factory.begin() as session:
        await store.set_grant(
            session, tenant_id=tenant.id, agent_name="private", who="listed", user_ids=[],
            group_ids=["ops"], set_by=None, now=NOW,
        )  # fmt: skip
    with pytest.raises(DirectMessageAccessRevoked):
        await require_dm_delivery(deps, reply)
