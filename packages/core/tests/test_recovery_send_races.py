"""Regression: recovery adopts only a session matching the current decision, and decides again before its send."""

import asyncio
from dataclasses import replace

import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Surface, build_agent_ref, build_subject, build_turn_place
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.thread_sessions import get_thread_session_by_id
from daimon.core.turn import prepare as prep_mod
from daimon.core.turn.admission import AdmissionGrant
from daimon.core.turn.errors import AdmissionDenied, SessionBusyError, SessionPreparationFailed
from daimon.core.turn.run import _replace_dead_session, run_prepared_turn
from daimon.testing.factories import make_account, make_tenant, make_thread_session
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.turn_fakes import RecordingLifecycle
from sqlalchemy.ext.asyncio import async_sessionmaker

from .test_action_time_races import setup
from .test_session_preparation import _agent, _register
from .turn.test_run_prepared_turn import (
    _admission,
    _deps,
    _prepared_turn,
    _recovery_lifecycle,
    _router,
)


async def test_recovery_adopt_seal_present_before_recovery(db_session, db_nullpool_engine):
    tenant, account, factory, transport, deps, admitted, bind, policy = await setup(
        db_session, db_nullpool_engine
    )
    first = await bind(admitted())
    moved = _agent(model_id="claude-opus-4-6")
    _register(transport.state, moved)
    second = await bind(admitted(moved))
    assert not second.admission.memory_read_only
    assert not transport.state.sessions[second.ma_session_id].metadata.get("daimon_sealed")
    await policy("seal")
    # A stale PreparedTurn is precisely the recovery input. The existing live
    # replacement was built before the seal, so it must never be adopted as-is.
    with pytest.raises(SessionBusyError):
        await _replace_dead_session(
            deps,
            first,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-1",
            dead_session_id=first.ma_session_id,
            dead_mapping_id=first.mapping_id,
        )


async def test_recovery_adopt_already_sealed_replacement_is_allowed(db_session, db_nullpool_engine):
    tenant, account, factory, transport, deps, admitted, bind, policy = await setup(
        db_session, db_nullpool_engine
    )
    first = await bind(admitted())
    await policy("seal")
    moved = _agent(model_id="claude-opus-4-6")
    _register(transport.state, moved)
    second = await bind(admitted(moved))
    assert second.admission.memory_read_only
    async with factory() as session:
        row = await get_thread_session_by_id(session, id=second.mapping_id)
    assert row.effective_config.memory_read_only
    assert transport.state.sessions[second.ma_session_id].metadata.get("daimon_sealed") == "vault"
    recovered = await _replace_dead_session(
        deps,
        first,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-1",
        dead_session_id=first.ma_session_id,
        dead_mapping_id=first.mapping_id,
    )
    assert recovered.adopted and recovered.ma_session_id == second.ma_session_id
    assert recovered.admission.memory_read_only


@pytest.mark.parametrize("change", ["none", "pin", "seal"])
async def test_recovery_reseed_policy_change_before_send(db_session, db_nullpool_engine, change):
    db_session_factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-2",
        ma_session_id="sess_old",
    )
    await db_session.commit()
    bodies, batches = [], []
    router = _router(session_bodies=bodies, dead_session_ids={"sess_old"}, sent_batches=batches)
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = ma_agent(id="ag_1", name="daimon", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
    place = build_turn_place(channel_id="vault", thread_id="thread-2")
    grant = AdmissionGrant(
        tenant_id=tenant.id,
        subject=build_subject(is_admin=False, platform_user_id="user-1"),
        surface=Surface.CHANNEL,
        turn_place=place,
        agent=build_agent_ref(agent.name, agent.metadata, "daimon"),
        run_place=place,
        channel_id="vault",
        thread_id="thread-2",
        is_dm=False,
    )
    admission = replace(
        _admission(account_id=account.id, agent=agent, env=env),
        grant=grant,
        origin_channel_id="vault",
        origin_thread_id="thread-2",
    )
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_old",
        mapping_id=row.id,
        session_account_id=account.id,
    )

    async def reseed():
        assert len(bodies) == 1, "the replacement exists before this awaited callback"
        if change != "none":
            policy = (
                TenantAccessPolicy(agent_channel_pins={"daimon": ("elsewhere",)})
                if change == "pin"
                else TenantAccessPolicy(sealed_channel_ids=("vault",))
            )
            async with db_session_factory.begin() as session:
                await set_access_policy(session, tenant_id=tenant.id, policy=policy)
        return "reseeded member request"

    async def run():
        return await run_prepared_turn(
            deps,
            prepared,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-2",
            external_user_id="user-1",
            user_message="hello",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=reseed,
            recovery_lifecycle=_recovery_lifecycle,
            render_interval_s=0.001,
        )

    if change == "none":
        outcome = await run()
        assert outcome.recovered and outcome.state.error is None
        assert any(sid == "sess_1" for sid, _ in batches)
    else:
        with pytest.raises(AdmissionDenied if change == "pin" else SessionBusyError):
            await run()
        assert not any(sid == "sess_1" for sid, _ in batches), (change, bodies, batches)


