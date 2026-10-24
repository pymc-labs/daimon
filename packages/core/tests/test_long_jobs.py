"""Durability, ownership and budget contracts with a fake worker, never MA I/O."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from daimon.core._models import LongJobPolicyHistory, TaskContinuation, Tenant
from daimon.core.authz import Subject
from daimon.core.config import LongJobsSettings
from daimon.core.errors import StoreError
from daimon.core.long_job_models import LongJob, LongJobManifest, LongJobPolicy
from daimon.core.long_jobs import LongJobExecutor, WorkerVanished, require_admission
from daimon.core.long_jobs_ma import worker_prompt
from daimon.core.stores import long_jobs as store
from daimon.core.stores.task_continuations import list_pending_continuations
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)
LIMITS = LongJobsSettings()
ADMIN = Subject(is_admin=True, platform_user_id="admin")


async def seed(sm, *, policy=True):
    async with sm.begin() as db:
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant)
        await db.execute(
            update(Tenant).where(Tenant.id == tenant.id).values(funding_mode="operator_funded")
        )
        if policy:
            await store.set_policy(
                db,
                tenant_id=tenant.id,
                agent_id=None,
                policy=LongJobPolicy(enabled=True),
                ceilings=LIMITS,
                actor=ADMIN,
                set_by="admin",
            )
    return LongJob(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        owner_account_id=account.id,
        owner_external_user_id="user",
        agent_id="agent_a",
        agent_name="a",
        agent_version=7,
        environment_id="env_a",
        environment_updated_at=NOW,
        platform="discord",
        parent_channel_id="channel",
        thread_id="thread",
        parent_session_id="ses_parent",
        tool_use_id="tool_approved",
        command="sleep 600",
        title="fit",
        deadline_minutes=60,
        max_usd=Decimal("5"),
        state="queued",
        version=0,
        created_at=NOW,
    )


async def claimed(sm, job, *, owner="executor", now=NOW):
    async with sm.begin() as db:
        await store.register(db, job, ceilings=LIMITS)
        rows = await store.claim(db, tenant_id=job.tenant_id, owner=owner, now=now, ceilings=LIMITS)
    assert len(rows) == 1
    return rows[0]


async def read(sm, job):
    async with sm.begin() as db:
        return await store.get(
            db, job.id, tenant_id=job.tenant_id, owner_account_id=job.owner_account_id
        )


async def test_registration_is_frozen_and_idempotent_before_launch(db_engine, db_clean):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    job = await seed(sm)
    async with sm.begin() as db:
        registered = await store.register(db, job, ceilings=LIMITS)
        replay = await store.register(
            db, job.model_copy(update={"command": "different", "id": uuid.uuid4()}), ceilings=LIMITS
        )
    assert registered == replay
    assert (
        registered.tenant_id,
        registered.owner_account_id,
        registered.agent_id,
        registered.agent_version,
        registered.environment_id,
        registered.thread_id,
        registered.parent_session_id,
    ) == (job.tenant_id, job.owner_account_id, "agent_a", 7, "env_a", "thread", "ses_parent")
    assert registered.worker_session_id is None and registered.state == "queued"


@pytest.mark.parametrize("actor", [Subject(), Subject(is_admin=True, via_agent_key=True)])
async def test_policy_requires_human_admin(db_session, actor):
    tenant = await make_tenant(db_session)
    with pytest.raises(PermissionError):
        await store.set_policy(
            db_session,
            tenant_id=tenant.id,
            agent_id=None,
            policy=LongJobPolicy(enabled=True),
            ceilings=LIMITS,
            actor=actor,
            set_by="user",
        )
    assert await db_session.scalar(select(func.count()).select_from(LongJobPolicyHistory)) == 0


async def test_policy_defaults_override_audit_and_ceiling_clamp(db_session):
    tenant = await make_tenant(db_session)
    kw = dict(tenant_id=tenant.id, ceilings=LIMITS)
    default = await store.get_policy(db_session, agent_id="agent_a", **kw)
    assert default == LongJobPolicy()
    tenant_policy = LongJobPolicy(enabled=True, max_usd=Decimal("10"))
    for _ in range(2):
        await store.set_policy(
            db_session, agent_id=None, policy=tenant_policy, actor=ADMIN, set_by="admin", **kw
        )
    assert await db_session.scalar(select(func.count()).select_from(LongJobPolicyHistory)) == 1
    agent_policy = LongJobPolicy(
        enabled=False, default_deadline_minutes=20, max_deadline_minutes=30, max_usd=Decimal("2")
    )
    await store.set_policy(
        db_session, agent_id="agent_a", policy=agent_policy, actor=ADMIN, set_by="admin", **kw
    )
    assert await store.get_policy(db_session, agent_id="agent_a", **kw) == agent_policy
    assert await store.get_policy(db_session, agent_id="other", **kw) == tenant_policy
    lowered = await store.get_policy(
        db_session,
        tenant_id=tenant.id,
        agent_id="other",
        ceilings=LongJobsSettings(max_deadline_minutes=25, max_usd=Decimal("1")),
    )
    assert (lowered.default_deadline_minutes, lowered.max_deadline_minutes, lowered.max_usd) == (
        25,
        25,
        Decimal("1"),
    )
    assert await db_session.scalar(select(func.count()).select_from(LongJobPolicyHistory)) == 2


@pytest.mark.parametrize(
    "policy",
    [
        LongJobPolicy(enabled=True, max_deadline_minutes=71),
        LongJobPolicy(enabled=True, max_usd=Decimal("21")),
        LongJobPolicy(enabled=True, default_deadline_minutes=70, max_deadline_minutes=60),
    ],
)
async def test_admin_cannot_exceed_ceiling_or_default_max(db_session, policy):
    tenant = await make_tenant(db_session)
    with pytest.raises(StoreError, match="ceiling"):
        await store.set_policy(
            db_session,
            tenant_id=tenant.id,
            agent_id=None,
            policy=policy,
            ceilings=LIMITS,
            actor=ADMIN,
            set_by="admin",
        )


@pytest.mark.parametrize(
    "enabled,changes,match",
    [
        (False, {}, "disabled"),
        (True, {"deadline_minutes": 71}, "70 minutes"),
        (True, {"max_usd": Decimal("6")}, "at most"),
    ],
)
async def test_registration_limit_refusals(db_engine, db_clean, enabled, changes, match):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    job = (await seed(sm, policy=enabled)).model_copy(update=changes)
    async with sm.begin() as db:
        with pytest.raises(StoreError, match=match):
            await store.register(db, job, ceilings=LIMITS)


async def test_balance_gate_refuses_prepaid_empty_tenant(db_engine, db_clean):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    job = await seed(sm)
    async with sm.begin() as db:
        await db.execute(
            update(Tenant).where(Tenant.id == job.tenant_id).values(funding_mode="prepaid")
        )
    with pytest.raises(StoreError, match="balance depleted"):
        await require_admission(sm, job)


async def test_claim_caps_and_expired_lease_reclaim(db_engine, db_clean):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    job = await seed(sm)
    async with sm.begin() as db:
        # Owners in jobs must belong to the same tenant: create this tenant's second owner.
        from daimon.core.stores.accounts import get_account

        original = await get_account(db, job.owner_account_id)
        assert original is not None
        from daimon.core.stores.tenants import get_tenant

        tenant = await get_tenant(db, job.tenant_id)
        other = await make_account(db, tenant=tenant)
        for i in range(5):
            await store.register(
                db,
                job.model_copy(
                    update={
                        "id": uuid.uuid4(),
                        "tool_use_id": str(i),
                        "owner_account_id": job.owner_account_id if i < 3 else other.id,
                    }
                ),
                ceilings=LIMITS,
            )
        rows = await store.claim(
            db, tenant_id=job.tenant_id, owner="first", now=NOW, ceilings=LIMITS
        )
        assert len(rows) == 3
        assert sum(r.owner_account_id == job.owner_account_id for r in rows) == 2
    async with sm.begin() as db:
        assert (
            await store.claim(db, tenant_id=job.tenant_id, owner="second", now=NOW, ceilings=LIMITS)
            == []
        )
        reclaimed = await store.claim(
            db,
            tenant_id=job.tenant_id,
            owner="second",
            now=NOW + timedelta(seconds=91),
            ceilings=LIMITS,
        )
        assert {r.id for r in reclaimed} == {r.id for r in rows}
        assert all(r.version == 2 and r.lease_owner == "second" for r in reclaimed)
        assert await store.transition(db, rows[0], state="running") is None


async def test_finish_cas_publishes_one_unattended_wake(db_engine, db_clean):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    job = await claimed(sm, await seed(sm))
    async with sm.begin() as db:
        for expected in (True, False):
            assert (
                await store.finish(
                    db,
                    job,
                    state="succeeded",
                    now=NOW,
                    manifest=LongJobManifest(exit_code=0, summary="done"),
                    cost=Decimal("0.11"),
                )
                is expected
            )
        wakes = await list_pending_continuations(
            db,
            tenant_id=job.tenant_id,
            platform="discord",
            thread_id=job.thread_id,
            include_future=True,
        )
    assert len(wakes) == 1
    assert wakes[0].reason == "job_finished" and not wakes[0].attended
    assert str(job.id) in wakes[0].requested_work
    assert (await read(sm, job)).cost_usd == Decimal("0.11")


async def test_owner_tenant_isolation_for_all_id_operations(db_engine, db_clean):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    job = await seed(sm)
    async with sm.begin() as db:
        await store.register(db, job, ceilings=LIMITS)
        for tenant_id, account in [
            (uuid.uuid4(), job.owner_account_id),
            (job.tenant_id, uuid.uuid4()),
        ]:
            kw = dict(tenant_id=tenant_id, owner_account_id=account)
            assert await store.get(db, job.id, **kw) is None
            assert await store.list_jobs(db, **kw) == []
            assert await store.request_cancel(db, job.id, **kw) is None
        assert (
            await store.list_jobs(
                db, tenant_id=uuid.uuid4(), owner_account_id=uuid.uuid4(), admin=True
            )
            == []
        )
        rows = await store.list_jobs(
            db, tenant_id=job.tenant_id, owner_account_id=uuid.uuid4(), admin=True
        )
        assert [r.id for r in rows] == [job.id]
        changed = await store.request_cancel(
            db, job.id, tenant_id=job.tenant_id, owner_account_id=uuid.uuid4(), admin=True
        )
        assert changed.cancel_requested


class FakeWorker:
    def __init__(self, *, missing=False, vanished=False, started=False, block=False):
        self.calls = []
        self.missing, self.vanished, self.started, self.block = missing, vanished, started, block

    async def create_or_find(self, job):
        self.calls.append("create")
        return "ses_worker"

    async def has_started(self, job):
        if self.vanished:
            raise WorkerVanished()
        return self.started

    async def drive(self, job, *, resume, send_guard):
        if not resume:
            async with send_guard():
                self.calls.append("send")
        else:
            self.calls.append("resume")
        if self.block:
            await asyncio.Event().wait()

    async def manifest(self, job):
        return None if self.missing else LongJobManifest(exit_code=0, summary="done")

    async def interrupt(self, job):
        self.calls.append("interrupt")

    async def archive(self, job):
        self.calls.append("archive")

    async def settle(self, job):
        self.calls.append("settle")
        if self.vanished:
            raise WorkerVanished()


@pytest.mark.parametrize("state", ["launching", "running"])
async def test_restart_reclaims_without_second_user_message(db_engine, db_clean, state):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    port = FakeWorker(started=True)
    executor = LongJobExecutor(
        sm, port, LIMITS, clock=lambda: NOW + timedelta(seconds=91), heartbeat_s=0.001
    )
    job = await claimed(sm, await seed(sm), owner="dead")
    async with sm.begin() as db:
        job = await store.transition(db, job, state=state, worker_session_id="ses_worker")
        reclaimed = (
            await store.claim(
                db,
                tenant_id=job.tenant_id,
                owner=executor.owner,
                now=NOW + timedelta(seconds=91),
                ceilings=LIMITS,
            )
        )[0]
    await executor.execute(reclaimed)
    assert (await read(sm, job)).state == "succeeded"
    assert (
        port.calls.count("resume") == 1 and "send" not in port.calls and "create" not in port.calls
    )


@pytest.mark.parametrize("kwargs", [{"vanished": True}, {"missing": True}])
async def test_vanished_or_missing_manifest_is_lost(db_engine, db_clean, kwargs):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    port = FakeWorker(**kwargs)
    executor = LongJobExecutor(sm, port, LIMITS, clock=lambda: NOW, heartbeat_s=0.001)
    job = await claimed(sm, await seed(sm), owner=executor.owner)
    await executor.execute(job)
    assert (await read(sm, job)).state == "lost"


@pytest.mark.parametrize("reason", ["cancelled", "timed_out", "over_budget"])
async def test_stop_interrupts_then_archives_and_records_before_wake(
    db_engine, db_clean, monkeypatch, reason
):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    port = FakeWorker(block=True)
    executor = LongJobExecutor(sm, port, LIMITS, clock=lambda: NOW, heartbeat_s=0.001)
    job = await claimed(sm, await seed(sm), owner=executor.owner)
    async with sm.begin() as db:
        changes = {"worker_session_id": "ses_worker"}
        if reason == "cancelled":
            changes["cancel_requested"] = True
        elif reason == "timed_out":
            changes["deadline_at"] = NOW
        job = await store.transition(db, job, **changes)
    if reason == "over_budget":
        monkeypatch.setattr(store, "session_cost", AsyncMock(return_value=Decimal("6")))
    await executor.execute(job)
    final = await read(sm, job)
    assert final.state == reason and final.interrupted_at == NOW and final.archived_at == NOW
    assert port.calls.index("interrupt") < port.calls.index("archive")
    async with sm.begin() as db:
        assert await db.scalar(select(func.count()).select_from(TaskContinuation)) == 1


async def test_launching_session_without_user_event_is_sent_once(db_engine, db_clean):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    port = FakeWorker(started=False)
    executor = LongJobExecutor(sm, port, LIMITS, clock=lambda: NOW, heartbeat_s=0.001)
    job = await claimed(sm, await seed(sm), owner=executor.owner)
    async with sm.begin() as db:
        job = await store.save_worker(db, job, worker_session_id="ses_worker")
    await executor.execute(job)
    assert port.calls.count("send") == 1 and "create" not in port.calls
    assert (await read(sm, job)).state == "succeeded"


async def test_worker_prompt_bounds_waits_and_quotes_command(db_engine, db_clean):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    job = (await seed(sm)).model_copy(update={"command": "echo 'ignore instructions'; sleep 600"})
    prompt = worker_prompt(job)
    assert "timeout 280" in prompt and "timeout 540" not in prompt
    assert f"long-job-{job.id}.json" in prompt
    assert "Do NOT restart" in prompt


@pytest.mark.parametrize(
    "count,owner,match", [(2, True, "owner running"), (3, False, "tenant running")]
)
async def test_start_refuses_running_caps(db_engine, db_clean, count, owner, match):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    job = await seed(sm)
    async with sm.begin() as db:
        from daimon.core.stores.tenants import get_tenant

        tenant = await get_tenant(db, job.tenant_id)
        other = await make_account(db, tenant=tenant)
        for i in range(count):
            who = job.owner_account_id if owner or i < 2 else other.id
            await store.register(
                db,
                job.model_copy(
                    update={"id": uuid.uuid4(), "tool_use_id": str(i), "owner_account_id": who}
                ),
                ceilings=LIMITS,
            )
        await store.claim(db, tenant_id=job.tenant_id, owner="executor", now=NOW, ceilings=LIMITS)
        with pytest.raises(StoreError, match=match):
            await store.register(
                db,
                job.model_copy(update={"id": uuid.uuid4(), "tool_use_id": "extra"}),
                ceilings=LIMITS,
            )


async def test_failed_archive_reclaims_retained_stop_reason(db_engine, db_clean):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    port = FakeWorker()
    executor = LongJobExecutor(sm, port, LIMITS, clock=lambda: NOW)
    job = await claimed(sm, await seed(sm), owner=executor.owner)
    async with sm.begin() as db:
        job = await store.transition(db, job, worker_session_id="ses_worker", deadline_at=NOW)
    original_archive = port.archive
    port.archive = AsyncMock(side_effect=RuntimeError("archive unavailable"))
    with pytest.raises(RuntimeError, match="archive unavailable"):
        await executor.execute(job)
    pending = await read(sm, job)
    assert pending.state in ("launching", "running") and pending.stop_state == "timed_out"
    assert pending.interrupted_at == NOW and pending.archived_at is None
    async with sm.begin() as db:
        assert await db.scalar(select(func.count()).select_from(TaskContinuation)) == 0
    port.archive = original_archive
    executor.clock = lambda: NOW + timedelta(seconds=91)
    async with sm.begin() as db:
        resumed = (
            await store.claim(
                db,
                tenant_id=job.tenant_id,
                owner=executor.owner,
                now=executor.clock(),
                ceilings=LIMITS,
            )
        )[0]
    await executor.execute(resumed)
    assert (await read(sm, job)).state == "timed_out"
    assert port.calls.count("interrupt") == 1


async def test_worker_creation_transport_is_empty_and_owner_billed(db_engine, db_clean):
    import httpx
    from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
    from daimon.core.long_jobs_ma import ManagedLongJobPort
    from daimon.testing.ma_models import ma_agent, ma_environment
    from mux.testing.drivers.anthropic.ma import (
        MARouter,
        build_fake_anthropic,
        json_body,
        list_response,
    )
    from mux.testing.drivers.anthropic.ma_models import ma_session
    from pydantic import SecretStr

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    job = (await seed(sm)).model_copy(
        update={
            "source_metadata": {
                "daimon_channel": "private",
                "daimon_thread": "thread",
                "daimon_sealed": "retained,private",
                "daimon_private_dm": "private-grant",
            }
        }
    )
    router = MARouter()
    router.add("GET", r"/v1/sessions$", lambda _r, _m: list_response([]))
    agent = ma_agent(id=job.agent_id, version=job.agent_version)
    env = ma_environment(id=job.environment_id, updated_at=job.environment_updated_at.isoformat())
    router.add_agent(agent)
    router.add_environment(env)
    created = []

    def create(request, _match):
        body = json_body(request)
        created.append(body)
        return httpx.Response(
            200,
            json=ma_session(
                id="ses_worker", agent=agent, environment_id=env.id, metadata=body["metadata"]
            ).model_dump(mode="json"),
        )

    router.add("POST", r"/v1/sessions$", create)
    client = build_fake_anthropic(router.dispatch)
    settings = Settings(
        database=DatabaseSettings(url="postgresql+asyncpg://unused/db"),
        anthropic=AnthropicSettings(api_key=SecretStr("fake")),
        _env_file=None,
    )
    try:
        port = ManagedLongJobPort(client, sm, settings)
        assert await port.create_or_find(job) == "ses_worker"
        [body] = created
        assert job.source_metadata.items() <= body["metadata"].items()
        assert body["resources"] == [] and body["vault_ids"] == []
        assert body["agent"]["skills"] == [] and body["agent"]["mcp_servers"] == []
        assert [t["type"] for t in body["agent"]["tools"]] == ["agent_toolset_20260401"]
        assert body["agent"]["version"] == job.agent_version
        assert body["metadata"]["daimon_account"] == str(job.owner_account_id)
        assert body["metadata"]["daimon_tenant"] == str(job.tenant_id)
        assert body["metadata"]["daimon_long_job"] == str(job.id)
    finally:
        await client.close()


async def test_rls_rejects_cross_tenant_reads_and_writes(db_engine, db_clean):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    job = await seed(sm)
    async with sm.begin() as db:
        await store.register(db, job, ceilings=LIMITS)
        await store.request_cancel(
            db, job.id, tenant_id=job.tenant_id, owner_account_id=job.owner_account_id
        )
        schema = await db.scalar(text("SELECT current_schema()"))
    assert schema.startswith("test_")
    role = "long_job_rls_" + uuid.uuid4().hex
    try:
        async with db_engine.begin() as conn:
            await conn.execute(text(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER NOBYPASSRLS'))
            await conn.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"'))
            for table in ("long_job", "long_job_cancel_audit"):
                await conn.execute(text(f'GRANT SELECT, UPDATE ON {table} TO "{role}"'))
                await conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
                await conn.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
                await conn.execute(
                    text(
                        f"CREATE POLICY tenant_scope ON {table} USING "
                        "(tenant_id = NULLIF(current_setting('daimon.tenant_id', true), '')::uuid) "
                        "WITH CHECK (tenant_id = NULLIF(current_setting('daimon.tenant_id', true), '')::uuid)"
                    )
                )
        async with db_engine.begin() as conn:
            await conn.execute(text(f'SET LOCAL ROLE "{role}"'))
            for table in ("long_job", "long_job_cancel_audit"):
                assert (await conn.execute(text(f"SELECT id FROM {table}"))).all() == []
            await conn.execute(select(func.set_config("daimon.tenant_id", str(uuid.uuid4()), True)))
            for table in ("long_job", "long_job_cancel_audit"):
                assert (await conn.execute(text(f"SELECT id FROM {table}"))).all() == []
            await conn.execute(
                select(func.set_config("daimon.tenant_id", str(job.tenant_id), True))
            )
            for table in ("long_job", "long_job_cancel_audit"):
                assert [
                    row[0] for row in (await conn.execute(text(f"SELECT id FROM {table}"))).all()
                ] == [job.id]
        for table in ("long_job", "long_job_cancel_audit"):
            with pytest.raises(DBAPIError, match="row-level security"):
                async with db_engine.begin() as conn:
                    await conn.execute(text(f'SET LOCAL ROLE "{role}"'))
                    await conn.execute(
                        select(func.set_config("daimon.tenant_id", str(job.tenant_id), True))
                    )
                    await conn.execute(
                        text(f"UPDATE {table} SET tenant_id=:other WHERE id=:job"),
                        {"other": uuid.uuid4(), "job": job.id},
                    )
    finally:
        async with db_engine.begin() as conn:
            await conn.execute(text(f'DROP OWNED BY "{role}"'))
            await conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))


async def test_monthly_cap_gate_refuses_owner(db_engine, db_clean):
    from daimon.core.stores.tenant_user_caps import set_override

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    job = await seed(sm)
    async with sm.begin() as db:
        await set_override(
            db, tenant_id=job.tenant_id, user_id=job.owner_external_user_id, amount=Decimal("0")
        )
    with pytest.raises(StoreError, match="monthly cap exceeded"):
        await require_admission(sm, job)


async def test_interrupt_ack_failure_still_archives_before_finish(db_engine, db_clean):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    port = FakeWorker()
    executor = LongJobExecutor(sm, port, LIMITS, clock=lambda: NOW)
    job = await claimed(sm, await seed(sm), owner=executor.owner)
    async with sm.begin() as db:
        job = await store.transition(db, job, worker_session_id="ses_worker", cancel_requested=True)
    port.interrupt = AsyncMock(side_effect=TimeoutError("ack timeout"))
    await executor.execute(job)
    final = await read(sm, job)
    assert final.state == "cancelled"
    assert final.interrupt_attempted_at == NOW and final.interrupted_at is None
    assert final.archived_at == NOW and "not acknowledged" in final.summary
    assert "archive" in port.calls


async def test_lowered_operator_ceiling_stops_existing_worker(db_engine, db_clean, monkeypatch):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    port = FakeWorker(block=True)
    lowered = LongJobsSettings(max_deadline_minutes=20, max_usd=Decimal("1"))
    executor = LongJobExecutor(sm, port, lowered, clock=lambda: NOW, heartbeat_s=0.001)
    job = await claimed(sm, await seed(sm), owner=executor.owner)
    async with sm.begin() as db:
        job = await store.save_worker(db, job, worker_session_id="ses_worker")
    monkeypatch.setattr(store, "session_cost", AsyncMock(return_value=Decimal("2")))
    assert executor.deadline(job) == NOW + timedelta(minutes=20)
    await executor.execute(job)
    assert (await read(sm, job)).state == "over_budget"


async def test_reader_failure_leaves_running_job_reclaimable(db_engine, db_clean):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    port = FakeWorker()
    executor = LongJobExecutor(sm, port, LIMITS, clock=lambda: NOW)
    job = await claimed(sm, await seed(sm), owner=executor.owner)
    port.drive = AsyncMock(side_effect=RuntimeError("stream unavailable"))
    with pytest.raises(RuntimeError, match="stream unavailable"):
        await executor.execute(job)
    pending = await read(sm, job)
    assert pending.state == "running" and pending.finished_at is None
    assert "archive" not in port.calls
    async with sm.begin() as db:
        assert await db.scalar(select(func.count()).select_from(TaskContinuation)) == 0
        rows = await store.claim(
            db,
            tenant_id=job.tenant_id,
            owner="restarted",
            now=NOW + timedelta(seconds=91),
            ceilings=LIMITS,
        )
        assert [row.id for row in rows] == [job.id]


async def test_real_driver_resume_replays_without_another_user_message():
    import httpx
    from daimon.core.mux_backend import resource_scope
    from daimon.core.turn.driver import run_turn
    from daimon.core.turn.posture import BillingExempt
    from daimon.testing.turn_fakes import RecordingLifecycle
    from mux.testing.drivers.anthropic.ma import (
        MARouter,
        build_fake_anthropic,
        list_response,
        sse_response,
    )
    from mux.testing.drivers.anthropic.ma_models import ma_session

    now = datetime.now(UTC).isoformat()
    events = [
        {
            "id": "initial",
            "type": "user.message",
            "processed_at": now,
            "content": [{"type": "text", "text": "approved job"}],
        },
        {
            "id": "finished",
            "type": "session.status_idle",
            "processed_at": now,
            "stop_reason": {"type": "end_turn"},
        },
    ]
    router = MARouter()
    snapshot = ma_session(id="ses_reclaim", status="idle")
    router.add(
        "GET",
        r"/v1/sessions/ses_reclaim$",
        lambda _r, _m: httpx.Response(200, json=snapshot.model_dump(mode="json")),
    )
    router.add("GET", r"/v1/sessions/ses_reclaim/events$", lambda _r, _m: list_response(events))
    router.add("GET", r"/v1/sessions/ses_reclaim/events/stream$", lambda _r, _m: sse_response([]))
    # No POST route is installed: replay cannot send a second initial event.
    client = build_fake_anthropic(router.dispatch)
    try:
        state = await run_turn(
            anthropic=client,
            session_id=snapshot.id,
            scope=resource_scope(
                tenant_id=str(uuid.uuid4()),
                account_id=str(uuid.uuid4()),
                authorization_id="worker replay regression",
            ),
            path="legacy",
            user_message="must never be sent",
            resume_existing=True,
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            billing=BillingExempt(reason="cli-operator-run"),
        )
        assert state.error is None and state.stop_reason.type == "end_turn"
    finally:
        await client.close()


async def test_concurrent_retry_keeps_result_after_running_caps_fill(
    db_engine, db_clean, monkeypatch
):
    from daimon.core._models import LongJobRecord

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    job = await seed(sm)
    read_empty = asyncio.Event()

    async def retry():
        async with sm.begin() as db:
            original = db.scalar

            async def scalar(statement, *args, **kwargs):
                result = await original(statement, *args, **kwargs)
                if (
                    getattr(statement, "column_descriptions", [{}])[0].get("entity")
                    is LongJobRecord
                    and result is None
                ):
                    read_empty.set()
                return result

            monkeypatch.setattr(db, "scalar", scalar)
            return await store.register(
                db, job.model_copy(update={"id": uuid.uuid4()}), ceilings=LIMITS
            )

    async with sm.begin() as blocker:
        await blocker.scalar(select(Tenant.id).where(Tenant.id == job.tenant_id).with_for_update())
        task = asyncio.create_task(retry())
        await asyncio.wait_for(read_empty.wait(), 10)
        first = await store.register(blocker, job, ceilings=LIMITS)
        await store.register(
            blocker,
            job.model_copy(update={"id": uuid.uuid4(), "tool_use_id": "other"}),
            ceilings=LIMITS,
        )
        assert (
            len(
                await store.claim(
                    blocker, tenant_id=job.tenant_id, owner="launcher", now=NOW, ceilings=LIMITS
                )
            )
            == 2
        )
    assert (await asyncio.wait_for(task, 10)).id == first.id


async def test_queued_cancel_does_not_wait_for_running_capacity(db_engine, db_clean):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    base = await seed(sm)
    async with sm.begin() as db:
        jobs = [
            await store.register(
                db,
                base.model_copy(update={"id": uuid.uuid4(), "tool_use_id": str(i)}),
                ceilings=LIMITS,
            )
            for i in range(3)
        ]
        running = await store.claim(
            db, tenant_id=base.tenant_id, owner="busy", now=NOW, ceilings=LIMITS
        )
        assert len(running) == 2
        queued = next(job for job in jobs if job.id not in {row.id for row in running})
        await store.request_cancel(
            db, queued.id, tenant_id=base.tenant_id, owner_account_id=base.owner_account_id
        )
    port = FakeWorker()
    executor = LongJobExecutor(sm, port, LIMITS, clock=lambda: NOW)
    async with sm.begin() as db:
        cancelled = await store.claim(
            db, tenant_id=base.tenant_id, owner=executor.owner, now=NOW, ceilings=LIMITS
        )
    assert [job.id for job in cancelled] == [queued.id]
    await executor.execute(cancelled[0])
    assert (await read(sm, queued)).state == "cancelled"
    assert port.calls == []


async def test_expired_executor_cannot_send_after_reclaimer_finished(db_engine, db_clean):
    """A process suspended before POST cannot send on its old launch decision."""
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    entered, release = asyncio.Event(), asyncio.Event()
    old_checked = asyncio.Event()
    sends = []

    class PausedWorker(FakeWorker):
        async def drive(self, job, *, resume, send_guard):
            entered.set()
            await release.wait()
            try:
                async with send_guard():
                    sends.append("old")
            finally:
                old_checked.set()

    class NewWorker(FakeWorker):
        async def drive(self, job, *, resume, send_guard):
            async with send_guard():
                sends.append("new")

    old = LongJobExecutor(sm, PausedWorker(), LIMITS, clock=lambda: NOW)
    job = await claimed(sm, await seed(sm), owner=old.owner)
    task = asyncio.create_task(old.execute(job))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        # Wait until the first normal heartbeat has committed, so the old
        # executor is suspended at the send boundary during its 20s wait.
        async with asyncio.timeout(10):
            while (await read(sm, job)).version < job.version + 3:
                await asyncio.sleep(0.001)
        new = LongJobExecutor(
            sm, NewWorker(), LIMITS, clock=lambda: NOW + timedelta(seconds=91), heartbeat_s=0.001
        )
        async with sm.begin() as db:
            [reclaimed] = await store.claim(
                db, tenant_id=job.tenant_id, owner=new.owner, now=new.clock(), ceilings=LIMITS
            )
        await new.execute(reclaimed)
        assert (await read(sm, job)).state == "succeeded"
        release.set()
        await asyncio.wait_for(old_checked.wait(), 10)
        await asyncio.wait_for(task, 10)
        assert sends == ["new"]
        assert (await read(sm, job)).state == "succeeded"
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_uncertain_initial_send_is_reconciled_without_retry(db_engine, db_clean):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    job = await claimed(sm, await seed(sm), owner="dead")
    async with sm.begin() as db:
        job = await store.save_worker(db, job, worker_session_id="ses_worker")
        job = await store.transition(db, job, state="running")
        assert await store.lock_initial_send(db, job, now=NOW, deadline=job.deadline_at)
    port = FakeWorker(started=False)
    executor = LongJobExecutor(
        sm, port, LIMITS, clock=lambda: NOW + timedelta(seconds=91), heartbeat_s=0.001
    )
    async with sm.begin() as db:
        [reclaimed] = await store.claim(
            db, tenant_id=job.tenant_id, owner=executor.owner, now=executor.clock(), ceilings=LIMITS
        )
    await executor.execute(reclaimed)
    assert (await read(sm, job)).state == "lost"
    assert "send" not in port.calls
    assert port.calls.index("interrupt") < port.calls.index("archive")


async def test_cancel_audit_is_content_free_and_tenant_scoped(db_engine, db_clean):
    from sqlalchemy import text

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    job = (await seed(sm)).model_copy(
        update={"command": "secret shell command", "summary": "secret output summary"}
    )
    async with sm.begin() as db:
        await store.register(db, job, ceilings=LIMITS)
        await store.request_cancel(
            db, job.id, tenant_id=job.tenant_id, owner_account_id=job.owner_account_id
        )
        assert await store.list_cancel_audits(db, tenant_id=uuid.uuid4(), job_id=job.id) == []
        rows = (
            (await db.execute(text("SELECT to_jsonb(a) FROM long_job_cancel_audit a")))
            .scalars()
            .all()
        )
    [audit] = rows
    assert set(audit) == {
        "audit_id",
        "tenant_id",
        "id",
        "state",
        "owner_account_id",
        "requested_by",
        "requested_at",
    }
    assert audit["id"] == str(job.id)
    assert "secret" not in str(audit)


async def test_account_purge_cascades_owned_jobs_and_cancel_audit(db_engine, db_clean):
    from daimon.core.purge import purge_account

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    job, other = await seed(sm), await seed(sm)
    async with sm.begin() as db:
        await store.register(db, job, ceilings=LIMITS)
        await store.register(db, other, ceilings=LIMITS)
        await store.request_cancel(
            db, job.id, tenant_id=job.tenant_id, owner_account_id=job.owner_account_id
        )
    report = await purge_account(sm=sm, account_id=job.owner_account_id)
    assert report.db.accounts == 1
    assert await read(sm, job) is None
    assert await read(sm, other) is not None
    async with sm.begin() as db:
        assert await store.list_cancel_audits(db, tenant_id=job.tenant_id, job_id=job.id) == []
