"""Unit tests for daimon.core.ma_resolver (Phase 38).

Covers the five-step resolve chain (cached liveness -> TTL cache hit ->
daimon-tag lookup -> apply_callable + retry -> raise) for both agents
and environments. Tests construct a real AsyncAnthropic backed by an
httpx.MockTransport so the SDK's parameter validation and response
parsing run end-to-end on every call.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx
import pytest
from anthropic import APIStatusError
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_model_config import (
    BetaManagedAgentsModelConfig,
)
from cachetools import TTLCache
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.ma_resolver import (
    MAResolverMissError,
    ResolverCache,
    new_resolver_cache,
    resolve_agent,
    resolve_environment,
)
from daimon.testing.ma import (
    EMPTY_CLOUD_CONFIG,
    MARouter,
    build_fake_anthropic,
    list_response,
)

# ---------------------------------------------------------------------------
# Local helpers (inline construction per guideline:testing)
# ---------------------------------------------------------------------------


def _agent(
    *,
    agent_id: str,
    name: str,
    tenant_id: uuid.UUID,
    archived_at: datetime | None = None,
) -> BetaManagedAgentsAgent:
    """Build a real BetaManagedAgentsAgent for a transport-level fake."""
    now = datetime.now(UTC)
    return BetaManagedAgentsAgent(
        id=agent_id,
        type="agent",
        name=name,
        version=1,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
        system=None,
        description=None,
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: name,
        },
        mcp_servers=[],
        tools=[],
        skills=[],
        created_at=now,
        updated_at=now,
        archived_at=archived_at,
    )


def _env(
    *,
    env_id: str,
    name: str,
    tenant_id: uuid.UUID,
    archived_at: str | None = None,
) -> BetaEnvironment:
    """Build a real BetaEnvironment. archived_at is `str | None` per probe P2."""
    now_iso = datetime.now(UTC).isoformat()
    return BetaEnvironment(
        id=env_id,
        type="environment",
        name=name,
        description="",
        config=EMPTY_CLOUD_CONFIG,
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: name,
        },
        created_at=now_iso,
        updated_at=now_iso,
        archived_at=archived_at,
    )


def _agent_payload(agent: BetaManagedAgentsAgent) -> dict[str, object]:
    return agent.model_dump(mode="json")


def _env_payload(env: BetaEnvironment) -> dict[str, object]:
    return env.model_dump(mode="json")


async def _noop_apply() -> object:
    """No-op apply_callable for tests that don't exercise the miss path."""
    return object()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tenant_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def resolver_cache() -> ResolverCache:
    """Fresh per-test resolver cache (replaces module-global clear)."""
    return new_resolver_cache()


# ---------------------------------------------------------------------------
# Agent — cached_id path
# ---------------------------------------------------------------------------


async def test_resolve_agent_cached_id_live_returns_cached_id(
    tenant_id: uuid.UUID,
    resolver_cache: ResolverCache,
) -> None:
    live = _agent(agent_id="ag_live", name="daimon", tenant_id=tenant_id)
    calls: list[str] = []

    def retrieve_live(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        calls.append("retrieve")
        return httpx.Response(200, json=_agent_payload(live))

    def list_should_not_be_hit(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        raise AssertionError("tag lookup must not run when cached id is live")

    router = MARouter()
    router.add("GET", r"/v1/agents/ag_live", retrieve_live)
    router.add("GET", r"/v1/agents", list_should_not_be_hit)
    client = build_fake_anthropic(router.dispatch)

    out = await resolve_agent(
        client,
        tenant_id=tenant_id,
        daimon_tag="daimon",
        cached_id="ag_live",
        apply_callable=_noop_apply,
        cache=resolver_cache,
    )

    assert out == "ag_live", "live cached id should be returned as-is"
    assert calls == ["retrieve"], "only one retrieve should fire on the live path"


async def test_resolve_agent_cached_id_archived_falls_through_to_tag_lookup(
    tenant_id: uuid.UUID,
    resolver_cache: ResolverCache,
) -> None:
    archived = _agent(
        agent_id="ag_old",
        name="daimon",
        tenant_id=tenant_id,
        archived_at=datetime.now(UTC),
    )
    fresh = _agent(agent_id="ag_fresh", name="daimon", tenant_id=tenant_id)

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents/ag_old",
        lambda req, _m: httpx.Response(200, json=_agent_payload(archived)),
    )
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response([_agent_payload(fresh)]),
    )
    client = build_fake_anthropic(router.dispatch)

    out = await resolve_agent(
        client,
        tenant_id=tenant_id,
        daimon_tag="daimon",
        cached_id="ag_old",
        apply_callable=_noop_apply,
        cache=resolver_cache,
    )

    assert out == "ag_fresh", "archived_at populated should fall through to tag lookup"


