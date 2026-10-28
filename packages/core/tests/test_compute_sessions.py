"""Compute lifecycle invariants, all external actions use the fake provider."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from daimon.core.compute_fly import FlyComputeProvider
from daimon.core.compute_models import ComputeResource
from daimon.core.compute_provider import ComputeProviderError, app_name
from daimon.core.compute_sessions import ComputeCaller, ComputeSessions
from daimon.core.config import ComputeProfile, ComputeSettings
from daimon.core.errors import StoreError
from daimon.core.stores.domain import Role
from daimon.core.stores.task_continuations import list_pending_continuations
from daimon.core.stores.turn_origins import bind_origin_session, create_origin, get_active_origin
from daimon.testing.compute import FakeComputeProvider
from daimon.testing.factories import make_account, make_tenant
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker

NOW = datetime(2026, 10, 28, tzinfo=UTC)


def settings(**kwargs):
    return ComputeSettings(
        enabled=True,
        deployment_id="test-deployment",
        api_token=SecretStr("fly-secret-test-only"),
        org_slug="test-org",
        session_auth_key=SecretStr("session-secret-test-only"),
        profiles={"mmm": ComputeProfile(image="registry.example/mmm@sha256:abc")},
        **kwargs,
    )


async def caller(sm, *, tenant=None, account=None, attended=True, human=True):
    async with sm.begin() as db:
        tenant = tenant or await make_tenant(db)
        account = account or await make_account(db, tenant=tenant)
        origin = await create_origin(
            db,
            tenant_id=tenant.id,
            account_id=account.id,
            platform="discord",
            parent_channel_id="parent",
            thread_id="thread",
            responder_ma_agent_id="agent",
            responder_name="Daimon",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.USER,
            expires_at=NOW + timedelta(days=30),
            now=NOW,
            sender_foreign=False,
            shared_channel=False,
            attended=attended,
        )
        await bind_origin_session(
            db, origin_id=origin.id, chat_session_id=str(origin.id), human_invocation=human
        )
        origin = await get_active_origin(
            db,
            tenant_id=tenant.id,
            account_id=account.id,
            origin_id=origin.id,
            platform="discord",
            now=NOW,
        )
    assert origin is not None
    return ComputeCaller(tenant.id, account.id, origin, str(origin.id), "user"), tenant, account


async def launch(service, owner, key="approved"):
    return await service.launch(
        owner, profile="mmm", region="iad", ttl_hours=24, approval_key=key, source_metadata={}
    )


async def test_owner_tenant_origin_and_machine_token_isolation(db_engine, db_clean):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    owner, tenant, _ = await caller(sm)
    other, _, _ = await caller(sm, tenant=tenant)
    foreign, _, _ = await caller(sm)
    fake = FakeComputeProvider()
    service = ComputeSessions(sm, settings(), fake, clock=lambda: NOW)
    session = await launch(service, owner)
    assert await launch(service, owner) == session
    assert len(fake.resources) == 1
    for stranger in (
        other,
        foreign,
        replace(owner, chat_session_id="forged"),
        replace(owner, origin=owner.origin.model_copy(update={"id": uuid.uuid4()})),
    ):
        for operation in (
            service.get(stranger, session.id),
            service.stop(stranger, session.id),
            service.call(stranger, session.id, tool="write_file", arguments={}),
            service.extend(stranger, session.id, hours=1, approval_key="x"),
        ):
            with pytest.raises(StoreError):
                await operation
    assert await service.list(other) == []
    assert await service.list(foreign) == []
    env = fake.machine_env[session.id]
    assert set(env) == {"MCP_AUTH_TOKEN"}
    assert env["MCP_AUTH_TOKEN"] not in {"fly-secret-test-only", "session-secret-test-only"}
    assert "secret-test" not in session.model_dump_json()
    admin = replace(other, admin=True)
    with pytest.raises(StoreError):
        await service.get(admin, session.id)
    with pytest.raises(StoreError):
        await service.extend(admin, session.id, hours=1, approval_key="admin-extend")
    with pytest.raises(StoreError):
        await service.stop(replace(foreign, admin=True), session.id)
    assert (await service.stop(admin, session.id)).state == "deleted"
    assert fake.deleted == [session.id]


@pytest.mark.parametrize("attended,human", [(False, True), (True, False), (True, None)])
async def test_unattended_and_worker_origins_cannot_launch(db_engine, db_clean, attended, human):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    owner, _, _ = await caller(sm, attended=attended, human=human)
    fake = FakeComputeProvider()
    service = ComputeSessions(sm, settings(), fake, clock=lambda: NOW)
    with pytest.raises(StoreError):
        await launch(service, owner)
    assert fake.resources == {}


async def test_caps_ttl_disabled_and_expiry(db_engine, db_clean):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    owner, _, _ = await caller(sm)
    fake = FakeComputeProvider()
    now = NOW
    service = ComputeSessions(
        sm, settings(per_person_sessions=1, max_ttl_hours=24), fake, clock=lambda: now
    )
    for hours in (0, 25):
        with pytest.raises(StoreError):
            await service.launch(
                owner,
                profile="mmm",
                region="iad",
                ttl_hours=hours,
                approval_key="approved",
                source_metadata={},
            )
    session = await launch(service, owner)
    with pytest.raises(StoreError, match="limit"):
        await launch(service, owner, "second")
    with pytest.raises(StoreError):
        await service.extend(owner, session.id, hours=25, approval_key="extend")
    with pytest.raises(StoreError):
        await launch(
            ComputeSessions(sm, settings().model_copy(update={"enabled": False}), fake), owner
        )
    now += timedelta(hours=23)
    await service.call(owner, session.id, tool="read_file", arguments={"path": "/output"})
    assert (await service.get(owner, session.id)).expires_at == session.expires_at
    await service.call(
        owner, session.id, tool="write_file", arguments={"path": "/input", "content": "ok"}
    )
    renewed = await service.get(owner, session.id)
    assert renewed.expires_at == now + timedelta(hours=24)
    extended = await service.extend(owner, session.id, hours=2, approval_key="extend")
    now += timedelta(hours=1)
    assert await service.extend(owner, session.id, hours=2, approval_key="extend") == extended
    now += timedelta(hours=2)
    with pytest.raises(StoreError, match="expired"):
        await service.call(owner, session.id, tool="run_bash", arguments={"command": "true"})
    await service.reconcile()
    assert (await service.get(owner, session.id)).state == "deleted"


async def test_partial_create_restart_warning_delete_retry_and_orphans(db_engine, db_clean):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    owner, _, _ = await caller(sm)
    fake = FakeComputeProvider()
    now = NOW
    service = ComputeSessions(sm, settings(), fake, clock=lambda: now)
    fake.fail_create = True
    with pytest.raises(ComputeProviderError):
        await launch(service, owner)
    [session] = await service.list(owner)
    assert session.state == "launching" and len(fake.resources) == 1
    fake.fail_create = False
    service = ComputeSessions(sm, settings(), fake, clock=lambda: now)
    await service.reconcile()
    assert (await service.get(owner, session.id)).state == "running"
    now += timedelta(hours=23, minutes=30)
    await service.reconcile()
    await service.reconcile()
    async with sm.begin() as db:
        wakes = await list_pending_continuations(
            db,
            tenant_id=owner.tenant_id,
            platform="discord",
            thread_id="thread",
            include_future=True,
        )
    assert len(wakes) == 1
    assert str(session.id) in wakes[0].requested_work
    now += timedelta(hours=1)
    fake.fail_delete = True
    await service.reconcile()
    assert (await service.get(owner, session.id)).state == "stopping"
    with pytest.raises(StoreError):
        await service.extend(owner, session.id, hours=1, approval_key="late")
    fake.fail_delete = False
    await service.reconcile()
    assert (await service.get(owner, session.id)).state == "deleted"
    orphan_id, foreign_id = uuid.uuid4(), uuid.uuid4()
    fake.resources[orphan_id] = ComputeResource(
        session_id=orphan_id,
        deployment_id="test-deployment",
        app_name=app_name("test-deployment", orphan_id),
    )
    fake.resources[foreign_id] = ComputeResource(
        session_id=foreign_id, deployment_id="other", app_name=app_name("other", foreign_id)
    )
    await service.reconcile()
    assert orphan_id not in fake.resources and foreign_id in fake.resources


async def test_renew_and_delete_are_serialized(db_committing_engine, db_clean):
    sm = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    owner, _, _ = await caller(sm)
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingProvider(FakeComputeProvider):
        async def call(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return await super().call(*args, **kwargs)

    fake = BlockingProvider()
    service = ComputeSessions(sm, settings(), fake, clock=lambda: NOW)
    session = await launch(service, owner)
    write = asyncio.create_task(service.call(owner, session.id, tool="write_file", arguments={}))
    await asyncio.wait_for(entered.wait(), 5)
    stop = asyncio.create_task(service.stop(owner, session.id))
    await asyncio.sleep(0)
    assert not stop.done()
    release.set()
    await write
    assert (await stop).state == "deleted"
    assert session.id not in fake.resources


async def test_provider_never_exposes_remote_errors_or_cross_deployment_calls():
    seen = []

    async def handler(request):
        seen.append(request)
        return httpx.Response(500, text="fly-secret-test-only session-secret-test-only")

    provider = FlyComputeProvider(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(ComputeProviderError) as error:
        await provider.inventory()
    assert "secret-test" not in str(error.value)
    assert "secret-test" not in repr(error.value)
    before = len(seen)
    other_id = uuid.uuid4()
    with pytest.raises(ComputeProviderError):
        await provider.delete(
            ComputeResource(
                session_id=other_id, deployment_id="other", app_name=app_name("other", other_id)
            )
        )
    assert len(seen) == before


async def test_concurrent_admission_cannot_exceed_the_person_cap(db_committing_engine, db_clean):
    sm = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    owner, _, _ = await caller(sm)
    fake = FakeComputeProvider()
    service = ComputeSessions(sm, settings(per_person_sessions=1), fake, clock=lambda: NOW)
    outcomes = await asyncio.gather(
        launch(service, owner, "one"), launch(service, owner, "two"), return_exceptions=True
    )
    assert sum(isinstance(result, StoreError) for result in outcomes) == 1
    assert len(fake.resources) == 1


async def test_reaper_skips_an_active_write_then_observes_its_renewal(
    db_committing_engine, db_clean
):
    sm = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    owner, _, _ = await caller(sm)
    entered, release = asyncio.Event(), asyncio.Event()
    now = NOW

    class BlockingProvider(FakeComputeProvider):
        async def call(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return await super().call(*args, **kwargs)

    fake = BlockingProvider()
    service = ComputeSessions(sm, settings(), fake, clock=lambda: now)
    session = await launch(service, owner)
    now += timedelta(hours=23, minutes=59)
    write = asyncio.create_task(service.call(owner, session.id, tool="write_file", arguments={}))
    await asyncio.wait_for(entered.wait(), 5)
    now += timedelta(minutes=2)
    try:
        await asyncio.wait_for(service.reconcile(), 5)
        assert fake.deleted == []
    finally:
        release.set()
        await write
    await service.reconcile()
    assert (await service.get(owner, session.id)).expires_at == now + timedelta(hours=24)
    assert session.id in fake.resources


@pytest.mark.parametrize("error", [ComputeProviderError, TimeoutError, RuntimeError])
async def test_inventory_failure_does_not_disable_known_session_cleanup(db_engine, db_clean, error):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    owner, _, _ = await caller(sm)

    class ListingOutage(FakeComputeProvider):
        async def inventory(self):
            raise error("Provider listing unavailable")

    fake = ListingOutage()
    now = NOW
    service = ComputeSessions(sm, settings(), fake, clock=lambda: now)
    session = await launch(service, owner)
    now += timedelta(days=2)
    await service.reconcile()
    assert (await service.get(owner, session.id)).state == "deleted"


async def test_disabling_launch_cleans_partial_creation_without_starting_a_machine(
    db_engine, db_clean
):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    owner, _, _ = await caller(sm)
    fake = FakeComputeProvider()
    fake.fail_create = True
    service = ComputeSessions(sm, settings(), fake, clock=lambda: NOW)
    with pytest.raises(ComputeProviderError):
        await launch(service, owner)
    [session] = await service.list(owner)
    assert session.state == "launching" and fake.machine_env == {}
    fake.fail_create = False
    disabled = ComputeSessions(
        sm, settings().model_copy(update={"enabled": False}), fake, clock=lambda: NOW
    )
    await disabled.reconcile()
    assert (await disabled.get(owner, session.id)).state == "deleted"
    assert fake.resources == {} and fake.machine_env == {}


async def test_reconcile_isolates_unexpected_errors_and_still_deletes_orphans(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from daimon.core import compute_sessions as module

    @asynccontextmanager
    async def transaction():
        yield None

    sm = SimpleNamespace(begin=transaction)
    fake = FakeComputeProvider()
    orphan_id = uuid.uuid4()
    fake.resources[orphan_id] = ComputeResource(
        session_id=orphan_id,
        deployment_id="test-deployment",
        app_name=app_name("test-deployment", orphan_id),
    )
    tenant = SimpleNamespace(id=uuid.uuid4(), archived_at=None)
    rows = [SimpleNamespace(id=uuid.uuid4(), deployment_id="test-deployment") for _ in range(2)]
    monkeypatch.setattr(module, "list_tenants_by_platform", AsyncMock(return_value=[tenant]))
    listing = AsyncMock(return_value=rows)
    monkeypatch.setattr(module.store, "list_sessions", listing)
    service = ComputeSessions(sm, settings(), fake)
    reconcile = AsyncMock(side_effect=[RuntimeError("remote-secret-body"), None])
    monkeypatch.setattr(service, "_reconcile_one", reconcile)
    log = Mock()
    monkeypatch.setattr(module, "log", log)
    await service.reconcile()
    listing.assert_awaited_once_with(None, tenant.id, include_deleted=False)
    assert reconcile.await_count == 2
    assert fake.resources == {}
    log.warning.assert_called_once_with("compute.reconcile_retry", session_id=str(rows[0].id))


async def test_deleted_registrations_are_excluded_but_lingering_apps_are_reaped(
    db_engine, db_clean
):
    from unittest.mock import AsyncMock

    from daimon.core.stores import compute_sessions as store

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    owner, tenant, _ = await caller(sm)
    fake = FakeComputeProvider()
    service = ComputeSessions(sm, settings(), fake, clock=lambda: NOW)
    session = await launch(service, owner)
    resource = fake.resources[session.id]
    await service.stop(owner, session.id)
    async with sm.begin() as db:
        assert await store.list_sessions(db, tenant.id, include_deleted=False) == []
        assert len(await store.list_sessions(db, tenant.id)) == 1
    # A resource left over after confirmed deletion is an orphan, not an active row.
    fake.resources[session.id] = resource
    service._reconcile_one = AsyncMock()
    await service.reconcile()
    service._reconcile_one.assert_not_awaited()
    assert fake.resources == {}


async def test_account_erasure_removes_registration_and_reaps_the_machine(db_engine, db_clean):
    from daimon.core.stores import compute_sessions as store
    from daimon.core.stores.accounts import delete_account

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    owner, tenant, _ = await caller(sm)
    fake = FakeComputeProvider()
    service = ComputeSessions(sm, settings(), fake, clock=lambda: NOW)
    session = await launch(service, owner)
    async with sm.begin() as db:
        await delete_account(db, account_id=owner.account_id)
    async with sm.begin() as db:
        assert await store.list_sessions(db, tenant.id) == []
    with pytest.raises(StoreError):
        await service.get(owner, session.id)
    await service.reconcile()
    assert fake.resources == {}
