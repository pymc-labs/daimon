"""One hosted OpenAI environment per tenant, created on its first real GPT root turn.

An agent switched to GPT runs on `openai.persistent_workspace`, which needs a
hosted environment. `daimon openai init` makes one per tenant by hand (and
writes a runtime-file row, which always wins over this); this makes one on
first use instead, so a tenant nobody configured can run GPT with zero setup.

The create goes through the mux operation store: the first caller persists the
intent, exactly one caller wins `claim_send` and talks to OpenAI, and the
result (the environment id) is recorded on the operation, which every later
turn reuses. A create whose outcome is unknown is never resent blindly: once it
is older than any in-flight request can be, it is reconciled by looking the
environment up by its fixed name, and only proven absence, moved by a
compare-and-set against the exact state that was read, lets it be claimed
again. Callers wait briefly for a create someone else is running, then refuse
with `provider_setup_pending` ("try again shortly").

The name carries the deployment (this host's GCP project id, from the metadata
server, which no environment variable can change), so two deployments sharing one
OpenAI project (and tenant ids) never adopt each other's environment; where it
can't be read, nothing is created. Only a real turn's
preparation calls this; previews never do. Logs name the tenant, project and
environment id, never the key.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx
import structlog
from daimon.core.turn.errors import AdmissionDenied
from mux.contracts.ids import Page, PageRequest, Scope
from mux.contracts.resources import Environment, EnvironmentFilter, EnvironmentSpec
from mux.state.operations import InvalidTransition, OperationRecord, SendClaimed, request_digest
from mux.state.store import StateStore

log = structlog.get_logger(__name__)

KEY_PREFIX = "openai_tenant_environment"
# The operation's lookup scope: one fixed account and principal, so every
# replica finds (and may finish) the same operation for a tenant.
_ACCOUNT = "daimon-openai-environment"
_PRINCIPAL = "daimon-host"
CREATE_TIMEOUT = timedelta(seconds=30)
# Far longer than any create can still be in flight, with room for clock skew
# between replicas, so a reconcile that finds nothing has proven the provider
# never made it.
STALE_AFTER = timedelta(minutes=5)
WAIT_FOR_OTHERS = timedelta(seconds=20)
_POLL_S = 1.0
METADATA_PROJECT_URL = "http://metadata.google.internal/computeMetadata/v1/project/project-id"
# GCP project ids: 6-30 lowercase letters, digits and hyphens, starting with a letter.
_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_deployment: str | None = None
# A failed lookup is remembered this long, so turns off GCP don't each wait for it.
MISS_TTL = timedelta(minutes=5)
_missed_until: datetime | None = None


class EnvironmentPort(Protocol):
    """The two `OpenAIEnvironments` calls this needs."""

    async def create(self, scope: Scope, spec: EnvironmentSpec, *, key: str) -> Environment: ...

    async def list(
        self, scope: Scope, *, filters: EnvironmentFilter, page: PageRequest
    ) -> Page[Environment]: ...


async def deployment_id(
    *, timeout_s: float = 2.0, transport: httpx.AsyncBaseTransport | None = None
) -> str | None:
    """This host's GCP project id, from the metadata server; None off GCP or unreadable.

    It tells deployments apart (staging and production are different projects)
    and no environment variable can change it. Kept once read; a miss is
    remembered for `MISS_TTL`, then asked again.
    """
    global _deployment, _missed_until
    if _deployment is not None:
        return _deployment
    checked_at = datetime.now(UTC)
    if _missed_until is not None and checked_at < _missed_until:
        return None
    try:
        async with httpx.AsyncClient(
            timeout=timeout_s, trust_env=False, transport=transport
        ) as http:
            response = await http.get(METADATA_PROJECT_URL, headers={"Metadata-Flavor": "Google"})
    except (httpx.HTTPError, OSError):
        _missed_until = checked_at + MISS_TTL
        return None
    value = response.text.strip() if response.status_code == 200 else ""
    if not _PROJECT_ID.fullmatch(value):
        _missed_until = checked_at + MISS_TTL
        return None
    _deployment, _missed_until = value, None
    return value


def environment_name(deployment: str, tenant_id: str) -> str:
    """The fixed name a tenant's environment is created and reconciled under.

    `deployment` is this host's GCP project id (`deployment_id`), so staging and
    production sharing an OpenAI project never share a name.
    """
    if not deployment.strip():
        raise ValueError("a deployment id is required")
    return f"daimon-{deployment}-{tenant_id}"


def operation_key(project: str) -> str:
    return f"{KEY_PREFIX}:{project}"


def operation_scope(tenant_id: str) -> Scope:
    """Where a tenant's create is recorded in the operation store."""
    return Scope(
        tenant_id=tenant_id,
        account_id=_ACCOUNT,
        principal_id=_PRINCIPAL,
        authorization_id="openai-tenant-environment",
    )


