"""Scheduler process entrypoint.

Imperative shell that:

1. Loads settings, builds engine + sessionmaker + AsyncAnthropic.
2. Acquires ``pg_try_advisory_lock`` on a DEDICATED connection. The lock is
   session-scoped — if the connection were ever returned to the pool and
   reused, the lock would die. The connection is therefore held open for
   the full process lifetime (created via ``engine.connect()``, never via
   ``async_sessionmaker``).
3. Installs SIGINT/SIGTERM signal handlers via the running event loop.
4. Loops: every ``tick_interval_s``, awaits ``run_one_tick``; the usage sweep,
   compute cleanup and compute task polling repeat on their own loops at the
   same interval.
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
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import anthropic
import httpx
import structlog
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
from cryptography.fernet import MultiFernet
from daimon.adapters.scheduler import workflows
from daimon.adapters.scheduler.boundary import scheduler_step
from daimon.adapters.scheduler.settings import SchedulerSettings
from daimon.core.billing import BillingConfig, load_billing_config
from daimon.core.bot_access import bot_access_blocked, bot_access_wait_on
from daimon.core.channel_backend import (
    backend_refusal_causes,
)
from daimon.core.compute_provider import compute_provider_for
from daimon.core.compute_sessions import ComputeSessions
from daimon.core.config import (
    Settings,
    load_settings,
)
from daimon.core.constants import MA_MAX_RETRIES
from daimon.core.db import build_engine, build_session_factory
from daimon.core.defaults.loader import parse_deployment_default
from daimon.core.defaults.provisioning import reconcile_tenant_defaults
from daimon.core.direct_messages import run_dm_routine
from daimon.core.discord_membership import DiscordRestMembershipResolver, send_discord_error_dm
from daimon.core.fire.run import FireDeps, fire_turn
from daimon.core.fire.run import routines_unsupported_error as routines_unsupported_error
from daimon.core.fire.subject import routine_subject
from daimon.core.github_app_session import (
    GitHubTokenMintUnavailable,
    archive_app_vault,
    effective_repo_state,
    revoke_session_tokens,
    revoke_token,
    rotate_live_app_tokens,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.github_installation_reconcile import drain_github_installation_reconciliations
from daimon.core.github_issued_token_sweep import sweep_closed_github_issued_tokens
from daimon.core.headless_runner import run_turn
from daimon.core.health import start_liveness_responder
from daimon.core.hub_oauth_kv_sweep import sweep_expired_hub_oauth_kv
from daimon.core.logging_setup import configure_logging, mark_shutting_down
from daimon.core.long_jobs import LongJobExecutor
from daimon.core.long_jobs_ma import ManagedLongJobPort
from daimon.core.ma_resolver import (
    ResolverCache,
    new_resolver_cache,
    resolve_agent,
    resolve_environment,
)
from daimon.core.mcp_vault_janitor import archive_idle_relay_vaults
from daimon.core.membership import MembershipResolver
from daimon.core.monitoring_sweep import sweep_monitored_events
from daimon.core.mux_compat import archive_session, retrieve_agent
from daimon.core.observability import init_sentry
from daimon.core.ops_events import drain_ops_events
from daimon.core.ops_events_sweep import sweep_ops_events
from daimon.core.owner_notices import build_owner_notifier, discord_owner_dm
from daimon.core.pending_carry_over_sweep import sweep_stale_pending_carry_overs
from daimon.core.pending_file_sweeper import sweep_pending_file_deletes
from daimon.core.promo_settlement import settle_promo_credit
from daimon.core.purge import sweep_privacy_session_deletes
from daimon.core.runtime_health import current_turn_counts, runtime_health
from daimon.core.scheduler import (
    FireFn,
    OwnerNotifier,
    RoutineDispatcher,
    RoutineFireStore,
    drain_owner_notices,
    run_one_tick,
)
from daimon.core.scope import DeploymentDefault
from daimon.core.session_compat import valid_unavailable_repo_urls
from daimon.core.session_mutation import session_mutation_fence
from daimon.core.session_ports_compat import retrieve_session_record, session_scope
from daimon.core.skill_sync.resync_queue import drain_github_push_resync_queue
from daimon.core.skills.rate_limit import SkillsRateLimitedTransport
from daimon.core.slack_event_dedup_sweep import sweep_expired_slack_event_dedup
from daimon.core.stores.domain import RoutineRow, ThreadSessionRow
from daimon.core.stores.github_connect import delete_expired_flows
from daimon.core.stores.github_issued_tokens import (
    LiveMcpAppSession,
    closed_app_session_for_id,
    decrypt_issued_token,
    finish_headless_app_session,
    list_closed_app_sessions,
    list_live_app_sessions,
    list_live_mcp_app_sessions,
    list_session_tokens,
    mark_headless_app_session_closed,
    mark_revoked,
    recheck_token_revoke_due,
    record_revoke_attempt,
    select_abandoned_pending_tokens,
    select_due_superseded_tokens,
    select_stale_tokens,
    session_token_needs_rotation,
    touch_running_mcp_app_session,
)
from daimon.core.stores.github_router_sessions import close_for_ma_session
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.routines import (
    record_result,
    update_routine_agent_id,
)
from daimon.core.stores.secret_shares import clear_lapsed_shares
from daimon.core.stores.security_audit import append_github_token_event
from daimon.core.stores.thread_sessions import mark_dead, record_app_token_refresh
from daimon.core.turn.deps import TurnDeps, build_turn_deps
from daimon.core.turn.outcomes import drain_outcomes
from daimon.core.turn_card_intent_sweep import sweep_retired_turn_card_intents
from daimon.core.usage_sweep import sweep_headless_usage
from daimon.core.wizard_sweep import sweep_expired_wizard_sessions
from daimon.core.workflows import waits as workflow_waits
from daimon.core.workflows.action_step import ActionStepExecutor
from daimon.core.workflows.agent_step import AgentStepExecutor, session_files
from daimon.core.workflows.call_step import WorkflowStepExecutor
from daimon.core.workflows.code_step import CodeStepExecutor
from daimon.core.workflows.code_worker import ManagedCodeWorker
from daimon.core.workflows.post_step import PostStepExecutor
from daimon.core.workflows.tool_client import McpPollClient, McpToolClient
from mux.errors import ScopeViolation
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


@scheduler_step
async def _run_one_tick_step(
    *,
    now: datetime,
    sm: async_sessionmaker[AsyncSession],
    fire: FireFn,
    max_age: timedelta,
    max_concurrent_fires: int,
    dispatch_timeout_s: float,
    dispatcher: RoutineDispatcher | None = None,
    wait_for_completion: bool = False,
    notify_owner: OwnerNotifier | None = None,
) -> None:
    """Run the routine claim and dispatch step behind the shared boundary.

    `run_one_tick` opens its transaction outside its own catches, so a database
    error on connect or commit would otherwise end every loop.
    """
    await run_one_tick(
        now=now,
        sm=sm,
        fire=fire,
        max_age=max_age,
        max_concurrent_fires=max_concurrent_fires,
        dispatch_timeout_s=dispatch_timeout_s,
        dispatcher=dispatcher,
        wait_for_completion=wait_for_completion,
        notify_owner=notify_owner,
    )


@scheduler_step
async def _sweep_compute(compute: ComputeSessions) -> None:
    # Cleanup is independent of launch enablement and user routines.
    if not compute.settings.deployment_id or not compute.settings.provider_configured:
        return
    try:
        await compute.reconcile()
    except Exception:
        log.warning("compute.sweep_retry")


@scheduler_step
async def _poll_compute_tasks(compute: ComputeSessions) -> None:
    # Background commands are read and reported even if new launches are disabled.
    if not compute.settings.deployment_id or compute.settings.session_auth_key is None:
        return
    try:
        await compute.poll_tasks()
    except Exception:
        log.warning("compute.task_poll_retry")


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
        # The lock is session-scoped and survives the commit. Leaving the
        # autobegun transaction open would hold a snapshot for the process
        # lifetime and block every CREATE INDEX CONCURRENTLY in migrations.
        await conn.commit()
    except Exception:
        await conn.close()
        raise
    if not got:
        await conn.close()
        return None
    return conn


async def _build_fire(
    *,
    client: AsyncAnthropic,
    sm: async_sessionmaker[AsyncSession],
    settings: Settings,
    deployment_default: DeploymentDefault,
    resolver_cache: ResolverCache,
    membership_resolver: MembershipResolver | None = None,
    billing_config: BillingConfig | None = None,
    dm_turn_deps: TurnDeps | None = None,
    send_dm: Callable[[str, str, str], Awaitable[None]] | None = None,
    notify_owner: OwnerNotifier | None = None,
) -> FireFn:
    """Construct the per-row ``fire`` callable consumed by ``run_one_tick``.

    Each fire runs `fire_turn` over a `RoutineFireStore` for the claimed row. The
    runtime clients are this module's names, read at fire time.
    Errors propagate out of this callable into the guarded gather member's
    named error boundary, which records ``last_error``.
    """

    membership_resolver = membership_resolver or (
        DiscordRestMembershipResolver(settings.discord.bot_token.get_secret_value())
        if settings.discord is not None
        else None
    )

    if send_dm is None and settings.discord is not None:
        send_dm = functools.partial(
            send_discord_error_dm, settings.discord.bot_token.get_secret_value()
        )

    turn_deps: list[TurnDeps] = [] if dm_turn_deps is None else [dm_turn_deps]

    def _turn_deps() -> TurnDeps:
        """The ordinary turn dependencies a DM routine runs with, built on first use."""
        if not turn_deps:
            turn_deps.append(
                replace(
                    build_turn_deps(
                        settings,
                        client,
                        sm,
                        deployment_default=deployment_default,
                        resolver_cache=resolver_cache,
                        billing_config=billing_config,
                    ),
                    membership_resolver=membership_resolver,
                )
            )
        return turn_deps[0]

    async def _fire(row: RoutineRow) -> None:
        store = RoutineFireStore(
            sm, row, notify_owner, record=record_result, heal=update_routine_agent_id
        )
        deps = FireDeps(
            client=client,
            sm=sm,
            settings=settings,
            deployment_default=deployment_default,
            resolver_cache=resolver_cache,
            membership_resolver=membership_resolver,
            turn_deps=_turn_deps,
            send_dm=send_dm,
            dm_open=workflows.dm_open,
            owner_granted=workflows.owner_granted,
            run_turn=run_turn,
            run_dm_routine=run_dm_routine,
            resolve_agent=resolve_agent,
            resolve_environment=resolve_environment,
            retrieve_agent=retrieve_agent,
            reconcile_tenant_defaults=reconcile_tenant_defaults,
            get_or_create_platform_principal=get_or_create_platform_principal,
            bot_access_wait_on=bot_access_wait_on,
            bot_access_blocked=bot_access_blocked,
        )
        await fire_turn(routine_subject(row), store, deps)

    return _fire


@scheduler_step
async def _drain_pending_usage(sm: async_sessionmaker[AsyncSession], *, markup: Decimal) -> None:
    """Re-read late OpenAI usage, then settle pending usage of profiles with no
    infrastructure charge; failures retry on the next scheduler tick (an applied row
    is never applied twice)."""
    from daimon.core.turn.openai_late_usage import DURABLE_REREADS
    from daimon.core.usage_outbox_drain import drain_pending_usage

    try:
        await DURABLE_REREADS.run(sm, markup=markup, now=datetime.now(UTC))
    except Exception:
        log.exception("scheduler.late_usage_reread.failed")
    try:
        await drain_pending_usage(sm, markup=markup)
    except Exception:
        log.exception("scheduler.pending_usage_drain.failed")


@scheduler_step
async def _sweep_pending_files(
    client: AsyncAnthropic, sm: async_sessionmaker[AsyncSession]
) -> None:
    """Drain the Files-API TTL queue once. Boundary catch: a sweep failure must
    not kill the scheduler loop — the failing rows stay queued for the next tick.

    Running every tick is fine: the sweeper no-ops when nothing is due.
    """
    try:
        await sweep_pending_file_deletes(client, sm, now=datetime.now(UTC))
    except anthropic.APIError as error:
        log.exception("scheduler.sweep.failed", causes=backend_refusal_causes(error))


@scheduler_step
async def _sweep_privacy_deletes(
    client: AsyncAnthropic, sm: async_sessionmaker[AsyncSession]
) -> None:
    """Retain failed MA erasures for the next tick."""
    try:
        await sweep_privacy_session_deletes(client, sm)
    except Exception:
        log.exception("scheduler.privacy_delete_sweep.failed")


@scheduler_step
async def _sweep_headless_usage(
    client: AsyncAnthropic,
    sm: async_sessionmaker[AsyncSession],
    *,
    markup: Decimal,
) -> None:
    """Settle the headless sends nobody polled to the end, once. Boundary catch:
    a failure must not kill the scheduler — idempotent recording means the next
    pass re-reads and records anything missed.
    """
    try:
        await sweep_headless_usage(client, sm, markup=markup)
    except (anthropic.APIError, SQLAlchemyError) as error:
        # Named boundary: a sweep failure (upstream MA error OR a DB write that
        # trips a constraint, e.g. a stray foreign-tenant session) must not kill
        # the sweep loop. Idempotent recording means the next pass retries.
        log.exception("scheduler.usage_sweep.failed", causes=backend_refusal_causes(error))


@scheduler_step
async def _sweep_long_jobs(long_jobs: LongJobExecutor) -> None:
    """Claim and start due long jobs; database errors retry on the next tick."""
    try:
        await long_jobs.sweep()
    except SQLAlchemyError:
        log.exception("scheduler.long_jobs_sweep.failed")


@scheduler_step
async def _sweep_wizard_sessions(sm: async_sessionmaker[AsyncSession]) -> None:
    """Abandon expired open wizard_session rows once. Boundary catch: a sweep
    failure must not kill the scheduler loop — the next tick retries.

    No `anthropic.APIError` in the catch: this sweep makes no upstream call.
    """
    try:
        await sweep_expired_wizard_sessions(sm, now=datetime.now(UTC))
    except SQLAlchemyError:
        log.exception("scheduler.wizard_sweep.failed")


@scheduler_step
async def _sweep_secret_shares(sm: async_sessionmaker[AsyncSession]) -> None:
    """Delete shared-secret values past their day, revealed or not, so none outlives it.
    Boundary catch: a sweep failure must not kill the scheduler loop; the next tick retries.
    """
    try:
        async with sm.begin() as session:
            cleared = await clear_lapsed_shares(session, now=datetime.now(UTC))
        log.info("secret_share_sweep.cleared", count=cleared)
    except SQLAlchemyError:
        log.exception("scheduler.secret_share_sweep.failed")


@scheduler_step
async def _sweep_slack_event_dedup(sm: async_sessionmaker[AsyncSession]) -> None:
    """Prune aged slack_event_dedup rows once. Boundary catch: a sweep
    failure must not kill the scheduler loop — the next tick retries.

    No `anthropic.APIError` in the catch: this sweep makes no upstream call.
    """
    try:
        await sweep_expired_slack_event_dedup(sm, now=datetime.now(UTC))
    except SQLAlchemyError:
        log.exception("scheduler.slack_event_dedup_sweep.failed")


@scheduler_step
async def _sweep_pending_carry_overs(sm: async_sessionmaker[AsyncSession]) -> None:
    """Drop framing no session was sent within a week; database errors retry next tick."""
    try:
        await sweep_stale_pending_carry_overs(sm, now=datetime.now(UTC))
    except SQLAlchemyError:
        log.exception("scheduler.pending_carry_over_sweep.failed")


@scheduler_step
async def _sweep_retired_turn_card_intents(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    """Prune old retired card intents; database errors retry on the next tick."""
    try:
        await sweep_retired_turn_card_intents(sm, now=datetime.now(UTC))
    except SQLAlchemyError:
        log.exception("scheduler.turn_card_intent_sweep.failed")


@scheduler_step
async def _sweep_hub_oauth_kv(sm: async_sessionmaker[AsyncSession]) -> None:
    """Prune expired hub login rows once. Boundary catch: a sweep failure
    must not kill the scheduler loop; the next tick retries. No upstream call.
    """
    try:
        await sweep_expired_hub_oauth_kv(sm, now=datetime.now(UTC))
    except SQLAlchemyError:
        log.exception("scheduler.hub_oauth_kv_sweep.failed")


@scheduler_step
async def _sweep_lead_intake(sm: async_sessionmaker[AsyncSession]) -> None:
    """Erase expired lead content independently of mailbox activity."""
    from daimon.core.stores.lead_intake import purge_expired

    try:
        async with sm.begin() as session:
            await purge_expired(session, datetime.now(UTC))
    except SQLAlchemyError:
        log.error("scheduler.lead_retention_failed")


@scheduler_step
async def _sweep_mcp_tokens(sm: async_sessionmaker[AsyncSession]) -> None:
    """Bound ephemeral credential history without pruning operator/external keys."""
    from daimon.core.admin_actions import BILLING_PANEL_TOKEN_LABEL
    from daimon.core.mcp_auth import (
        CHAT_TOKEN_LABEL_PREFIX,
        EPHEMERAL_TOKEN_RETENTION,
        HELD_CHAT_TOKEN_LABEL_PREFIX,
        INTERNAL_TOKEN_LABEL_PREFIX,
    )
    from daimon.core.stores.mcp_tokens import prune_expired_ephemeral_tokens

    try:
        async with sm.begin() as session:
            await prune_expired_ephemeral_tokens(
                session,
                expires_before=datetime.now(UTC) - EPHEMERAL_TOKEN_RETENTION,
                chat_label_prefixes=(
                    CHAT_TOKEN_LABEL_PREFIX,
                    HELD_CHAT_TOKEN_LABEL_PREFIX,
                    INTERNAL_TOKEN_LABEL_PREFIX,
                ),
                panel_label=BILLING_PANEL_TOKEN_LABEL,
            )
    except SQLAlchemyError:
        log.exception("scheduler.mcp_token_sweep.failed")


@scheduler_step
async def _sweep_github_connect_flows(sm: async_sessionmaker[AsyncSession]) -> None:
    """Discard expired encrypted GitHub browser-flow tokens."""
    try:
        async with sm.begin() as session:
            await delete_expired_flows(session, now=datetime.now(UTC))
    except SQLAlchemyError:
        log.exception("scheduler.github_connect_flow_sweep.failed")


# How often to look for closed GitHub token rows. Each run scans the table, and
# about 3,000 rows close a day, so a tick every 30 seconds would only repeat it.
_GITHUB_TOKEN_PRUNE_INTERVAL = timedelta(minutes=10)
# Start of the last run, under one key so the module needs no `global`.
_last_github_token_prune: dict[str, datetime] = {}


@scheduler_step
async def _sweep_github_issued_tokens(
    sm: async_sessionmaker[AsyncSession], *, now: datetime | None = None
) -> None:
    """Delete closed GitHub token rows at most once per interval. Boundary catch:
    a sweep failure must not kill the scheduler loop. A failed run waits out the
    interval too, so a database fault is logged once per interval, not per tick.

    No GitHub call: only revoked and never-stored rows go, and nothing is
    revoked here.
    """
    current = now or datetime.now(UTC)
    last = _last_github_token_prune.get("started")
    if last is not None and current - last < _GITHUB_TOKEN_PRUNE_INTERVAL:
        return
    _last_github_token_prune["started"] = current
    try:
        await sweep_closed_github_issued_tokens(sm, now=current)
    except SQLAlchemyError:
        log.exception("scheduler.github_issued_token_sweep.failed")


# Retention is counted in days, so an hourly run deletes a row at most an hour late.
_MONITORING_PRUNE_INTERVAL = timedelta(hours=1)
_last_monitoring_prune: dict[str, datetime] = {}


@scheduler_step
async def _sweep_monitored_events(
    sm: async_sessionmaker[AsyncSession], *, retention_days: int, now: datetime | None = None
) -> None:
    """Delete monitored-agent rows past retention at most once per interval. Boundary
    catch: a sweep failure must not kill the scheduler loop; a failed run waits too."""
    current = now or datetime.now(UTC)
    last = _last_monitoring_prune.get("started")
    if last is not None and current - last < _MONITORING_PRUNE_INTERVAL:
        return
    _last_monitoring_prune["started"] = current
    try:
        await sweep_monitored_events(sm, now=current, retention_days=retention_days)
    except SQLAlchemyError:
        log.exception("scheduler.monitoring_sweep.failed")


# Retention is counted in days, so an hourly run deletes a row at most an hour late.
_OPS_EVENTS_PRUNE_INTERVAL = timedelta(hours=1)
_last_ops_events_prune: dict[str, datetime] = {}


@scheduler_step
async def _sweep_ops_events(
    sm: async_sessionmaker[AsyncSession], *, retention_days: int, now: datetime | None = None
) -> None:
    """Delete ops events past retention at most once per interval. Boundary catch: a sweep
    failure must not kill the scheduler loop; a failed run waits too."""
    current = now or datetime.now(UTC)
    last = _last_ops_events_prune.get("started")
    if last is not None and current - last < _OPS_EVENTS_PRUNE_INTERVAL:
        return
    _last_ops_events_prune["started"] = current
    try:
        await sweep_ops_events(sm, now=current, retention_days=retention_days)
    except SQLAlchemyError:
        log.exception("scheduler.ops_events_sweep.failed")


# Each run reads every idle Gemini row; idleness is counted in days.
_GEMINI_RETENTION_INTERVAL = timedelta(hours=1)
_last_gemini_retention: dict[str, datetime] = {}


@scheduler_step
async def _prune_gemini_records(
    sm: async_sessionmaker[AsyncSession], *, retention_days: int, now: datetime | None = None
) -> None:
    """Prune idle Gemini conversations at most once per interval. Boundary catch:
    a sweep failure must not kill the scheduler loop; the next interval retries.
    """
    current = now or datetime.now(UTC)
    last = _last_gemini_retention.get("started")
    if last is not None and current - last < _GEMINI_RETENTION_INTERVAL:
        return
    _last_gemini_retention["started"] = current
    # Lazy: the sweep pulls the Gemini SDK, which an Anthropic-only install lacks.
    try:
        from daimon.core.turn.gemini_retention import prune_idle_records
    except ImportError:
        return
    try:
        counts = await prune_idle_records(sm, now=current, window=timedelta(days=retention_days))
    except SQLAlchemyError:
        log.exception("scheduler.gemini_retention.failed")
        return
    if counts:
        log.info("scheduler.gemini_retention", **counts)


# Each run lists every vault, and live sessions once a relay vault is old enough.
# Seeded at import so a restart does not sweep at once.
_RELAY_VAULT_ARCHIVE_INTERVAL = timedelta(minutes=10)
_last_relay_vault_archive: dict[str, datetime] = {"started": datetime.now(UTC)}


@scheduler_step
async def _archive_idle_relay_vaults(
    client: AsyncAnthropic, *, now: datetime | None = None
) -> None:
    """Archive relay vaults no live session mounts, at most once per interval.
    Boundary catch: a failure must not kill the scheduler loop; the next run retries.
    """
    current = now or datetime.now(UTC)
    last = _last_relay_vault_archive.get("started")
    if last is not None and current - last < _RELAY_VAULT_ARCHIVE_INTERVAL:
        return
    _last_relay_vault_archive["started"] = current
    try:
        await archive_idle_relay_vaults(client, now=current)
    except (anthropic.APIError, ValueError) as error:
        log.exception("scheduler.relay_vault_archive.failed", causes=backend_refusal_causes(error))


@scheduler_step
async def _sweep_github_app_tokens(
    sm: async_sessionmaker[AsyncSession], *, fernet: MultiFernet | None
) -> None:
    if fernet is None:
        return
    async with sm() as session:
        stale = await select_stale_tokens(session)
        stale.extend(await select_abandoned_pending_tokens(session))
        due_superseded = await select_due_superseded_tokens(session)
        stale.extend(due_superseded)
    stale = list({row.token_id: row for row in stale}.values())
    async with httpx.AsyncClient() as github:
        for candidate in stale:
            async with (
                session_mutation_fence(sm, candidate.session_id, check=False),
                sm.begin() as session,
            ):
                checked = await recheck_token_revoke_due(
                    session, token_id=candidate.token_id, now=datetime.now(UTC)
                )
                if checked is None:
                    continue
                row, naturally_expired = checked
                if not naturally_expired:
                    token = decrypt_issued_token(row, fernet=fernet)
                    if token is None:
                        continue
                    try:
                        await revoke_token(github, token)
                    except httpx.HTTPError:
                        await record_revoke_attempt(session, token_id=row.token_id)
                        log.exception(
                            "scheduler.github_token_revoke.failed", token_id=str(row.token_id)
                        )
                        continue
                await mark_revoked(session, token_id=row.token_id)
                await append_github_token_event(
                    session,
                    tenant_id=row.tenant_id,
                    agent_id=row.agent_id,
                    account_id=row.requester_account_id,
                    kind="github_token_revoke",
                    outcome="allowed",
                    reason="stale access",
                    token_id=row.token_id,
                    session_id=row.session_id,
                    installation_id=row.installation_id,
                    repo_ids=row.repo_ids,
                    permissions=row.permissions,
                    expires_at=row.expires_at,
                    grant_versions=row.grant_versions,
                )


_last_app_access_checks: dict[str, datetime] = {}
# Session id -> (consecutive failures, earliest next attempt). A failing refresh
# mints fresh tokens each attempt, so retries back off from 1 to 30 minutes.
_app_refresh_failures: dict[str, tuple[int, datetime]] = {}
# MA session id -> why its refresh stopped. A session MA reports archived or gone is
# never refreshed again; the turn path replaces it on the next message. In memory
# only, so a restart gives each one more attempt before it is retired again.
_app_refresh_retired: dict[str, str] = {}
_REFRESH_BACKOFF_START = timedelta(minutes=1)
_REFRESH_BACKOFF_MAX = timedelta(minutes=30)
# A refresh is due 15 minutes before the token expires. Backoff attempts land at
# 0, 1, 3, 7 and 15 minutes, so the fifth consecutive failure means the old token
# is about to expire. Until then a GitHub mint outage is retried quietly (WARNING);
# from then on it is an ERROR, so a real outage or broken session still alerts.
_REFRESH_DEFERRED_FAILURES = 4
# A turn running continuously for longer than this is treated as abandoned and
# stops renewing its tokens. Mapped turns use active_turn_started_at; MCP
# sessions use when the scheduler first saw MA report them running.
_ACTIVE_TURN_REFRESH_CAP = timedelta(hours=12)
_mcp_running_since: dict[str, datetime] = {}


def _refresh_backing_off(session_id: str, now: datetime) -> bool:
    failure = _app_refresh_failures.get(session_id)
    return failure is not None and failure[1] > now


def _record_refresh_failure(session_id: str, now: datetime) -> None:
    count = _app_refresh_failures.get(session_id, (0, now))[0] + 1
    delay = min(_REFRESH_BACKOFF_START * 2 ** min(count - 1, 5), _REFRESH_BACKOFF_MAX)
    _app_refresh_failures[session_id] = (count, now + delay)


# With this little life left on the current token, a mint outage is no longer
# something to wait out. The count above resets when the scheduler restarts, so
# the token's own expiry (persisted) is the second trigger.
_REFRESH_URGENT_MARGIN = timedelta(minutes=5)


def _is_transient_cause(error: BaseException) -> bool:
    """True when the wrapped cause is clearly a passing GitHub or network problem.

    `GitHubTokenMintUnavailable` wraps every mint failure, including a 401 and
    unknown bugs, so only a timeout, a network error, a 5xx or a 429 count.
    """
    seen: set[int] = set()
    cause = error.__cause__ or error.__context__
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, httpx.TransportError):
            return True
        if isinstance(cause, httpx.HTTPStatusError):
            status = cause.response.status_code
            return status == 429 or status >= 500
        cause = cause.__cause__ or cause.__context__
    return False


def _log_refresh_failure(
    event_prefix: str,
    session_id: str,
    error: Exception,
    *,
    expires_at: datetime | None,
    now: datetime,
) -> None:
    """Log a failed refresh as `<prefix>.deferred` (WARNING) or `<prefix>.failed` (ERROR).

    Call it inside the `except` block, after `_record_refresh_failure`, so the
    traceback is attached and the failure count includes this failure. It defers
    only a transient mint outage, only while the session is inside its retry
    budget and its token is not about to expire. Used by the mapped-session
    loop (`scheduler.github_app_session_refresh`) and the MCP loop
    (`scheduler.github_mcp_app_session_refresh`).
    """
    failures = _app_refresh_failures[session_id][0]
    token_urgent = expires_at is not None and expires_at <= now + _REFRESH_URGENT_MARGIN
    if (
        isinstance(error, GitHubTokenMintUnavailable)
        and _is_transient_cause(error)
        and failures <= _REFRESH_DEFERRED_FAILURES
        and not token_urgent
    ):
        log.warning(
            f"{event_prefix}.deferred", session_id=session_id, consecutive_failures=failures
        )
        return
    log.exception(f"{event_prefix}.failed", session_id=session_id)


def _dead_session_refresh_reason(error: BaseException) -> str | None:
    """Name the failure when MA says the session is archived or gone, else None.

    Only these two messages count. Any other 400 or 404 (a vault or resource not
    found, say), auth, rate limit, server and network errors stay retryable.
    """
    message = str(error).lower()
    if isinstance(error, anthropic.BadRequestError) and "archived session" in message:
        return "session_archived"
    if isinstance(error, anthropic.NotFoundError) and "session not found" in message:
        return "session_not_found"
    return None


async def _ma_session_confirmed_gone(
    anthropic_client: AsyncAnthropic, mapping: ThreadSessionRow, session_id: str
) -> bool:
    """True only when MA itself shows the session archived, terminated or missing."""
    try:
        record = await retrieve_session_record(
            anthropic_client,
            session_id,
            scope=session_scope(
                tenant_id=mapping.tenant_id,
                account_id=mapping.account_id,
                call_site="scheduler:refresh_github_app_sessions",
            ),
        )
    except anthropic.NotFoundError:
        return True
    except Exception:
        return False
    return record.archived_at is not None or record.status == "terminated"


async def _retire_mcp_app_session(
    anthropic_client: AsyncAnthropic,
    sm: async_sessionmaker[AsyncSession],
    item: LiveMcpAppSession,
    *,
    fernet: MultiFernet,
) -> None:
    scope = session_scope(
        tenant_id=item.tenant_id,
        account_id=item.account_id,
        call_site="scheduler:retire_mcp_app_session",
    )
    # Local revocation first: the router bearer stops working before remote cleanup.
    async with sm.begin() as session:
        await close_for_ma_session(session, ma_session_id=item.session_id)
    await archive_session(anthropic_client, item.session_id, scope=scope)
    async with sm.begin() as session:
        await finish_headless_app_session(session, session_id=item.session_id)
    async with httpx.AsyncClient() as github:
        await revoke_session_tokens(sm, github, session_id=item.session_id, fernet=fernet)
    await archive_app_vault(anthropic_client, vault_id=item.vault_id, scope=scope)
    async with sm.begin() as session:
        await mark_headless_app_session_closed(session, session_id=item.session_id)


@scheduler_step
async def _refresh_github_app_sessions(
    anthropic_client: AsyncAnthropic,
    sm: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    fernet: MultiFernet | None,
) -> None:
    if fernet is None:
        return
    now = datetime.now(UTC)
    async with sm() as session:
        live = await list_live_app_sessions(session)
    for candidate in live:
        session_id = candidate.mapping.ma_session_id
        if session_id in _app_refresh_retired:
            continue
        due_for_expiry = candidate.expires_at is None or candidate.expires_at <= now + timedelta(
            minutes=15
        )
        last_check = _last_app_access_checks.get(session_id)
        due_for_access = last_check is None or last_check <= now - timedelta(minutes=5)
        if not due_for_expiry and not due_for_access:
            continue
        if _refresh_backing_off(session_id, now):
            continue
        try:
            async with session_mutation_fence(sm, session_id, check=False):
                # Turn start and finish take this fence too. Re-read the mapping
                # inside it so the active-turn decision can't go stale.
                async with sm() as session:
                    item = next(
                        iter(await list_live_app_sessions(session, session_id=session_id)), None
                    )
                if item is None:
                    continue
                snapshot = item.mapping.effective_config
                if snapshot is None or snapshot.vault_id is None:
                    continue
                active_turn = item.mapping.active_turn_message_id is not None
                started = item.mapping.active_turn_started_at
                if active_turn and (started is None or started <= now - _ACTIVE_TURN_REFRESH_CAP):
                    # An abandoned running turn cannot renew its tokens indefinitely.
                    continue
                desired_urls, desired_permissions = await effective_repo_state(
                    sm,
                    tenant_id=item.mapping.tenant_id,
                    agent_id=item.agent_id,
                    account_id=item.mapping.account_id,
                    config=settings.github_app,
                    fernet=fernet,
                    mounted_only=True,
                )
                valid_exclusions = valid_unavailable_repo_urls(snapshot, now=now)
                comparable_urls = tuple(url for url in desired_urls if url not in valid_exclusions)
                # A session that ever had a refused repo, or still owes a notice, is
                # replaced at the turn boundary instead, which carries notice history.
                refusal_history = bool(
                    snapshot.unavailable_repos or snapshot.unavailable_notice_pending
                )
                if (
                    comparable_urls != snapshot.repo_urls
                    and not active_turn
                    and not refusal_history
                ):
                    scope = session_scope(
                        tenant_id=item.mapping.tenant_id,
                        account_id=item.mapping.account_id,
                        call_site="scheduler:refresh_github_app_sessions",
                    )
                    async with sm.begin() as session:
                        await close_for_ma_session(session, ma_session_id=session_id)
                    await archive_session(anthropic_client, session_id, scope=scope)
                    async with sm.begin() as session:
                        await mark_dead(session, id=item.mapping.id)
                    async with httpx.AsyncClient() as github:
                        await revoke_session_tokens(
                            sm, github, session_id=session_id, fernet=fernet
                        )
                    await archive_app_vault(
                        anthropic_client, vault_id=snapshot.vault_id, scope=scope
                    )
                    _app_refresh_failures.pop(session_id, None)
                    continue
                level = {"none": 0, "read": 1, "write": 2}
                narrowed = any(
                    level.get(desired_permissions.get(repo_id, {}).get(key, "none"), 0)
                    < level.get(value, 0)
                    for repo_id, current in item.permissions_by_repo.items()
                    for key, value in current.items()
                )
                async with sm() as session:
                    changed_token = await session_token_needs_rotation(
                        session, session_id=session_id
                    )
                if not due_for_expiry and not narrowed and not changed_token:
                    _last_app_access_checks[session_id] = now
                    _app_refresh_failures.pop(session_id, None)
                    continue
                access = await rotate_live_app_tokens(
                    anthropic_client,
                    sm,
                    session_id=session_id,
                    tenant_id=item.mapping.tenant_id,
                    agent_id=item.agent_id,
                    account_id=item.mapping.account_id,
                    vault_id=snapshot.vault_id,
                    resource_ids=snapshot.repo_resource_ids,
                    unavailable_repos=snapshot.unavailable_repos,
                    config=settings.github_app,
                    fernet=fernet,
                    active_turn=active_turn,
                )
                async with sm.begin() as session:
                    await record_app_token_refresh(
                        session,
                        ma_session_id=session_id,
                        issued_at=int(now.timestamp()),
                        access=access,
                        now=now,
                    )
            _last_app_access_checks[session_id] = now
            _app_refresh_failures.pop(session_id, None)
        except Exception as error:
            _last_app_access_checks[session_id] = now
            reason = _dead_session_refresh_reason(error)
            if reason is not None and await _ma_session_confirmed_gone(
                anthropic_client, candidate.mapping, session_id
            ):
                _app_refresh_retired[session_id] = reason
                _app_refresh_failures.pop(session_id, None)
                log.info(
                    "scheduler.github_app_session_refresh.retired",
                    session_id=session_id,
                    reason=reason,
                )
                continue
            _record_refresh_failure(session_id, now)
            _log_refresh_failure(
                "scheduler.github_app_session_refresh",
                session_id,
                error,
                expires_at=candidate.expires_at,
                now=now,
            )
    async with sm() as session:
        mcp_live = await list_live_mcp_app_sessions(session, now=now, include_expired=True)
    for candidate in mcp_live:
        try:
            async with session_mutation_fence(sm, candidate.session_id, check=False):
                # Continue and close use this fence too. Re-read after acquiring it.
                async with sm() as session:
                    current = next(
                        iter(
                            await list_live_mcp_app_sessions(
                                session,
                                now=datetime.now(UTC),
                                session_id=candidate.session_id,
                                include_expired=True,
                            )
                        ),
                        None,
                    )
                if current is None:
                    continue
                try:
                    observed = await retrieve_session_record(
                        anthropic_client,
                        current.session_id,
                        scope=session_scope(
                            tenant_id=current.tenant_id,
                            account_id=current.account_id,
                            call_site="scheduler:refresh_mcp_app_sessions",
                        ),
                    )
                except anthropic.NotFoundError:
                    async with sm.begin() as session:
                        await finish_headless_app_session(session, session_id=current.session_id)
                    continue
                active_turn = observed.status in ("running", "rescheduling")
                if observed.status not in ("idle", "running", "rescheduling"):
                    async with sm.begin() as session:
                        await finish_headless_app_session(session, session_id=current.session_id)
                    continue
                if not active_turn:
                    _mcp_running_since.pop(current.session_id, None)
                    if current.last_started_at <= now - timedelta(minutes=46):
                        continue
                else:
                    async with sm.begin() as session:
                        await touch_running_mcp_app_session(
                            session, session_id=current.session_id, now=datetime.now(UTC)
                        )
                    running_since = _mcp_running_since.setdefault(current.session_id, now)
                    if running_since <= now - _ACTIVE_TURN_REFRESH_CAP:
                        # An abandoned running turn cannot renew its tokens indefinitely.
                        continue
                if current.account_id is None:
                    if active_turn:
                        continue
                    await _retire_mcp_app_session(anthropic_client, sm, current, fernet=fernet)
                    continue
                due_for_expiry = (
                    current.expires_at is None or current.expires_at <= now + timedelta(minutes=15)
                )
                last_check = _last_app_access_checks.get(current.session_id)
                due_for_access = last_check is None or last_check <= now - timedelta(minutes=5)
                if not due_for_expiry and not due_for_access:
                    continue
                if _refresh_backing_off(current.session_id, now):
                    continue
                desired_urls, desired_permissions = await effective_repo_state(
                    sm,
                    tenant_id=current.tenant_id,
                    agent_id=current.agent_id,
                    account_id=current.account_id,
                    config=settings.github_app,
                    fernet=fernet,
                    mounted_only=True,
                )
                if desired_urls != current.repo_urls and not active_turn:
                    await _retire_mcp_app_session(anthropic_client, sm, current, fernet=fernet)
                    continue
                level = {"none": 0, "read": 1, "write": 2}
                narrowed = any(
                    level.get(desired_permissions.get(repo_id, {}).get(key, "none"), 0)
                    < level.get(value, 0)
                    for repo_id, permissions in current.permissions_by_repo.items()
                    for key, value in permissions.items()
                )
                async with sm() as session:
                    changed_token = await session_token_needs_rotation(
                        session, session_id=current.session_id
                    )
                if not due_for_expiry and not narrowed and not changed_token:
                    _last_app_access_checks[current.session_id] = now
                    _app_refresh_failures.pop(current.session_id, None)
                    continue
                access = None
                if current.expires_at is not None or current.repo_urls or desired_permissions:
                    access = await rotate_live_app_tokens(
                        anthropic_client,
                        sm,
                        session_id=current.session_id,
                        tenant_id=current.tenant_id,
                        agent_id=current.agent_id,
                        account_id=current.account_id,
                        vault_id=current.vault_id,
                        resource_ids=current.repo_resource_ids,
                        config=settings.github_app,
                        fernet=fernet,
                        active_turn=active_turn,
                    )
                _last_app_access_checks[current.session_id] = now
                if access is not None and access.excluded_repo_urls:
                    _record_refresh_failure(current.session_id, now)
                else:
                    _app_refresh_failures.pop(current.session_id, None)
        except Exception as error:
            _last_app_access_checks[candidate.session_id] = now
            _record_refresh_failure(candidate.session_id, now)
            _log_refresh_failure(
                "scheduler.github_mcp_app_session_refresh",
                candidate.session_id,
                error,
                expires_at=candidate.expires_at,
                now=now,
            )
    # Drop state for sessions that died or closed since the last sweep.
    async with sm() as session:
        tracked = {item.mapping.ma_session_id for item in await list_live_app_sessions(session)}
        tracked.update(
            item.session_id
            for item in await list_live_mcp_app_sessions(session, now=now, include_expired=True)
        )
    for state in (
        _last_app_access_checks,
        _app_refresh_failures,
        _app_refresh_retired,
        _mcp_running_since,
    ):
        for session_id in set(state) - tracked:
            del state[session_id]


@scheduler_step
async def _close_github_app_sessions(
    anthropic_client: AsyncAnthropic,
    sm: async_sessionmaker[AsyncSession],
    *,
    fernet: MultiFernet | None,
) -> None:
    async with sm() as session:
        closed = await list_closed_app_sessions(session, now=datetime.now(UTC))
    async with httpx.AsyncClient() as github:
        for item in closed:
            try:
                async with session_mutation_fence(sm, item.session_id, check=False):
                    async with sm() as session:
                        current = await closed_app_session_for_id(
                            session, session_id=item.session_id, now=datetime.now(UTC)
                        )
                    if current is None:
                        continue
                    if current.tenant_id is None:
                        raise ScopeViolation(item.session_id, "closed app session has no tenant")
                    scope = session_scope(
                        tenant_id=current.tenant_id,
                        account_id=current.account_id,
                        call_site="scheduler:close_github_app_sessions",
                    )
                    if current.is_mcp:
                        try:
                            observed = await retrieve_session_record(
                                anthropic_client, item.session_id, scope=scope
                            )
                        except anthropic.NotFoundError:
                            observed = None
                        except Exception:
                            # Unknown upstream state must not destroy a running turn.
                            continue
                        if observed is not None and observed.status in ("running", "rescheduling"):
                            async with sm.begin() as session:
                                await touch_running_mcp_app_session(
                                    session, session_id=item.session_id, now=datetime.now(UTC)
                                )
                            continue
                    if fernet is None:
                        async with sm() as session:
                            if await list_session_tokens(session, session_id=item.session_id):
                                continue
                    else:
                        await revoke_session_tokens(
                            sm, github, session_id=item.session_id, fernet=fernet
                        )
                    if current.vault_id is not None:
                        await archive_app_vault(
                            anthropic_client, vault_id=current.vault_id, scope=scope
                        )
                    async with sm.begin() as session:
                        await mark_headless_app_session_closed(session, session_id=item.session_id)
            except Exception:
                log.exception(
                    "scheduler.github_app_session_close.failed",
                    session_id=item.session_id,
                )


@scheduler_step
async def _settle_promo_credit(sm: async_sessionmaker[AsyncSession]) -> None:
    """Grant opened timed promo windows, expire closed ones, credit back late spend.

    The usage sweep runs on its own loop, so a session's spend can land up to
    two passes plus one tick interval late. ``LATE_SPEND_GRACE`` assumes that
    is well under its 15 minutes; a debit landing later counts as ordinary
    spend. Idempotent; boundary catch so a DB failure retries on the next tick.
    """
    try:
        await settle_promo_credit(sm, now=datetime.now(UTC))
    except SQLAlchemyError:
        log.exception("scheduler.promo_credit_settle.failed")


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


async def _repeat_until_stopped(
    step: Callable[[], Awaitable[None]], *, interval_s: float, stop_event: asyncio.Event
) -> None:
    while not stop_event.is_set():
        await step()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)


_TICK_LOG_INTERVAL_S = 60.0


def _tick_progress(
    tick: Callable[[], Awaitable[None]], *, clock: Callable[[], float] = time.monotonic
) -> Callable[[], Awaitable[None]]:
    """`tick` that logs `scheduler.tick_completed` after a tick that finished,
    at most once per minute (the first always). A tick that raises logs nothing."""
    last_logged: float | None = None

    async def step() -> None:
        nonlocal last_logged
        started = clock()
        await tick()
        now = clock()
        if last_logged is None or now - last_logged >= _TICK_LOG_INTERVAL_S:
            last_logged = now
            log.info("scheduler.tick_completed", duration_ms=int((now - started) * 1000))

    return step


@scheduler_step
async def _usage_sweep_once(*, enabled: bool, sweep: Callable[[], Awaitable[None]]) -> None:
    """One usage pass for `--once`, honouring the same switch as the loop."""
    if not enabled:
        log.warning("scheduler.usage_sweep.disabled")
        return
    await sweep()


@scheduler_step
async def _workflow_loops(
    engine: workflows.WorkflowEngine, *, stop_event: asyncio.Event, db: AsyncEngine
) -> None:
    """The workflow engine's own loops (`scheduler.workflows`), each step guarded there."""
    await workflows.run_loops(engine, stop_event=stop_event, db=db)


