"""Admission of held turns: clients (another organisation) and shared channels."""

from __future__ import annotations

from pathlib import Path

import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, AgentRef, Place, Surface, authorize
from daimon.core.stores.accounts import get_account, set_role
from daimon.core.stores.domain import Role, TenantRow
from daimon.core.stores.identity import (
    find_platform_principal,
    get_or_create_platform_principal,
    mark_principal_foreign,
)
from daimon.core.stores.shared_channels import is_channel_shared, mark_channel_shared
from daimon.core.turn.admission import Admission, AdmissionDenied, admit, reauthorize
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn_hold import RequesterOrg, TurnHold
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_admission import _NOW, _admittable_router, _deps, _seed_admittable_tenant

PLATFORM = "slack"
CHANNEL = "C_SHARED"
USER = "U_EXTERNAL"
HOME = "T_OTHER"


def _deps_for(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path, tenant: TenantRow
) -> TurnDeps:
    return _deps(sessionmaker=factory, defaults_root=tmp_path, router=_admittable_router(tenant))


async def _admit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    tenant: TenantRow,
    *,
    org: RequesterOrg = RequesterOrg.HOST,
    role: Role | None = Role.USER,
    home_id: str | None = None,
    channel_shared: bool | None = None,
    is_dm: bool = False,
    channel_id: str = CHANNEL,
    user: str = USER,
) -> Admission:
    return await admit(
        _deps_for(factory, tmp_path, tenant),
        tenant_id=tenant.id,
        platform=PLATFORM,
        external_user_id=user,
        channel_id=channel_id,
        thread_id=None if is_dm else "1700000000.000100",
        now=_NOW,
        role=role,
        is_dm=is_dm,
        requester_org=org,
        requester_home_id=home_id,
        channel_shared=channel_shared,
    )


async def _role_and_marker(
    db_session: AsyncSession, tenant: TenantRow, user: str = USER
) -> tuple[Role | None, str | None]:
    db_session.expire_all()
    principal = await find_platform_principal(
        db_session, tenant_id=tenant.id, platform=PLATFORM, external_id=user
    )
    assert principal is not None
    account = await get_account(db_session, principal.account_id)
    return (None if account is None else Role(account.role)), principal.foreign_home_id


async def _store_role(db_session: AsyncSession, tenant: TenantRow, role: Role) -> None:
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform=PLATFORM, external_id=USER
    )
    await set_role(db_session, principal.account_id, role)
    await db_session.commit()