@pytest.mark.parametrize("change", ["pin", "seal"])
@pytest.mark.parametrize("path", ["fresh", "replacement"])
async def test_policy_change_inside_create_session(
    db_session, db_nullpool_engine, monkeypatch, change, path
):
    tenant, account, factory, transport, deps, admitted, bind, policy = await setup(
        db_session, db_nullpool_engine
    )
    agent = None
    if path == "replacement":
        await bind(admitted())
        agent = _agent(model_id="claude-opus-4-6")
        _register(transport.state, agent)
    original = prep_mod.create_session
    armed = True

    async def during(*args, **kwargs):
        nonlocal armed
        if armed:
            armed = False
            await policy(change)  # lands during vault/env/memory work, before the fence
        return await original(*args, **kwargs)

    monkeypatch.setattr(prep_mod, "create_session", during)
    before = transport.creates
    with pytest.raises((AdmissionDenied, SessionBusyError, SessionPreparationFailed)) as info:
        await bind(admitted(agent))
    assert transport.creates == before, "a session was created after the change"
    if change == "pin":
        assert isinstance(info.value, AdmissionDenied), type(info.value).__name__
    else:
        # The retry must succeed sealed, without looping.
        again = await bind(admitted(agent))
        assert (
            transport.state.sessions[again.ma_session_id].metadata.get("daimon_sealed") == "vault"
        )
        assert again.admission.memory_read_only
        assert transport.creates == before + 1
        assert isinstance(info.value, SessionBusyError), (
            f"seal fence surfaced as {type(info.value).__name__}, not SessionBusyError"
        )


@pytest.mark.parametrize("change", ["pin", "seal"])
async def test_checkpoint_after_lock_wait(db_session, db_nullpool_engine, monkeypatch, change):
    """Does the checkpoint (executes the OLD session) run after a policy change
    that landed during the preparation lock wait?"""
    import asyncio

    from daimon.core import session_preparation
    from daimon.core.session_preparation import PreparedReplacement
    from daimon.core.session_preparation_stages import lock_preparation

    tenant, account, factory, transport, deps, admitted, bind, policy = await setup(
        db_session, db_nullpool_engine
    )
    await bind(admitted())
    moved = _agent(model_id="claude-opus-4-6")
    _register(transport.state, moved)
    ran = []

    async def transfer(**kwargs):
        ran.append(kwargs["old_session_id"])
        return PreparedReplacement(
            extra_resources=(), transfer_file_id=None, transfer_kind="transcript", user_prefix="x"
        )

    original = session_preparation.lock_preparation
    entered = asyncio.Event()

    async def waiting(*a, **k):
        entered.set()
        await original(*a, **k)

    monkeypatch.setattr(session_preparation, "lock_preparation", waiting)
    async with factory() as holder, holder.begin():
        await lock_preparation(
            holder,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-1",
            account_id=account.id,
        )
        task = asyncio.create_task(bind(admitted(moved), transfer))
        await asyncio.wait_for(entered.wait(), 5)
        await policy(change)
    if change == "pin":
        with pytest.raises(AdmissionDenied):
            await task
    else:
        await task
    assert not ran, "checkpoint executed the old session after the policy change"