@scheduler_step
async def _workflows_once(engine: workflows.WorkflowEngine) -> None:
    await workflows.run_once(engine)


async def _run_loops(
    *,
    tick: Callable[[], Awaitable[None]],
    usage_sweep: Callable[[], Awaitable[None]] | None,
    privacy_sweep: Callable[[], Awaitable[None]],
    compute_sweep: Callable[[], Awaitable[None]],
    compute_task_poll: Callable[[], Awaitable[None]],
    interval_s: float,
    stop_event: asyncio.Event,
    workflow_loops: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Run ticks and upstream sweeps on separate loops until stopped.

    ``usage_sweep=None`` (the sweep is switched off) runs no usage loop; the
    privacy deletion and compute cleanup loops always run, since deletion must
    not wait on an unrelated setting. Compute inventory may outlast many ticks;
    it has no total sweep timeout, so growing inventory cannot starve cleanup.
    Background command polling has its own loop, so a slow machine delays
    neither cleanup nor dispatch.

    A usage pass settles whole session histories and can outlast many ticks;
    inline, routine claims waited for it. On stop the passes in flight are
    cancelled: each model call commits on its own and an unfinished session
    keeps its settle request. An uncaught error in any loop ends all loops.
    `workflow_loops` runs until `stop_event` on its own intervals.
    """
    async with asyncio.TaskGroup() as loops:
        background = [
            loops.create_task(
                _repeat_until_stopped(privacy_sweep, interval_s=interval_s, stop_event=stop_event)
            ),
            loops.create_task(
                _repeat_until_stopped(compute_sweep, interval_s=interval_s, stop_event=stop_event)
            ),
            loops.create_task(
                _repeat_until_stopped(
                    compute_task_poll, interval_s=interval_s, stop_event=stop_event
                )
            ),
        ]
        if workflow_loops is not None:
            run_workflows = workflow_loops

            async def workflows_until_stopped() -> None:
                await run_workflows()

            background.append(loops.create_task(workflows_until_stopped()))
        if usage_sweep is not None:
            background.append(
                loops.create_task(
                    _repeat_until_stopped(usage_sweep, interval_s=interval_s, stop_event=stop_event)
                )
            )
        await _repeat_until_stopped(
            _tick_progress(tick), interval_s=interval_s, stop_event=stop_event
        )
        for task in background:
            task.cancel()


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
    configure_logging(settings.log.level)
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

    engine = _engine_override or build_engine(
        str(settings.database.url),
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        pool_timeout=settings.database.pool_timeout,
    )
    sm = build_session_factory(
        engine,
        crypto_keys=tuple(k.get_secret_value() for k in settings.crypto.keys),
        allow_plaintext=settings.crypto.allow_plaintext,
    )

    client = (
        await _anthropic_factory(settings)
        if _anthropic_factory is not None
        else AsyncAnthropic(
            api_key=settings.anthropic.resolved_api_key(),
            base_url=str(settings.anthropic.base_url),
            max_retries=MA_MAX_RETRIES,
            http_client=DefaultAsyncHttpxClient(
                transport=SkillsRateLimitedTransport(settings.anthropic.skills_requests_per_minute)
            ),
        )
    )
    crypto_keys = tuple(secret.get_secret_value() for secret in settings.crypto.keys)
    push_resync_fernet = build_multifernet(crypto_keys) if crypto_keys else None

    lock_conn = await _acquire_advisory_lock(engine, scheduler_settings.advisory_lock_key)
    if lock_conn is None:
        log.warning(
            "scheduler.lock.not_acquired",
            key=scheduler_settings.advisory_lock_key,
        )
        await client.close()
        await drain_outcomes()
        await drain_ops_events()
        await engine.dispose()
        return 1

    # Liveness responder on THIS loop (OB-3): a hung loop stops answering → the
    # platform's health check restarts the process. Started after the lock so
    # only the active scheduler serves the check; closed in the finally below.
    health_server = await start_liveness_responder(scheduler_settings.health_port)

    deployment_default = parse_deployment_default(settings.defaults_root)
    resolver_cache = new_resolver_cache()
    billing_config = load_billing_config()
    dm_turn_deps = build_turn_deps(
        settings,
        client,
        sm,
        deployment_default=deployment_default,
        resolver_cache=resolver_cache,
        billing_config=billing_config,
    )
    notify_owner = build_owner_notifier(
        sm=sm,
        settings=settings,
        anthropic=client,
        discord_dm=discord_owner_dm(settings.discord.bot_token.get_secret_value())
        if settings.discord is not None
        else None,
        membership=DiscordRestMembershipResolver(settings.discord.bot_token.get_secret_value())
        if settings.discord is not None
        else None,
    )
    fire = await _build_fire(
        client=client,
        sm=sm,
        settings=settings,
        deployment_default=deployment_default,
        resolver_cache=resolver_cache,
        billing_config=billing_config,
        dm_turn_deps=dm_turn_deps,
        notify_owner=notify_owner,
    )

    dispatcher = RoutineDispatcher(scheduler_settings.max_concurrent_fires)
    compute = ComputeSessions(sm, settings.compute, compute_provider_for(settings.compute))
    long_jobs = LongJobExecutor(
        sm,
        ManagedLongJobPort(client, sm, settings),
        settings.long_jobs,
        settings=settings,
        markup=settings.billing.markup,
    )
    stop_event = asyncio.Event()
    lane_turn = workflows.HeadlessLaneTurn(client, sm, settings, deployment_default, resolver_cache)
    tool_client = McpToolClient(client, sm, settings, lane_turn.targets)
    workflow_engine = workflows.WorkflowEngine(
        sm,
        settings,
        {
            "agent": AgentStepExecutor(
                sm,
                settings,
                lane_turn,
                tool_client,
                owner_dm=workflows.OwnerDmTurn(sm, settings, dm_turn_deps),
                collect_files=functools.partial(session_files, client),
            ),
            "action": ActionStepExecutor(sm, settings, tool_client),
            "workflow": WorkflowStepExecutor(sm, settings),
            "post": PostStepExecutor(sm),
            "code": CodeStepExecutor(
                sm, ManagedCodeWorker(client, sm, settings, lane_turn.targets)
            ),
            **workflow_waits.executors(sm),
        },
        poll_client=McpPollClient(client, sm, settings, lane_turn.agent_id),
    )
    loop = asyncio.get_running_loop()

    def on_signal() -> None:
        mark_shutting_down()
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, on_signal)
        except NotImplementedError:
            # Signal handlers aren't available on every platform (e.g. Windows
            # ProactorEventLoop). Tests / non-Unix runs proceed without them.
            log.debug("scheduler.signal_handler_unavailable", signal=sig.name)

    try:
        if not args.once:
            dm_turn_deps.start_context_providers()
        if args.once:
            await _run_one_tick_step(
                now=datetime.now(UTC),
                sm=sm,
                fire=fire,
                max_age=timedelta(seconds=scheduler_settings.max_age_s),
                max_concurrent_fires=scheduler_settings.max_concurrent_fires,
                dispatch_timeout_s=scheduler_settings.dispatch_timeout_s,
                wait_for_completion=True,
                notify_owner=notify_owner,
            )
            await _sweep_long_jobs(long_jobs)
            await _sweep_compute(compute)
            await _poll_compute_tasks(compute)
            await _drain_pending_usage(sm, markup=settings.billing.markup)
            await _sweep_pending_files(client, sm)
            await _sweep_privacy_deletes(client, sm)
            await _usage_sweep_once(
                enabled=scheduler_settings.usage_sweep_enabled,
                sweep=lambda: _sweep_headless_usage(client, sm, markup=settings.billing.markup),
            )
            await _sweep_wizard_sessions(sm)
            await _sweep_secret_shares(sm)
            await _sweep_slack_event_dedup(sm)
            await _sweep_retired_turn_card_intents(sm)
            await _sweep_pending_carry_overs(sm)
            await _sweep_hub_oauth_kv(sm)
            await _sweep_mcp_tokens(sm)
            await _sweep_lead_intake(sm)
            await _sweep_github_connect_flows(sm)
            await _sweep_github_issued_tokens(sm)
            await _sweep_monitored_events(sm, retention_days=settings.monitoring.retention_days)
            await _sweep_ops_events(sm, retention_days=settings.ops_events.retention_days)
            await _prune_gemini_records(sm, retention_days=settings.gemini.records_retention_days)
            await _archive_idle_relay_vaults(client)
            await _sweep_github_app_tokens(sm, fernet=push_resync_fernet)
            await _refresh_github_app_sessions(
                client, sm, settings=settings, fernet=push_resync_fernet
            )
            await _close_github_app_sessions(client, sm, fernet=push_resync_fernet)
            await _settle_promo_credit(sm)
            await _drain_github_push_resync(
                engine=engine,
                sm=sm,
                client=client,
                settings=settings,
                fernet=push_resync_fernet,
            )
            await _drain_github_installation_reconciliations(sm=sm, settings=settings)
            await _workflows_once(workflow_engine)
            return 0

        async def tick() -> None:
            await _run_one_tick_step(
                now=datetime.now(UTC),
                sm=sm,
                fire=fire,
                max_age=timedelta(seconds=scheduler_settings.max_age_s),
                max_concurrent_fires=scheduler_settings.max_concurrent_fires,
                dispatch_timeout_s=scheduler_settings.dispatch_timeout_s,
                dispatcher=dispatcher,
                notify_owner=notify_owner,
            )
            await _sweep_long_jobs(long_jobs)
            await _drain_pending_usage(sm, markup=settings.billing.markup)
            await _sweep_pending_files(client, sm)
            await _sweep_wizard_sessions(sm)
            await _sweep_secret_shares(sm)
            await _sweep_slack_event_dedup(sm)
            await _sweep_retired_turn_card_intents(sm)
            await _sweep_pending_carry_overs(sm)
            await _sweep_hub_oauth_kv(sm)
            await _sweep_mcp_tokens(sm)
            await _sweep_lead_intake(sm)
            await _sweep_github_connect_flows(sm)
            await _sweep_github_issued_tokens(sm)
            await _sweep_monitored_events(sm, retention_days=settings.monitoring.retention_days)
            await _sweep_ops_events(sm, retention_days=settings.ops_events.retention_days)
            await _prune_gemini_records(sm, retention_days=settings.gemini.records_retention_days)
            await _archive_idle_relay_vaults(client)
            await _sweep_github_app_tokens(sm, fernet=push_resync_fernet)
            await _refresh_github_app_sessions(
                client, sm, settings=settings, fernet=push_resync_fernet
            )
            await _close_github_app_sessions(client, sm, fernet=push_resync_fernet)
            await _settle_promo_credit(sm)
            await _drain_github_push_resync(
                engine=engine,
                sm=sm,
                client=client,
                settings=settings,
                fernet=push_resync_fernet,
            )
            await _drain_github_installation_reconciliations(sm=sm, settings=settings)

        async def usage_sweep() -> None:
            await _sweep_headless_usage(client, sm, markup=settings.billing.markup)

        async def privacy_sweep() -> None:
            await _sweep_privacy_deletes(client, sm)

        async with runtime_health(
            "scheduler", engine, settings.observability.health_interval_s, current_turn_counts
        ):
            if not scheduler_settings.usage_sweep_enabled:
                log.warning("scheduler.usage_sweep.disabled")
            await _run_loops(
                tick=tick,
                usage_sweep=usage_sweep if scheduler_settings.usage_sweep_enabled else None,
                privacy_sweep=privacy_sweep,
                compute_sweep=lambda: _sweep_compute(compute),
                compute_task_poll=lambda: _poll_compute_tasks(compute),
                interval_s=scheduler_settings.tick_interval_s,
                stop_event=stop_event,
                workflow_loops=lambda: _workflow_loops(
                    workflow_engine, stop_event=stop_event, db=engine
                ),
            )

        return 0
    finally:
        # Match live-DM shutdown: grace for accepted turns, then the normal
        # best-effort current-mapping interrupt. No durable stop obligation.
        dm_tasks = dm_turn_deps.running_turns.tasks()
        if dm_tasks:
            await asyncio.wait(dm_tasks, timeout=60)
        dm_turn_deps.running_turns.begin_shutdown()
        await long_jobs.close()
        await dispatcher.close()
        await drain_owner_notices(timeout_s=30)
        await dm_turn_deps.context_providers.aclose()
        await dm_turn_deps.provider_clients.aclose()
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
        await drain_outcomes()
        await drain_ops_events()
        if _engine_override is None:
            await engine.dispose()


@scheduler_step
async def _drain_github_push_resync(
    *,
    engine: AsyncEngine,
    sm: async_sessionmaker[AsyncSession],
    client: AsyncAnthropic,
    settings: Settings,
    fernet: MultiFernet | None,
) -> None:
    """Keep queue failures inside a named scheduler boundary; later ticks retry."""
    try:
        await drain_github_push_resync_queue(
            engine=engine,
            sessionmaker=sm,
            fernet=fernet,
            anthropic_client=client,
            github_settings=settings.github,
            github_app=settings.github_app,
        )
    except Exception:
        log.exception("scheduler.github_push_resync.failed")


@scheduler_step
async def _drain_github_installation_reconciliations(
    *, sm: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """Keep GitHub API failures inside a scheduler boundary; later ticks retry."""
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as http_client:
            await drain_github_installation_reconciliations(
                sessionmaker=sm,
                http_client=http_client,
                github_app=settings.github_app,
            )
    except Exception:
        log.exception("scheduler.github_installation_reconciliation.failed")


def run_sync() -> None:
    """Console-script entrypoint. ``daimon-scheduler`` resolves here."""
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    run_sync()
