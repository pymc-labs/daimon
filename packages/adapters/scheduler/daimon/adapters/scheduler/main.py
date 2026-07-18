"""Scheduler process entrypoint.

Imperative shell that:

1. Loads settings, builds engine + sessionmaker + AsyncAnthropic.
2. Acquires ``pg_try_advisory_lock`` on a DEDICATED connection. The lock is
   session-scoped — if the connection were ever returned to the pool and
   reused, the lock would die. The connection is therefore held open for
   the full process lifetime (created via ``engine.connect()``, never via
   ``async_sessionmaker``).
3. Installs SIGINT/SIGTERM signal handlers via the running event loop.
4. Loops: every ``tick_interval_s``, awaits ``run_one_tick``.
5. On stop: releases the lock, disposes the engine, exits.

The ``--once`` flag runs a single tick and exits 0. The tick uses
``asyncio.gather`` internally so all fires settle before exit.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import signal
import sys
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import anthropic
import structlog
from anthropic import AsyncAnthropic
from daimon.adapters.scheduler.settings import SchedulerSettings
from daimon.core.billing import BillingConfig, is_over_cap, load_billing_config
from daimon.core.config import Settings, load_settings
from daimon.core.db import build_engine, build_session_factory
from daimon.core.defaults.loader import parse_deployment_default
from daimon.core.defaults.provisioning import reconcile_tenant_defaults
from daimon.core.github_credentials import build_multifernet
from daimon.core.headless_runner import run_turn
from daimon.core.health import start_liveness_responder
from daimon.core.logging_setup import configure_log_level
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import (
    ResolverCache,
    new_resolver_cache,
    resolve_agent,
    resolve_environment,
)
from daimon.core.observability import init_sentry
from daimon.core.pending_file_sweeper import sweep_pending_file_deletes
from daimon.core.pricing import MODEL_PRICING
from daimon.core.scheduler import FireFn, run_one_tick
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.routines import record_result, update_routine_agent_id
from daimon.core.stores.tenants import get_tenant
from daimon.core.tenant_balance import is_over_balance
from daimon.core.usage_recording import record_turn_usage
from daimon.core.usage_sweep import sweep_headless_usage
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

log = structlog.get_logger(__name__)


async def _acquire_advisory_lock(engine: AsyncEngine, key: int) -> AsyncConnection | None:
    """Try ``pg_try_advisory_lock(key)`` on a dedicated connection.

    Returns the held connection on success (caller MUST keep it open and
    eventually call ``pg_advisory_unlock`` + ``conn.close()``), or
    ``None`` if the key is held by another session.

    Pitfall: the connection is created via ``engine.connect()``, which
    bypasses the pool's normal checkin/checkout cycle. Returning it to
    the pool would release the lock; holding it keeps the lock alive for
    the process lifetime.
    """
    conn = await engine.connect()
    try:
        result = await conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        got = result.scalar_one()
    except Exception:
        await conn.close()
        raise
    if not got:
        await conn.close()
        return None
    return conn


class _CapsAdapter:
    """Wraps the free ``is_over_cap`` function in a ``CapsCheck``-shaped object.

    The core scheduler ``CapsCheck`` Protocol expects an object with
    ``is_over_cap(tenant_id, user_id) -> bool``; we wrap the free function so
    the existing ``run_one_tick`` contract holds without leaking sessionmaker
    into ``run_one_tick``'s signature.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        billing_config: BillingConfig | None,
    ) -> None:
        self._sm = sessionmaker
        self._billing_config = billing_config

    async def is_over_cap(self, tenant_id: uuid.UUID, user_id: str) -> bool:
        return await is_over_cap(
            billing_config=self._billing_config,
            sessionmaker=self._sm,
            tenant_id=tenant_id,
            user_id=user_id,
            now=datetime.now(UTC),
        )


