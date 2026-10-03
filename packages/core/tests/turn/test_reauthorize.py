"""Admission decided again at the moment the session is built (model: G_recheck_at_create).

`admit()` decides on the policy as it was; the session is built, reused,
replaced or recovered later. Each test admits a turn, changes the policy the
way an operator would in between, and asks `reauthorize` -- the check
`bind_session` and the dead-session recovery run before any session does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import Role
from daimon.core.turn.admission import AdmissionDenied, admit, reauthorize
from daimon.testing.ma import resolved_agent_env_router
from daimon.testing.ma_models import ma_agent, ma_environment
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_admission import (
    _ISOLATED,
    _NOW,
    _admit_as,
    _admittable_router,
    _answer_in,
    _deps,
    _seed_admittable_tenant,
)


async def _admit(db_session_factory, tmp_path: Path, tenant, *, role=Role.USER, is_dm=False):  # type: ignore[no-untyped-def]
    deps = _deps(
        sessionmaker=db_session_factory, defaults_root=tmp_path, router=_admittable_router(tenant)
    )
    admission = await admit(
        deps,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="member-1",
        channel_id="chan-1",
        thread_id="thr-1",
        now=_NOW,
        role=role,
        is_dm=is_dm,
    )
    return deps, admission


async def _set_policy(db_session: AsyncSession, tenant, policy: TenantAccessPolicy) -> None:  # type: ignore[no-untyped-def]
    await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
    await db_session.commit()


async def test_a_pin_added_after_admission_refuses_the_session(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    deps, admission = await _admit(db_session_factory, tmp_path, tenant)

    await _set_policy(
        db_session, tenant, TenantAccessPolicy(agent_channel_pins={"daimon": ("elsewhere",)})
    )

    with pytest.raises(AdmissionDenied) as exc_info:
        await reauthorize(deps, admission)
    assert exc_info.value.reason == "agent_pinned_elsewhere"


async def test_a_channel_protected_after_admission_refuses_the_session(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    deps, admission = await _admit(db_session_factory, tmp_path, tenant)

    await _set_policy(db_session, tenant, TenantAccessPolicy(protected_channel_ids=("chan-1",)))

    with pytest.raises(AdmissionDenied) as exc_info:
        await reauthorize(deps, admission)
    assert exc_info.value.reason == "channel_protected"


async def test_an_invoker_removed_after_admission_is_refused(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    deps, admission = await _admit(db_session_factory, tmp_path, tenant)

    await _set_policy(db_session, tenant, TenantAccessPolicy(invoker_user_ids=("someone-else",)))

    with pytest.raises(AdmissionDenied) as exc_info:
        await reauthorize(deps, admission)
    assert exc_info.value.reason == "invoker_not_allowed"


async def test_a_seal_added_after_admission_is_stamped_and_makes_memory_read_only(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    deps, admission = await _admit(db_session_factory, tmp_path, tenant)
    assert admission.origin_seal_ids == frozenset()

    await _set_policy(db_session, tenant, TenantAccessPolicy(sealed_channel_ids=("chan-1",)))

    current = await reauthorize(deps, admission)
    assert current.origin_seal_ids == frozenset({"chan-1"})
    assert current.memory_read_only
    assert current.source_sealed


async def test_an_unseal_after_admission_never_drops_a_seal_the_turn_had(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await _seed_admittable_tenant(
        db_session, policy=TenantAccessPolicy(sealed_channel_ids=("chan-1",))
    )
    deps, admission = await _admit(db_session_factory, tmp_path, tenant)

    await _set_policy(db_session, tenant, TenantAccessPolicy())

    current = await reauthorize(deps, admission)
    assert current.origin_seal_ids == frozenset({"chan-1"})


async def test_an_admin_dm_stays_exempt_and_an_unchanged_policy_returns_the_same_admission(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await _seed_admittable_tenant(
        db_session, policy=TenantAccessPolicy(agent_channel_pins={"daimon": ("elsewhere",)})
    )
    deps, admission = await _admit(
        db_session_factory, tmp_path, tenant, role=Role.ADMIN, is_dm=True
    )

    assert await reauthorize(deps, admission) is admission


async def test_bind_session_decides_again_before_any_session_is_found_or_created(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chokepoint wiring: a pin added after admit refuses inside `bind_session`,
    before `prepare_session_for_turn` runs."""
    from daimon.core import session_preparation
    from daimon.core.turn.prepare import bind_session

    tenant = await _seed_admittable_tenant(db_session, policy=None)
    deps, admission = await _admit(db_session_factory, tmp_path, tenant)
    await _set_policy(
        db_session, tenant, TenantAccessPolicy(agent_channel_pins={"daimon": ("elsewhere",)})
    )

    async def must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a refused turn must not reach session preparation")

    monkeypatch.setattr(session_preparation, "prepare_session_for_turn", must_not_run)

    with pytest.raises(AdmissionDenied):
        await bind_session(
            deps,
            admission,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="member-1",
            thread_id="thr-1",
            session_account_id=admission.account_id,
            reuse_existing=True,
        )


