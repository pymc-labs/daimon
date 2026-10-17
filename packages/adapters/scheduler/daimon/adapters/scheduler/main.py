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
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine
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
from daimon.core.github_link_refresh import refresh_expiring_links
from daimon.core.github_requester_access import (
    drain_detached_refreshes,
    settle_detached_refreshes,
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
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import archive_session, retrieve_agent
from daimon.core.observability import init_sentry
from daimon.core.pending_file_sweeper import sweep_pending_file_deletes
from daimon.core.permissions import any_agent_rules, any_own_readers
from daimon.core.pricing import MODEL_PRICING
from daimon.core.promo_settlement import settle_promo_credit
from daimon.core.purge import sweep_privacy_session_deletes
from daimon.core.routine_delivery import (
    DirectPost,
    agent_posted_to,
    delivery_target,
    placement_unknown_is_unsafe,
    record_failure,
    render_routine_controls,
)
from daimon.core.rule_views import (
    routine_answers_only_owner,
    routine_destination_channel,
    routine_destination_place,
    routine_origin,
)
from daimon.core.runtime_health import current_turn_counts, runtime_health
from daimon.core.scheduler import FireFn, RoutineDispatcher, run_one_tick
from daimon.core.scope import DeploymentDefault, ScopeContext
from daimon.core.session_compat import valid_unavailable_repo_urls
from daimon.core.session_mutation import session_mutation_fence
from daimon.core.session_ports_compat import retrieve_session_record, session_scope
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
    recheck_token_revoke_due,
    record_revoke_attempt,
    select_abandoned_pending_tokens,
    select_due_superseded_tokens,
    select_stale_tokens,
    session_token_needs_rotation,
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
from daimon.core.usage_sweep import sweep_headless_usage
from daimon.core.wizard_sweep import sweep_expired_wizard_sessions
from mux.errors import ScopeViolation
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
            if tenant.archived_at is not None:
                await record_result(s, row.id, tail=None, error="routine tenant archived")
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
                # A destination protected since the routine was made
                # is never offered to the agent as a place to post (the protection
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
                await record_failure(
                    s,
                    row.id,
                    error="balance_depleted",
                    reason=TerminationReason.ADMISSION_BALANCE_DEPLETED,
                )
            return

        # Bind routine context now; headless_runner calls the factory once
        # (session_id, model_id) are known after create_session.
        platform_user_id = row.created_by_user_id

        def usage_record_factory(
            session_id: str, model_id: str
        ) -> Callable[..., Awaitable[object]]:
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
            ran = await retrieve_agent(
                client, resolved_agent_id, scope=resource_scope(tenant_id=str(row.tenant_id))
            )
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
                await record_failure(
                    s,
                    row.id,
                    error="channel_budget_exceeded",
                    reason=TerminationReason.ADMISSION_CHANNEL_BUDGET_EXCEEDED,
                )
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

        github_fallback_pat: str | None = None
        github_app_id: str | None = settings.github_app.app_id
        github_app_private_key: str | None = (
            settings.github_app.private_key.get_secret_value()
            if settings.github_app.private_key is not None
            else None
        )

        # A routine with a destination opens with host-supplied
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
            # The owner's personal memory, read-only, only for a routine whose
            # result goes to nobody else; decided again from the row each fire.
            personal_memory=routine_answers_only_owner(row),
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


async def _sweep_privacy_deletes(
    client: AsyncAnthropic, sm: async_sessionmaker[AsyncSession]
) -> None:
    """Retain failed MA erasures for the next tick."""
    try:
        await sweep_privacy_session_deletes(client, sm)
    except Exception:
        log.exception("scheduler.privacy_delete_sweep.failed")


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


async def _sweep_mcp_tokens(sm: async_sessionmaker[AsyncSession]) -> None:
    """Bound ephemeral credential history without pruning operator/external keys."""
    from daimon.core.mcp_auth import (
        CHAT_TOKEN_LABEL_PREFIX,
        EPHEMERAL_TOKEN_RETENTION,
        PANEL_TOKEN_LABEL,
    )
    from daimon.core.stores.mcp_tokens import prune_expired_ephemeral_tokens

    try:
        async with sm.begin() as session:
            await prune_expired_ephemeral_tokens(
                session,
                expires_before=datetime.now(UTC) - EPHEMERAL_TOKEN_RETENTION,
                chat_label_prefix=CHAT_TOKEN_LABEL_PREFIX,
                panel_label=PANEL_TOKEN_LABEL,
            )
    except SQLAlchemyError:
        log.exception("scheduler.mcp_token_sweep.failed")


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
_GITHUB_LINK_REFRESH_INTERVAL_S = 3600.0
# What the hourly sweep may spend before it stops starting new people. It never
# cancels a refresh under way: one person's refresh ends within its lock timeout,
# the HTTP client's timeout and database time.
_GITHUB_LINK_REFRESH_DEADLINE_S = 120.0
# One budget for everything GitHub-link-refresh related at shutdown, taken once
# at the start of `run`'s `finally`. The worker's stop_grace_period is 90 s
# (docker-compose.yml; `up --timeout 95`). The refresh part waits at most 45 s
# plus `_SHUTDOWN_CANCEL_SLACK_S` (50 s); what is left of the 90 s goes to the
# tick already running and the rest of shutdown (dispatcher, health server,
# advisory unlock, client close, outcome drain, engine dispose), which is not
# guaranteed to fit. Three waits come out of the budget, never in addition to it:
#   1. the sweep gets up to `_GITHUB_LINK_REFRESH_SHUTDOWN_GRACE_S` to finish
#      its current person (the grace is also capped by what remains);
#   2. past the grace the sweep is cancelled, and its HTTP-client context holds
#      the client open while a refresh detached from it stores GitHub's answer,
#      for at most what remains of the budget (`_SingleFlight.settle_s`);
#   3. the final drain gets whatever remains, zero if nothing does.
# A refresh still running when the budget is gone may lose GitHub's answer when
# its client closes (GitHub retires the old token pair as it answers); that
# person's link is then broken and they relink on next use. It is logged as
# `scheduler.github_link_refresh.drain_timed_out`. This caps the refresh part
# of shutdown (45 s plus `_SHUTDOWN_CANCEL_SLACK_S`), not shutdown as a whole:
# a tick already running finishes first and the steps after it have their own
# waits. Docker's SIGKILL at 90 s is the hard stop.
_GITHUB_LINK_REFRESH_SHUTDOWN_BUDGET_S = 45.0
_GITHUB_LINK_REFRESH_SHUTDOWN_GRACE_S = 20.0
# Past the budget, how long shutdown still waits for the cancelled sweep's
# database cancel and client close, which the budget cannot cap.
_SHUTDOWN_CANCEL_SLACK_S = 5.0
# How long a sweep cancelled outside shutdown (no shared deadline, as in
# `--once`) holds its client open for a detached refresh.
_GITHUB_LINK_REFRESH_DRAIN_S = 30.0
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
    scope = session_scope(
        tenant_id=item.tenant_id,
        account_id=item.account_id,
        call_site="scheduler:retire_mcp_app_session",
    )
    await archive_session(anthropic_client, item.session_id, scope=scope)
    async with sm.begin() as session:
        await finish_headless_app_session(session, session_id=item.session_id)
    async with httpx.AsyncClient() as github:
        await revoke_session_tokens(sm, github, session_id=item.session_id, fernet=fernet)
    await archive_app_vault(anthropic_client, vault_id=item.vault_id, scope=scope)
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
                    is_external=item.mapping.is_external,
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
                    is_external=item.mapping.is_external,
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
                    is_external=current.is_external,
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
                        is_external=current.is_external,
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
    privacy_sweep: Callable[[], Awaitable[None]],
    interval_s: float,
    stop_event: asyncio.Event,
) -> None:
    """Run ticks and upstream sweeps on separate loops until stopped.

    ``usage_sweep=None`` (the sweep is switched off) runs no usage loop; the
    privacy deletion loop always runs, since a pending erasure must not wait on
    an unrelated setting.

    A usage pass settles whole session histories and can outlast many ticks;
    inline, routine claims waited for it. On stop the passes in flight are
    cancelled: each model call commits on its own and an unfinished session
    keeps its settle request. An uncaught error in any loop ends all loops.
    """
    async with asyncio.TaskGroup() as loops:
        background = [
            loops.create_task(
                _repeat_until_stopped(privacy_sweep, interval_s=interval_s, stop_event=stop_event)
            )
        ]
        if usage_sweep is not None:
            background.append(
                loops.create_task(
                    _repeat_until_stopped(usage_sweep, interval_s=interval_s, stop_event=stop_event)
                )
            )
        await _repeat_until_stopped(tick, interval_s=interval_s, stop_event=stop_event)
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
    link_refresh_throttle = _IntervalThrottle(_GITHUB_LINK_REFRESH_INTERVAL_S)
    link_refresh_flight = _SingleFlight()
    link_refresh_backoff: dict[int, datetime] = {}

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
            await _sweep_privacy_deletes(client, sm)
            await _usage_sweep_once(
                enabled=scheduler_settings.usage_sweep_enabled,
                sweep=lambda: _sweep_headless_usage(client, sm, markup=settings.billing.markup),
            )
            await _sweep_wizard_sessions(sm)
            await _sweep_slack_event_dedup(sm)
            await _sweep_retired_turn_card_intents(sm)
            await _sweep_hub_oauth_kv(sm)
            await _sweep_mcp_tokens(sm)
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
            await _refresh_expiring_github_links(
                sm=sm,
                settings=settings,
                fernet=push_resync_fernet,
                throttle=link_refresh_throttle,
                backoff=link_refresh_backoff,
            )
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
            await _sweep_mcp_tokens(sm)
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
            await _refresh_expiring_github_links(
                sm=sm,
                settings=settings,
                fernet=push_resync_fernet,
                throttle=link_refresh_throttle,
                flight=link_refresh_flight,
                backoff=link_refresh_backoff,
            )

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
                interval_s=scheduler_settings.tick_interval_s,
                stop_event=stop_event,
            )

        return 0
    finally:
        await _shutdown_github_link_refresh(link_refresh_flight)
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


