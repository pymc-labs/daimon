"""An agent can have several owners: the primary owner and any number of co-owners."""

from __future__ import annotations

import uuid

import pytest
from daimon.core._models import AgentCoOwner, PlatformPrincipal
from daimon.core.agent_ownership import (
    ForeignOwnerError,
    add_agent_co_owner,
    agent_co_owners,
    agent_owner,
    clear_agent_owner,
    owns_agent,
    remove_agent_co_owner,
)
from daimon.core.agent_reach import load_target_facts
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.native_admins import AdminCaller
from daimon.core.operation_policy import decide_operation
from daimon.core.purge import purge_account
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import agent_owners, security_audit
from daimon.testing.db import DatabasePreflight, require_owned_sessionmaker
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

MA_AGENT = "ag_shared"


@pytest.fixture
async def people(db_preflight: DatabasePreflight, db_committing_engine: AsyncEngine):
    sessions = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    require_owned_sessionmaker(sessions)
    async with sessions.begin() as session:
        tenant = await make_tenant(session, platform="slack")
        admin = await make_account(session, tenant=tenant)
        primary = await make_account(session, tenant=tenant)
        co_owner = await make_account(session, tenant=tenant)
        member = await make_account(session, tenant=tenant)
        for account, user in ((primary, "U-PRIMARY"), (co_owner, "U-CO"), (member, "U-MEMBER")):
            await make_platform_principal(
                session, tenant=tenant, account=account, platform="slack", external_id=user
            )
        other_tenant = await make_tenant(session, platform="slack")
        stranger = await make_account(session, tenant=other_tenant)
        agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=MA_AGENT)
        await agent_owners.set_owner(
            session,
            tenant_id=tenant.id,
            agent_id=agent_id,
            owner_account_id=primary.id,
            source="admin_assigned",
        )
    return sessions, tenant, agent_id, admin, primary, co_owner, member, stranger


async def _owns(sessions, tenant, *, account=None, user=None) -> bool:
    async with sessions() as session:
        return await owns_agent(
            session,
            tenant_id=tenant.id,
            ma_agent_id=MA_AGENT,
            account_id=account,
            platform="slack" if user else None,
            platform_user_id=user,
        )


async def test_a_co_owner_owns_the_agent_exactly_as_the_primary_owner_does(people) -> None:
    sessions, tenant, agent_id, admin, primary, co_owner, member, _ = people
    assert not await _owns(sessions, tenant, account=co_owner.id)
    assert await add_agent_co_owner(
        sessions,
        tenant_id=tenant.id,
        agent_id=agent_id,
        account_id=co_owner.id,
        by_account_id=admin.id,
    )
    for account, user, expected in (
        (primary.id, "U-PRIMARY", True),
        (co_owner.id, "U-CO", True),
        (member.id, "U-MEMBER", False),
    ):
        assert await _owns(sessions, tenant, account=account) is expected
        assert await _owns(sessions, tenant, user=user) is expected
    # The primary owner's own record is untouched by a co-owner.
    assert await agent_owner(sessions, tenant_id=tenant.id, agent_id=agent_id) == primary.id
    assert await agent_co_owners(sessions, tenant_id=tenant.id, agent_id=agent_id) == [co_owner.id]


async def test_a_co_owner_stays_an_owner_when_the_primary_owner_is_cleared(people) -> None:
    sessions, tenant, agent_id, admin, primary, co_owner, _, _ = people
    await add_agent_co_owner(
        sessions,
        tenant_id=tenant.id,
        agent_id=agent_id,
        account_id=co_owner.id,
        by_account_id=admin.id,
    )
    await clear_agent_owner(sessions, tenant_id=tenant.id, agent_id=agent_id, by_account_id=None)
    assert not await _owns(sessions, tenant, account=primary.id)
    assert await _owns(sessions, tenant, account=co_owner.id)


async def test_removing_a_co_owner_ends_their_ownership(people) -> None:
    sessions, tenant, agent_id, admin, _, co_owner, _, _ = people
    await add_agent_co_owner(
        sessions,
        tenant_id=tenant.id,
        agent_id=agent_id,
        account_id=co_owner.id,
        by_account_id=admin.id,
    )
    assert await remove_agent_co_owner(
        sessions,
        tenant_id=tenant.id,
        agent_id=agent_id,
        account_id=co_owner.id,
        by_account_id=admin.id,
    )
    assert not await _owns(sessions, tenant, account=co_owner.id)
    assert not await remove_agent_co_owner(
        sessions,
        tenant_id=tenant.id,
        agent_id=agent_id,
        account_id=co_owner.id,
        by_account_id=admin.id,
    )


async def test_adding_an_existing_owner_changes_nothing(people) -> None:
    sessions, tenant, agent_id, admin, primary, co_owner, _, _ = people
    args = {"tenant_id": tenant.id, "agent_id": agent_id, "by_account_id": admin.id}
    assert not await add_agent_co_owner(sessions, account_id=primary.id, **args)
    assert await add_agent_co_owner(sessions, account_id=co_owner.id, **args)
    assert not await add_agent_co_owner(sessions, account_id=co_owner.id, **args)
    assert await agent_co_owners(sessions, tenant_id=tenant.id, agent_id=agent_id) == [co_owner.id]