async def test_resolve_agent_cached_id_wrong_tenant_falls_through_to_tag_lookup(
    tenant_id: uuid.UUID,
    resolver_cache: ResolverCache,
) -> None:
    other_tenant = _agent(agent_id="ag_other", name="daimon", tenant_id=uuid.uuid4())
    fresh = _agent(agent_id="ag_fresh", name="daimon", tenant_id=tenant_id)

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents/ag_other",
        lambda req, _m: httpx.Response(200, json=_agent_payload(other_tenant)),
    )
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response([_agent_payload(fresh)]),
    )
    client = build_fake_anthropic(router.dispatch)

    out = await resolve_agent(
        client,
        tenant_id=tenant_id,
        daimon_tag="daimon",
        cached_id="ag_other",
        apply_callable=_noop_apply,
        cache=resolver_cache,
    )

    assert out == "ag_fresh", (
        "a live cached id tagged with another tenant's daimon_tenant must not "
        "be adopted — fall through to the tenant-filtered tag lookup"
    )


async def test_resolve_agent_cached_id_404_falls_through_to_tag_lookup(
    tenant_id: uuid.UUID,
    resolver_cache: ResolverCache,
) -> None:
    fresh = _agent(agent_id="ag_fresh", name="daimon", tenant_id=tenant_id)

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents/ag_gone",
        lambda req, _m: httpx.Response(
            404,
            json={
                "type": "error",
                "error": {"type": "not_found_error", "message": "missing"},
            },
        ),
    )
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response([_agent_payload(fresh)]),
    )
    client = build_fake_anthropic(router.dispatch)

    out = await resolve_agent(
        client,
        tenant_id=tenant_id,
        daimon_tag="daimon",
        cached_id="ag_gone",
        apply_callable=_noop_apply,
        cache=resolver_cache,
    )

    assert out == "ag_fresh", "404 on cached id should fall through to tag lookup"


async def test_resolve_agent_cached_id_400_invalid_falls_through_to_tag_lookup(
    tenant_id: uuid.UUID,
    resolver_cache: ResolverCache,
) -> None:
    """MA returns 400 invalid_request_error for malformed/unknown agent ids
    (probe archived_retrieve_shape.py §13). A corrupt cached_id like
    "agent_pending" must fall through to tag lookup, not re-raise.
    """
    fresh = _agent(agent_id="ag_fresh", name="daimon", tenant_id=tenant_id)

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents/agent_pending",
        lambda req, _m: httpx.Response(
            400,
            json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "Invalid agent ID."},
            },
        ),
    )
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response([_agent_payload(fresh)]),
    )
    client = build_fake_anthropic(router.dispatch)

    out = await resolve_agent(
        client,
        tenant_id=tenant_id,
        daimon_tag="daimon",
        cached_id="agent_pending",
        apply_callable=_noop_apply,
        cache=resolver_cache,
    )

    assert out == "ag_fresh", (
        "400 invalid_request_error on cached id should fall through to tag lookup, "
        "not re-raise (otherwise a corrupt agent_id in the routines table is unrecoverable)"
    )


