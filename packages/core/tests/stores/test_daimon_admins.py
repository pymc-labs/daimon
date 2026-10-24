"""Delegation boundaries, immediate stored-role demotion, audit and privacy."""

from __future__ import annotations

import pytest
from daimon.core.admin_actions import DelegatedAction
from daimon.core.native_admins import (
    AdminCaller,
    is_daimon_admin,
    load_live_subject,
    read_stored_admin,
)
from daimon.core.stores.accounts import get_account, set_platform_role_ids, set_role
from daimon.core.stores.daimon_admins import change_grant, erase_user, has_grant, list_admins
from daimon.core.stores.domain import Platform, Role
from daimon.core.stores.security_audit import erase_admin_subject, list_events
from daimon.testing.factories import make_platform_principal, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
async def test_user_grant_revoke_is_scoped_and_atomic(
    db_session: AsyncSession, platform: Platform
) -> None:
    tenant = await make_tenant(db_session, platform=platform)
    other = await make_tenant(db_session, platform=platform)
    principal = await make_platform_principal(
        db_session, tenant=tenant, platform=platform, external_id="member"
    )
    key = dict(
        tenant_id=tenant.id,
        platform=platform,
        subject_kind="user",
        subject_id="member",
        actor_user_id="owner",
    )
    await change_grant(db_session, **key, is_platform_admin=True)
    subject = await load_live_subject(
        db_session,
        tenant_id=tenant.id,
        platform=platform,
        caller=AdminCaller(platform_user_id="member"),
        action=DelegatedAction.AGENT_SETTINGS,
    )
    assert subject.is_admin
    assert not await is_daimon_admin(
        db_session,
        tenant_id=other.id,
        platform=platform,
        platform_user_id="member",
        is_platform_admin=False,
    )
    assert not await is_daimon_admin(
        db_session,
        tenant_id=tenant.id,
        platform="wrong",
        platform_user_id="member",
        is_platform_admin=True,
    )
    # Delegation never promotes the platform-admin account snapshot.
    await change_grant(db_session, **key, is_platform_admin=True, revoke=True)
    account = await get_account(db_session, principal.account_id)
    assert account is not None and account.role is Role.USER
    assert not (
        await load_live_subject(
            db_session,
            tenant_id=tenant.id,
            platform=platform,
            caller=AdminCaller(platform_user_id="member"),
            action=DelegatedAction.AGENT_SETTINGS,
        )
    ).is_admin
    assert not (
        await read_stored_admin(
            db_session,
            tenant_id=tenant.id,
            platform=platform,
            account_id=principal.account_id,
            platform_user_id="member",
        )
    ).is_admin
    # Revoking a stored grant can never revoke platform authority.
    assert await is_daimon_admin(
        db_session,
        tenant_id=tenant.id,
        platform=platform,
        platform_user_id="member",
        is_platform_admin=True,
    )
    audit = await list_events(db_session, tenant_id=tenant.id, tool_name="daimon_admins")
    assert [(row.operation, row.platform_user_id, row.admin_subject_id) for row in audit] == [
        ("grant", "owner", "member"),
        ("revoke", "owner", "member"),
    ]
    assert all(row.occurred_at.tzinfo is not None for row in audit)


@pytest.mark.parametrize("revoke", [False, True])
async def test_delegated_admin_cannot_change_grants(db_session: AsyncSession, revoke: bool) -> None:
    tenant = await make_tenant(db_session)
    await change_grant(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        subject_kind="user",
        subject_id="delegate",
        actor_user_id="owner",
        is_platform_admin=True,
    )
    before = await list_events(db_session, tenant_id=tenant.id)
    with pytest.raises(PermissionError):
        await change_grant(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            subject_kind="user",
            subject_id="new-admin",
            actor_user_id="delegate",
            is_platform_admin=False,
            revoke=revoke,
        )
    assert [row.subject_id for row in await list_admins(db_session, tenant_id=tenant.id)] == [
        "delegate"
    ]
    assert await list_events(db_session, tenant_id=tenant.id) == before