async def _build_fire(
    *,
    client: AsyncAnthropic,
    sm: async_sessionmaker[AsyncSession],
    settings: Settings,
    deployment_default: DeploymentDefault,
    resolver_cache: ResolverCache,
) -> FireFn:
    """Construct the per-row ``fire`` callable consumed by ``run_one_tick``.

    Flow per fire:

    1. Resolve ``(platform, created_by_user_id) -> account_id`` via
       ``get_or_create_platform_principal`` using ``row.tenant_id``.
       If ``created_by_user_id`` is NULL on the row, record an error and
       bail — there is no account to bind the daimon-mcp vault to.
    2. Drive ``run_turn`` — the headless single-turn drain. ``run_turn``
       owns the daimon-mcp vault attach (via ``ensure_mcp_vault``) when
       ``mcp_settings`` and ``account_id`` are both supplied.
    3. On success, write ``last_result_tail`` via ``record_result`` on a
       FRESH session (independent of any other transaction).

    Errors propagate out of this callable into the guarded gather member's
    named error boundary, which records ``last_error``.
    """

    async def _fire(row: RoutineRow) -> None:
        if row.created_by_user_id is None:
            async with sm() as s, s.begin():
                await record_result(
                    s,
                    row.id,
                    tail=None,
                    error="routine has no created_by_user_id",
                )
            return

        async with sm() as s, s.begin():
            tenant = await get_tenant(s, row.tenant_id)
            if tenant is None:
                await record_result(s, row.id, tail=None, error="routine tenant not found")
                return
            principal = await get_or_create_platform_principal(
                s,
                tenant_id=row.tenant_id,
                # Source the platform from the routine's tenant so slack-created
                # routines resolve a slack principal (not a mismatched discord one).
                platform=tenant.platform,
                external_id=row.created_by_user_id,
            )
            account_id = principal.account_id

        # Admission gate: per-tenant balance — independent of Stripe config.
        # Keys on row.tenant_id (NOT NULL). Mirror run_one_tick's cap_exceeded skip.
        if await is_over_balance(sessionmaker=sm, tenant_id=row.tenant_id):
            log.info(
                "routine.skipped.over_balance",
                routine_id=str(row.id),
                tenant_id=str(row.tenant_id),
            )
            async with sm() as s, s.begin():
                await record_result(s, row.id, tail=None, error="balance_depleted")
            return

        # Bind routine context now; headless_runner calls the factory once
        # (session_id, model_id) are known after create_session.
        platform_user_id = row.created_by_user_id

        def usage_record_factory(session_id: str, model_id: str) -> Callable[..., Awaitable[None]]:
            return functools.partial(
                record_turn_usage,
                sessionmaker=sm,
                platform_user_id=platform_user_id,
                managed_session_id=session_id,
                model_id=model_id,
                tenant_id=row.tenant_id,
                markup=settings.billing.markup,
                pricing=MODEL_PRICING.get(model_id),
            )

        # Resolve agent + environment by daimon-tag at fire time,
        # self-healing across MA archive/recreate.
        # defensive: post-0012 agent_name is NOT NULL but the fallback is harmless and free.
        agent_tag = row.agent_name or deployment_default.agent_name or "daimon"
        _public_url = str(settings.mcp.public_url) if settings.mcp.public_url else None
        resolved_agent_id = await resolve_agent(
            client,
            tenant_id=row.tenant_id,
            daimon_tag=agent_tag,
            cached_id=row.agent_id,
            apply_callable=lambda: reconcile_tenant_defaults(
                client, settings.defaults_root, tenant_id=row.tenant_id, public_url=_public_url
            ),
            cache=resolver_cache,
        )
        resolved_env_id = await resolve_environment(
            client,
            tenant_id=row.tenant_id,
            daimon_tag=deployment_default.environment_name or "default",
            cached_id=None,
            apply_callable=lambda: reconcile_tenant_defaults(
                client, settings.defaults_root, tenant_id=row.tenant_id, public_url=_public_url
            ),
            cache=resolver_cache,
        )
        if resolved_agent_id != row.agent_id:
            async with sm() as heal_s, heal_s.begin():
                await update_routine_agent_id(heal_s, row.id, resolved_agent_id)

        # Mount the agent's assembled .env on the headless turn: derive the
        # tenant-scoped agent UUID and pass tenant/agent/session_factory so
        # run_turn uploads + mounts the credential file.
        agent_uuid = derive_agent_uuid(tenant_id=row.tenant_id, ma_agent_id=resolved_agent_id)

        # Fernet decrypts the per-agent GitHub PAT so a bound repo clones into
        # the routine's session workspace. None when no crypto keys configured.
        crypto_keys = tuple(secret.get_secret_value() for secret in settings.crypto.keys)
        fernet = build_multifernet(crypto_keys) if crypto_keys else None

        github_fallback_pat: str | None = (
            settings.github.fallback_pat.get_secret_value()
            if settings.github.fallback_pat is not None
            else None
        )
        github_app_id: str | None = settings.github.app_id
        github_app_private_key: str | None = (
            settings.github.app_private_key.get_secret_value()
            if settings.github.app_private_key is not None
            else None
        )

        tail = await run_turn(
            anthropic=client,
            agent_id=resolved_agent_id,
            environment_id=resolved_env_id,
            trigger_message=row.trigger_message,
            mcp_settings=settings.mcp,
            account_id=account_id,
            usage_record_factory=usage_record_factory,
            tenant_id=row.tenant_id,
            agent_uuid=agent_uuid,
            session_factory=sm,
            fernet=fernet,
            github_fallback_pat=github_fallback_pat,
            github_app_id=github_app_id,
            github_app_private_key=github_app_private_key,
        )

        async with sm() as s, s.begin():
            await record_result(s, row.id, tail=tail, error=None)

    return _fire


