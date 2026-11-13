"""Retire a tenant, then delete what it left with the provider and the hosts.

`retire` deletes the tenant's data in batches and queues the provider
sessions, memory stores, vaults and skills its rows named, plus its report and
notebook host content. `clean_upstream` works one tenant's queue off and keeps
whatever failed; the scheduler retries every queue through `sweep`.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime

import anthropic as anthropic_errors
import httpx
import structlog
from anthropic import AsyncAnthropic
from daimon.core.config import Settings
from daimon.core.ma import delete_skill_and_versions
from daimon.core.mux_backend import managed_agents, resource_ref, resource_scope
from daimon.core.mux_compat import archive_vault, delete_memory_store, legacy_call
from daimon.core.stores import tenant_retirement as store
from mux.contracts.ids import Scope
from mux.drivers.anthropic import CoreAdmin
from mux.errors import ProviderError
from pydantic import HttpUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

_HOST_TIMEOUT_SECONDS = 30.0

Deleter = Callable[[str], Awaitable[None]]


PURGE_BATCH = 500


async def retire(
    sm: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    archived_before: datetime | None = None,
) -> bool:
    """Retire the tenant now, in short transactions so a large tenant never holds locks
    for long. False when it does not exist, is not due (`archived_before`, the sweep's
    rule), or a reinstall stopped it. An interrupted retirement resumes on the next call.
    """
    async with sm() as session, session.begin():
        archived_at = await store.begin_retirement(
            session, tenant_id=tenant_id, archived_before=archived_before
        )
    if archived_at is None:
        return False
    for table in store.purge_tables():
        while True:
            async with sm() as session, session.begin():
                removed = await store.purge_batch(
                    session,
                    tenant_id=tenant_id,
                    archived_at=archived_at,
                    table=table,
                    limit=PURGE_BATCH,
                )
            if removed is None:
                return False
            if removed < PURGE_BATCH:
                break
    async with sm() as session, session.begin():
        return await store.finish_retirement(session, tenant_id=tenant_id, archived_at=archived_at)


def _session_deleter(client: AsyncAnthropic, scope: Scope) -> Deleter:
    async def delete(session_id: str) -> None:
        backend = managed_agents(
            client, scope=scope, resources=frozenset({("session", session_id)})
        )
        port = backend.extension(CoreAdmin, namespace="anthropic.core_admin", version=1)
        await legacy_call(
            port.delete_session(scope, resource_ref(backend, "session", session_id, scope=scope))
        )

    return delete


def _host_deleter(settings: Settings, http: httpx.AsyncClient, tenant_id: uuid.UUID) -> Deleter:
    targets: dict[str, tuple[HttpUrl | None, SecretStr | None]] = {
        "reports": (settings.report_host.host_url, settings.report_host.admin_secret),
        "notebooks": (settings.notebook.host_url, settings.notebook.admin_secret),
    }

    async def delete(host: str) -> None:
        url, secret = targets[host]
        if url is None or secret is None:
            return  # No host configured, so nothing was published there.
        response = await http.delete(
            f"{str(url).rstrip('/')}/admin/tenants/{tenant_id}/{host}",
            headers={"Authorization": f"Bearer {secret.get_secret_value()}"},
            timeout=_HOST_TIMEOUT_SECONDS,
        )
        if response.status_code != 404:
            response.raise_for_status()

    return delete


def deleters(
    client: AsyncAnthropic, settings: Settings, http: httpx.AsyncClient, tenant_id: uuid.UUID
) -> dict[str, Deleter]:
    """One delete per queued kind; each treats an already missing resource as done."""
    scope = resource_scope(tenant_id=str(tenant_id), authorization_id="tenant-retirement")

    async def memory_store(store_id: str) -> None:
        await delete_memory_store(client, store_id, scope=scope)

    async def vault(vault_id: str) -> None:
        await archive_vault(client, vault_id, scope=scope)

    async def skill(skill_id: str) -> None:
        await delete_skill_and_versions(client, skill_id, scope=scope)

    return {
        "sessions": _session_deleter(client, scope),
        "memory_stores": memory_store,
        "vaults": vault,
        "skills": skill,
        "hosts": _host_deleter(settings, http, tenant_id),
    }


async def clean_upstream(
    sm: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, deleters: dict[str, Deleter]
) -> bool:
    """Delete one tenant's queued upstream resources. True when nothing is left."""
    async with sm() as session:
        pending = await store.get_work(session, tenant_id=tenant_id)
    if pending is None:
        return True
    finished = False
    for kind in store.PENDING_KINDS:
        done: set[str] = set()
        for item in pending.get(kind, []):
            try:
                await deleters[kind](item)
            except anthropic_errors.NotFoundError:
                pass
            except (anthropic_errors.APIError, ProviderError, httpx.HTTPError) as error:
                log.warning(
                    "tenant_retirement.upstream_failed",
                    tenant_id=str(tenant_id),
                    kind=kind,
                    error=type(error).__name__,
                )
                continue
            done.add(item)
        async with sm() as session, session.begin():
            finished = await store.remove_done(session, tenant_id=tenant_id, kind=kind, done=done)
    return finished


async def sweep(
    client: AsyncAnthropic, sm: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """Retry every retired tenant's upstream cleanup, isolating failures by tenant."""
    async with sm() as session:
        work = await store.list_work(session)
    async with httpx.AsyncClient() as http:
        for tenant_id, _pending in work:
            try:
                await clean_upstream(
                    sm, tenant_id=tenant_id, deleters=deleters(client, settings, http, tenant_id)
                )
            except Exception:
                log.exception("tenant_retirement.sweep_failed", tenant_id=str(tenant_id))
