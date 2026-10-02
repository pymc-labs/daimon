"""Opt-in local reproduction of warm Discord mention admission latency.

Run with ``DAIMON_BENCH_ADMISSION=1 uv run pytest -s
tests/performance/test_discord_admission_latency.py`` against a test Postgres.
The normal test suite skips this load benchmark.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import statistics
import time
import uuid
from collections import Counter
from collections.abc import Callable, Coroutine
from contextlib import ExitStack
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import McpSettings
from daimon.core.ma_resolver import ResolverCache, new_resolver_cache
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import set_platform_role_ids
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.domain import Role
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.turn_card_intents import create_turn_card_intent
from daimon.core.turn import admission as admission_module
from daimon.core.turn.admission import admit
from daimon.core.turn.deps import TurnDeps
from daimon.testing.db import build_test_engine
from daimon.testing.factories import make_channel_budget, make_ledger_entry, make_tenant
from daimon.testing.ma import MARouter
from daimon.testing.ma_models import ma_agent, ma_environment
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytest_plugins = ["daimon.testing.db"]

pytestmark = pytest.mark.skipif(
    os.environ.get("DAIMON_BENCH_ADMISSION") != "1", reason="opt-in load benchmark"
)

_N = 100
_CHANNELS = 65
_NOW = datetime.now(UTC)
_TIMINGS: contextvars.ContextVar[dict[str, float] | None] = contextvars.ContextVar(
    "admission_benchmark_timings", default=None
)


def _timed(
    name: str, function: Callable[..., Coroutine[Any, Any, Any]]
) -> Callable[..., Coroutine[Any, Any, Any]]:
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        begin = time.perf_counter()
        try:
            return await function(*args, **kwargs)
        finally:
            timings = _TIMINGS.get()
            if timings is not None:
                timings[name] = timings.get(name, 0.0) + time.perf_counter() - begin

    return wrapper


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * percentile + 0.999999) - 1)]


async def _seed(
    session: AsyncSession,
    router: MARouter,
    all_agents: list[BetaManagedAgentsAgent],
    all_environments: list[BetaEnvironment],
    *,
    channels: int,
    isolated: bool,
) -> tuple[uuid.UUID, list[str]]:
    tenant = await make_tenant(session)
    channel_ids = [str(100000000000000000 + n) for n in range(channels)]
    agents = [
        ma_agent(id=f"ag_{tenant.id.hex}_{n}", name=f"agent-{n}", tenant_id=tenant.id)
        for n in range(channels)
    ]
    environment = ma_environment(id=f"env_{tenant.id.hex}", name="default", tenant_id=tenant.id)
    all_agents.extend(agents)
    all_environments.append(environment)
    router.add_environment(environment)
    for agent in agents:
        router.add_agent(agent)
    for n, channel_id in enumerate(channel_ids):
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=channel_id),
            tenant_id=tenant.id,
            agent_name=f"agent-{n}",
            environment_name="default",
        )
        await make_channel_budget(
            session, tenant=tenant, channel_id=channel_id, limit_usd=Decimal("100")
        )
        await set_channel_admins(
            session,
            tenant_id=tenant.id,
            platform="discord",
            channel_id=channel_id,
            role_ids=[],
            user_ids=["200000000000000000"],
            actor_account_id=None,
        )
    if isolated:
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(
                sealed_channel_ids=tuple(channel_ids),
                isolated_channel_ids=tuple(channel_ids),
                agent_channel_pins={
                    f"agent-{n}": (channel_id,) for n, channel_id in enumerate(channel_ids)
                },
            ),
        )
    await make_ledger_entry(session, tenant=tenant, delta_usd=Decimal("1000"))
    await session.commit()
    return tenant.id, channel_ids


async def _run_batch(
    factory: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    targets: list[tuple[uuid.UUID, str]],
    defaults_root: Path,
    observer: AsyncEngine,
    resolver_cache: ResolverCache,
) -> tuple[list[float], Counter[str], dict[str, list[float]]]:
    deps = TurnDeps(
        anthropic=anthropic,
        sessionmaker=factory,
        deployment_default=DeploymentDefault(),
        resolver_cache=resolver_cache,
        defaults_root=defaults_root,
        mcp=McpSettings(),
        billing_config=None,
        markup=Decimal("1"),
        fernet=None,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        public_url=None,
    )
    started = asyncio.Event()
    finished = asyncio.Event()
    waits: Counter[str] = Counter()
    stage_times: dict[str, list[float]] = {}

    async def sample_waits() -> None:
        await started.wait()
        while not finished.is_set():
            async with observer.connect() as conn:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT COALESCE(wait_event_type, 'running'), "
                            "COALESCE(wait_event, 'running'), count(*) FROM pg_stat_activity "
                            "WHERE application_name = current_setting('application_name') "
                            "AND pid <> pg_backend_pid() AND state = 'active' GROUP BY 1, 2"
                        )
                    )
                ).all()
                blocked = (
                    await conn.execute(
                        text(
                            "SELECT wait_event, left(query, 120), pg_blocking_pids(pid) "
                            "FROM pg_stat_activity "
                            "WHERE application_name = current_setting('application_name') "
                            "AND wait_event_type = 'Lock'"
                        )
                    )
                ).all()
            for wait_type, wait_event, count in rows:
                waits[f"{wait_type}:{wait_event}"] += count
            for wait_event, query, blockers in blocked:
                waits[f"lock statement {wait_event}: {query} blockers={blockers}"] += 1
            await asyncio.sleep(0.02)

    async def one(index: int, tenant_id: uuid.UUID, channel_id: str) -> float:
        await started.wait()
        timings: dict[str, float] = {}
        token = _TIMINGS.set(timings)
        begin = time.perf_counter()
        try:
            await admit(
                deps,
                tenant_id=tenant_id,
                platform="discord",
                external_user_id="200000000000000000",
                channel_id=channel_id,
                thread_id=f"thread-{index}",
                role=Role.USER,
                platform_role_ids=[],
                now=_NOW,
            )
            timings["admit_total"] = time.perf_counter() - begin
            async with factory() as session:
                await create_turn_card_intent(
                    session,
                    tenant_id=tenant_id,
                    platform="discord",
                    thread_id=f"thread-{index}",
                    turn_token=uuid.uuid4(),
                )
                await session.commit()
            total = time.perf_counter() - begin
            timings["intent_total"] = total - timings["admit_total"]
            for name, duration in timings.items():
                stage_times.setdefault(name, []).append(duration)
            return total
        finally:
            _TIMINGS.reset(token)

    timed_names = (
        "get_or_create_platform_principal",
        "set_role",
        "set_platform_role_ids",
        "load_access_policy",
        "load_administered_channel_ids",
        "resolve_config",
        "resolve_agent",
        "resolve_environment",
        "is_over_balance",
        "is_over_cap",
        "is_over_channel_budget",
    )
    with ExitStack() as stack:
        for name in timed_names:
            stack.enter_context(
                patch.object(admission_module, name, _timed(name, getattr(admission_module, name)))
            )
        sampler = asyncio.create_task(sample_waits())
        tasks = [
            asyncio.create_task(one(i, tenant_id, channel_id))
            for i, (tenant_id, channel_id) in enumerate(targets)
        ]
        started.set()
        try:
            elapsed = await asyncio.gather(*tasks)
        finally:
            finished.set()
            await sampler
    return elapsed, waits, stage_times


async def test_discord_admission_100_one_tenant_vs_100_tenants(
    db_engine: AsyncEngine,
    db_schema: str,
    tmp_path: Path,
) -> None:
    url = os.environ["DAIMON_DATABASE__TEST_URL"]
    engine = build_test_engine(url, db_schema, pool_size=20, max_overflow=10)
    observer = build_test_engine(url, db_schema, pool_size=1, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    anthropic: AsyncAnthropic | None = None
    try:
        router = MARouter()
        all_agents: list[BetaManagedAgentsAgent] = []
        all_environments: list[BetaEnvironment] = []
        async with factory() as session:
            one_tenant, channels = await _seed(
                session,
                router,
                all_agents,
                all_environments,
                channels=_CHANNELS,
                isolated=True,
            )
            many: list[tuple[uuid.UUID, str]] = []
            for _ in range(_N):
                tenant_id, tenant_channels = await _seed(
                    session,
                    router,
                    all_agents,
                    all_environments,
                    channels=1,
                    isolated=True,
                )
                many.append((tenant_id, tenant_channels[0]))
        router.add_agent_list(*all_agents)
        router.add_environment_list(*all_environments)
        api_calls: Counter[str] = Counter()

        async def delayed_ma(request: httpx.Request) -> httpx.Response:
            api_calls[f"{request.method} {request.url.path}"] += 1
            await asyncio.sleep(0.2)
            return router.dispatch(request)

        anthropic = AsyncAnthropic(
            api_key="test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(delayed_ma)),
        )
        cold_resolver_cache = new_resolver_cache()
        warm_resolver_cache = new_resolver_cache()
        for agent in all_agents:
            warm_resolver_cache[
                (uuid.UUID(agent.metadata["daimon_tenant"]), "agent", agent.metadata["daimon_name"])
            ] = agent.id
        for environment in all_environments:
            warm_resolver_cache[
                (
                    uuid.UUID(environment.metadata["daimon_tenant"]),
                    "environment",
                    environment.metadata["daimon_name"],
                )
            ] = environment.id
        cases = {
            "one tenant, 65 isolated channels": [
                (one_tenant, channels[n % _CHANNELS]) for n in range(_N)
            ],
            "100 tenants, one channel each": many,
        }
        # Warm threads already have a platform principal from their first turn.
        async with factory() as session:
            for tenant_id in {tenant_id for targets in cases.values() for tenant_id, _ in targets}:
                principal = await get_or_create_platform_principal(
                    session,
                    tenant_id=tenant_id,
                    platform="discord",
                    external_id="200000000000000000",
                )
                await set_platform_role_ids(session, principal.account_id, [])
            await session.commit()
        connections = await asyncio.gather(*(engine.connect() for _ in range(20)))
        await asyncio.gather(*(connection.close() for connection in connections))
        runs = [
            (
                "one tenant, cold resolver",
                cases["one tenant, 65 isolated channels"],
                cold_resolver_cache,
            ),
            *[(label, targets, warm_resolver_cache) for label, targets in cases.items()],
        ]
        for label, targets, resolver_cache in runs:
            calls_before = api_calls.copy()
            values, waits, stages = await _run_batch(
                factory, anthropic, targets, tmp_path, observer, resolver_cache
            )
            calls = api_calls - calls_before
            call_summary = Counter(
                {
                    "agents_list": calls["GET /v1/agents"],
                    "environments_list": calls["GET /v1/environments"],
                    "agents_retrieve": sum(
                        count
                        for route, count in calls.items()
                        if route.startswith("GET /v1/agents/")
                    ),
                    "environments_retrieve": sum(
                        count
                        for route, count in calls.items()
                        if route.startswith("GET /v1/environments/")
                    ),
                }
            )
            print(
                f"\n{label}: p50={statistics.median(values):.3f}s "
                f"p95={_percentile(values, 0.95):.3f}s "
                f"max={max(values):.3f}s api_calls={dict(call_summary)} "
                f"waits={waits.most_common(10)}"
            )
            print(
                "stage p95:",
                {
                    name: round(_percentile(durations, 0.95), 3)
                    for name, durations in stages.items()
                },
            )
    finally:
        if anthropic is not None:
            await anthropic.close()
        await engine.dispose()
        await observer.dispose()