async def _shutdown_github_link_refresh(flight: _SingleFlight) -> None:
    """Stop the sweep and settle detached refreshes inside one shared budget.

    The deadline is taken once, here. The sweep's grace, the cancelled sweep's
    settle and the final drain all come out of it (see the budget comment at
    `_GITHUB_LINK_REFRESH_SHUTDOWN_BUDGET_S`). A refresh detached from the
    cancelled sweep must store its rotated tokens before the clients close and
    the engine is disposed.
    """
    deadline = time.monotonic() + _GITHUB_LINK_REFRESH_SHUTDOWN_BUDGET_S
    await flight.aclose(deadline=deadline)
    if await drain_detached_refreshes(max(0.0, deadline - time.monotonic())):
        log.warning("scheduler.github_link_refresh.drain_timed_out")


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


class _IntervalThrottle:
    """Let a job run at most once per interval within this process."""

    def __init__(self, interval_s: float) -> None:
        self._interval_s = interval_s
        self._next_at = 0.0

    def due(self) -> bool:
        now = time.monotonic()
        if now < self._next_at:
            return False
        self._next_at = now + self._interval_s
        return True


class _SingleFlight:
    """Run one background job at a time, so a slow sweep never delays a tick.

    `start` skips while the previous job is still running. `aclose` asks the job
    to stop through `stopping` and waits for it, so a job in the middle of
    something that must finish gets to; only past `grace_s` does it cancel.
    Given a `deadline` (a `time.monotonic` value), the grace is cut to what
    remains of it and the cancelled job's own cleanup reads the rest through
    `settle_s`.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._deadline: float | None = None
        self.stop_requested = asyncio.Event()

    def settle_s(self, default: float) -> float:
        """How long a cancelled job may keep clients open: the shutdown remainder, or `default`."""
        if self._deadline is None:
            return default
        return max(0.0, self._deadline - time.monotonic())

    def stopping(self) -> bool:
        return self.stop_requested.is_set()

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, job: Callable[[], Coroutine[object, object, None]]) -> bool:
        if self.running():
            return False
        self._task = asyncio.create_task(job())
        return True

    async def aclose(
        self,
        *,
        grace_s: float | None = None,
        deadline: float | None = None,
    ) -> None:
        if grace_s is None:
            grace_s = _GITHUB_LINK_REFRESH_SHUTDOWN_GRACE_S
        self.stop_requested.set()
        self._deadline = deadline
        task, self._task = self._task, None
        if task is None or task.done():
            return
        if deadline is not None:
            grace_s = min(grace_s, max(0.0, deadline - time.monotonic()))
        done, _ = await asyncio.wait({task}, timeout=grace_s)
        if done:
            return
        task.cancel()
        if deadline is None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
            return
        # The cancelled job's settle reads what remains of the deadline, but
        # its database cancel and client close are not capped by it; give them
        # a few seconds past the deadline, then leave the task to the loop's
        # own shutdown rather than hold the worker's stop.
        done, _ = await asyncio.wait(
            {task}, timeout=max(0.0, deadline - time.monotonic()) + _SHUTDOWN_CANCEL_SLACK_S
        )
        if not done:
            log.warning("scheduler.github_link_refresh.cancel_outlived_budget")


async def _run_github_link_refresh(
    *,
    sm: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    client_id: str,
    client_secret: str,
    backoff: dict[int, datetime],
    should_stop: Callable[[], bool] | None = None,
    settle_s: Callable[[], float] | None = None,
) -> None:
    """One sweep: it stops starting new people at its budget or on `should_stop`.

    `settle_s` says, when the sweep is cancelled, how long its HTTP client stays
    open for a refresh detached from it; the default is
    `_GITHUB_LINK_REFRESH_DRAIN_S`. Shutdown passes what remains of its budget.
    """
    try:
        async with (
            httpx.AsyncClient(timeout=20.0, follow_redirects=False) as http_client,
            # A cancel mid-refresh leaves the refresh running detached; it needs
            # this client until it stores GitHub's answer.
            settle_detached_refreshes(settle_s or _GITHUB_LINK_REFRESH_DRAIN_S),
        ):
            counts = await refresh_expiring_links(
                sm,
                http_client,
                fernet=fernet,
                client_id=client_id,
                client_secret=client_secret,
                now=datetime.now(UTC),
                backoff=backoff,
                budget_s=_GITHUB_LINK_REFRESH_DEADLINE_S,
                should_stop=should_stop,
            )
        if counts.refreshed or counts.broken or counts.failed or counts.not_attempted:
            log.info(
                "scheduler.github_link_refresh",
                refreshed=counts.refreshed,
                broken=counts.broken,
                failed=counts.failed,
                not_attempted=counts.not_attempted,
            )
    except Exception:
        log.exception("scheduler.github_link_refresh.failed")


async def _refresh_expiring_github_links(
    *,
    sm: async_sessionmaker[AsyncSession],
    settings: Settings,
    fernet: MultiFernet | None,
    throttle: _IntervalThrottle,
    flight: _SingleFlight | None = None,
    backoff: dict[int, datetime] | None = None,
) -> None:
    """Keep linked GitHub tokens from reaching refresh-token expiry; hourly, best effort.

    With a `flight` the sweep runs in the background, one at a time, so a slow
    GitHub never delays routine dispatch; without one (`--once`) it is awaited,
    still within its budget. `backoff` is the caller's per-person failure memory.
    """
    app = settings.github_app
    if fernet is None or app.client_id is None or app.client_secret is None:
        return
    if flight is not None and flight.running():
        # Asked before the throttle, so a skipped hour is not consumed.
        return
    if not throttle.due():
        return

    async def sweep() -> None:
        assert app.client_id is not None and app.client_secret is not None
        await _run_github_link_refresh(
            sm=sm,
            fernet=fernet,
            client_id=app.client_id,
            client_secret=app.client_secret.get_secret_value(),
            backoff=backoff if backoff is not None else {},
            should_stop=flight.stopping if flight is not None else None,
            settle_s=(
                functools.partial(flight.settle_s, _GITHUB_LINK_REFRESH_DRAIN_S)
                if flight is not None
                else None
            ),
        )

    if flight is None:
        await sweep()
    else:
        flight.start(sweep)


def run_sync() -> None:
    """Console-script entrypoint. ``daimon-scheduler`` resolves here."""
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    run_sync()
