"""J3 offline load E2E for smoke-5 OWNER 3: three fleets of three at the tenant cap.

Smoke-5 (26250cd6): three concurrent fleets x three one-sentence workers on the code
defaults (tenant cap 3), under a spawn burst that made the budget watch's reads slow.
Workers ran for minutes or were cancelled ``usage_unavailable``; in smoke-4 the same
tasks took seconds. This drives nine real workers through the orchestrator at once,
with the watch's lock-free producer read slowed to smoke-5's 2.5 s, against a fake
native OpenAI that serves every worker its own agent and session. Every worker must
succeed, none cancelled, well under the run deadline.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import re
import time
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest
from daimon.core import channel_budget
from daimon.core.fleet_orchestration import FleetOrchestrator, HostFleetPorts
from daimon.core.stores import fleet
from mux.drivers.openai.transport import SDKTransport
from mux.testing.isolated_sdk import build_async_openai

from .conversation_worker_support import conversation_root as conversation_root
from .conversation_worker_support import db_preflight as db_preflight
from .conversation_worker_support import owned_database_preflight as owned_database_preflight
from .conversation_worker_support import worker_root as worker_root
from .test_fleet_native_dispatch import _policy, _spawn_terms
from .test_openai_host import Wire

#: Smoke-5's measured slow producer read.
SLOW_READ_S = 2.5
FLEETS, WORKERS = 3, 3


_WORKER_KEY = re.compile(r"^(fleet:[0-9a-f-]+:spawn:[0-9]+):")


def _worker_key(call: httpx.Request) -> str:
    """`fleet:<run>:spawn:<ordinal>` from a worker's idempotency key."""
    match = _WORKER_KEY.match(call.headers.get("Idempotency-Key", ""))
    assert match is not None, "a worker's native write carries its spawn key"
    return match[1]


class MultiWorkerNative:
    """A fake native OpenAI for many concurrent workers: each allocated agent and each
    session gets its own id, and every session call is answered by that session's wire."""

    def __init__(self, owned_wire, agents: dict[str, dict], spend_limit: int | None) -> None:
        self._owned = owned_wire
        self.agents = dict(agents)
        self.sessions: dict[str, tuple[Wire, str]] = {}
        self._ids = itertools.count(1)
        self._spend_limit = spend_limit
        self.by_worker: dict[str, str] = {}

    def __call__(self, call: httpx.Request) -> httpx.Response:
        path = call.url.path
        if path == "/v1/agents" and call.method == "POST":
            agent_id = f"allocated-worker-{next(self._ids)}"
            self.by_worker[_worker_key(call)] = agent_id
            self.agents[agent_id] = {
                **json.loads(call.content),
                "id": agent_id,
                "created_at": 1791792000,
            }
            return httpx.Response(200, json=self.agents[agent_id])
        if path.startswith("/v1/agents/") and path.rsplit("/", 1)[1] in self.agents:
            return httpx.Response(200, json=self.agents[path.rsplit("/", 1)[1]])
        if path == "/v1/agents/environments/templates/owned-environment":
            return httpx.Response(
                200,
                json={
                    **self._owned.environment,
                    "network": {"access": "enabled"},
                    "packages": {"system": [], "python": [], "npm": []},
                },
            )
        if path == "/v1/agents/sessions" and call.method == "POST":
            # The create describes its agent inline; the worker's operation key ties it
            # to the agent that worker allocated.
            named = self.by_worker[_worker_key(call)]
            session_id = f"native-openai-session-{next(self._ids)}"
            wire = Wire(session_id=session_id)
            wire.expected_spend_limit = self._spend_limit
            self.sessions[session_id] = (wire, named)
            body = json.loads(call.content)
            assert body["environment"] == {"type": "none"} and body["input"]
            original = wire.session

            def session(original=original, named=named, metadata=body["metadata"]):
                raw = original()
                raw["agent"]["id"] = named
                raw["environment"] = {"type": "none"}
                raw["metadata"] = metadata
                return raw

            wire.session = session
            internal = httpx.Request(
                "POST",
                f"https://openai.invalid/v1/agents/sessions/{session_id}/events",
                json={"events": [{"type": "agent.session.input", "input": body["input"]}]},
                headers={"OpenAI-Beta": "agents=v1", "Idempotency-Key": "fixture:send"},
            )
            wire.handle(internal)
            for turn in wire.turns:
                turn["agent_id"] = named
            return httpx.Response(200, json=wire.session())
        for session_id, (wire, agent_id) in self.sessions.items():
            if path.startswith("/v1/agents/sessions/" + session_id):
                response = wire.handle(call)
                for turn in wire.turns:
                    turn["agent_id"] = agent_id
                if (
                    call.method == "GET"
                    and response.status_code == 200
                    and path.endswith(session_id)
                ):
                    raw = json.loads(response.content)
                    raw["agent"]["id"] = agent_id
                    return httpx.Response(200, json=raw)
                return response
        return httpx.Response(404, json={"error": {"message": f"offline: no route {path}"}})


