"""Compute cleanup progresses independently of routine dispatch."""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from daimon.adapters.scheduler import main
from daimon.core import compute_sessions as compute_module
from daimon.core.compute_fly import FlyComputeProvider
from daimon.core.compute_models import ComputeSession
from daimon.core.compute_provider import app_name
from daimon.core.compute_sessions import ComputeSessions
from daimon.core.config import ComputeSettings
from pydantic import SecretStr


async def test_compute_sweep_skips_unconfigured_provider():
    compute = SimpleNamespace(
        settings=SimpleNamespace(deployment_id="", api_token=None), reconcile=AsyncMock()
    )
    await main._sweep_compute(compute)
    compute.reconcile.assert_not_awaited()


async def test_dispatch_keeps_ticking_and_shutdown_cancels_stalled_compute():
    stop = asyncio.Event()
    started, cancelled = asyncio.Event(), asyncio.Event()
    ticks = 0

    async def stalled():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def tick():
        nonlocal ticks
        await started.wait()
        ticks += 1
        if ticks == 3:
            stop.set()

    async with asyncio.timeout(5):
        await main._run_loops(
            tick=tick,
            usage_sweep=AsyncMock(),
            privacy_sweep=AsyncMock(),
            compute_sweep=stalled,
            compute_task_poll=AsyncMock(),
            interval_s=0.01,
            stop_event=stop,
        )
    assert ticks == 3
    assert cancelled.is_set()


async def test_task_poll_skips_unconfigured_and_contains_errors():
    compute = SimpleNamespace(
        settings=SimpleNamespace(deployment_id="", session_auth_key=None), poll_tasks=AsyncMock()
    )
    await main._poll_compute_tasks(compute)
    compute.poll_tasks.assert_not_awaited()
    compute.settings = SimpleNamespace(deployment_id="d", session_auth_key=SecretStr("k"))
    compute.poll_tasks = AsyncMock(side_effect=RuntimeError("remote body with secrets"))
    await main._poll_compute_tasks(compute)
    compute.poll_tasks.assert_awaited_once()


async def test_stalled_task_polling_blocks_neither_dispatch_nor_cleanup():
    stop = asyncio.Event()
    started, cancelled = asyncio.Event(), asyncio.Event()
    ticks = cleanups = 0

    async def stalled():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def cleanup():
        nonlocal cleanups
        cleanups += 1

    async def tick():
        nonlocal ticks
        await started.wait()
        ticks += 1
        if ticks >= 3 and cleanups >= 2:
            stop.set()

    async with asyncio.timeout(5):
        await main._run_loops(
            tick=tick,
            usage_sweep=None,
            privacy_sweep=AsyncMock(),
            compute_sweep=cleanup,
            compute_task_poll=stalled,
            interval_s=0.01,
            stop_event=stop,
        )
    assert ticks >= 3 and cleanups >= 2
    assert cancelled.is_set()


@pytest.mark.parametrize("inventory", ["slow", "timeout", "unexpected_error"])
async def test_many_expired_apps_are_deleted_without_delaying_dispatch(monkeypatch, inventory):
    now = datetime.now(UTC)
    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    settings = ComputeSettings(
        deployment_id="test-deployment",
        api_token=SecretStr("test-only"),
        org_slug="test-org",
    )
    rows = {}
    for _ in range(40):
        session_id = uuid.uuid4()
        rows[session_id] = ComputeSession(
            id=session_id,
            tenant_id=tenant_id,
            owner_account_id=account_id,
            owner_external_user_id="owner",
            deployment_id=settings.deployment_id,
            platform="discord",
            parent_channel_id="parent",
            thread_id="thread",
            agent_id="agent",
            agent_name="Daimon",
            request_key=str(session_id),
            app_name=app_name(settings.deployment_id, session_id),
            profile="test",
            image="registry.example/test",
            region="iad",
            cpu_kind="shared",
            cpus=1,
            memory_mb=256,
            state="running",
            ttl_hours=1,
            expires_at=now - timedelta(hours=1),
            created_at=now - timedelta(hours=2),
        )
    apps = {row.app_name: row.id for row in rows.values()}
    inventory_started, inventory_finished = asyncio.Event(), asyncio.Event()
    inventory_requests = 0

    async def handler(request):
        nonlocal inventory_requests
        path = request.url.path
        # Reproduce 1+N sequential inventory requests taking over ten seconds.
        # Deletion requests remain fast, including the ownership rechecks.
        if not inventory_finished.is_set():
            inventory_started.set()
            inventory_requests += 1
            if inventory == "timeout":
                inventory_finished.set()
                raise TimeoutError("remote-secret-body")
            if inventory == "unexpected_error":
                inventory_finished.set()
                raise RuntimeError("remote-secret-body")
            await asyncio.sleep(0.3)
            if inventory_requests == 41:
                inventory_finished.set()
        if path == "/v1/apps":
            return httpx.Response(200, json={"apps": [{"name": name} for name in apps]})
        name = path.split("/")[3]
        if path.endswith("/machines"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "machine",
                        "state": "started",
                        "config": {
                            "metadata": {
                                "daimon_deployment": settings.deployment_id,
                                "daimon_session": str(apps[name]),
                            }
                        },
                    }
                ],
            )
        if request.method == "DELETE":
            apps.pop(name)
            return httpx.Response(200)
        assert name not in apps
        return httpx.Response(404)

    @asynccontextmanager
    async def transaction():
        yield None

    async def listing(*args, **kwargs):
        return [row for row in rows.values() if row.state != "deleted"]

    async def get(_db, _tenant, session_id, **kwargs):
        return rows[session_id]

    async def save(_db, row):
        rows[row.id] = row

    # Keep real lifecycle and HTTP ownership/delete logic; replace only persistence.
    monkeypatch.setattr(
        compute_module,
        "list_tenants_by_platform",
        AsyncMock(return_value=[SimpleNamespace(id=tenant_id, archived_at=None)]),
    )
    monkeypatch.setattr(compute_module.store, "list_sessions", listing)
    monkeypatch.setattr(compute_module.store, "get", get)
    monkeypatch.setattr(compute_module.store, "save", save)
    log = Mock()
    monkeypatch.setattr(compute_module, "log", log)
    provider = FlyComputeProvider(settings, transport=httpx.MockTransport(handler))
    service = ComputeSessions(
        SimpleNamespace(begin=transaction), settings, provider, clock=lambda: now
    )
    stop = asyncio.Event()
    iterations = 0
    ticks_during_inventory = 0

    async def sweep():
        nonlocal iterations
        await main._sweep_compute(service)
        iterations += 1
        if all(row.state == "deleted" for row in rows.values()) or iterations == 2:
            stop.set()

    async def tick():
        nonlocal ticks_during_inventory
        if inventory_started.is_set() and not inventory_finished.is_set():
            ticks_during_inventory += 1

    async with asyncio.timeout(30):
        await main._run_loops(
            tick=tick,
            usage_sweep=AsyncMock(),
            privacy_sweep=AsyncMock(),
            compute_sweep=sweep,
            compute_task_poll=AsyncMock(),
            interval_s=0.01,
            stop_event=stop,
        )
    assert iterations == 1
    assert all(row.state == "deleted" for row in rows.values())
    assert apps == {}
    if inventory == "slow":
        assert inventory_requests == 41
        assert ticks_during_inventory >= 3
        log.warning.assert_not_called()
    else:
        log.warning.assert_called_once_with("compute.inventory_retry")