async def test_discord_roles_are_live_and_revoke_preserves_platform_snapshot(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    principal = await make_platform_principal(
        db_session, tenant=tenant, platform="discord", external_id="member"
    )
    await set_platform_role_ids(db_session, principal.account_id, ["role"])
    await set_role(db_session, principal.account_id, Role.ADMIN)
    key = dict(
        tenant_id=tenant.id,
        platform="discord",
        subject_kind="role",
        subject_id="role",
        actor_user_id="owner",
        is_platform_admin=True,
    )
    await change_grant(db_session, **key)
    assert await has_grant(
        db_session, tenant_id=tenant.id, platform="discord", user_id="member", role_ids=["role"]
    )
    assert not await has_grant(
        db_session, tenant_id=tenant.id, platform="discord", user_id="member", role_ids=[]
    )
    await change_grant(db_session, **key, revoke=True)
    assert not await has_grant(
        db_session, tenant_id=tenant.id, platform="discord", user_id="member", role_ids=["role"]
    )
    account = await get_account(db_session, principal.account_id)
    assert account is not None and account.role is Role.ADMIN


async def test_erasure_scrubs_grantee_and_grantor(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    for subject in ("user", "other"):
        await change_grant(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            subject_kind="user",
            subject_id=subject,
            actor_user_id="user",
            is_platform_admin=True,
        )
    await erase_user(db_session, tenant_id=tenant.id, platform="discord", platform_user_id="user")
    await erase_admin_subject(
        db_session, tenant_id=tenant.id, platform="discord", platform_user_id="user"
    )
    grants = await list_admins(db_session, tenant_id=tenant.id)
    assert len(grants) == 1 and grants[0].subject_id == "other" and grants[0].granted_by is None
    events = await list_events(db_session, tenant_id=tenant.id)
    assert all(row.platform_user_id is None and row.admin_subject_id != "user" for row in events)


@pytest.mark.parametrize("platform", ["slack", "teams"])
async def test_non_discord_roles_refused(db_session: AsyncSession, platform: Platform) -> None:
    tenant = await make_tenant(db_session, platform=platform)
    with pytest.raises(ValueError):
        await change_grant(
            db_session,
            tenant_id=tenant.id,
            platform=platform,
            subject_kind="role",
            subject_id="role",
            actor_user_id="owner",
            is_platform_admin=True,
        )
    assert await list_admins(db_session, tenant_id=tenant.id) == []


async def test_stored_subject_cannot_borrow_another_principals_grant(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    user = await make_platform_principal(
        db_session, tenant=tenant, platform="discord", external_id="user"
    )
    await change_grant(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        subject_kind="user",
        subject_id="admin",
        actor_user_id="owner",
        is_platform_admin=True,
    )
    assert not (
        await read_stored_admin(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            account_id=user.account_id,
            platform_user_id="admin",
        )
    ).is_admin


async def test_account_purge_erases_grants_and_audit_targets(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.purge import purge_account

    tenant = await make_tenant(db_session)
    user = await make_platform_principal(
        db_session, tenant=tenant, platform="discord", external_id="user"
    )
    for target in ("user", "other"):
        await change_grant(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            subject_kind="user",
            subject_id=target,
            actor_user_id="user",
            is_platform_admin=True,
        )
    await db_session.commit()
    report = await purge_account(sm=db_session_factory, account_id=user.account_id)
    assert report.db.daimon_admins == 2
    grants = await list_admins(db_session, tenant_id=tenant.id)
    assert len(grants) == 1 and grants[0].granted_by is None
    events = await list_events(db_session, tenant_id=tenant.id)
    assert all(row.platform_user_id is None and row.admin_subject_id != "user" for row in events)


async def test_shared_configuration_helpers_recheck_delegation(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from daimon.core.agent_reach import may_bind_as_channel_default
    from daimon.core.github_panel import can_manage_agent_github
    from daimon.core.mcp_attach import decide_mcp_replacement
    from daimon.core.scope import DeploymentDefault
    from daimon.testing.ma_models import ma_agent

    tenant = await make_tenant(db_session)
    key = dict(
        tenant_id=tenant.id,
        platform="discord",
        subject_kind="user",
        subject_id="delegate",
        actor_user_id="owner",
        is_platform_admin=True,
    )
    caller = AdminCaller(platform_user_id="delegate")
    common = dict(
        tenant_id=tenant.id,
        platform="discord",
        caller=caller,
        agent_names=("Helper",),
        ma_agent_id="ma-helper",
        default=DeploymentDefault(),
        is_daimon_managed=True,
    )
    agent = ma_agent(id="ma-helper", name="Helper", metadata={"daimon_managed": "true"})
    for revoke in (False, True):
        await change_grant(db_session, **key, revoke=revoke)
        await db_session.commit()
        assert not await can_manage_agent_github(
            db_session, tenant_id=tenant.id, platform="discord", caller=caller
        ), "GitHub is platform-only"
        assert (
            await may_bind_as_channel_default(db_session, **common, channel_id="channel")
            is not revoke
        )
        decision = await decide_mcp_replacement(
            db_session_factory,
            tenant_id=tenant.id,
            platform="discord",
            agent=agent,
            caller=caller,
            default=DeploymentDefault(),
        )
        assert decision != "allow", "MCP connections stay platform-only even before revocation"
        assert (
            await decide_mcp_replacement(
                db_session_factory,
                tenant_id=tenant.id,
                platform="discord",
                agent=agent,
                caller=AdminCaller(platform_user_id="native", is_server_admin=True),
                default=DeploymentDefault(),
            )
            == "allow"
        )
