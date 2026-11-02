"""Admission of web chat turns: the web surface, the person's own principal, the bound agent."""

from __future__ import annotations

from pathlib import Path

import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Surface
from daimon.core.errors import UserFacingError
from daimon.core.stores.domain import Role, TenantRow
from daimon.core.stores.identity import find_platform_principal, get_or_create_platform_principal
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.core.turn.admission import Admission, AdmissionDenied, admit
from daimon.core.turn_hold import RequesterOrg, TurnHold
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_admission import _NOW, _admittable_router, _deps, _seed_admittable_tenant

THREAD = "web:00000000-0000-0000-0000-000000000001"


async def _admit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    tenant: TenantRow,
    *,
    user: str,
    principal_platform: str = "web",
    org: RequesterOrg = RequesterOrg.HOST,
    home_id: str | None = None,
    role: Role = Role.USER,
) -> Admission:
    return await admit(
        _deps(sessionmaker=factory, defaults_root=tmp_path, router=_admittable_router(tenant)),
        tenant_id=tenant.id,
        platform="web",
        external_user_id=user,
        channel_id="web",
        thread_id=THREAD,
        now=_NOW,
        role=role,
        requester_org=org,
        requester_home_id=home_id,
        surface=Surface.WEB,
        principal_platform=principal_platform,
    )


async def test_member_runs_as_their_linked_platform_principal(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    linked = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="U1"
    )
    await db_session.commit()

    admission = await _admit(
        db_session_factory, tmp_path, tenant, user="U1", principal_platform="discord"
    )

    assert admission.account_id == linked.account_id, "spend and memory stay the person's own"
    assert admission.grant is not None and admission.grant.surface is Surface.WEB
    assert admission.hold is None, "a member is not held"
    assert (
        await find_platform_principal(
            db_session, tenant_id=tenant.id, platform="web", external_id="U1"
        )
        is None
    ), "no second principal for the same person"


async def test_visitor_runs_as_a_web_principal_held_to_the_web_place(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)

    admission = await _admit(
        db_session_factory,
        tmp_path,
        tenant,
        user="person-1",
        org=RequesterOrg.OTHER,
        home_id="invite:1",
    )

    principal = await find_platform_principal(
        db_session, tenant_id=tenant.id, platform="web", external_id="person-1"
    )
    assert principal is not None and admission.account_id == principal.account_id
    assert admission.hold == TurnHold("web", "web", client=True), "a visitor is held"


async def test_channel_bound_agent_is_refused_on_the_web_even_for_an_admin(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(
        db_session, policy=TenantAccessPolicy(agent_channel_pins={"daimon": ("C1",)})
    )

    with pytest.raises(AdmissionDenied) as denied:
        await _admit(db_session_factory, tmp_path, tenant, user="U1", role=Role.ADMIN)

    assert denied.value.reason == "runs_elsewhere", "no admin exemption on the web"


async def test_web_binding_answers_only_its_creator(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    owner = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="web", external_id="owner"
    )
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="web",
        parent_channel_id="web",
        thread_id=THREAD,
        responder_ma_agent_id="ag_1",
        responder_name="daimon",
        creator_account_id=owner.account_id,
        kind="web",
    )
    await db_session.commit()

    admission = await _admit(db_session_factory, tmp_path, tenant, user="owner")
    assert admission.agent.id == "ag_1", "the chat's own agent answers"
    assert admission.config.thread_binding_kind == "web"

    with pytest.raises(UserFacingError):
        await _admit(db_session_factory, tmp_path, tenant, user="someone-else")