def _ports(request, prices) -> HostFleetPorts:
    async def preparation(parent):
        return request

    return HostFleetPorts(
        request.deps.sessionmaker,
        prices=prices,
        markup=Decimal(1),
        preparation=preparation,
        pool=fleet.WorkerPool(max_concurrent_workers=3, max_concurrent_workers_per_tenant=3),
        deps=replace(request.deps, fleet_enabled=True),
    )


async def _row(request, root, worker_id: str):
    _, rows = await fleet.run_status(
        request.deps.sessionmaker, tenant_id=request.tenant_id, run_id=root.fleet.run_id
    )
    return next(row for row in rows if str(row.id) == worker_id)


@contextlib.asynccontextmanager
async def _watch_lock_busy(sessionmaker, tenant_id, monkeypatch):
    """Smoke-5 J3/J10: the watch's tenant lock attempt never gets the lock.

    Only the watch's own attempt (channel_budget's statement) is redirected to a
    dedicated key that one transaction holds for the whole test, so its short lock
    wait ends in Postgres's real lock timeout (55P03). Admissions keep the real
    tenant key. The holder the watch names rotates, like smoke-5's admission burst.
    """
    from sqlalchemy import text as real_text

    watch_lock = "SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"
    held, release = asyncio.Event(), asyncio.Event()

    def text(sql: str, *args, **kwargs):
        if sql == watch_lock:
            sql = "SELECT pg_advisory_xact_lock(hashtextextended(:key || '|watch-busy', 0))"
        return real_text(sql, *args, **kwargs)

    holders = itertools.count(10_000)

    async def holder(_session, _key):
        return {"pid": next(holders), "xact_start": "admission"}

    from sqlalchemy.ext.asyncio import create_async_engine

    # Its own connection: holding one of the app's pool for the whole test would starve
    # the workers and the watch of connections, which smoke-5 never did.
    engine = create_async_engine(sessionmaker.kw["bind"].url, pool_size=1, max_overflow=0)

    async def hold():
        async with engine.begin() as connection:
            await connection.execute(
                real_text(
                    "SELECT pg_advisory_xact_lock(hashtextextended(:key || '|watch-busy', 0))"
                ),
                {"key": f"fleet:{tenant_id}"},
            )
            held.set()
            await release.wait()

    monkeypatch.setattr(channel_budget, "text", text)
    monkeypatch.setattr(channel_budget, "_lock_holder", holder)
    task = asyncio.create_task(hold())
    ready = asyncio.create_task(held.wait())
    async with asyncio.timeout(10):
        await asyncio.wait({task, ready}, return_when=asyncio.FIRST_COMPLETED)
    if task.done():
        task.result()  # The holder failed: say why, never wait on it.
    try:
        yield
    finally:
        release.set()
        await task
        await engine.dispose()