async def _sweep_pending_files(
    client: AsyncAnthropic, sm: async_sessionmaker[AsyncSession]
) -> None:
    """Drain the Files-API TTL queue once. Boundary catch: a sweep failure must
    not kill the scheduler loop — the failing rows stay queued for the next tick.

    Running every tick is fine: the sweeper no-ops when nothing is due.
    """
    try:
        await sweep_pending_file_deletes(client, sm, now=datetime.now(UTC))
    except anthropic.APIError:
        log.exception("scheduler.sweep.failed")


async def _sweep_headless_usage(
    client: AsyncAnthropic, sm: async_sessionmaker[AsyncSession], *, markup: Decimal
) -> None:
    """Backfill usage for headless MCP turns once. Boundary catch: a sweep
    failure must not kill the scheduler loop — idempotent recording means the
    next tick re-reads and records anything missed.
    """
    try:
        await sweep_headless_usage(client, sm, markup=markup)
    except (anthropic.APIError, SQLAlchemyError):
        # Named boundary: a sweep failure (upstream MA error OR a DB write that
        # trips a constraint, e.g. a stray foreign-tenant session) must not kill
        # the tick loop. Idempotent recording means the next tick retries.
        log.exception("scheduler.usage_sweep.failed")


def _validate_mcp_settings(settings: Settings) -> None:
    """Single-tenant deployments require both ``settings.mcp.jwt_secret`` and
    ``settings.mcp.public_url`` so each routine fire can bind the daimon-mcp
    vault per-account. Surface a clear error at boot rather than failing per fire."""
    if settings.mcp.jwt_secret is None:
        raise RuntimeError(
            "DAIMON_MCP__JWT_SECRET is required for the scheduler — "
            "routine fires bind the daimon-mcp vault per-account using this secret."
        )
    if settings.mcp.public_url is None:
        raise RuntimeError(
            "DAIMON_MCP__PUBLIC_URL is required for the scheduler — "
            "routine fires bind the daimon-mcp vault per-account"
        )


