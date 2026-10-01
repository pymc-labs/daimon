"""Regression: a policy change during a wait after a re-check applies before the effect."""

import asyncio
from dataclasses import replace

import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Surface, build_agent_ref, build_subject, build_turn_place
from daimon.core.session_preparation import PreparedReplacement
from daimon.core.session_preparation_stages import lock_preparation
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.turn.admission import AdmissionDenied, AdmissionGrant
from daimon.core.turn.errors import SessionBusyError
from daimon.core.turn.prepare import bind_session
from daimon.core.turn.run import _replace_dead_session
from sqlalchemy.ext.asyncio import async_sessionmaker

from .test_session_preparation import _NOW, _admission, _agent, _deps, _register, _Transport


async def setup(db_session, db_nullpool_engine):
    from daimon.testing.factories import make_account, make_tenant

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    transport = _Transport()
    deps = _deps(factory, transport)

    def admitted(agent=None):
        agent = agent or _agent()
        place = build_turn_place(channel_id="vault", thread_id="thread-1")
        grant = AdmissionGrant(
            tenant_id=tenant.id,
            subject=build_subject(is_admin=False, platform_user_id="user-1"),
            surface=Surface.CHANNEL,
            turn_place=place,
            agent=build_agent_ref(agent.name, agent.metadata, "daimon"),
            run_place=place,
            channel_id="vault",
            thread_id="thread-1",
            is_dm=False,
        )
        return replace(
            _admission(account=account, agent=agent),
            origin_channel_id="vault",
            origin_thread_id="thread-1",
            grant=grant,
        )

    async def no_transfer(**kwargs):
        return PreparedReplacement(
            extra_resources=(),
            transfer_file_id=None,
            transfer_kind="transcript",
            user_prefix="previous work",
        )

    async def bind(admission, transfer=no_transfer):
        return await bind_session(
            deps,
            admission,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            thread_id="thread-1",
            session_account_id=account.id,
            reuse_existing=True,
            transfer=transfer,
            now=lambda: _NOW,
        )

    async def policy(change):
        value = (
            TenantAccessPolicy(agent_channel_pins={"daimon": ("elsewhere",)})
            if change == "pin"
            else TenantAccessPolicy(sealed_channel_ids=("vault",))
        )
        async with factory.begin() as session:
            await set_access_policy(session, tenant_id=tenant.id, policy=value)

    return tenant, account, factory, transport, deps, admitted, bind, policy


@pytest.mark.parametrize("change", ["pin", "seal"])
async def test_successor_policy_change_during_predecessor_fetch(
    db_session, db_nullpool_engine, change
):
    tenant, account, factory, transport, deps, admitted, bind, policy = await setup(
        db_session, db_nullpool_engine
    )
    first = await bind(admitted())
    moved = _agent(model_id="claude-opus-4-6")
    _register(transport.state, moved)
    armed = False

    async def transfer(**kwargs):
        nonlocal armed
        armed = True
        return PreparedReplacement(
            extra_resources=(),
            transfer_file_id=None,
            transfer_kind="transcript",
            user_prefix="previous work",
        )

    inner = deps.anthropic._client._transport

    async def changing(req):
        nonlocal armed
        if armed and req.method == "GET" and req.url.path == f"/v1/sessions/{first.ma_session_id}":
            armed = False
            await policy(change)
        return await inner.handle_async_request(req)

    import httpx
    from anthropic import AsyncAnthropic

    object.__setattr__(
        deps,
        "anthropic",
        AsyncAnthropic(
            api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(changing))
        ),
    )
    before = transport.creates
    if change == "pin":
        with pytest.raises(AdmissionDenied):
            await bind(admitted(moved), transfer)
        assert transport.creates == before
    else:
        second = await bind(admitted(moved), transfer)
        assert second.admission.memory_read_only, (
            second.admission.memory_read_only,
            transport.state.sessions[second.ma_session_id].metadata,
        )
        assert (
            transport.state.sessions[second.ma_session_id].metadata.get("daimon_sealed") == "vault"
        )


@pytest.mark.parametrize("change", ["pin", "seal"])
async def test_reuse_after_real_preparation_lock_wait(
    db_session, db_nullpool_engine, monkeypatch, change
):
    tenant, account, factory, transport, deps, admitted, bind, policy = await setup(
        db_session, db_nullpool_engine
    )
    await bind(admitted())
    from daimon.core import session_preparation

    original = session_preparation.lock_preparation
    entered = asyncio.Event()

    async def waiting(*args, **kwargs):
        entered.set()
        await original(*args, **kwargs)

    monkeypatch.setattr(session_preparation, "lock_preparation", waiting)
    async with factory() as holder, holder.begin():
        await lock_preparation(
            holder,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-1",
            account_id=account.id,
        )
        task = asyncio.create_task(bind(admitted()))
        await asyncio.wait_for(entered.wait(), 5)
        await policy(change)
        assert not task.done()
    if change == "pin":
        with pytest.raises(AdmissionDenied):
            await task
        return
    # Decided again inside the lock: the writable session is not reused, and the
    # turn gets a read-only, sealed replacement.
    bound = await task
    assert bound.admission.memory_read_only
    assert not bound.reused
    assert transport.state.sessions[bound.ma_session_id].metadata.get("daimon_sealed") == "vault"