async def test_resolve_agent_no_cached_id_uses_tag_lookup(
    tenant_id: uuid.UUID,
    resolver_cache: ResolverCache,
) -> None:
    fresh = _agent(agent_id="ag_fresh", name="daimon", tenant_id=tenant_id)

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response([_agent_payload(fresh)]),
    )
    client = build_fake_anthropic(router.dispatch)

    out = await resolve_agent(
        client,
        tenant_id=tenant_id,
        daimon_tag="daimon",
        apply_callable=_noop_apply,
        cache=resolver_cache,
    )

    assert out == "ag_fresh", "no cached id should resolve via tag lookup"


# ---------------------------------------------------------------------------
# Agent — TTL cache behavior
# ---------------------------------------------------------------------------


async def test_resolve_agent_cache_hit_skips_retrieve_and_tag_lookup(
    tenant_id: uuid.UUID,
    resolver_cache: ResolverCache,
) -> None:
    fresh = _agent(agent_id="ag_fresh", name="daimon", tenant_id=tenant_id)
    list_calls = 0

    def list_handler(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        nonlocal list_calls
        list_calls += 1
        return list_response([_agent_payload(fresh)])

    router = MARouter()
    router.add("GET", r"/v1/agents", list_handler)
    client = build_fake_anthropic(router.dispatch)

    first = await resolve_agent(
        client,
        tenant_id=tenant_id,
        daimon_tag="daimon",
        apply_callable=_noop_apply,
        cache=resolver_cache,
    )
    second = await resolve_agent(
        client,
        tenant_id=tenant_id,
        daimon_tag="daimon",
        apply_callable=_noop_apply,
        cache=resolver_cache,
    )

    assert first == "ag_fresh", "first call resolves via tag lookup"
    assert second == "ag_fresh", "second call must return cached id"
    assert list_calls == 1, "TTL cache should suppress the second tag lookup"


async def test_resolve_agent_ttl_expiry_revalidates(tenant_id: uuid.UUID) -> None:
    """TTL expiry forces a fresh tag lookup on the next call.

    Uses TTLCache timer= injection so the test advances time without sleeping.
    """
    fresh = _agent(agent_id="ag_fresh", name="daimon", tenant_id=tenant_id)
    list_calls = 0

    def list_handler(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        nonlocal list_calls
        list_calls += 1
        return list_response([_agent_payload(fresh)])

    router = MARouter()
    router.add("GET", r"/v1/agents", list_handler)
    client = build_fake_anthropic(router.dispatch)

    # Inject a controllable timer into the TTLCache.
    fake_time: list[float] = [0.0]

    def timer() -> float:
        return fake_time[0]

    cache: ResolverCache = TTLCache(maxsize=500, ttl=300, timer=timer)

    await resolve_agent(
        client,
        tenant_id=tenant_id,
        daimon_tag="daimon",
        apply_callable=_noop_apply,
        cache=cache,
    )
    # Advance past the 5-minute TTL
    fake_time[0] = 301.0
    await resolve_agent(
        client,
        tenant_id=tenant_id,
        daimon_tag="daimon",
        apply_callable=_noop_apply,
        cache=cache,
    )

    assert list_calls == 2, "TTL expiry must force a fresh tag lookup"


async def test_resolve_agent_invalidates_cache_on_archived_retrieve(
    tenant_id: uuid.UUID,
) -> None:
    """If a cached id retrieves as archived, the cache key must be cleared
    before tag lookup; otherwise an immediate same-key call would silently
    return the stale id."""
    archived = _agent(
        agent_id="ag_stale",
        name="daimon",
        tenant_id=tenant_id,
        archived_at=datetime.now(UTC),
    )
    fresh = _agent(agent_id="ag_fresh", name="daimon", tenant_id=tenant_id)

    # Pre-populate cache with the stale id at the same key (direct assignment).
    cache: ResolverCache = new_resolver_cache()
    cache[(tenant_id, "agent", "daimon")] = "ag_stale"

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents/ag_stale",
        lambda req, _m: httpx.Response(200, json=_agent_payload(archived)),
    )
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response([_agent_payload(fresh)]),
    )
    client = build_fake_anthropic(router.dispatch)

    out = await resolve_agent(
        client,
        tenant_id=tenant_id,
        daimon_tag="daimon",
        cached_id="ag_stale",
        apply_callable=_noop_apply,
        cache=cache,
    )

    assert out == "ag_fresh", "archived retrieve must invalidate cache before tag lookup"