async def test_an_account_outside_the_tenant_cannot_be_added(people) -> None:
    sessions, tenant, agent_id, admin, _, _, _, stranger = people
    with pytest.raises(LookupError):
        await add_agent_co_owner(
            sessions,
            tenant_id=tenant.id,
            agent_id=agent_id,
            account_id=stranger.id,
            by_account_id=admin.id,
        )


async def test_a_person_from_another_organisation_is_refused_at_add_and_at_use(people) -> None:
    sessions, tenant, agent_id, admin, _, co_owner, member, _ = people
    async with sessions.begin() as session:
        await session.execute(
            update(PlatformPrincipal)
            .where(PlatformPrincipal.account_id == member.id)
            .values(foreign_home_id="other-org")
        )
    with pytest.raises(ForeignOwnerError):
        await add_agent_co_owner(
            sessions,
            tenant_id=tenant.id,
            agent_id=agent_id,
            account_id=member.id,
            by_account_id=admin.id,
        )
    # Added while still a member of this organisation, then marked foreign: never an owner.
    await add_agent_co_owner(
        sessions,
        tenant_id=tenant.id,
        agent_id=agent_id,
        account_id=co_owner.id,
        by_account_id=admin.id,
    )
    assert await _owns(sessions, tenant, account=co_owner.id)
    async with sessions.begin() as session:
        await session.execute(
            update(PlatformPrincipal)
            .where(PlatformPrincipal.account_id == co_owner.id)
            .values(foreign_home_id="other-org")
        )
    assert not await _owns(sessions, tenant, account=co_owner.id)
    assert not await _owns(sessions, tenant, user="U-CO")


async def test_owned_agents_list_includes_co_owned_ones(people) -> None:
    sessions, tenant, agent_id, admin, primary, co_owner, member, _ = people
    await add_agent_co_owner(
        sessions,
        tenant_id=tenant.id,
        agent_id=agent_id,
        account_id=co_owner.id,
        by_account_id=admin.id,
    )
    async with sessions() as session:
        assert await agent_owners.owned_by(session, tenant.id, co_owner.id) == {agent_id}
        assert await agent_owners.owned_by(session, tenant.id, primary.id) == {agent_id}
        assert await agent_owners.owned_by(session, tenant.id, member.id) == frozenset()


async def test_each_change_leaves_an_audit_row_naming_the_person(people) -> None:
    sessions, tenant, agent_id, admin, _, co_owner, _, _ = people
    await add_agent_co_owner(
        sessions,
        tenant_id=tenant.id,
        agent_id=agent_id,
        account_id=co_owner.id,
        by_account_id=admin.id,
    )
    await remove_agent_co_owner(
        sessions,
        tenant_id=tenant.id,
        agent_id=agent_id,
        account_id=co_owner.id,
        by_account_id=admin.id,
    )
    async with sessions() as session:
        rows = await security_audit.list_events(
            session, tenant_id=tenant.id, tool_name="agent_owner"
        )
    assert sorted(row.operation or "" for row in rows) == ["add_co_owner", "remove_co_owner"]
    assert {row.account_id for row in rows} == {admin.id}
    assert all(row.scope and row.scope.endswith(str(co_owner.id)) for row in rows)


async def test_a_co_owner_passes_the_github_policy_for_the_agent_only(people) -> None:
    sessions, tenant, agent_id, admin, _, co_owner, member, _ = people
    await add_agent_co_owner(
        sessions,
        tenant_id=tenant.id,
        agent_id=agent_id,
        account_id=co_owner.id,
        by_account_id=admin.id,
    )

    async def outcome(user: str, ma_agent_id: str) -> str:
        async with sessions() as session:
            facts = await load_target_facts(
                session,
                "github_connect",
                tenant_id=tenant.id,
                platform="slack",
                agent_names=("helper",),
                ma_agent_id=ma_agent_id,
                default=DeploymentDefault(),
                caller=AdminCaller(platform_user_id=user),
                is_daimon_managed=False,
            )
        return decide_operation("github_connect", is_admin=False, target=facts)

    assert await outcome("U-CO", MA_AGENT) == "allow"
    assert await outcome("U-CO", "ag_someone_elses") == "needs_admin"
    assert await outcome("U-MEMBER", MA_AGENT) == "needs_admin"


async def test_erasing_a_co_owner_removes_their_row_and_erasing_the_adder_keeps_it(
    people, db_preflight
) -> None:
    sessions, tenant, agent_id, admin, _, co_owner, _, _ = people
    await add_agent_co_owner(
        sessions,
        tenant_id=tenant.id,
        agent_id=agent_id,
        account_id=co_owner.id,
        by_account_id=admin.id,
    )

    async def rows() -> list[tuple[uuid.UUID, uuid.UUID | None]]:
        async with sessions() as session:
            result = await session.execute(
                select(AgentCoOwner.account_id, AgentCoOwner.added_by_account_id).where(
                    AgentCoOwner.tenant_id == tenant.id
                )
            )
            return [(account, added_by) for account, added_by in result.all()]

    await purge_account(sm=sessions, account_id=admin.id)
    assert await rows() == [(co_owner.id, None)]
    await purge_account(sm=sessions, account_id=co_owner.id)
    assert await rows() == []