@pytest.mark.parametrize("change", ["pin", "seal"])
async def test_recovery_adopt_rechecks_after_real_preparation_lock_wait(
    db_session, db_nullpool_engine, monkeypatch, change
):
    tenant, account, factory, transport, deps, admitted, bind, policy = await setup(
        db_session, db_nullpool_engine
    )
    first = await bind(admitted())
    moved = _agent(model_id="claude-opus-4-6")
    _register(transport.state, moved)
    second = await bind(admitted(moved))
    assert second.ma_session_id != first.ma_session_id
    from daimon.core.turn import run

    original = run.lock_preparation
    entered = asyncio.Event()

    async def waiting(*args, **kwargs):
        entered.set()
        await original(*args, **kwargs)

    monkeypatch.setattr(run, "lock_preparation", waiting)
    async with factory() as holder, holder.begin():
        await lock_preparation(
            holder,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-1",
            account_id=account.id,
        )
        task = asyncio.create_task(
            _replace_dead_session(
                deps,
                first,
                tenant_id=tenant.id,
                platform="discord",
                thread_id="thread-1",
                dead_session_id=first.ma_session_id,
                dead_mapping_id=first.mapping_id,
            )
        )
        await asyncio.wait_for(entered.wait(), 5)
        await policy(change)
        assert not task.done()
    with pytest.raises(AdmissionDenied if change == "pin" else SessionBusyError):
        await task


@pytest.mark.parametrize("change", ["pin", "seal"])
async def test_recovery_create_rechecks_after_transcript_replay(
    db_session, db_nullpool_engine, monkeypatch, change
):
    tenant, account, factory, transport, deps, admitted, bind, policy = await setup(
        db_session, db_nullpool_engine
    )
    first = await bind(admitted())
    from daimon.core.turn import run

    async def replay(*args, **kwargs):
        await policy(change)
        return None

    monkeypatch.setattr(run, "_replay_previous_session", replay)
    before = transport.creates

    async def recover():
        return await _replace_dead_session(
            deps,
            first,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-1",
            dead_session_id=first.ma_session_id,
            dead_mapping_id=first.mapping_id,
        )

    if change == "pin":
        with pytest.raises(AdmissionDenied):
            await recover()
        assert transport.creates == before
    else:
        recovered = await recover()
        assert (
            transport.state.sessions[recovered.ma_session_id].metadata.get("daimon_sealed")
            == "vault"
        )


@pytest.mark.parametrize("change", ["pin", "seal"])
async def test_create_rechecks_right_before_sessions_create(
    db_session, db_nullpool_engine, monkeypatch, change
):
    tenant, account, factory, transport, deps, admitted, bind, policy = await setup(
        db_session, db_nullpool_engine
    )
    from daimon.core.turn import prepare

    original = prepare._env_bytes_sha256

    async def env_hash_then_change(*args, **kwargs):
        result = await original(*args, **kwargs)
        await policy(change)
        return result

    monkeypatch.setattr(prepare, "_env_bytes_sha256", env_hash_then_change)
    before = transport.creates
    with pytest.raises(AdmissionDenied if change == "pin" else SessionBusyError):
        await bind(admitted())
    assert transport.creates == before


async def test_dm_source_sealed_during_replacement_is_not_a_preparation_failure(
    db_session, db_nullpool_engine
):
    from daimon.core.turn.admission import DmSource
    from daimon.core.turn.errors import DmSourceSealedError

    tenant, account, factory, transport, deps, admitted, bind, policy = await setup(
        db_session, db_nullpool_engine
    )

    def from_dm(agent=None):
        admission = admitted(agent)
        assert admission.grant is not None
        grant = replace(admission.grant, dm_source=DmSource(channel_id="source", thread_id=None))
        return replace(admission, grant=grant)

    await bind(from_dm())
    moved = _agent(model_id="claude-opus-4-6")
    _register(transport.state, moved)
    before = transport.creates

    async def seal_source_during_transfer(**kwargs):
        async with factory.begin() as session:
            await set_access_policy(
                session,
                tenant_id=tenant.id,
                policy=TenantAccessPolicy(sealed_channel_ids=("source",)),
            )
        return PreparedReplacement(
            extra_resources=(),
            transfer_file_id=None,
            transfer_kind="transcript",
            user_prefix="previous work",
        )

    with pytest.raises(DmSourceSealedError):
        await bind(from_dm(moved), seal_source_during_transfer)
    assert transport.creates == before