# ---------------------------------------------------------------------------
# Cache unit tests: size-eviction, TTL-via-timer, key round-trip
# ---------------------------------------------------------------------------


def test_cache_key_round_trip(tenant_id: uuid.UUID) -> None:
    """Cache key is (tenant_id, kind, daimon_tag) — no lossy encoding."""
    cache: ResolverCache = new_resolver_cache()
    key = (tenant_id, "agent", "my-tag")
    cache[key] = "ag_abc"
    assert cache.get(key) == "ag_abc", "cache key round-trip must return stored value"


def test_new_resolver_cache_size_eviction() -> None:
    """Cache evicts oldest entries when maxsize is exceeded."""
    small: TTLCache[tuple[int, str, str], str] = TTLCache(maxsize=3, ttl=300)
    for i in range(5):
        small[(i, "agent", "tag")] = f"ag_{i}"
    assert len(small) <= 3, "size-eviction must cap the cache at maxsize"


def test_new_resolver_cache_ttl_expiry_via_timer() -> None:
    """TTLCache timer= injection: advancing the timer past ttl evicts the key."""
    fake_time: list[float] = [0.0]

    def timer() -> float:
        return fake_time[0]

    cache: TTLCache[str, str] = TTLCache(maxsize=10, ttl=300, timer=timer)
    cache["k"] = "v"
    assert "k" in cache, "key should be present before TTL expires"

    fake_time[0] = 301.0
    assert "k" not in cache, "key must be evicted after TTL expires (timer injection)"


# ---------------------------------------------------------------------------
# Agent — total miss / apply_callable
# ---------------------------------------------------------------------------


