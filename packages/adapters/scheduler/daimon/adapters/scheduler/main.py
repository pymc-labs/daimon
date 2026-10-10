"""Scheduler process entrypoint.

Imperative shell that:

1. Loads settings, builds engine + sessionmaker + AsyncAnthropic.
2. Acquires ``pg_try_advisory_lock`` on a DEDICATED connection. The lock is
   session-scoped — if the connection were ever returned to the pool and
   reused, the lock would die. The connection is therefore held open for
   the full process lifetime (created via ``engine.connect()``, never via
   ``async_sessionmaker``).
3. Installs SIGINT/SIGTERM signal handlers via the running event loop.
4. Loops: every ``tick_interval_s``, awaits ``run_one_tick``; the usage sweep
   repeats on its own loop at the same interval.
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
import httpx
import structlog
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
from cryptography.fernet import MultiFernet
from daimon.adapters.scheduler.settings import SchedulerSettings
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import (
    Action,
    AgentRef,
    Place,
    Subject,
    Surface,
    authorize,
    build_agent_ref,
    build_subject,
)
from daimon.core.billing import BillingConfig, is_over_cap, load_billing_config
from daimon.core.channel_budget import is_over_channel_budget
from daimon.core.config import Settings, load_settings
from daimon.core.constants import MA_MAX_RETRIES
from daimon.core.db import build_engine, build_session_factory
from daimon.core.defaults.loader import parse_deployment_default
from daimon.core.defaults.provisioning import reconcile_tenant_defaults
from daimon.core.github_app_session import (
    archive_app_vault,
    effective_repo_state,
    revoke_session_tokens,
    revoke_token,
    rotate_live_app_tokens,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.github_installation_reconcile import (
    drain_github_installation_reconciliations,
)
from daimon.core.headless_runner import run_turn
from daimon.core.health import start_liveness_responder
from daimon.core.hub_oauth_kv_sweep import sweep_expired_hub_oauth_kv
from daimon.core.logging_setup import configure_logging
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import (
    ResolverCache,
    new_resolver_cache,
    resolve_agent,
    resolve_environment,
)
from daimon.core.observability import init_sentry
from daimon.core.pending_file_sweeper import sweep_pending_file_deletes
from daimon.core.permissions import any_agent_rules, any_own_readers
from daimon.core.pricing import MODEL_PRICING
from daimon.core.promo_settlement import settle_promo_credit
from daimon.core.routine_delivery import (
    DirectPost,
    agent_posted_to,
    delivery_target,
    placement_unknown_is_unsafe,
    render_routine_controls,
)
from daimon.core.rule_views import (
    routine_destination_channel,
    routine_destination_place,
    routine_origin,
)
from daimon.core.runtime_health import current_turn_counts, runtime_health
from daimon.core.scheduler import FireFn, RoutineDispatcher, run_one_tick
from daimon.core.scope import DeploymentDefault, ScopeContext
from daimon.core.session_mutation import session_mutation_fence
from daimon.core.skill_sync.resync_queue import drain_github_push_resync_queue
from daimon.core.skills.rate_limit import SkillsRateLimitedTransport
from daimon.core.slack_event_dedup_sweep import sweep_expired_slack_event_dedup
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import Role, RoutineRow
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
    record_revoke_attempt,
    select_abandoned_pending_tokens,
    select_deactivated_tokens,
    select_due_superseded_tokens,
    select_stale_tokens,
    touch_running_mcp_app_session,
)
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.routines import record_result, update_routine_agent_id
from daimon.core.stores.scoped_config_read import resolve
from daimon.core.stores.security_audit import append_github_token_event
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.thread_sessions import mark_dead, record_app_token_refresh
from daimon.core.tenant_balance import is_over_balance
from daimon.core.turn.outcomes import current_outcome, drain_outcomes
from daimon.core.turn.state import TurnState
from daimon.core.turn.termination import TerminationReason
from daimon.core.turn_card_intent_sweep import sweep_retired_turn_card_intents
from daimon.core.usage_recording import record_turn_usage
from daimon.core.usage_sweep import UsageSweepWatermark, sweep_headless_usage
from daimon.core.wizard_sweep import sweep_expired_wizard_sessions
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
            platform = tenant.platform
            principal = await get_or_create_platform_principal(
                s,
                tenant_id=row.tenant_id,
                # Source the platform from the routine's tenant so slack-created
                # routines resolve a slack principal (not a mismatched discord one).
                platform=tenant.platform,
                external_id=row.created_by_user_id,
            )
            account_id = principal.account_id

            # Invoker policy at fire time: someone taken off the allowlist
            # stops running headless turns through routines they created. The
            # stored role is the only admin signal a fire has. Unreadable
            # policy fails closed.
            direct_post: DirectPost = "allowed"
            fire_policy: TenantAccessPolicy | None = None
            fire_channel_id: str | None = None
            try:
                policy = await load_access_policy(s, tenant_id=row.tenant_id)
            except AccessPolicyUnreadable:
                policy_error: str | None = "access_policy_unreadable"
            else:
                # FEAT-085: a destination protected since the routine was made
                # is never offered to the agent as a place to post (SYS-048's
                # guard is not on send_message yet). The poster re-checks with
                # the parent channel and category it resolves.
                # The scheduler cannot resolve a Discord thread's parent or
                # a channel's category, so when the policy protects either,
                # the agent is not invited to post directly at all.
                target = delivery_target(row, platform=platform)
                fire_policy = policy
                fire_channel_id = target.channel_id if target is not None else None
                if target is not None:
                    if not authorize(
                        policy,
                        subject=Subject(),
                        action=Action.POST,
                        surface=Surface.ROUTINE,
                        place=Place(channel_id=target.channel_id),
                    ):
                        direct_post = "protected"
                    elif placement_unknown_is_unsafe(
                        policy, platform=platform, kind=row.destination_kind
                    ):
                        direct_post = "unverified"
                account = await get_account(s, account_id)
                is_admin = account is not None and account.role is Role.ADMIN
                allowed = authorize(
                    policy,
                    subject=build_subject(
                        is_admin=is_admin, platform_user_id=row.created_by_user_id
                    ),
                    action=Action.ACT_FOR_CREATOR,
                    surface=Surface.ROUTINE,
                )
                policy_error = None if allowed else "invoker_not_allowed"
                # A pinned agent fires only when it posts straight into one of
                # its pinned channels; `_check_agent_pin` refuses anything else
                # at save time, and this holds for a pin added since. Isolation
                # needs every name, so it waits for the resolved agent below.
                if (
                    policy_error is None
                    and authorize(
                        policy,
                        subject=Subject(),
                        action=Action.RUN_AGENT,
                        surface=Surface.ROUTINE,
                        agent=AgentRef.of(row.agent_name),
                        place=Place(channel_id=target.channel_id if target is not None else None),
                    ).reason
                    == "runs_elsewhere"
                ):
                    policy_error = "runs_elsewhere"
            if policy_error is not None:
                log.info(
                    "routine.skipped.invoker_policy",
                    routine_id=str(row.id),
                    tenant_id=str(row.tenant_id),
                    reason=policy_error,
                )
                await record_result(s, row.id, tail=None, error=policy_error)
                return

        # Admission gate: per-tenant balance — independent of Stripe config.
        # Keys on row.tenant_id (NOT NULL). Mirror run_one_tick's cap_exceeded skip.
        if await is_over_balance(sessionmaker=sm, tenant_id=row.tenant_id):
            if (observation := current_outcome.get()) is not None:
                observation.finish(reason=TerminationReason.ADMISSION_BALANCE_DEPLETED)
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
                channel_id=row.channel_id,
            )

        # Resolve agent + environment by daimon-tag at fire time,
        # self-healing across MA archive/recreate. The environment follows the
        # routine's channel, then the tenant default, like a turn there would.
        # defensive: post-0012 agent_name is NOT NULL but the fallback is harmless and free.
        agent_tag = row.agent_name or deployment_default.agent_name or "daimon"
        _public_url = str(settings.mcp.public_url) if settings.mcp.public_url else None
        resolved_agent_id = await resolve_agent(
            client,
            tenant_id=row.tenant_id,
            daimon_tag=agent_tag,
            cached_id=row.agent_id,
            apply_callable=lambda: reconcile_tenant_defaults(
                client, sm, settings.defaults_root, tenant_id=row.tenant_id, public_url=_public_url
            ),
            cache=resolver_cache,
        )
        async with sm() as scope_s:
            scoped = await resolve(
                scope_s,
                context=ScopeContext(
                    tenant_id=row.tenant_id,
                    channel_id=routine_destination_channel(row) or row.channel_id,
                ),
                default=deployment_default,
            )
        resolved_env_id = await resolve_environment(
            client,
            tenant_id=row.tenant_id,
            daimon_tag=scoped.environment_name or "default",
            cached_id=None,
            apply_callable=lambda: reconcile_tenant_defaults(
                client, sm, settings.defaults_root, tenant_id=row.tenant_id, public_url=_public_url
            ),
            cache=resolver_cache,
        )
        # The pin and channel isolation are checked again on the agent that
        # will actually run, by every name a pin can be keyed by, after
        # self-healing may have picked a replacement: the saved routine name
        # alone can miss a pin on the display name or a rename.
        if fire_policy is not None and (
            any_agent_rules(fire_policy) or any_own_readers(fire_policy)
        ):
            ran = await client.beta.agents.retrieve(resolved_agent_id)
            decision = authorize(
                fire_policy,
                subject=Subject(),
                action=Action.RUN_AGENT,
                surface=Surface.ROUTINE,
                agent=build_agent_ref(ran.name, ran.metadata, row.agent_name),
                # A thread destination sits under its saved parent channel; one
                # saved without it can't be placed here and fails closed while
                # anything is isolated.
                place=routine_destination_place(row, channel_id=fire_channel_id),
            )
            if decision.reason is not None:
                refusal = decision.reason
                log.info(
                    "routine.skipped.invoker_policy",
                    routine_id=str(row.id),
                    tenant_id=str(row.tenant_id),
                    reason=refusal,
                )
                async with sm() as pin_s, pin_s.begin():
                    await record_result(pin_s, row.id, tail=None, error=refusal)
                return
        # Admission gate: the routine's channel budget, after the cap (run_one_tick),
        # balance and pin gates. Keyed on `row.channel_id`, the channel the fire is
        # billed to (the destination's parent, else where the routine was made).
        if await is_over_channel_budget(
            sessionmaker=sm,
            tenant_id=row.tenant_id,
            platform=platform,
            channel_id=row.channel_id,
            now=datetime.now(UTC),
        ):
            if (observation := current_outcome.get()) is not None:
                observation.finish(reason=TerminationReason.ADMISSION_CHANNEL_BUDGET_EXCEEDED)
            log.info(
                "routine.skipped.over_channel_budget",
                routine_id=str(row.id),
                tenant_id=str(row.tenant_id),
                channel_id=row.channel_id,
            )
            async with sm() as s, s.begin():
                await record_result(s, row.id, tail=None, error="channel_budget_exceeded")
            return
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

        # FEAT-085: a routine with a destination opens with host-supplied
        # controls naming it; one without sends its trigger message as before.
        trigger_message = row.trigger_message
        if row.destination_kind is not None:
            trigger_message = (
                render_routine_controls(row, platform=platform, direct_post=direct_post)
                + "\n"
                + row.trigger_message
            )
        final_state: list[TurnState] = []

        result = await run_turn(
            anthropic=client,
            agent_id=resolved_agent_id,
            environment_id=resolved_env_id,
            trigger_message=trigger_message,
            on_state=final_state.append,
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
            agent_github_app=settings.github_app,
            tool_safety=settings.tool_safety,
            budget_channel_id=row.channel_id,
            # Stamped like a turn in the destination, so an isolated or sealed
            # channel's routine transcript reads only from inside it.
            origin_place=(
                routine_origin(fire_policy, row, platform=platform)
                if fire_policy is not None
                else None
            ),
        )

        if row.destination_kind is None:
            async with sm() as s, s.begin():
                await record_result(s, row.id, tail=result, error=None)
            return

        # Fallback post: only when the agent did not deliver to the
        # destination itself. The chat adapter for `platform` posts it.
        posted = bool(final_state) and agent_posted_to(final_state[0], row)
        async with sm() as s, s.begin():
            await record_result(
                s,
                row.id,
                tail=result,
                error=None,
                delivery="skipped" if posted else "pending",
                delivery_note="agent_posted" if posted else None,
            )

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
    client: AsyncAnthropic,
    sm: async_sessionmaker[AsyncSession],
    *,
    markup: Decimal,
    watermark: UsageSweepWatermark,
) -> None:
    """Backfill usage for headless MCP turns once. Boundary catch: a sweep
    failure must not kill the scheduler — idempotent recording means the next
    pass re-reads and records anything missed.
    """
    try:
        await sweep_headless_usage(client, sm, markup=markup, watermark=watermark)
    except (anthropic.APIError, SQLAlchemyError):
        # Named boundary: a sweep failure (upstream MA error OR a DB write that
        # trips a constraint, e.g. a stray foreign-tenant session) must not kill
        # the sweep loop. Idempotent recording means the next pass retries.
        log.exception("scheduler.usage_sweep.failed")


async def _sweep_wizard_sessions(sm: async_sessionmaker[AsyncSession]) -> None:
    """Abandon expired open wizard_session rows once. Boundary catch: a sweep
    failure must not kill the scheduler loop — the next tick retries.

    No `anthropic.APIError` in the catch: this sweep makes no upstream call.
    """
    try:
        await sweep_expired_wizard_sessions(sm, now=datetime.now(UTC))
    except SQLAlchemyError:
        log.exception("scheduler.wizard_sweep.failed")


async def _sweep_slack_event_dedup(sm: async_sessionmaker[AsyncSession]) -> None:
    """Prune aged slack_event_dedup rows once. Boundary catch: a sweep
    failure must not kill the scheduler loop — the next tick retries.

    No `anthropic.APIError` in the catch: this sweep makes no upstream call.
    """
    try:
        await sweep_expired_slack_event_dedup(sm, now=datetime.now(UTC))
    except SQLAlchemyError:
        log.exception("scheduler.slack_event_dedup_sweep.failed")


async def _sweep_retired_turn_card_intents(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    """Prune old retired card intents; database errors retry on the next tick."""
    try:
        await sweep_retired_turn_card_intents(sm, now=datetime.now(UTC))
    except SQLAlchemyError:
        log.exception("scheduler.turn_card_intent_sweep.failed")


async def _sweep_hub_oauth_kv(sm: async_sessionmaker[AsyncSession]) -> None:
    """Prune expired hub login rows once. Boundary catch: a sweep failure
    must not kill the scheduler loop; the next tick retries. No upstream call.
    """
    try:
        await sweep_expired_hub_oauth_kv(sm, now=datetime.now(UTC))
    except SQLAlchemyError:
        log.exception("scheduler.hub_oauth_kv_sweep.failed")


async def _sweep_github_connect_flows(sm: async_sessionmaker[AsyncSession]) -> None:
    """Discard expired encrypted GitHub browser-flow tokens."""
    try:
        async with sm.begin() as session:
            await delete_expired_flows(session, now=datetime.now(UTC))
    except SQLAlchemyError:
        log.exception("scheduler.github_connect_flow_sweep.failed")


async def _sweep_github_app_tokens(
    sm: async_sessionmaker[AsyncSession], *, fernet: MultiFernet | None
) -> None:
    if fernet is None:
        return
    async with sm() as session:
        stale = await select_stale_tokens(session)
        stale.extend(await select_deactivated_tokens(session))
        stale.extend(await select_abandoned_pending_tokens(session))
        stale_ids = {row.token_id for row in stale}
        due_superseded = await select_due_superseded_tokens(session)
        natural_expired_ids = {
            row.token_id
            for row in due_superseded
            if row.revoke_after is not None and row.revoke_after >= row.expires_at
        }
        stale.extend(due_superseded)
    stale = list({row.token_id: row for row in stale}.values())
    async with httpx.AsyncClient() as github:
        for row in stale:
            if row.token_id in natural_expired_ids and row.token_id not in stale_ids:
                # GitHub has already expired the token. No DELETE is needed.
                async with sm.begin() as session:
                    await mark_revoked(session, token_id=row.token_id)
                continue
            token = decrypt_issued_token(row, fernet=fernet)
            if token is None:
                continue
            try:
                await revoke_token(github, token)
            except httpx.HTTPError:
                async with sm.begin() as session:
                    await record_revoke_attempt(session, token_id=row.token_id)
                log.exception("scheduler.github_token_revoke.failed", token_id=str(row.token_id))
                continue
            async with sm.begin() as session:
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
_REFRESH_BACKOFF_START = timedelta(minutes=1)
_REFRESH_BACKOFF_MAX = timedelta(minutes=30)
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


async def _retire_mcp_app_session(
    anthropic_client: AsyncAnthropic,
    sm: async_sessionmaker[AsyncSession],
    item: LiveMcpAppSession,
    *,
    fernet: MultiFernet,
) -> None:
    await anthropic_client.beta.sessions.archive(item.session_id)
    async with sm.begin() as session:
        await finish_headless_app_session(session, session_id=item.session_id)
    async with httpx.AsyncClient() as github:
        await revoke_session_tokens(sm, github, session_id=item.session_id, fernet=fernet)
    await archive_app_vault(anthropic_client, vault_id=item.vault_id)
    async with sm.begin() as session:
        await mark_headless_app_session_closed(session, session_id=item.session_id)


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
        due_for_expiry = candidate.expires_at <= now + timedelta(minutes=15)
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
                    is_external=False,
                    config=settings.github_app,
                    fernet=fernet,
                )
                if desired_urls != snapshot.repo_urls and not active_turn:
                    await anthropic_client.beta.sessions.archive(session_id)
                    async with sm.begin() as session:
                        await mark_dead(session, id=item.mapping.id)
                    async with httpx.AsyncClient() as github:
                        await revoke_session_tokens(
                            sm, github, session_id=session_id, fernet=fernet
                        )
                    await archive_app_vault(anthropic_client, vault_id=snapshot.vault_id)
                    _app_refresh_failures.pop(session_id, None)
                    continue
                level = {"none": 0, "read": 1, "write": 2}
                narrowed = any(
                    level.get(desired_permissions.get(repo_id, {}).get(key, "none"), 0)
                    < level.get(value, 0)
                    for repo_id, current in item.permissions_by_repo.items()
                    for key, value in current.items()
                )
                if not due_for_expiry and not narrowed:
                    _last_app_access_checks[session_id] = now
                    _app_refresh_failures.pop(session_id, None)
                    continue
                await rotate_live_app_tokens(
                    anthropic_client,
                    sm,
                    session_id=session_id,
                    tenant_id=item.mapping.tenant_id,
                    agent_id=item.agent_id,
                    account_id=item.mapping.account_id,
                    is_external=False,
                    vault_id=snapshot.vault_id,
                    resource_ids=snapshot.repo_resource_ids,
                    config=settings.github_app,
                    fernet=fernet,
                    active_turn=active_turn,
                )
                async with sm.begin() as session:
                    await record_app_token_refresh(
                        session,
                        ma_session_id=session_id,
                        issued_at=int(now.timestamp()),
                    )
            _last_app_access_checks[session_id] = now
            _app_refresh_failures.pop(session_id, None)
        except Exception:
            _last_app_access_checks[session_id] = now
            _record_refresh_failure(session_id, now)
            log.exception("scheduler.github_app_session_refresh.failed", session_id=session_id)
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
                    observed = await anthropic_client.beta.sessions.retrieve(current.session_id)
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
                    is_external=False,
                    config=settings.github_app,
                    fernet=fernet,
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
                if not due_for_expiry and not narrowed:
                    _last_app_access_checks[current.session_id] = now
                    _app_refresh_failures.pop(current.session_id, None)
                    continue
                if current.expires_at is not None or current.repo_urls:
                    await rotate_live_app_tokens(
                        anthropic_client,
                        sm,
                        session_id=current.session_id,
                        tenant_id=current.tenant_id,
                        agent_id=current.agent_id,
                        account_id=current.account_id,
                        is_external=False,
                        vault_id=current.vault_id,
                        resource_ids=current.repo_resource_ids,
                        config=settings.github_app,
                        fernet=fernet,
                        active_turn=active_turn,
                    )
                _last_app_access_checks[current.session_id] = now
                _app_refresh_failures.pop(current.session_id, None)
        except Exception:
            _last_app_access_checks[candidate.session_id] = now
            _record_refresh_failure(candidate.session_id, now)
            log.exception(
                "scheduler.github_mcp_app_session_refresh.failed",
                session_id=candidate.session_id,
            )
    # Drop state for sessions that died or closed since the last sweep.
    async with sm() as session:
        tracked = {item.mapping.ma_session_id for item in await list_live_app_sessions(session)}
        tracked.update(
            item.session_id
            for item in await list_live_mcp_app_sessions(session, now=now, include_expired=True)
        )
    for state in (_last_app_access_checks, _app_refresh_failures, _mcp_running_since):
        for session_id in set(state) - tracked:
            del state[session_id]


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
                    if current.is_mcp:
                        try:
                            observed = await anthropic_client.beta.sessions.retrieve(
                                item.session_id
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
                        await archive_app_vault(anthropic_client, vault_id=current.vault_id)
                    async with sm.begin() as session:
                        await mark_headless_app_session_closed(session, session_id=item.session_id)
            except Exception:
                log.exception(
                    "scheduler.github_app_session_close.failed",
                    session_id=item.session_id,
                )


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


async def _usage_sweep_once(*, enabled: bool, sweep: Callable[[], Awaitable[None]]) -> None:
    """One usage pass for `--once`, honouring the same switch as the loop."""
    if not enabled:
        log.warning("scheduler.usage_sweep.disabled")
        return
    await sweep()


async def _run_loops(
    *,
    tick: Callable[[], Awaitable[None]],
    usage_sweep: Callable[[], Awaitable[None]] | None,
    interval_s: float,
    stop_event: asyncio.Event,
) -> None:
    """Run ticks and usage sweeps on separate loops until ``stop_event`` is set.

    ``usage_sweep=None`` (the sweep is switched off) runs ticks alone.

    A usage pass lists every session in the workspace and can outlast many
    ticks; inline, routine claims waited for it. On stop the pass in flight is
    cancelled: each model call commits on its own and an unfinished pass leaves
    the watermark in place. A crash in either loop ends both and propagates.
    """
    if usage_sweep is None:
        await _repeat_until_stopped(tick, interval_s=interval_s, stop_event=stop_event)
        return
    async with asyncio.TaskGroup() as loops:
        sweeps = loops.create_task(
            _repeat_until_stopped(usage_sweep, interval_s=interval_s, stop_event=stop_event)
        )
        await _repeat_until_stopped(tick, interval_s=interval_s, stop_event=stop_event)
        sweeps.cancel()


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
            api_key=settings.anthropic.api_key.get_secret_value(),
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
        await engine.dispose()
        return 1

    # Liveness responder on THIS loop (OB-3): a hung loop stops answering → the
    # platform's health check restarts the process. Started after the lock so
    # only the active scheduler serves the check; closed in the finally below.
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

    dispatcher = RoutineDispatcher(scheduler_settings.max_concurrent_fires)
    usage_watermark = UsageSweepWatermark()
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
                wait_for_completion=True,
            )
            await _sweep_pending_files(client, sm)
            await _usage_sweep_once(
                enabled=scheduler_settings.usage_sweep_enabled,
                sweep=lambda: _sweep_headless_usage(
                    client, sm, markup=settings.billing.markup, watermark=usage_watermark
                ),
            )
            await _sweep_wizard_sessions(sm)
            await _sweep_slack_event_dedup(sm)
            await _sweep_retired_turn_card_intents(sm)
            await _sweep_hub_oauth_kv(sm)
            await _sweep_github_connect_flows(sm)
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
            return 0

        async def tick() -> None:
            await run_one_tick(
                now=datetime.now(UTC),
                sm=sm,
                caps=caps,
                fire=fire,
                max_age=timedelta(seconds=scheduler_settings.max_age_s),
                max_concurrent_fires=scheduler_settings.max_concurrent_fires,
                dispatch_timeout_s=scheduler_settings.dispatch_timeout_s,
                dispatcher=dispatcher,
            )
            await _sweep_pending_files(client, sm)
            await _sweep_wizard_sessions(sm)
            await _sweep_slack_event_dedup(sm)
            await _sweep_retired_turn_card_intents(sm)
            await _sweep_hub_oauth_kv(sm)
            await _sweep_github_connect_flows(sm)
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
            await _sweep_headless_usage(
                client, sm, markup=settings.billing.markup, watermark=usage_watermark
            )

        async with runtime_health(
            "scheduler", engine, settings.observability.health_interval_s, current_turn_counts
        ):
            if not scheduler_settings.usage_sweep_enabled:
                log.warning("scheduler.usage_sweep.disabled")
            await _run_loops(
                tick=tick,
                usage_sweep=usage_sweep if scheduler_settings.usage_sweep_enabled else None,
                interval_s=scheduler_settings.tick_interval_s,
                stop_event=stop_event,
            )

        return 0
    finally:
        await dispatcher.close()
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
        if _engine_override is None:
            await engine.dispose()


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
        )
    except Exception:
        log.exception("scheduler.github_push_resync.failed")


async def _drain_github_installation_reconciliations(
    *, sm: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """Keep GitHub API failures inside a scheduler boundary; later ticks retry."""
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as http_client:
            await drain_github_installation_reconciliations(
                sessionmaker=sm,
                http_client=http_client,
                github_settings=settings.github,
            )
    except Exception:
        log.exception("scheduler.github_installation_reconciliation.failed")


def run_sync() -> None:
    """Console-script entrypoint. ``daimon-scheduler`` resolves here."""
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    run_sync()
