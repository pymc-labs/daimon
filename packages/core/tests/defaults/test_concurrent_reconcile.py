from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.core.defaults._reconcile import _reconcile_core
from daimon.core.ma_resolver import new_resolver_cache, resolve_agent
from daimon.testing.ma_models import ma_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def _test_dsn() -> str:
    dsn = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not dsn:
        pytest.skip("DAIMON_DATABASE__TEST_URL must be set for the concurrency test")
    return dsn


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> AsyncAnthropic:
    return AsyncAnthropic(
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_retries=0,
    )


def _defaults_tree(root: Path) -> None:
    (root / "agents").mkdir()
    (root / "agents" / "daimon.yaml").write_text(
        "name: daimon\nmodel: claude-sonnet-4-6\nsystem: hello\n"
    )


async def _reconcile(
    client: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    defaults_root: Path,
    tenant_id: uuid.UUID,
) -> object:
    return await _reconcile_core(
        client,
        session_factory,
        defaults_root,
        tenant_id=tenant_id,
        account_id=None,
        dry_run=False,
        run_preflight=False,
        public_url=None,
    )


async def test_concurrent_same_tenant_reconciles_serialize_create_and_dedup(
    tmp_path: Path,
) -> None:
    """Cross-connection calls must not create an ID another reconcile archives."""
    dsn = _test_dsn()
    engine_a = create_async_engine(dsn)
    engine_b = create_async_engine(dsn)
    session_factory_a = async_sessionmaker(engine_a, expire_on_commit=False)
    session_factory_b = async_sessionmaker(engine_b, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    defaults_root = tmp_path
    _defaults_tree(defaults_root)

    agents: list[dict[str, Any]] = []
    post_ids = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_ids
        if request.method == "GET" and request.url.path == "/v1/agents":
            active = [agent for agent in agents if agent["archived_at"] is None]
            return httpx.Response(200, json={"data": active, "next_page": None})
        if request.method == "POST" and request.url.path == "/v1/agents":
            post_ids += 1
            await asyncio.sleep(0.1)  # expose the list-then-create race without the DB lock
            payload = json.loads(request.content)
            agent = ma_agent(
                id=f"ag_race_{post_ids}",
                name="daimon",
                model=payload["model"],
                metadata=payload["metadata"],
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            ).model_dump(mode="json")
            agents.append(agent)
            return httpx.Response(200, json=agent)
        if request.method == "POST" and request.url.path.endswith("/archive"):
            agent_id = request.url.path.split("/")[-2]
            for agent in agents:
                if agent["id"] == agent_id:
                    agent["archived_at"] = "2026-01-02T00:00:00Z"
                    return httpx.Response(200, json=agent)
            return httpx.Response(404, json={"error": "not found"})
        if request.method == "GET" and request.url.path.startswith("/v1/agents/"):
            agent_id = request.url.path.rsplit("/", maxsplit=1)[-1]
            for agent in agents:
                if agent["id"] == agent_id:
                    return httpx.Response(200, json=agent)
            return httpx.Response(404, json={"error": "not found"})
        if request.method == "GET" and request.url.path in {
            "/v1/environments",
            "/v1/skills",
        }:
            return httpx.Response(200, json={"data": [], "next_page": None})
        raise AssertionError(f"unexpected fake MA request: {request.method} {request.url}")

    client_a = _client(handler)
    client_b = _client(handler)
    try:
        await asyncio.gather(
            _reconcile(client_a, session_factory_a, defaults_root, tenant_id),
            _reconcile(client_b, session_factory_b, defaults_root, tenant_id),
        )

        active = [agent for agent in agents if agent["archived_at"] is None]
        assert post_ids == 1, f"the same tenant should create one agent, got {post_ids}"
        assert len(active) == 1, f"only one managed agent should remain active, got {active!r}"

        resolved_id = await resolve_agent(
            client_a,
            tenant_id=tenant_id,
            daimon_tag="daimon",
            apply_callable=lambda: _reconcile(
                client_a, session_factory_a, defaults_root, tenant_id
            ),
            cache=new_resolver_cache(),
        )
        assert resolved_id == active[0]["id"], "resolver should choose the sole active agent"
        resolved = await client_a.beta.agents.retrieve(resolved_id)
        assert resolved.archived_at is None, "a successful reconcile must not cache an archived ID"
    finally:
        await client_a.close()
        await client_b.close()
        await engine_a.dispose()
        await engine_b.dispose()


async def test_cancelled_reconcile_releases_tenant_lock(tmp_path: Path) -> None:
    """Cancellation rolls back the advisory-lock transaction for the next caller."""
    dsn = _test_dsn()
    engine_a = create_async_engine(dsn)
    engine_b = create_async_engine(dsn)
    session_factory_a = async_sessionmaker(engine_a, expire_on_commit=False)
    session_factory_b = async_sessionmaker(engine_b, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    defaults_root = tmp_path
    _defaults_tree(defaults_root)

    agents: list[dict[str, Any]] = []
    first_create_started = asyncio.Event()
    release_first_create = asyncio.Event()
    list_count = 0
    second_list_started = asyncio.Event()
    post_ids = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal list_count, post_ids
        if request.method == "GET" and request.url.path == "/v1/agents":
            list_count += 1
            if list_count > 1:
                second_list_started.set()
            active = [agent for agent in agents if agent["archived_at"] is None]
            return httpx.Response(200, json={"data": active, "next_page": None})
        if request.method == "POST" and request.url.path == "/v1/agents":
            post_ids += 1
            if post_ids == 1:
                first_create_started.set()
                await release_first_create.wait()
            payload = json.loads(request.content)
            agent = ma_agent(
                id=f"ag_cancel_{post_ids}",
                name="daimon",
                model=payload["model"],
                metadata=payload["metadata"],
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            ).model_dump(mode="json")
            agents.append(agent)
            return httpx.Response(200, json=agent)
        if request.method == "POST" and request.url.path.endswith("/archive"):
            agent_id = request.url.path.split("/")[-2]
            for agent in agents:
                if agent["id"] == agent_id:
                    agent["archived_at"] = "2026-01-02T00:00:00Z"
                    return httpx.Response(200, json=agent)
            return httpx.Response(404, json={"error": "not found"})
        if request.method == "GET" and request.url.path in {
            "/v1/environments",
            "/v1/skills",
        }:
            return httpx.Response(200, json={"data": [], "next_page": None})
        raise AssertionError(f"unexpected fake MA request: {request.method} {request.url}")

    client_a = _client(handler)
    client_b = _client(handler)
    first: asyncio.Task[object] | None = None
    second: asyncio.Task[object] | None = None
    try:
        first = asyncio.create_task(
            _reconcile(client_a, session_factory_a, defaults_root, tenant_id)
        )
        await asyncio.wait_for(first_create_started.wait(), timeout=5)
        second = asyncio.create_task(
            _reconcile(client_b, session_factory_b, defaults_root, tenant_id)
        )
        await asyncio.sleep(0.1)
        assert not second_list_started.is_set(), "second caller must block behind the tenant lock"

        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release_first_create.set()
        await asyncio.wait_for(second, timeout=5)
        assert second_list_started.is_set(), "next caller should proceed after cancellation"
    finally:
        if first is not None and not first.done():
            first.cancel()
        release_first_create.set()
        if second is not None and not second.done():
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
        await client_a.close()
        await client_b.close()
        await engine_a.dispose()
        await engine_b.dispose()