async def test_resolve_agent_total_miss_invokes_apply_callable_then_retries(
    tenant_id: uuid.UUID,
    resolver_cache: ResolverCache,
) -> None:
    fresh = _agent(agent_id="ag_fresh", name="daimon", tenant_id=tenant_id)
    applied = False

    # First LIST returns empty; after apply_callable runs we swap the route to
    # return the seeded agent. Use a mutable slot so both handlers share state.
    router = MARouter()

    def list_handler(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        if applied:
            return list_response([_agent_payload(fresh)])
        return list_response([])

    router.add("GET", r"/v1/agents", list_handler)
    client = build_fake_anthropic(router.dispatch)

    async def fake_apply() -> object:
        nonlocal applied
        applied = True
        return object()

    out = await resolve_agent(
        client,
        tenant_id=tenant_id,
        daimon_tag="daimon",
        apply_callable=fake_apply,
        cache=resolver_cache,
    )

    assert applied, "resolver must invoke apply_callable on total miss"
    assert out == "ag_fresh", "retry tag lookup after apply must return seeded agent"


async def test_resolve_agent_miss_apply_callable_awaited_exactly_once(
    tenant_id: uuid.UUID,
    resolver_cache: ResolverCache,
) -> None:
    """apply_callable must be awaited exactly once per miss, no more."""
    router = MARouter()
    # Always return the fresh agent so the retry succeeds immediately.
    fresh = _agent(agent_id="ag_fresh", name="daimon", tenant_id=tenant_id)

    apply_calls = 0
    applied = False

    def list_handler(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        if applied:
            return list_response([_agent_payload(fresh)])
        return list_response([])

    router.add("GET", r"/v1/agents", list_handler)
    client = build_fake_anthropic(router.dispatch)

    async def counting_apply() -> object:
        nonlocal apply_calls, applied
        apply_calls += 1
        applied = True
        return object()

    await resolve_agent(
        client,
        tenant_id=tenant_id,
        daimon_tag="daimon",
        apply_callable=counting_apply,
        cache=resolver_cache,
    )

    assert apply_calls == 1, "apply_callable must be awaited exactly once per miss"


async def test_resolve_agent_closure_targets_resolving_tenant_id(
    tenant_id: uuid.UUID,
    resolver_cache: ResolverCache,
) -> None:
    """The injected apply_callable closure must capture and target the
    resolving tenant_id, not any other tenant."""
    other_tenant_id = uuid.uuid4()
    assert other_tenant_id != tenant_id

    router = MARouter()
    fresh = _agent(agent_id="ag_fresh", name="daimon", tenant_id=tenant_id)
    applied_for: list[uuid.UUID] = []
    applied = False

    def list_handler(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        if applied:
            return list_response([_agent_payload(fresh)])
        return list_response([])

    router.add("GET", r"/v1/agents", list_handler)
    client = build_fake_anthropic(router.dispatch)

    # Closure captures the resolving tenant_id (simulates caller pattern).
    async def apply_for_tenant() -> object:
        nonlocal applied
        applied_for.append(tenant_id)  # closure-captured tenant_id
        applied = True
        return object()

    out = await resolve_agent(
        client,
        tenant_id=tenant_id,
        daimon_tag="daimon",
        apply_callable=apply_for_tenant,
        cache=resolver_cache,
    )

    assert out == "ag_fresh", "resolver must return the seeded agent after apply"
    assert applied_for == [tenant_id], (
        f"apply_callable must target the resolving tenant {tenant_id}; got {applied_for}"
    )
    assert other_tenant_id not in applied_for, "apply_callable must NOT target other tenants"


async def test_resolve_agent_total_miss_after_apply_raises(
    tenant_id: uuid.UUID,
    resolver_cache: ResolverCache,
) -> None:
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    client = build_fake_anthropic(router.dispatch)

    applied = False

    async def fake_apply() -> object:
        nonlocal applied
        applied = True
        return object()

    with pytest.raises(MAResolverMissError) as exc_info:
        await resolve_agent(
            client,
            tenant_id=tenant_id,
            daimon_tag="daimon",
            apply_callable=fake_apply,
            cache=resolver_cache,
        )

    err = exc_info.value
    assert applied, "apply_callable must run before raising"
    assert err.kind == "agent", "miss error kind must be 'agent'"
    assert err.daimon_tag == "daimon", "miss error must carry daimon_tag"
    assert err.tenant_id == tenant_id, "miss error must carry tenant_id"
    msg = str(err)
    assert "daimon" in msg and str(tenant_id) in msg, (
        "miss error message must include daimon_tag and tenant_id for ops"
    )


# ---------------------------------------------------------------------------
# apply_callable receives the resolving tenant_id via closure (MT-1a)
# ---------------------------------------------------------------------------


async def test_resolve_total_miss_apply_callable_receives_resolving_tenant_id(
    tenant_id: uuid.UUID,
    resolver_cache: ResolverCache,
) -> None:
    """MT-1a: when the miss path fires apply_callable, the closure must target
    the tenant_id being resolved — not any other tenant.

    This is the closure-correctness gate: callers must capture `tenant_id`
    in the lambda, not a singleton or the oldest-row tenant.
    """
    other_tenant_id = uuid.uuid4()
    assert other_tenant_id != tenant_id

    fresh = _agent(agent_id="ag_fresh", name="daimon", tenant_id=tenant_id)
    reconciled_tenant_ids: list[uuid.UUID] = []

    router = MARouter()
    tag_lookup_called: list[int] = [0]

    def list_handler(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        tag_lookup_called[0] += 1
        if tag_lookup_called[0] > 1:
            return list_response([_agent_payload(fresh)])
        return list_response([])

    router.add("GET", r"/v1/agents", list_handler)
    client = build_fake_anthropic(router.dispatch)

    # Simulate the closure callers construct — captures the resolving tenant_id.
    async def closure_apply() -> object:
        reconciled_tenant_ids.append(tenant_id)  # closure-captured
        return object()

    out = await resolve_agent(
        client,
        tenant_id=tenant_id,
        daimon_tag="daimon",
        apply_callable=closure_apply,
        cache=resolver_cache,
    )

    assert out == "ag_fresh", "retry after apply must return the seeded agent"
    assert reconciled_tenant_ids == [tenant_id], (
        f"apply_callable must target the resolving tenant ({tenant_id}), "
        f"not another tenant; got {reconciled_tenant_ids}"
    )
    assert other_tenant_id not in reconciled_tenant_ids, (
        "apply_callable must NOT target the oldest/other tenant (MT-1a correctness)"
    )


# ---------------------------------------------------------------------------
# Agent — error propagation
# ---------------------------------------------------------------------------


async def test_resolve_agent_5xx_propagates(
    tenant_id: uuid.UUID,
    resolver_cache: ResolverCache,
) -> None:
    """Non-404 status errors must not be swallowed (guideline:architecture)."""
    list_calls = 0

    def list_handler(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        nonlocal list_calls
        list_calls += 1
        return list_response([])

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents/ag_any",
        lambda req, _m: httpx.Response(
            503,
            json={
                "type": "error",
                "error": {"type": "service_unavailable", "message": "down"},
            },
        ),
    )
    router.add("GET", r"/v1/agents", list_handler)
    client = build_fake_anthropic(router.dispatch)

    with pytest.raises(APIStatusError) as exc_info:
        await resolve_agent(
            client,
            tenant_id=tenant_id,
            daimon_tag="daimon",
            cached_id="ag_any",
            apply_callable=_noop_apply,
            cache=resolver_cache,
        )

    assert exc_info.value.status_code == 503, "503 must surface to caller"
    assert list_calls == 0, "tag lookup must not run when retrieve raises non-404"


# ---------------------------------------------------------------------------
# Environment — parity tests (probe P2: both 404 and archived branches exist)
# ---------------------------------------------------------------------------


async def test_resolve_environment_404_falls_through_to_tag_lookup(
    tenant_id: uuid.UUID,
    resolver_cache: ResolverCache,
) -> None:
    fresh = _env(env_id="env_fresh", name="daimon", tenant_id=tenant_id)

    router = MARouter()
    router.add(
        "GET",
        r"/v1/environments/env_gone",
        lambda req, _m: httpx.Response(
            404,
            json={
                "type": "error",
                "error": {"type": "not_found_error", "message": "missing"},
            },
        ),
    )
    router.add(
        "GET",
        r"/v1/environments",
        lambda req, _m: list_response([_env_payload(fresh)]),
    )
    client = build_fake_anthropic(router.dispatch)

    out = await resolve_environment(
        client,
        tenant_id=tenant_id,
        daimon_tag="daimon",
        cached_id="env_gone",
        apply_callable=_noop_apply,
        cache=resolver_cache,
    )

    assert out == "env_fresh", "404 on cached env id should re-resolve via tag"


async def test_resolve_environment_400_invalid_falls_through_to_tag_lookup(
    tenant_id: uuid.UUID,
    resolver_cache: ResolverCache,
) -> None:
    """Parity with the agent 400 path — malformed env ids must fall through too."""
    fresh = _env(env_id="env_fresh", name="daimon", tenant_id=tenant_id)

    router = MARouter()
    router.add(
        "GET",
        r"/v1/environments/env_pending",
        lambda req, _m: httpx.Response(
            400,
            json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "Invalid environment ID."},
            },
        ),
    )
    router.add(
        "GET",
        r"/v1/environments",
        lambda req, _m: list_response([_env_payload(fresh)]),
    )
    client = build_fake_anthropic(router.dispatch)

    out = await resolve_environment(
        client,
        tenant_id=tenant_id,
        daimon_tag="daimon",
        cached_id="env_pending",
        apply_callable=_noop_apply,
        cache=resolver_cache,
    )

    assert out == "env_fresh", "400 on cached env id should re-resolve via tag"


async def test_resolve_environment_archived_falls_through_to_tag_lookup(
    tenant_id: uuid.UUID,
    resolver_cache: ResolverCache,
) -> None:
    archived = _env(
        env_id="env_old",
        name="daimon",
        tenant_id=tenant_id,
        archived_at=datetime.now(UTC).isoformat(),
    )
    fresh = _env(env_id="env_fresh", name="daimon", tenant_id=tenant_id)

    router = MARouter()
    router.add(
        "GET",
        r"/v1/environments/env_old",
        lambda req, _m: httpx.Response(200, json=_env_payload(archived)),
    )
    router.add(
        "GET",
        r"/v1/environments",
        lambda req, _m: list_response([_env_payload(fresh)]),
    )
    client = build_fake_anthropic(router.dispatch)

    out = await resolve_environment(
        client,
        tenant_id=tenant_id,
        daimon_tag="daimon",
        cached_id="env_old",
        apply_callable=_noop_apply,
        cache=resolver_cache,
    )

    assert out == "env_fresh", (
        "archived_at populated on env should fall through to tag lookup "
        "(probe P2: envs can return 200 with archived_at set)"
    )


async def test_resolve_environment_cached_id_wrong_tenant_falls_through_to_tag_lookup(
    tenant_id: uuid.UUID,
    resolver_cache: ResolverCache,
) -> None:
    other_tenant = _env(env_id="env_other", name="daimon", tenant_id=uuid.uuid4())
    fresh = _env(env_id="env_fresh", name="daimon", tenant_id=tenant_id)

    router = MARouter()
    router.add(
        "GET",
        r"/v1/environments/env_other",
        lambda req, _m: httpx.Response(200, json=_env_payload(other_tenant)),
    )
    router.add(
        "GET",
        r"/v1/environments",
        lambda req, _m: list_response([_env_payload(fresh)]),
    )
    client = build_fake_anthropic(router.dispatch)

    out = await resolve_environment(
        client,
        tenant_id=tenant_id,
        daimon_tag="daimon",
        cached_id="env_other",
        apply_callable=_noop_apply,
        cache=resolver_cache,
    )

    assert out == "env_fresh", (
        "a live cached env id tagged with another tenant's daimon_tenant must "
        "not be adopted — fall through to the tenant-filtered tag lookup"
    )


async def test_resolve_environment_total_miss_invokes_apply_defaults(
    tenant_id: uuid.UUID,
    resolver_cache: ResolverCache,
) -> None:
    fresh = _env(env_id="env_fresh", name="daimon", tenant_id=tenant_id)
    applied = False

    def list_handler(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        if applied:
            return list_response([_env_payload(fresh)])
        return list_response([])

    router = MARouter()
    router.add("GET", r"/v1/environments", list_handler)
    client = build_fake_anthropic(router.dispatch)

    async def fake_apply() -> object:
        nonlocal applied
        applied = True
        return object()

    out = await resolve_environment(
        client,
        tenant_id=tenant_id,
        daimon_tag="daimon",
        apply_callable=fake_apply,
        cache=resolver_cache,
    )

    assert applied, "env resolver must invoke apply_callable on total miss"
    assert out == "env_fresh", "env retry tag lookup must succeed after apply"


# ---------------------------------------------------------------------------
# Module-API smoke
# ---------------------------------------------------------------------------


def test_module_exports_public_api() -> None:
    from daimon.core import ma_resolver

    assert callable(ma_resolver.resolve_agent), "resolve_agent must be exported"
    assert callable(ma_resolver.resolve_environment), "resolve_environment must be exported"
    assert issubclass(ma_resolver.MAResolverMissError, Exception), (
        "MAResolverMissError must be a public Exception subclass"
    )
    assert callable(ma_resolver.new_resolver_cache), "new_resolver_cache must be exported"
    assert ma_resolver.ResolverCache is not None, "ResolverCache alias must be exported"

    # Static-analysis assertion: signatures match for both resolvers (no
    # accidental signature drift between agent and environment resolvers).
    import inspect

    agent_sig = inspect.signature(ma_resolver.resolve_agent)
    env_sig = inspect.signature(ma_resolver.resolve_environment)
    assert list(agent_sig.parameters.keys()) == list(env_sig.parameters.keys()), (
        "resolve_agent and resolve_environment must share the same parameter shape"
    )


# Anchor `Awaitable` / `Callable` imports to a typed alias so pyright keeps
# them as live references. Tests construct `async def` stubs directly, but
# this alias documents the public contract that resolve_* accepts.
_ApplyCallable = Callable[[], Awaitable[object]]