@pytest.mark.parametrize(
    "lock",
    [
        # Load + timeouts: green on main.
        "free",
        # Smoke-5's metering kill under load: red on main. On the OWNER 3 fix the kill is
        # gone, but a watch lock held for the whole run still slows the worker turn after
        # bind past this leg's budget: ROOT item 3, owned by nc-fleet, flips it green.
        pytest.param(
            "busy",
            marks=pytest.mark.xfail(
                strict=False,
                reason="the watch parks a pooled connection on a busy lock; follow-up: watch try-lock (nc-click), ROOT item 3; flaky until try-lock",
            ),
        ),
    ],
)
async def test_three_fleets_of_three_finish_well_under_their_deadline(
    worker_root, monkeypatch, lock
):
    request, root, _, resolved, prices, _, wire, _, _ = worker_root
    import mux.drivers.openai as native_transport

    native = MultiWorkerNative(None, {}, wire.expected_spend_limit)
    real_snapshot = channel_budget.read_fleet_budget_snapshot

    async def slow_snapshot(*args, **kwargs):
        # The watch's own lock-free read only: spawn admission keeps its real speed.
        await asyncio.sleep(SLOW_READ_S)
        return await real_snapshot(*args, **kwargs)

    monkeypatch.setattr(channel_budget, "read_fleet_budget_snapshot", slow_snapshot)
    first, _ = await fleet.run_status(
        request.deps.sessionmaker, tenant_id=request.tenant_id, run_id=root.fleet.run_id
    )
    roots = []
    for index in range(FLEETS):
        run = await fleet.create_run(
            request.deps.sessionmaker,
            tenant_id=first.tenant_id,
            orchestrator_account_id=first.orchestrator_account_id,
            orchestrator_session_id=first.orchestrator_session_id,
            orchestrator_binding_id=first.orchestrator_binding_id,
            platform=first.platform,
            channel_id=first.channel_id,
            thread_id=first.thread_id,
            operation_key=f"j3-load-{index}",
            total_budget_usd=first.total_budget_usd,
            deadline_at=first.deadline_at,
            max_workers=WORKERS,
            created_by="host",
        )
        roots.append(replace(root, fleet=replace(root.fleet, run_id=run.id)))
    # One-sentence workers, each within a quarter of its run's budget.
    small = resolved.limits.model_copy(
        update={"budget_usd": min(resolved.limits.budget_usd, first.total_budget_usd / 4)}
    )
    async with (
        _watch_lock_busy(request.deps.sessionmaker, request.tenant_id, monkeypatch)
        if lock == "busy"
        else contextlib.nullcontext(),
        build_async_openai(
            transport=httpx.MockTransport(native),
            api_key="offline",
            base_url="https://openai.invalid/v1",
        ) as sdk,
    ):
        monkeypatch.setattr(
            native_transport, "configured_transport", lambda **kwargs: SDKTransport(sdk)
        )
        ports = _ports(request, prices)
        owner = FleetOrchestrator(ports)
        policy = await _policy(request)
        budget = (first.deadline_at - request.now()).total_seconds() / 2
        started = time.monotonic()
        try:
            spawned = []
            for fleet_root in roots:
                for index in range(WORKERS):
                    terms = _spawn_terms(
                        request, fleet_root, resolved, f"j3-load-{fleet_root.fleet.run_id}-{index}"
                    )
                    reply = await owner.spawn(fleet_root, policy, **(terms | {"limits": small}))
                    spawned.append((fleet_root, reply["worker_id"]))
            ended = []
            try:
                async with asyncio.timeout(budget):
                    for fleet_root, worker_id in spawned:
                        while (row := await _row(request, fleet_root, worker_id)).status in (
                            "pending",
                            "starting",
                            "running",
                        ):
                            await asyncio.sleep(0.1)
                        ended.append(row)
            except TimeoutError:
                rows = [
                    await _row(request, fleet_root, worker_id) for fleet_root, worker_id in spawned
                ]
                states = sorted(
                    (str(row.run_id)[:8], row.ordinal, row.status, row.error_code) for row in rows
                )
                pytest.fail(f"workers still unfinished at half the run deadline: {states}")
        finally:
            await ports.close()
    outcomes = sorted(
        (str(row.run_id)[:8], row.ordinal, row.status, row.error_code) for row in ended
    )
    assert all(row.status == "succeeded" for row in ended), (
        f"not every worker succeeded: {outcomes}"
    )
    assert time.monotonic() - started < budget
    assert len(native.sessions) == FLEETS * WORKERS, "each worker ran its own native session"