def _provider_scope(tenant_id: str) -> Scope:
    # The same operator-level authorization `daimon openai init` uses: this
    # creates (and lists, to reconcile) one tenant-wide template, not a
    # resource of the requester's own conversation.
    return Scope(
        tenant_id=tenant_id,
        account_id=_ACCOUNT,
        principal_id=_PRINCIPAL,
        authorization_id="openai-tenant-environment",
        platform_reason="set up GPT once for this server, on its first GPT reply",
    )


def _pending() -> AdmissionDenied:
    return AdmissionDenied(reason="provider_setup_pending")


async def tenant_environment(
    store: StateStore,
    environments: EnvironmentPort,
    *,
    tenant_id: str,
    project: str,
    deployment: str,
    now: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str:
    """The tenant's environment id: recorded, or created now exactly once.

    Raises `AdmissionDenied("provider_setup_pending")` when it can't be had
    yet: the create failed (logged with its cause) or another turn is still
    making it.
    """
    scope = operation_scope(tenant_id)
    key = operation_key(project)
    # Every GPT turn after the first: one unlocked read, no row lock.
    known = await store.get_operation(scope, key)
    if known is not None and known.operation.status == "processed":
        return _recorded_id(known)
    name = environment_name(deployment, tenant_id)
    digest = request_digest({"project": project, "name": name})
    begun = await store.begin_operation(
        scope, key=key, request_digest=digest, operation_id=str(uuid.uuid4()), now=now()
    )
    record = begun.record
    give_up = now() + WAIT_FOR_OTHERS
    while True:
        status = record.operation.status
        if status == "processed":
            return _recorded_id(record)
        if status == "pending":
            try:
                record = await store.claim_send(scope, key, now=now(), fence=None)
            except SendClaimed:
                record = await _reread(store, scope, key)
                continue
            return await _create(store, environments, record, scope, tenant_id, name, now)
        if status in ("sent", "outcome_unknown", "accepted"):
            if now() - record.operation.updated_at >= STALE_AFTER:
                record = await _reconcile(store, environments, record, scope, tenant_id, name, now)
                continue
        else:  # "failed": never written by this module; treat as unusable
            log.warning("openai_environment.unusable", tenant_id=tenant_id, status=status)
            raise _pending()
        if now() >= give_up:
            log.info("openai_environment.still_pending", tenant_id=tenant_id, status=status)
            raise _pending()
        await sleep(_POLL_S)
        record = await _reread(store, scope, key)


async def _reread(store: StateStore, scope: Scope, key: str) -> OperationRecord:
    record = await store.get_operation(scope, key)
    if record is None:  # begun above; an operation is never deleted
        raise _pending()
    return record


def _recorded_id(record: OperationRecord) -> str:
    environment_id = record.result.get("environment_id")
    if not isinstance(environment_id, str) or not environment_id:
        raise _pending()
    return environment_id


async def _create(
    store: StateStore,
    environments: EnvironmentPort,
    record: OperationRecord,
    scope: Scope,
    tenant_id: str,
    name: str,
    now: Callable[[], datetime],
) -> str:
    key = record.operation.key
    # Every write below is a compare-and-set on this caller's own claim: a create
    # that stalled past `STALE_AFTER` may meanwhile have been reconciled (and even
    # re-claimed) by another turn, whose state this one must never overwrite.
    claimed_status, claimed_at = record.operation.status, record.operation.updated_at
    try:
        async with asyncio.timeout(CREATE_TIMEOUT.total_seconds()):
            created = await environments.create(
                _provider_scope(tenant_id), EnvironmentSpec(name=name), key=key
            )
    except Exception as error:
        # The request may have reached OpenAI, so it is never resent from here.
        # If someone else has moved it on meanwhile, their state stands.
        with contextlib.suppress(InvalidTransition):
            await store.advance_operation(
                scope,
                key,
                "outcome_unknown",
                now=now(),
                fence=None,
                expected_status=claimed_status,
                expected_updated_at=claimed_at,
            )
        log.warning(
            "openai_environment.create_failed",
            tenant_id=tenant_id,
            error_class=type(error).__name__,
        )
        raise _pending() from error
    try:
        settled = await store.advance_operation(
            scope,
            key,
            "processed",
            now=now(),
            fence=None,
            resource=created.ref,
            result={"environment_id": created.ref.id},
            expected_status=claimed_status,
            expected_updated_at=claimed_at,
        )
    except InvalidTransition:
        # Reconciled meanwhile: use what is recorded, or let the next turn retry.
        settled = await _reread(store, scope, key)
        if settled.operation.status != "processed":
            log.warning(
                "openai_environment.create_superseded",
                tenant_id=tenant_id,
                unused=created.ref.id,
            )
            raise _pending() from None
    # The recorded id, which is this one unless the operation was already settled.
    environment_id = _recorded_id(settled)
    log.info(
        "openai_environment.created",
        tenant_id=tenant_id,
        environment_id=environment_id,
        unused=created.ref.id if created.ref.id != environment_id else None,
    )
    return environment_id


async def _reconcile(
    store: StateStore,
    environments: EnvironmentPort,
    record: OperationRecord,
    scope: Scope,
    tenant_id: str,
    name: str,
    now: Callable[[], datetime],
) -> OperationRecord:
    """Settle a stale create by name: found → processed; proven absent → pending.

    Either move is a compare-and-set on the state that was read: if anyone moved
    the operation meanwhile (another reconcile, or the creator's own late
    outcome), this one gives way and takes theirs.
    """
    key = record.operation.key
    read_status, read_at = record.operation.status, record.operation.updated_at
    found = await _find_by_name(environments, tenant_id, name)
    try:
        if found is not None:
            log.info(
                "openai_environment.reconciled",
                tenant_id=tenant_id,
                environment_id=found.ref.id,
            )
            return await store.advance_operation(
                scope,
                key,
                "processed",
                now=now(),
                fence=None,
                resource=found.ref,
                result={"environment_id": found.ref.id},
                expected_status=read_status,
                expected_updated_at=read_at,
            )
        log.info("openai_environment.absent_retrying", tenant_id=tenant_id)
        return await store.advance_operation(
            scope,
            key,
            "pending",
            now=now(),
            fence=None,
            expected_status=read_status,
            expected_updated_at=read_at,
        )
    except InvalidTransition:
        return await _reread(store, scope, key)


async def _find_by_name(
    environments: EnvironmentPort, tenant_id: str, name: str
) -> Environment | None:
    cursor: str | None = None
    while True:
        page = await environments.list(
            _provider_scope(tenant_id),
            filters=EnvironmentFilter(),
            page=PageRequest(cursor=cursor, limit=100),
        )
        matches = [item for item in page.data if item.spec.name == name]
        if matches:
            # The oldest is the one first created; any later duplicate is unused.
            return min(matches, key=lambda item: item.created_at)
        if not page.has_more or page.next_cursor is None:
            return None
        cursor = page.next_cursor
