"""Admission decided again at the moment the session is built (model: G_recheck_at_create).

`admit()` decides on the policy as it was; the session is built, reused,
replaced or recovered later. Each test admits a turn, changes the policy the
way an operator would in between, and asks `reauthorize` -- the check
`bind_session` and the dead-session recovery run before any session does.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import Role
from daimon.core.turn.admission import AdmissionDenied, admit, reauthorize
from daimon.core.turn_hold import RequesterOrg
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_admission import (
    _NOW,
    _admittable_router,
    _deps,
    _seed_admittable_tenant,
)


async def _admit(db_session_factory, tmp_path: Path, tenant, *, role=Role.USER, is_dm=False):  # type: ignore[no-untyped-def]
    if is_dm:
        from daimon.core.stores.direct_messages import set_dm_enabled

        async with db_session_factory.begin() as session:
            await set_dm_enabled(session, tenant_id=tenant.id, enabled=True)
    from daimon.core.membership import CachedMembershipResolver, Membership

    async def member(*_args):
        return Membership(is_member=True, is_admin=role is Role.ADMIN)

    deps = _deps(
        sessionmaker=db_session_factory, defaults_root=tmp_path, router=_admittable_router(tenant)
    )
    from dataclasses import replace

    deps = replace(deps, membership_resolver=CachedMembershipResolver(member))
    admission = await admit(
        deps,
        tenant_id=tenant.id,
        requester_org=RequesterOrg.HOST,
        platform="discord",
        external_user_id="member-1",
        channel_id="chan-1",
        thread_id="thr-1",
        now=_NOW,
        role=role,
        is_dm=is_dm,
        verified_membership=True,
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
    assert exc_info.value.reason == "runs_elsewhere"


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
    assert exc_info.value.reason == "writers_none"


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

    assert await reauthorize(deps, admission) == admission


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
    assert exc_info.value.reason == "own_agents_only"


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


async def test_role_removed_after_admission_refuses_reauthorization(
    db_session, db_session_factory, tmp_path
):  # type: ignore[no-untyped-def]
    from dataclasses import replace

    from daimon.core.membership import CachedMembershipResolver, Membership

    tenant = await _seed_admittable_tenant(
        db_session, policy=TenantAccessPolicy(invoker_role_ids=("123",))
    )
    facts = [Membership(is_member=True, role_ids=("123",))]

    async def fetch(tenant_id, guild_id, user_id):  # type: ignore[no-untyped-def]
        return facts[0]

    resolver = CachedMembershipResolver(fetch)
    deps = replace(
        _deps(
            sessionmaker=db_session_factory,
            defaults_root=tmp_path,
            router=_admittable_router(tenant),
        ),
        membership_resolver=resolver,
    )
    admission = await admit(
        deps,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="member-1",
        channel_id="chan-1",
        now=_NOW,
        requester_org=RequesterOrg.HOST,
    )
    facts[0] = Membership(is_member=True)
    resolver.invalidate(tenant.id, tenant.external_id, "member-1")
    with pytest.raises(AdmissionDenied, match="invoker_not_allowed"):
        await reauthorize(deps, admission)


async def test_concurrent_role_admissions_do_not_need_nested_pool_connections(
    db_session, db_engine, tmp_path
):  # type: ignore[no-untyped-def]
    import asyncio
    from dataclasses import replace

    from daimon.core.membership import CachedMembershipResolver, Membership

    tenant = await _seed_admittable_tenant(
        db_session, policy=TenantAccessPolicy(invoker_role_ids=("123",))
    )

    async def fetch(tenant_id, guild_id, user_id):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0)
        return Membership(is_member=True, role_ids=("123",))

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    deps = replace(
        _deps(
            sessionmaker=factory,
            defaults_root=tmp_path,
            router=_admittable_router(tenant),
        ),
        membership_resolver=CachedMembershipResolver(fetch),
    )
    async with asyncio.timeout(10):
        admissions = await asyncio.gather(
            *(
                admit(
                    deps,
                    tenant_id=tenant.id,
                    platform="discord",
                    external_user_id=user,
                    channel_id="chan-1",
                    now=_NOW,
                    requester_org=RequesterOrg.HOST,
                )
                for user in ("one", "two")
            )
        )
    assert len(admissions) == 2


async def test_native_private_surface_does_not_inherit_conversation_opt_in(
    db_session, db_session_factory, tmp_path
):
    from daimon.core.stores.direct_messages import set_dm_enabled

    tenant = await _seed_admittable_tenant(db_session, policy=None)
    deps, admission = await _admit(db_session_factory, tmp_path, tenant, is_dm=True)
    assert not admission.grant.cold_dm and admission.grant.dm_source is None
    async with db_session_factory.begin() as session:
        await set_dm_enabled(session, tenant_id=tenant.id, enabled=False)
    assert await reauthorize(deps, admission) == admission


async def test_admin_bypass_disabled_after_admission(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    policy = TenantAccessPolicy(invoker_user_ids=("someone-else",))
    tenant = await _seed_admittable_tenant(db_session, policy=policy)
    deps, admission = await _admit(db_session_factory, tmp_path, tenant, role=Role.ADMIN)
    await _set_policy(db_session, tenant, policy.model_copy(update={"invoker_admin_bypass": False}))
    with pytest.raises(AdmissionDenied, match="invoker_not_allowed"):
        await reauthorize(deps, admission)


@pytest.mark.parametrize("is_admin", [False, True])
@pytest.mark.parametrize("checkpoint", ["continuation", "queued_send", "tool_recheck"])
async def test_agent_role_revoked_without_cache_invalidation_denies_every_recheck(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    is_admin: bool,
    checkpoint: str,
) -> None:
    from dataclasses import replace

    from daimon.core.access_policy import AgentRule
    from daimon.core.membership import CachedMembershipResolver, Membership
    from daimon.core.turn.admission import decide_before_send

    tenant = await _seed_admittable_tenant(
        db_session,
        policy=TenantAccessPolicy(agent_rules={"daimon": AgentRule(invoker_role_ids=("123",))}),
    )
    facts = [Membership(is_member=True, role_ids=("123",), is_admin=is_admin)]
    fresh: list[bool] = []

    class Resolver(CachedMembershipResolver):
        async def resolve(
            self,
            tenant_id: UUID,
            guild_id: str,
            user_id: str,
            *,
            require_fresh: bool = False,
        ) -> Membership:
            fresh.append(require_fresh)
            return await super().resolve(tenant_id, guild_id, user_id, require_fresh=require_fresh)

    async def fetch(*_args: object) -> Membership:
        return facts[0]

    deps = replace(
        _deps(
            sessionmaker=db_session_factory,
            defaults_root=tmp_path,
            router=_admittable_router(tenant),
        ),
        membership_resolver=Resolver(fetch),
    )
    admission = await admit(
        deps,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="member-1",
        channel_id="chan-1",
        thread_id="thr-1",
        now=_NOW,
        requester_org=RequesterOrg.HOST,
    )
    assert admission.grant is not None
    assert admission.grant.subject.role_ids == ("123",)
    facts[0] = Membership(is_member=True, is_admin=is_admin)
    with pytest.raises(AdmissionDenied, match="invoker_not_allowed"):
        if checkpoint == "queued_send":
            await decide_before_send(deps, admission)()
        elif checkpoint == "continuation":
            await admit(
                deps,
                tenant_id=tenant.id,
                platform="discord",
                external_user_id="member-1",
                channel_id="chan-1",
                thread_id="thr-1",
                now=_NOW,
                requester_org=RequesterOrg.HOST,
            )
        else:
            await reauthorize(deps, admission)
    assert fresh[-1] is True


async def test_agent_role_gate_denies_missing_resolver_on_new_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    from daimon.core.access_policy import AgentRule

    tenant = await _seed_admittable_tenant(
        db_session,
        policy=TenantAccessPolicy(agent_rules={"daimon": AgentRule(invoker_role_ids=("123",))}),
    )
    deps = _deps(
        sessionmaker=db_session_factory, defaults_root=tmp_path, router=_admittable_router(tenant)
    )
    with pytest.raises(AdmissionDenied, match="invoker_not_allowed"):
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="member-1",
            channel_id="chan-1",
            now=_NOW,
            requester_org=RequesterOrg.HOST,
            role=Role.ADMIN,
            role_ids=("123",),
            verified_membership=True,
        )


@pytest.mark.parametrize(
    "origin,attended",
    [
        ("chat", False),  # timer continuation retains the chat rendering origin
        ("watch_fired", True),
        ("job_finished", True),
        ("worker_finished", True),
        ("routine", True),
        ("long_job", True),
        ("cli", True),
        ("headless", True),
        ("new_wake_reason", True),
    ],
)
async def test_nonhuman_origins_refuse_gated_agent_at_admission(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    origin: str,
    attended: bool,
) -> None:
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from daimon.core.access_policy import AgentRule
    from daimon.core.membership import CachedMembershipResolver, Membership

    tenant = await _seed_admittable_tenant(db_session, policy=None)
    deps, _ = await _admit(db_session_factory, tmp_path, tenant)
    deps = replace(
        deps,
        membership_resolver=CachedMembershipResolver(
            AsyncMock(return_value=Membership(is_member=True, role_ids=("123",))),
        ),
    )
    await _set_policy(
        db_session,
        tenant,
        TenantAccessPolicy(agent_rules={"daimon": AgentRule(invoker_role_ids=("123",))}),
    )
    with pytest.raises(AdmissionDenied, match="invoker_not_allowed"):
        await admit(
            deps,
            tenant_id=tenant.id,
            requester_org=RequesterOrg.HOST,
            platform="discord",
            external_user_id="member-1",
            channel_id="chan-1",
            thread_id="thr-1",
            now=_NOW,
            origin=origin,
            attended=attended,
        )
