"""MA resolver: live-id lookup with TTL cache and self-heal via injected apply_callable.

Per fire/turn, the scheduler/Discord/CLI ask `resolve_agent` /
`resolve_environment` for a live MA id by daimon-tag. Internal chain:
cached_id retrieve (archived_at liveness + daimon_tenant ownership check) ->
TTL cache hit -> daimon-tag lookup -> apply_callable + retry ->
MAResolverMissError.

`archived_at is None` is the load-bearing liveness invariant. MA returns
200 with `archived_at` populated for archived agents (probe P1) and for
archived environments (probe P2). Treating only 404 as "missing" lets a
stale cached id flow downstream and corrupt downstream sessions.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Literal

import structlog
from anthropic import APIStatusError, AsyncAnthropic
from cachetools import TTLCache
from daimon.core.defaults.ma_index import (
    list_agents_by_tenant,
    list_environments_by_tenant,
)
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_TENANT,
)

_log = structlog.get_logger(__name__)

Kind = Literal["agent", "environment"]
_CacheKey = tuple[uuid.UUID, Kind, str]


class ResolverCache(TTLCache[_CacheKey, str]):
    """Resolver ids and one shared listing per tenant and resource kind."""

    def __init__(
        self, maxsize: int = 500, ttl: float = 1800, timer: Callable[[], float] = monotonic
    ) -> None:
        super().__init__(maxsize=maxsize, ttl=ttl, timer=timer)
        self.inflight: dict[tuple[uuid.UUID, Kind], asyncio.Task[dict[str, str]]] = {}


def new_resolver_cache() -> ResolverCache:
    """Construct a bounded TTL cache for MA resolver results.

    maxsize=500: supports 100-tenant deployments with 5 simultaneous
    agent+environment resolutions each without eviction.
    ttl=1800: 30 minutes limits org-wide MA listings during active turns.
    """
    return ResolverCache()


class MAResolverMissError(Exception):
    """Resolver could not produce a live MA id even after apply_callable ran."""

    def __init__(self, *, kind: Kind, tenant_id: uuid.UUID, daimon_tag: str) -> None:
        super().__init__(
            f"could not resolve {kind} {daimon_tag!r} for tenant {tenant_id} "
            f"(apply_defaults did not produce a matching resource)"
        )
        self.kind: Kind = kind
        self.tenant_id: uuid.UUID = tenant_id
        self.daimon_tag: str = daimon_tag


async def _is_live_agent(client: AsyncAnthropic, agent_id: str, tenant_id: uuid.UUID) -> bool:
    try:
        obj = await client.beta.agents.retrieve(agent_id)
    except APIStatusError as err:
        # MA returns 400 for malformed / unknown ids (probe
        # `archived_retrieve_shape.py` §13: status_code=400,
        # type=invalid_request_error, message="Invalid agent ID.") and 404 for
        # ids that fail an internal lookup. Both mean "this cached id won't
        # ever resolve a live resource"; fall through to tag lookup so a
        # corrupt DB cell can self-heal. 5xx / 401 / 403 / 429 still propagate.
        if err.status_code in (400, 404):
            return False
        raise
    # Ownership check alongside liveness: a stored id pointing at another
    # tenant's live agent (re-key drift) must not be adopted — fall through to
    # the tenant-filtered tag lookup instead.
    return obj.archived_at is None and obj.metadata.get(MA_METADATA_KEY_TENANT) == str(tenant_id)


async def _is_live_environment(client: AsyncAnthropic, env_id: str, tenant_id: uuid.UUID) -> bool:
    try:
        obj = await client.beta.environments.retrieve(env_id)
    except APIStatusError as err:
        # MA returns 400 for malformed / unknown ids (probe
        # `archived_retrieve_shape.py` §13: status_code=400,
        # type=invalid_request_error, message="Invalid agent ID.") and 404 for
        # ids that fail an internal lookup. Both mean "this cached id won't
        # ever resolve a live resource"; fall through to tag lookup so a
        # corrupt DB cell can self-heal. 5xx / 401 / 403 / 429 still propagate.
        if err.status_code in (400, 404):
            return False
        raise
    # Same ownership check as _is_live_agent (issue #198).
    return obj.archived_at is None and obj.metadata.get(MA_METADATA_KEY_TENANT) == str(tenant_id)


async def _list_tag_ids(client: AsyncAnthropic, tenant_id: uuid.UUID, kind: Kind) -> dict[str, str]:
    if kind == "agent":
        resources = await list_agents_by_tenant(client, tenant_id=tenant_id)
    else:
        resources = await list_environments_by_tenant(client, tenant_id=tenant_id)
    canonical: dict[str, tuple[str, object, str | None]] = {}
    duplicates: dict[str, list[str | None]] = {}
    for resource in resources:
        name = resource.metadata.get(MA_METADATA_KEY_NAME)
        if name is None:
            continue
        prior = canonical.get(name)
        account = resource.metadata.get(MA_METADATA_KEY_ACCOUNT)
        if prior is None:
            canonical[name] = (resource.id, resource.created_at, account)
        elif str(resource.created_at) > str(prior[1]):
            duplicates.setdefault(name, []).append(prior[2])
            canonical[name] = (resource.id, resource.created_at, account)
        else:
            duplicates.setdefault(name, []).append(account)
    for name, accounts in duplicates.items():
        adopted_id, _, adopted_account = canonical[name]
        _log.warning("ma_index.multi_match", kind=f"{kind}s", count=len(accounts) + 1)
        if kind == "agent":
            _log.warning(
                "ma_index.resolver_ambiguous_name",
                tenant_id=str(tenant_id),
                name=name,
                count=len(accounts) + 1,
                adopted_id=adopted_id,
                adopted_account=adopted_account,
                duplicate_accounts=accounts,
            )
    return {name: resource_id for name, (resource_id, _, _) in canonical.items()}


async def _lookup_tags(
    client: AsyncAnthropic, tenant_id: uuid.UUID, kind: Kind, cache: ResolverCache
) -> dict[str, str]:
    scope = (tenant_id, kind)
    task = cache.inflight.get(scope)
    if task is None:

        async def load() -> dict[str, str]:
            found = await _list_tag_ids(client, tenant_id, kind)
            for name, resource_id in found.items():
                cache[(tenant_id, kind, name)] = resource_id
            return found

        task = asyncio.create_task(load())
        cache.inflight[scope] = task

        def clear(done: asyncio.Task[dict[str, str]]) -> None:
            if cache.inflight.get(scope) is done:
                del cache.inflight[scope]
            if not done.cancelled():
                done.exception()

        task.add_done_callback(clear)
    return await asyncio.shield(task)


async def resolve_agent(
    client: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    daimon_tag: str,
    apply_callable: Callable[[], Awaitable[object]],
    cache: ResolverCache,
    cached_id: str | None = None,
) -> str:
    return await _resolve(
        kind="agent",
        client=client,
        tenant_id=tenant_id,
        daimon_tag=daimon_tag,
        cached_id=cached_id,
        apply_callable=apply_callable,
        cache=cache,
        liveness=_is_live_agent,
    )


async def resolve_environment(
    client: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    daimon_tag: str,
    apply_callable: Callable[[], Awaitable[object]],
    cache: ResolverCache,
    cached_id: str | None = None,
) -> str:
    return await _resolve(
        kind="environment",
        client=client,
        tenant_id=tenant_id,
        daimon_tag=daimon_tag,
        cached_id=cached_id,
        apply_callable=apply_callable,
        cache=cache,
        liveness=_is_live_environment,
    )


async def _resolve(
    *,
    kind: Kind,
    client: AsyncAnthropic,
    tenant_id: uuid.UUID,
    daimon_tag: str,
    cached_id: str | None,
    apply_callable: Callable[[], Awaitable[object]],
    cache: ResolverCache,
    liveness: Callable[[AsyncAnthropic, str, uuid.UUID], Awaitable[bool]],
) -> str:
    key: _CacheKey = (tenant_id, kind, daimon_tag)

    # 1. cached_id liveness probe
    if cached_id is not None:
        if await liveness(client, cached_id, tenant_id):
            cache[key] = cached_id
            return cached_id
        cache.pop(key, None)

    # 2. TTL cache
    hit = cache.get(key)
    if hit is not None:
        return hit

    # 3. tag lookup
    found = (await _lookup_tags(client, tenant_id, kind, cache)).get(daimon_tag)
    if found is not None:
        cache[key] = found
        return found

    # 4. apply_callable + retry (idempotent per fire)
    _log.info(
        "ma_resolver.total_miss_applying_defaults",
        kind=kind,
        daimon_tag=daimon_tag,
        tenant_id=str(tenant_id),
    )
    await apply_callable()

    retried = (await _lookup_tags(client, tenant_id, kind, cache)).get(daimon_tag)
    if retried is not None:
        cache[key] = retried
        return retried

    # 5. still missing -> raise
    raise MAResolverMissError(kind=kind, tenant_id=tenant_id, daimon_tag=daimon_tag)