async def test_an_ordinary_member_in_an_unshared_channel_is_not_held(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    admission = await _admit(db_session_factory, tmp_path, tenant)
    assert admission.hold is None
    assert admission.shared_channel is False


async def test_a_verified_other_organisation_is_stored_as_a_user_and_marked_foreign(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    await _store_role(db_session, tenant, Role.ADMIN)

    admission = await _admit(
        db_session_factory,
        tmp_path,
        tenant,
        org=RequesterOrg.OTHER,
        role=Role.ADMIN,
        home_id=HOME,
    )

    assert await _role_and_marker(db_session, tenant) == (Role.USER, HOME)
    assert admission.hold == TurnHold(PLATFORM, CHANNEL, client=True)
    assert admission.grant is not None
    assert admission.grant.subject.client is True
    assert admission.grant.subject.is_admin is False


async def test_an_other_organisation_requires_its_home_id(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    with pytest.raises(ValueError, match="requester_home_id"):
        await _admit(db_session_factory, tmp_path, tenant, org=RequesterOrg.OTHER)


async def test_an_unknown_requester_is_held_but_changes_nothing_stored(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    await _store_role(db_session, tenant, Role.ADMIN)

    admission = await _admit(
        db_session_factory, tmp_path, tenant, org=RequesterOrg.UNKNOWN, role=Role.USER
    )

    assert await _role_and_marker(db_session, tenant) == (Role.ADMIN, None)
    assert admission.hold == TurnHold(PLATFORM, CHANNEL, client=True)
    assert admission.grant is not None and admission.grant.subject.is_admin is False
    assert (
        await is_channel_shared(
            db_session, tenant_id=tenant.id, platform=PLATFORM, channel_id=CHANNEL
        )
        is False
    )
    assert admission.shared_channel is False


async def test_a_principal_already_marked_foreign_is_a_client_even_when_the_adapter_says_host(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    await _admit(db_session_factory, tmp_path, tenant, org=RequesterOrg.OTHER, home_id=HOME)

    admission = await _admit(
        db_session_factory, tmp_path, tenant, org=RequesterOrg.HOST, role=Role.ADMIN
    )

    assert admission.hold is not None and admission.hold.client
    assert admission.grant is not None and admission.grant.subject.client
    assert (await _role_and_marker(db_session, tenant))[1] == HOME, "the marker is sticky"


@pytest.mark.parametrize("org", [RequesterOrg.OTHER, RequesterOrg.UNKNOWN])
async def test_a_client_is_refused_a_direct_message(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    org: RequesterOrg,
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    with pytest.raises(AdmissionDenied) as denied:
        await _admit(
            db_session_factory, tmp_path, tenant, org=org, home_id=HOME, is_dm=True, channel_id="D1"
        )
    assert denied.value.reason == "client_requester"


async def test_a_marked_client_is_refused_a_direct_message_even_as_host(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    await _admit(db_session_factory, tmp_path, tenant, org=RequesterOrg.OTHER, home_id=HOME)
    with pytest.raises(AdmissionDenied) as denied:
        await _admit(db_session_factory, tmp_path, tenant, is_dm=True, channel_id="D1")
    assert denied.value.reason == "client_requester"


async def test_a_member_still_gets_a_direct_message(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    admission = await _admit(db_session_factory, tmp_path, tenant, is_dm=True, channel_id="D1")
    assert admission.hold is None


async def test_the_platforms_shared_signal_holds_a_member_and_is_recorded(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    admission = await _admit(db_session_factory, tmp_path, tenant, channel_shared=True)

    assert admission.hold == TurnHold(PLATFORM, CHANNEL, client=False)
    assert admission.shared_channel is True
    assert await is_channel_shared(
        db_session, tenant_id=tenant.id, platform=PLATFORM, channel_id=CHANNEL
    )
    source = (await db_session.execute(text("SELECT source FROM shared_channels"))).scalar_one()
    assert source == "platform"


async def test_a_recorded_shared_channel_holds_later_turns_without_a_signal(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    await mark_channel_shared(
        db_session, tenant_id=tenant.id, platform=PLATFORM, channel_id=CHANNEL, source="client"
    )
    await db_session.commit()

    admission = await _admit(db_session_factory, tmp_path, tenant, channel_shared=None)
    assert admission.hold == TurnHold(PLATFORM, CHANNEL, client=False)
    assert admission.shared_channel is True


async def test_a_verified_other_requester_reveals_the_channel_as_shared(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    admission = await _admit(
        db_session_factory, tmp_path, tenant, org=RequesterOrg.OTHER, home_id=HOME
    )
    assert admission.shared_channel is True
    source = (await db_session.execute(text("SELECT source FROM shared_channels"))).scalar_one()
    assert source == "client"

    member = await _admit(db_session_factory, tmp_path, tenant, user="U_MEMBER")
    assert member.hold == TurnHold(PLATFORM, CHANNEL, client=False)


async def test_a_held_turn_in_a_shared_channel_has_no_admin_exemption(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    admission = await _admit(
        db_session_factory, tmp_path, tenant, role=Role.ADMIN, channel_shared=True
    )
    assert admission.grant is not None
    subject = admission.grant.subject
    assert subject.is_admin is False and subject.client is False

    pinned = TenantAccessPolicy(agent_channel_pins={"daimon": ("elsewhere",)})
    agent = AgentRef.of("daimon")
    unheld_admin = authorize(
        pinned,
        subject=subject.__class__(is_admin=True, platform_user_id=USER),
        action=Action.RUN_AGENT,
        surface=Surface.DM,
        agent=agent,
        place=Place(),
    )
    held = authorize(
        pinned,
        subject=subject,
        action=Action.RUN_AGENT,
        surface=Surface.DM,
        agent=agent,
        place=Place(),
        hold=admission.hold,
    )
    assert unheld_admin.allowed, "an unheld admin keeps the DM exemption"
    assert not held.allowed, "a held turn has none"


async def test_an_admin_outside_a_shared_channel_keeps_their_admin_subject(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    admission = await _admit(db_session_factory, tmp_path, tenant, role=Role.ADMIN)
    assert admission.grant is not None and admission.grant.subject.is_admin is True


async def test_reauthorize_keeps_the_hold_and_the_subject(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    deps = _deps_for(db_session_factory, tmp_path, tenant)
    admission = await _admit(
        db_session_factory, tmp_path, tenant, org=RequesterOrg.OTHER, home_id=HOME
    )
    current = await reauthorize(deps, admission)
    assert current.hold == admission.hold == TurnHold(PLATFORM, CHANNEL, client=True)
    assert current.shared_channel is True
    assert current.grant is not None and current.grant.hold == admission.hold
    assert current.grant.subject.client is True


async def test_a_protection_added_after_admission_still_refuses_a_held_turn(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    from daimon.core.stores.access_policy import set_access_policy

    tenant = await _seed_admittable_tenant(db_session, policy=None)
    deps = _deps_for(db_session_factory, tmp_path, tenant)
    admission = await _admit(db_session_factory, tmp_path, tenant, channel_shared=True)
    await set_access_policy(
        db_session, tenant_id=tenant.id, policy=TenantAccessPolicy(protected_channel_ids=(CHANNEL,))
    )
    await db_session.commit()
    with pytest.raises(AdmissionDenied) as denied:
        await reauthorize(deps, admission)
    assert denied.value.reason == "writers_none"


async def test_a_default_agent_still_runs_in_a_shared_channel_under_agent_rules(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """The hold is separate from the agent's rules: a pin naming the channel still admits."""
    from daimon.core.stores.access_policy import set_access_policy

    tenant = await _seed_admittable_tenant(db_session, policy=None)
    pins = TenantAccessPolicy(agent_channel_pins={"daimon": (CHANNEL, "C_OPS")})
    await set_access_policy(db_session, tenant_id=tenant.id, policy=pins)
    await db_session.commit()
    admission = await _admit(
        db_session_factory, tmp_path, tenant, org=RequesterOrg.OTHER, home_id=HOME
    )
    assert admission.hold is not None


async def test_a_marked_principal_never_regains_a_stored_admin_role_from_a_host_claim(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    await _admit(db_session_factory, tmp_path, tenant, org=RequesterOrg.OTHER, home_id=HOME)

    await _admit(db_session_factory, tmp_path, tenant, org=RequesterOrg.HOST, role=Role.ADMIN)

    assert await _role_and_marker(db_session, tenant) == (Role.USER, HOME)


async def test_a_dm_move_from_a_shared_channel_is_refused_before_history_is_read(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    from daimon.core.direct_messages import require_unsealed_source
    from daimon.core.errors import UserFacingError

    tenant = await _seed_admittable_tenant(db_session, policy=None)
    deps = _deps_for(db_session_factory, tmp_path, tenant)

    async def move(*, channel_shared: bool | None) -> Admission:
        return await admit(
            deps,
            tenant_id=tenant.id,
            platform=PLATFORM,
            external_user_id="U_MEMBER",
            channel_id=CHANNEL,
            now=_NOW,
            role=Role.USER,
            is_dm=True,
            dm_source_channel_id=CHANNEL,
            requester_org=RequesterOrg.HOST,
            channel_shared=channel_shared,
        )

    require_unsealed_source(await move(channel_shared=None))
    reported = await move(channel_shared=True)
    with pytest.raises(UserFacingError, match="shared with another organisation"):
        require_unsealed_source(reported)
    with pytest.raises(UserFacingError):
        require_unsealed_source(await move(channel_shared=None))  # now in the registry


async def test_reauthorize_holds_a_turn_admitted_unheld_once_the_channel_is_marked_shared(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    deps = _deps_for(db_session_factory, tmp_path, tenant)
    admission = await _admit(db_session_factory, tmp_path, tenant, role=Role.ADMIN)
    assert admission.hold is None
    await mark_channel_shared(
        db_session, tenant_id=tenant.id, platform=PLATFORM, channel_id=CHANNEL, source="client"
    )
    await db_session.commit()

    current = await reauthorize(deps, admission)

    assert current.hold == TurnHold(PLATFORM, CHANNEL, client=False)
    assert current.shared_channel is True
    assert current.grant is not None and current.grant.hold == current.hold
    assert current.grant.subject.is_admin is False


async def test_reauthorize_makes_a_requester_marked_foreign_since_admission_a_client(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    deps = _deps_for(db_session_factory, tmp_path, tenant)
    admission = await _admit(db_session_factory, tmp_path, tenant)
    assert admission.hold is None
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform=PLATFORM, external_id=USER
    )
    await mark_principal_foreign(db_session, principal_id=principal.id, home_id=HOME)
    await db_session.commit()

    current = await reauthorize(deps, admission)

    assert current.hold == TurnHold(PLATFORM, CHANNEL, client=True)
    assert current.grant is not None and current.grant.subject.client is True


async def test_before_send_waits_when_the_turn_was_tightened_after_its_session_was_built(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    from daimon.core.turn.admission import decide_before_send
    from daimon.core.turn.errors import SessionBusyError

    tenant = await _seed_admittable_tenant(db_session, policy=None)
    deps = _deps_for(db_session_factory, tmp_path, tenant)
    admission = await _admit(db_session_factory, tmp_path, tenant)
    await decide_before_send(deps, admission)()
    await mark_channel_shared(
        db_session, tenant_id=tenant.id, platform=PLATFORM, channel_id=CHANNEL, source="client"
    )
    await db_session.commit()

    with pytest.raises(SessionBusyError):
        await decide_before_send(deps, admission)()


async def test_reauthorize_never_relaxes_a_hold(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    deps = _deps_for(db_session_factory, tmp_path, tenant)
    admission = await _admit(db_session_factory, tmp_path, tenant, channel_shared=True)
    await db_session.execute(text("DELETE FROM shared_channels"))
    await db_session.commit()

    current = await reauthorize(deps, admission)

    assert current.hold == admission.hold and current.shared_channel is True


async def _admit_dm(deps: TurnDeps, tenant: TenantRow) -> Admission:
    return await admit(
        deps,
        tenant_id=tenant.id,
        platform=PLATFORM,
        external_user_id=USER,
        channel_id=CHANNEL,
        now=_NOW,
        role=Role.USER,
        is_dm=True,
        dm_source_channel_id=CHANNEL,
        requester_org=RequesterOrg.HOST,
    )


async def test_reauthorize_refuses_a_dm_whose_requester_was_marked_foreign_since_admission(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    deps = _deps_for(db_session_factory, tmp_path, tenant)
    admission = await _admit_dm(deps, tenant)
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform=PLATFORM, external_id=USER
    )
    await mark_principal_foreign(db_session, principal_id=principal.id, home_id=HOME)
    await db_session.commit()

    with pytest.raises(AdmissionDenied) as denied:
        await reauthorize(deps, admission)
    assert denied.value.reason == "client_requester"


async def test_reauthorize_refuses_a_dm_whose_source_channel_was_marked_shared_since_admission(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    from daimon.core.turn.errors import DmSourceSealedError

    tenant = await _seed_admittable_tenant(db_session, policy=None)
    deps = _deps_for(db_session_factory, tmp_path, tenant)
    admission = await _admit_dm(deps, tenant)
    await mark_channel_shared(
        db_session, tenant_id=tenant.id, platform=PLATFORM, channel_id=CHANNEL, source="client"
    )
    await db_session.commit()

    with pytest.raises(DmSourceSealedError):
        await reauthorize(deps, admission)


async def test_reauthorize_keeps_an_ordinary_member_dm(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    deps = _deps_for(db_session_factory, tmp_path, tenant)
    admission = await _admit_dm(deps, tenant)

    current = await reauthorize(deps, admission)

    assert current.hold is None