async def test_a_dm_source_sealed_after_admission_refuses_the_dm_session(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The DM path records its source in the grant; a seal on it since admission stops the DM."""
    from dataclasses import replace

    from daimon.core.turn.admission import DmSource
    from daimon.core.turn.errors import DmSourceSealedError

    tenant = await _seed_admittable_tenant(db_session, policy=None)
    deps, admission = await _admit(db_session_factory, tmp_path, tenant, is_dm=True)
    assert admission.grant is not None
    admission = replace(
        admission,
        grant=replace(
            admission.grant, dm_source=DmSource(channel_id="src-chan", thread_id="src-thr")
        ),
    )

    await _set_policy(db_session, tenant, TenantAccessPolicy(sealed_channel_ids=("src-chan",)))

    with pytest.raises(DmSourceSealedError):
        await reauthorize(deps, admission)


async def test_an_isolation_added_after_admission_refuses_a_shared_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    deps, admission = await _admit(db_session_factory, tmp_path, tenant)

    await _set_policy(
        db_session,
        tenant,
        TenantAccessPolicy(
            sealed_channel_ids=("chan-1",),
            isolated_channel_ids=("chan-1",),
            agent_channel_pins={"local": ("chan-1",)},
        ),
    )

    with pytest.raises(AdmissionDenied) as exc_info:
        await reauthorize(deps, admission)
    assert exc_info.value.reason == "channel_isolated"


async def test_an_isolated_channels_own_agent_keeps_its_memory_writable(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    isolated = TenantAccessPolicy(
        sealed_channel_ids=("chan-1",),
        isolated_channel_ids=("chan-1",),
        agent_channel_pins={"daimon": ("chan-1",)},
    )
    tenant = await _seed_admittable_tenant(db_session, policy=isolated)
    deps, admission = await _admit(db_session_factory, tmp_path, tenant)
    assert admission.source_sealed and not admission.memory_read_only

    current = await reauthorize(deps, admission)
    assert current.source_sealed and not current.memory_read_only


async def test_an_external_participant_is_refused_once_their_channel_is_no_longer_isolated(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=_ISOLATED)
    await _answer_in(db_session, tenant, "chan-1", "own")
    admission = await _admit_as(db_session_factory, tmp_path, tenant, is_external=True)
    router = resolved_agent_env_router(
        ma_agent(id="ag_own", name="own", tenant_id=tenant.id),
        ma_environment(id="env_1", name="default", tenant_id=tenant.id),
    )
    deps = _deps(sessionmaker=db_session_factory, defaults_root=tmp_path, router=router)

    await _set_policy(db_session, tenant, _ISOLATED.model_copy(update={"isolated_channel_ids": ()}))

    with pytest.raises(AdmissionDenied) as exc_info:
        await reauthorize(deps, admission)
    assert exc_info.value.reason == "external_participant"