async def run(
    argv: list[str] | None = None,
    *,
    _anthropic_factory: Callable[[Settings], Awaitable[AsyncAnthropic]] | None = None,
    _engine_override: AsyncEngine | None = None,
) -> int:
    """Entrypoint. Returns the process exit code.

    ``_anthropic_factory`` is a test seam: integration tests substitute a
    fake ``AsyncAnthropic`` (built via ``httpx.MockTransport`` or the
    in-process event-shaped fake) without globally monkeypatching the
    SDK constructor. Production passes ``None``.

    ``_engine_override`` is a test seam: integration tests pass a
    pre-configured engine (typically bound to a per-test schema via
    ``schema_translate_map``) so ``run()`` operates against the same
    database state as the test's setup code. When provided, ``run`` does
    NOT dispose the engine on exit — the test owns its lifecycle.
    """
    parser = argparse.ArgumentParser(prog="daimon-scheduler")
    parser.add_argument("--once", action="store_true", help="Run exactly one tick and exit")
    args = parser.parse_args(argv)

    settings = load_settings()
    # Configure the JSON log chain BEFORE the first log line so structured output
    # takes effect for the whole process (OB-1; this entrypoint owns the call site
    # since 61 is unexecuted).
    configure_log_level(settings.log.level)
    init_sentry(
        dsn=settings.sentry.dsn.get_secret_value() if settings.sentry.dsn else None,
        environment=settings.sentry.environment,
        process="scheduler",
        release=None,
        traces_sample_rate=settings.sentry.traces_sample_rate,
        integrations=[AsyncioIntegration()],
    )
    scheduler_settings = SchedulerSettings()
    _validate_mcp_settings(settings)

    engine = _engine_override or build_engine(str(settings.database.url))
    sm = build_session_factory(engine)

    client = (
        await _anthropic_factory(settings)
        if _anthropic_factory is not None
        else AsyncAnthropic(
            api_key=settings.anthropic.api_key.get_secret_value(),
            base_url=str(settings.anthropic.base_url),
        )
    )

    lock_conn = await _acquire_advisory_lock(engine, scheduler_settings.advisory_lock_key)
    if lock_conn is None:
        log.warning(
            "scheduler.lock.not_acquired",
            key=scheduler_settings.advisory_lock_key,
        )
        await client.close()
        await engine.dispose()
        return 1

    # Liveness responder on THIS loop (OB-3): a hung loop stops answering → Fly
    # restarts the machine. Started after the lock so only the active scheduler
    # serves the check; closed in the finally below.
    health_server = await start_liveness_responder(scheduler_settings.health_port)

    deployment_default = parse_deployment_default(settings.defaults_root)
    caps = _CapsAdapter(sm, billing_config=load_billing_config())
    resolver_cache = new_resolver_cache()
    fire = await _build_fire(
        client=client,
        sm=sm,
        settings=settings,
        deployment_default=deployment_default,
        resolver_cache=resolver_cache,
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Signal handlers aren't available on every platform (e.g. Windows
            # ProactorEventLoop). Tests / non-Unix runs proceed without them.
            log.debug("scheduler.signal_handler_unavailable", signal=sig.name)

    try:
        if args.once:
            await run_one_tick(
                now=datetime.now(UTC),
                sm=sm,
                caps=caps,
                fire=fire,
                max_age=timedelta(seconds=scheduler_settings.max_age_s),
                max_concurrent_fires=scheduler_settings.max_concurrent_fires,
                dispatch_timeout_s=scheduler_settings.dispatch_timeout_s,
            )
            await _sweep_pending_files(client, sm)
            await _sweep_headless_usage(client, sm, markup=settings.billing.markup)
            return 0

        while not stop_event.is_set():
            await run_one_tick(
                now=datetime.now(UTC),
                sm=sm,
                caps=caps,
                fire=fire,
                max_age=timedelta(seconds=scheduler_settings.max_age_s),
                max_concurrent_fires=scheduler_settings.max_concurrent_fires,
                dispatch_timeout_s=scheduler_settings.dispatch_timeout_s,
            )
            await _sweep_pending_files(client, sm)
            await _sweep_headless_usage(client, sm, markup=settings.billing.markup)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    stop_event.wait(), timeout=scheduler_settings.tick_interval_s
                )

        return 0
    finally:
        health_server.close()
        await health_server.wait_closed()
        try:
            await lock_conn.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": scheduler_settings.advisory_lock_key},
            )
        except Exception:
            log.exception("advisory unlock failed")
        await lock_conn.close()
        await client.close()
        if _engine_override is None:
            await engine.dispose()


def run_sync() -> None:
    """Console-script entrypoint. ``daimon-scheduler`` resolves here."""
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    run_sync()
