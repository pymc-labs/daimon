"""One fire: its guard (`fire`) and the turn it guards (`fire_turn`), over a store.

Extracted from the routine scheduler unchanged. Runtime clients come in `FireDeps`, so
the scheduler's own bindings (and the tests patching them) are the ones that run.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

import httpx
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.access_policy import TenantAccessPolicy, agent_invoker_roles
from daimon.core.authz import (
    Action,
    AgentRef,
    Place,
    Surface,
    authorize,
    build_agent_ref,
    build_subject,
)
from daimon.core.authz import Subject as Invoker
from daimon.core.bot_access import BOT_ACCESS_UNAVAILABLE, bot_access_blocked, bot_access_wait_on
from daimon.core.channel_audience import requester_standing
from daimon.core.channel_backend import (
    agent_native_provider,
    channel_provider,
    effective_thread_backend,
)
from daimon.core.config import Settings, get_repo_memory_settings
from daimon.core.defaults.metadata import BUILTIN_AGENT_NAME
from daimon.core.defaults.provisioning import reconcile_tenant_defaults
from daimon.core.defaults.report import ApplyReport
from daimon.core.direct_messages import DirectMessageAccessRevoked, DmRoutineReply, run_dm_routine
from daimon.core.features.snapshot import FeaturePlace, place_feature_or_none
from daimon.core.fire.failure import record_fire_error
from daimon.core.fire.store import FireGuardStore, FireResult, FireStatus, FireStore
from daimon.core.fire.subject import Subject
from daimon.core.github_credentials import build_multifernet
from daimon.core.headless_runner import run_turn
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import ResolverCache, resolve_agent, resolve_environment
from daimon.core.membership import MembershipResolver
from daimon.core.membership_subject import resolve_subject
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import retrieve_agent
from daimon.core.observability import capture_exception_with_scope
from daimon.core.permissions import any_agent_rules, any_own_readers
from daimon.core.pricing import MODEL_PRICING
from daimon.core.routine_delivery import (
    OWNER_LACKS_ROLE_ERROR,
    DirectPost,
    agent_posted_to,
    delivery_target,
    lapsed_role,
    placement_unknown_is_unsafe,
    render_routine_controls,
    routine_links,
)
from daimon.core.routine_files import collect_routine_files
from daimon.core.routine_hold import delivery_hold_value, routine_hold
from daimon.core.routines import mounts_personal_memory
from daimon.core.rule_views import (
    is_routine_parent_unknown,
    keeps_routine_inside,
    routine_destination_channel,
    routine_destination_place,
    routine_origin,
)
from daimon.core.scope import DeploymentDefault, ScopeContext
from daimon.core.spend_gate import SpendBlock, SpendCheck, SpendRequest, spend_gate_in_new_session
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import PlatformPrincipalRow, Role
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.scoped_config_read import resolve
from daimon.core.stores.tenants import get_tenant
from daimon.core.turn.admission import DmSource
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.outcomes import TurnObservation, current_outcome
from daimon.core.turn.state import TurnState
from daimon.core.turn.termination import TerminationReason, termination_reason
from daimon.core.usage_recording import record_turn_usage
from mux.contracts.config import DEFAULT_BACKEND
from mux.contracts.ids import Provider
from mux.profiles import PROVIDER_LABELS
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Dispatch:
    """What the dispatcher lends one fire: its semaphore, shared with every fire, and bound."""

    sm: async_sessionmaker[AsyncSession]
    semaphore: asyncio.Semaphore
    timeout_s: float
    capture: Callable[[BaseException], None] = capture_exception_with_scope


async def fire(
    subject: Subject,
    store: FireGuardStore,
    turn: Callable[[], Awaitable[None]],
    dispatch: Dispatch,
    *,
    routines_gate: bool,
    intake_bound: bool,
    settle: Callable[[FireStatus], Awaitable[None]] | None = None,
) -> FireStatus:
    """Run `turn` under the feature gates, the execution grant and the dispatch bound.

    `routines_gate` reads the `routines` feature first; `intake_bound` (bound as lead
    intake) reads `lead_intake` too. A failure is recorded through `store`; `settle` runs
    last, with the status, even when the fire is cancelled.
    """
    sm = dispatch.sm
    timeout_s = dispatch.timeout_s
    loop = asyncio.get_running_loop()
    observation = TurnObservation(
        sm, subject.tenant_id, "scheduler", origin="routine", agent_id=subject.agent_id
    )
    unreadable = False
    started = loop.time()
    outcome: FireStatus = "ok"
    skip_reason: str | None = None
    try:
        async with dispatch.semaphore:
            try:
                place = FeaturePlace(
                    subject.tenant_id, "", subject.channel_id, agent_id=subject.agent_id
                )
                if routines_gate:
                    routines = await place_feature_or_none(sm, None, place, "routines")
                    if routines is None or not routines.on:
                        # Off here, or unreadable: the schedule does not run this tick.
                        # Unreadable is not Off: the owner sees why, and the slot is given
                        # back so the next tick tries again, as a watch's poll does.
                        unreadable = routines is None
                        reason = "feature_read_failed" if unreadable else "routines_turned_off"
                        outcome, skip_reason = "skipped", reason
                        await record_fire_error(sm, subject, store, reason, None)
                        return "unreadable" if unreadable else outcome
                if intake_bound:
                    intake = await place_feature_or_none(sm, None, place, "lead_intake")
                    if intake is None or not intake.on:
                        unreadable = intake is None
                        reason = "feature_read_failed" if unreadable else "lead_intake_turned_off"
                        outcome, skip_reason = "skipped", reason
                        await record_fire_error(sm, subject, store, reason, None)
                        return "unreadable" if unreadable else outcome
                with observation.activate():
                    async with store.execution(timeout_s):
                        await asyncio.wait_for(turn(), timeout=timeout_s)
            except TimeoutError as err:
                outcome = "failed"
                observation.finish(error=err, reason=TerminationReason.CEILING)
                # Capture to Sentry, then keep the existing swallow —
                # sibling tasks continue and the result is still recorded.
                dispatch.capture(err)
                timeout = f"timeout: exceeded {timeout_s}s"
                await record_fire_error(sm, subject, store, timeout, TerminationReason.CEILING)
            except Exception as err:
                outcome = "failed"
                reason = termination_reason(err)
                observation.finish(error=err, reason=reason)
                dispatch.capture(err)
                await record_fire_error(
                    sm,
                    subject,
                    store,
                    "execution_failed"
                    if subject.destination_kind == "dm"
                    else f"{type(err).__name__}: {err}"[:500],
                    reason,
                    error_class=type(err).__name__,
                )
    except asyncio.CancelledError as err:
        outcome = "failed"
        observation.finish(error=err)
        # Includes cancellation while waiting for a dispatch slot.
        await record_fire_error(sm, subject, store, "scheduler_shutdown", None)
        raise
    finally:
        observation.finish(reason=TerminationReason.UNKNOWN, fallback=True)
        log.info(
            "routine.finished",
            tenant_id=str(subject.tenant_id),
            routine_id=str(subject.id),
            agent_id=subject.agent_id,
            outcome=outcome,
            skip_reason=skip_reason,
            duration_ms=int((loop.time() - started) * 1000),
        )
        if settle is not None:
            await settle("unreadable" if unreadable else outcome)
    return outcome


@dataclass(frozen=True)
class FireDeps:
    """The runtime one fire's turn runs with; the callables default to the real ones."""

    client: AsyncAnthropic
    sm: async_sessionmaker[AsyncSession]
    settings: Settings
    deployment_default: DeploymentDefault
    resolver_cache: ResolverCache
    membership_resolver: MembershipResolver | None
    #: The ordinary turn dependencies a DM fire runs with, built on first use.
    turn_deps: Callable[[], TurnDeps]
    send_dm: Callable[[str, str, str], Awaitable[None]] | None
    #: Whether a fire may DM its owner (session, settings, tenant_id=, platform=, owner=).
    dm_open: Callable[..., Awaitable[bool]]
    #: Whether the agent's DM grant admits the owner (sm, resolver, tenant=, agent_name=, owner=).
    owner_granted: Callable[..., Awaitable[bool]]
    run_turn: Callable[..., Awaitable[str]] = run_turn
    run_dm_routine: Callable[..., Awaitable[DmRoutineReply]] = run_dm_routine
    resolve_agent: Callable[..., Awaitable[str]] = resolve_agent
    resolve_environment: Callable[..., Awaitable[str]] = resolve_environment
    retrieve_agent: Callable[..., Awaitable[BetaManagedAgentsAgent]] = retrieve_agent
    reconcile_tenant_defaults: Callable[..., Awaitable[ApplyReport]] = reconcile_tenant_defaults
    get_or_create_platform_principal: Callable[..., Awaitable[PlatformPrincipalRow]] = (
        get_or_create_platform_principal
    )
    bot_access_wait_on: Callable[..., Awaitable[bool]] = bot_access_wait_on
    bot_access_blocked: Callable[..., Awaitable[bool]] = bot_access_blocked


async def spend_block(
    sm: async_sessionmaker[AsyncSession],
    settings: Settings,
    subject: Subject,
    *,
    platform: str,
    check: SpendCheck,
    agent_id: str | None = None,
) -> SpendBlock | None:
    """What one check of the spend gate stops this fire with, if anything.

    The allowance check answers `cap_exceeded` when the creator's monthly limit is what
    has no room.
    """
    return await spend_gate_in_new_session(
        sm,
        request=SpendRequest(
            tenant_id=subject.tenant_id,
            platform_user_id=subject.created_by_user_id,
            platform=platform,
            channel_id=subject.channel_id,
            agent_id=agent_id,
        ),
        markup=settings.billing.markup,
        checks=frozenset({check}),
    )


async def blocked(
    sm: async_sessionmaker[AsyncSession],
    settings: Settings,
    subject: Subject,
    *,
    platform: str,
    check: SpendCheck,
) -> bool:
    """Whether one check of the spend gate stops this fire."""
    return await spend_block(sm, settings, subject, platform=platform, check=check) is not None


#: A DM routine whose source is restricted: its result may not leave that channel.
RESTRICTED_SOURCE_ERROR = "restricted source cannot deliver by DM"


def routines_unsupported_error(provider: Provider) -> str:
    """The `last_error` a routine records when its agent, channel or thread is not on Anthropic."""
    if provider == "selfhosted":
        return "selfhosted_routines_unsupported"
    return (
        f"Routines run only on Anthropic, and this one is set to {PROVIDER_LABELS[provider]}. "
        "Switch its agent or channel back to Anthropic."
    )


async def _owner_dm_open(deps: FireDeps, s: AsyncSession, subject: Subject, platform: str) -> bool:
    """Whether a fire may DM its owner: Discord, routine DMs on, owner allowed."""
    owner = subject.created_by_user_id
    return owner is not None and await deps.dm_open(
        s, deps.settings, tenant_id=subject.tenant_id, platform=platform, owner=owner
    )


async def _tell_lapsed_owner(
    deps: FireDeps, subject: Subject, store: FireStore, platform: str
) -> None:
    """DM the owner once, after the lapsed-role pause committed; a failure is logged."""
    owner = subject.created_by_user_id
    async with deps.sm() as s:
        if (
            owner is None
            or deps.send_dm is None
            or not await _owner_dm_open(deps, s, subject, platform)
        ):
            return
    try:
        await deps.send_dm(owner, *await store.lapsed_role_card())
    except (httpx.HTTPError, ValueError) as exc:
        # The routine stays paused; the panel shows why.
        log.warning(
            "routine.lapsed_role_dm_failed", routine_id=str(subject.id), error=type(exc).__name__
        )


async def _refused_over_channel_budget(
    deps: FireDeps, subject: Subject, store: FireStore, *, platform: str
) -> bool:
    """Record and refuse a fire over its channel budget. Keyed on `subject.channel_id`,
    the channel the fire is billed to (the destination's parent, else where the
    routine was made)."""
    sm, settings = deps.sm, deps.settings
    if not await blocked(sm, settings, subject, platform=platform, check="budget_exceeded"):
        return False
    if (observation := current_outcome.get()) is not None:
        observation.finish(reason=TerminationReason.ADMISSION_CHANNEL_BUDGET_EXCEEDED)
    log.info(
        "routine.skipped.over_channel_budget",
        routine_id=str(subject.id),
        tenant_id=str(subject.tenant_id),
        channel_id=subject.channel_id,
    )
    reason = TerminationReason.ADMISSION_CHANNEL_BUDGET_EXCEEDED
    async with sm() as s, s.begin():
        told = await store.fail(s, error="channel_budget_exceeded", reason=reason)
    store.tell_owner(reason, channel_told=told)
    return True


async def _fire_dm(
    deps: FireDeps,
    subject: Subject,
    store: FireStore,
    owner_user_id: str,
    *,
    direct_post: DirectPost,
) -> None:
    """Run an explicit owner-DM routine as a fresh private DM turn of its owner.

    Same admission, session, memory and tools as a fresh live DM of the
    owner with the routine's agent, unattended; the final text is queued
    for the owner's DM only. Admission rechecks the owner's live
    membership, the cold-DM agent rules and the creation source's seal.
    """
    sm, settings = deps.sm, deps.settings
    if not subject.source_provenance_known or subject.destination_id != owner_user_id:
        async with sm.begin() as session:
            await store.refuse(session, "source_provenance_unknown", fence="private")
        return
    if await _refused_over_channel_budget(deps, subject, store, platform="discord"):
        return
    prompt = (
        render_routine_controls(
            subject,
            platform="discord",
            direct_post=direct_post,
            web=await routine_links(sm, settings, subject, "discord"),
        )
        + "\n"
        + subject.trigger_message
    )
    # Source-free stays source-free; a known source is rechecked for a seal.
    source = (
        DmSource(channel_id=subject.source_channel_id, thread_id=subject.source_thread_id)
        if subject.source_channel_id is not None
        else None
    )
    turn_deps = deps.turn_deps()
    try:
        # The owner's standing at this fire: an owner who lost the internal role
        # starts no private turn (member mirror, then one fresh member fetch).
        async with sm() as standing_session:
            standing = await requester_standing(
                standing_session,
                tenant_id=subject.tenant_id,
                user_id=owner_user_id,
                is_dm=True,
                resolver=turn_deps.membership_resolver,
            )
        reply = await deps.run_dm_routine(
            turn_deps,
            requester_org=standing,
            tenant_id=subject.tenant_id,
            owner_user_id=owner_user_id,
            agent_name=subject.agent_name,
            prompt=prompt,
            source=source,
            budget_channel_id=subject.channel_id,
            # Built-ins only under an operator's opt-in for the agent: a DM
            # routine takes no On of its own.
            sandbox=await store.sandbox(settings, platform="discord"),
        )
    except (AdmissionDenied, DirectMessageAccessRevoked) as error:
        log.info(
            "routine.skipped.invoker_policy",
            routine_id=str(subject.id),
            tenant_id=str(subject.tenant_id),
            reason=error.reason,
        )
        async with sm() as s, s.begin():
            await store.refuse(s, error.reason, fence="destination")
        return
    payload = await collect_routine_files(deps.client, session_id=reply.session_id, text=reply.text)
    await store.finish(
        FireResult(
            tail=reply.text,
            payload=payload,
            delivery="pending",
            producing_agent_id=reply.producing_agent_id,
        )
    )


async def _owner_granted(deps: FireDeps, subject: Subject, owner: str) -> bool:
    """The agent's DM grant admits the owner, by their current server roles.

    Read before the firing transaction, so the role lookup holds no connection.
    """
    async with deps.sm() as s:
        tenant = await get_tenant(s, subject.tenant_id)
    return tenant is not None and await deps.owner_granted(
        deps.sm,
        deps.membership_resolver,
        tenant=tenant,
        agent_name=subject.agent_name,
        owner=owner,
    )


async def _bot_locked_out(deps: FireDeps, subject: Subject) -> bool:
    """Whether the workspace waits for the bot's access and Discord says it has none here.

    Read before the firing transaction, so the REST call holds no connection.
    """
    target = delivery_target(subject, platform="discord")
    if target is None or not target.channel_id.isdigit():
        return False
    async with deps.sm() as s:
        tenant = await get_tenant(s, subject.tenant_id)
    if tenant is None or tenant.platform != "discord":
        return False
    if not await deps.bot_access_wait_on(deps.sm, tenant_id=subject.tenant_id, platform="discord"):
        return False
    async with httpx.AsyncClient(timeout=10) as http:
        return await deps.bot_access_blocked(
            http, deps.settings, guild_id=tenant.external_id, destination_id=target.channel_id
        )


async def fire_turn(subject: Subject, store: FireStore, deps: FireDeps) -> None:
    """One fire's turn: the owner, tenant, provider, policy and spend checks, then the turn.

    1. Resolve ``(platform, created_by_user_id) -> account_id`` via
       ``get_or_create_platform_principal``. With no owner, refuse: there is no
       account to bind the daimon-mcp vault to.
    2. Drive ``run_turn`` — the headless single-turn drain. ``run_turn``
       owns the daimon-mcp vault attach (via ``ensure_mcp_vault``) when
       ``mcp_settings`` and ``account_id`` are both supplied.
    3. On success, record the result through ``store.finish``.

    Errors propagate to the guard (`fire`), which records them.
    """
    client, sm, settings = deps.client, deps.sm, deps.settings
    membership_resolver = deps.membership_resolver
    if subject.created_by_user_id is None:
        async with sm() as s, s.begin():
            await store.refuse(s, "no_owner", error="This routine has no owner. Create it again.")
        return
    granted = subject.destination_kind != "dm" or await _owner_granted(
        deps, subject, subject.created_by_user_id
    )
    bot_locked_out = await _bot_locked_out(deps, subject)

    async with sm() as s, s.begin():
        tenant = await get_tenant(s, subject.tenant_id)
        if tenant is None:
            await store.refuse(
                s, "tenant_not_found", error="The routine's team was not found", fault=True
            )
            return
        if tenant.archived_at is not None:
            await store.refuse(s, "tenant_archived", error="Daimon was removed from here.")
            return
        platform = tenant.platform
        if subject.destination_kind == "dm" and not (
            granted and await _owner_dm_open(deps, s, subject, platform)
        ):
            await store.refuse(
                s,
                "routine_dms_closed" if granted else "owner_not_allowed",
                error="routine DMs are disabled or the owner is not allowed",
            )
            return
        # Headless routines run only on Anthropic Managed Agents. A thread, channel
        # or agent on another provider is refused before anything is resolved at
        # Anthropic, never quietly run on Claude.
        target = delivery_target(subject, platform=platform)
        if is_routine_parent_unknown(subject):
            await store.refuse(s, "routine_destination_parent_unknown", fault=True)
            return
        parent_channel = routine_destination_channel(subject)
        provider: Provider | None = None
        if subject.destination_kind == "thread" and parent_channel is not None:
            thread_id = (
                target.thread_ts
                if platform == "slack" and target is not None
                else subject.destination_id
            )
            provider = await effective_thread_backend(
                s,
                tenant_id=subject.tenant_id,
                platform=platform,
                channel_id=parent_channel,
                thread_id=thread_id,
                caller_id=subject.created_by_user_id,
            )
        channels = {
            channel
            for channel in (
                subject.channel_id,
                routine_destination_channel(subject),
                target.channel_id if target is not None else None,
            )
            if channel is not None
        }
        for routine_channel in channels:
            if provider in (None, DEFAULT_BACKEND):
                provider = await channel_provider(
                    s, tenant_id=subject.tenant_id, platform=platform, channel_id=routine_channel
                )
        if provider in (None, DEFAULT_BACKEND):
            provider = await agent_native_provider(
                s,
                tenant_id=subject.tenant_id,
                agent_id=subject.agent_id,
                agent_name=subject.agent_name,
            )
        if provider is not None and provider != DEFAULT_BACKEND:
            await store.refuse(
                s, "routines_unsupported", error=routines_unsupported_error(provider)
            )
            return
        principal = await deps.get_or_create_platform_principal(
            s,
            tenant_id=subject.tenant_id,
            # Source the platform from the routine's tenant so slack-created
            # routines resolve a slack principal (not a mismatched discord one).
            platform=tenant.platform,
            external_id=subject.created_by_user_id,
        )
        account_id = principal.account_id

        # Invoker policy at fire time: someone taken off the allowlist
        # stops running headless turns through routines they created. The
        # creator facts come from live Discord membership, never a saved
        # Discord admin role. Unreadable policy fails closed.
        direct_post: DirectPost = "allowed"
        # The owner as freshly resolved this fire; checked again against the
        # resolved agent's names below. Unresolved stays an unknown subject.
        fire_subject = Invoker()
        paused = False
        fire_policy: TenantAccessPolicy | None = None
        fire_channel_id: str | None = None
        try:
            policy = await load_access_policy(s, tenant_id=subject.tenant_id)
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
            target = delivery_target(subject, platform=platform)
            fire_policy = policy
            fire_channel_id = target.channel_id if target is not None else None
            if target is not None:
                if not authorize(
                    policy,
                    subject=Invoker(),
                    action=Action.POST,
                    surface=Surface.ROUTINE,
                    place=Place(channel_id=target.channel_id),
                ):
                    direct_post = "protected"
                elif placement_unknown_is_unsafe(
                    policy, platform=platform, kind=subject.destination_kind
                ):
                    direct_post = "unverified"
            account = await get_account(s, account_id) if platform != "discord" else None
            is_admin = account is not None and account.role is Role.ADMIN and platform != "discord"
            invoker = build_subject(is_admin=is_admin, platform_user_id=subject.created_by_user_id)
            if platform == "discord" and (
                policy.invoker_role_ids
                or membership_resolver is not None
                or subject.destination_kind == "dm"
            ):
                invoker = await resolve_subject(
                    sm,
                    membership_resolver,
                    tenant_id=subject.tenant_id,
                    user_id=subject.created_by_user_id,
                    require_fresh=True,
                    session=s,
                )
            allowed = authorize(
                policy,
                subject=invoker,
                action=Action.ACT_FOR_CREATOR,
                surface=Surface.ROUTINE,
            )
            membership_required = platform == "discord" and (
                bool(policy.invoker_role_ids)
                or membership_resolver is not None
                or subject.destination_kind == "dm"
            )
            fire_subject = invoker
            policy_error = (
                None
                if allowed and (not membership_required or invoker.verified_membership is True)
                else "invoker_not_allowed"
            )
            if (
                policy_error is not None
                and membership_required
                and invoker.verified_membership is None
                and agent_invoker_roles(policy, (subject.agent_name,))
            ):
                # The platform could not say: retry next fire, tell nobody.
                policy_error = "membership_unavailable"
            if subject.destination_kind == "dm" and keeps_routine_inside(policy, subject):
                policy_error = RESTRICTED_SOURCE_ERROR
            # A pinned agent fires only when it posts straight into one of
            # its pinned channels; `_check_agent_pin` refuses anything else
            # at save time, and this holds for a pin added since. Isolation
            # needs every name, so it waits for the resolved agent below.
            if policy_error is None:
                initial = authorize(
                    policy,
                    subject=invoker,
                    action=Action.RUN_AGENT,
                    surface=Surface.ROUTINE,
                    agent=AgentRef.of(subject.agent_name),
                    place=Place(channel_id=target.channel_id if target is not None else None),
                )
                if initial.reason in ("runs_elsewhere", "invoker_not_allowed"):
                    policy_error = (
                        OWNER_LACKS_ROLE_ERROR
                        if initial.reason == "invoker_not_allowed"
                        and lapsed_role(policy, (subject.agent_name,), invoker)
                        else initial.reason
                    )
            # The bot's own access comes after every owner check, so a revoked owner
            # still stops the routine. Waiting costs no turn and tells nobody.
            if policy_error is None and bot_locked_out:
                policy_error = BOT_ACCESS_UNAVAILABLE
        if policy_error is not None:
            log.info(
                "routine.skipped.invoker_policy",
                routine_id=str(subject.id),
                tenant_id=str(subject.tenant_id),
                reason=policy_error,
            )
            if policy_error == OWNER_LACKS_ROLE_ERROR:
                paused = await store.pause_for_lapsed_role(s, error=policy_error)
            else:
                # A wait on the platform or the bot's access clears by itself: no notice.
                waits = policy_error in ("membership_unavailable", BOT_ACCESS_UNAVAILABLE)
                await store.refuse(
                    s,
                    "restricted_source"
                    if policy_error == RESTRICTED_SOURCE_ERROR
                    else policy_error,
                    error=policy_error,
                    fault=policy_error == "membership_unavailable",
                    tell=not waits,
                )
    if policy_error is not None:
        if paused:
            await _tell_lapsed_owner(deps, subject, store, platform)
        return

    # A destination in a channel shared with another organisation is read by
    # people from it, so the fire runs held to that channel (its own memory,
    # the held credential). It never runs unheld.
    async with sm() as hold_s:
        fire_hold = await routine_hold(hold_s, subject, platform=platform)

    # Admission gate: per-tenant balance — independent of Stripe config.
    if await blocked(sm, settings, subject, platform=platform, check="balance_depleted"):
        if (observation := current_outcome.get()) is not None:
            observation.finish(reason=TerminationReason.ADMISSION_BALANCE_DEPLETED)
        log.info(
            "routine.skipped.over_balance",
            routine_id=str(subject.id),
            tenant_id=str(subject.tenant_id),
        )
        reason = TerminationReason.ADMISSION_BALANCE_DEPLETED
        async with sm() as s, s.begin():
            told = await store.fail(s, error="balance_depleted", reason=reason)
        store.tell_owner(reason, channel_told=told)
        return

    if subject.destination_kind == "dm":
        await _fire_dm(deps, subject, store, subject.created_by_user_id, direct_post=direct_post)
        return

    # Bind routine context now; headless_runner calls the factory once
    # (session_id, model_id) are known after create_session.
    platform_user_id = subject.created_by_user_id

    def usage_record_factory(session_id: str, model_id: str) -> Callable[..., Awaitable[object]]:
        return functools.partial(
            record_turn_usage,
            sessionmaker=sm,
            platform_user_id=platform_user_id,
            managed_session_id=session_id,
            model_id=model_id,
            tenant_id=subject.tenant_id,
            markup=settings.billing.markup,
            pricing=MODEL_PRICING.get(model_id),
            channel_id=subject.channel_id,
            agent_id=resolved_agent_id,
        )

    # Resolve agent + environment by daimon-tag at fire time,
    # self-healing across MA archive/recreate. The environment follows the
    # routine's channel, then the tenant default, like a turn there would.
    # defensive: post-0012 agent_name is NOT NULL but the fallback is harmless and free.
    agent_tag = subject.agent_name or deps.deployment_default.agent_name or BUILTIN_AGENT_NAME
    _public_url = str(settings.mcp.public_url) if settings.mcp.public_url else None
    resolved_agent_id = await deps.resolve_agent(
        client,
        tenant_id=subject.tenant_id,
        daimon_tag=agent_tag,
        cached_id=subject.agent_id,
        apply_callable=lambda: deps.reconcile_tenant_defaults(
            client, sm, settings.defaults_root, tenant_id=subject.tenant_id, public_url=_public_url
        ),
        cache=deps.resolver_cache,
    )
    async with sm() as scope_s:
        scoped = await resolve(
            scope_s,
            context=ScopeContext(
                tenant_id=subject.tenant_id,
                channel_id=routine_destination_channel(subject) or subject.channel_id,
            ),
            default=deps.deployment_default,
        )
    resolved_env_id = await deps.resolve_environment(
        client,
        tenant_id=subject.tenant_id,
        daimon_tag=scoped.environment_name or "default",
        cached_id=None,
        apply_callable=lambda: deps.reconcile_tenant_defaults(
            client, sm, settings.defaults_root, tenant_id=subject.tenant_id, public_url=_public_url
        ),
        cache=deps.resolver_cache,
    )
    # The pin and channel isolation are checked again on the agent that
    # will actually run, by every name a pin can be keyed by, after
    # self-healing may have picked a replacement: the saved routine name
    # alone can miss a pin on the display name or a rename.
    if fire_policy is not None and (any_agent_rules(fire_policy) or any_own_readers(fire_policy)):
        ran = await deps.retrieve_agent(
            client,
            resolved_agent_id,
            scope=resource_scope(tenant_id=str(subject.tenant_id)),
            records=sm,
        )
        ran_ref = build_agent_ref(ran.name, ran.metadata, subject.agent_name)
        decision = authorize(
            fire_policy,
            subject=fire_subject,
            action=Action.RUN_AGENT,
            surface=Surface.ROUTINE,
            agent=ran_ref,
            # A thread destination sits under its saved parent channel; one
            # saved without it can't be placed here and fails closed while
            # anything is isolated.
            place=routine_destination_place(subject, channel_id=fire_channel_id),
        )
        if decision.reason is not None:
            refusal = (
                OWNER_LACKS_ROLE_ERROR
                if decision.reason == "invoker_not_allowed"
                and lapsed_role(fire_policy, ran_ref.names, fire_subject)
                else decision.reason
            )
            log.info(
                "routine.skipped.invoker_policy",
                routine_id=str(subject.id),
                tenant_id=str(subject.tenant_id),
                reason=refusal,
            )
            async with sm() as pin_s, pin_s.begin():
                if refusal == OWNER_LACKS_ROLE_ERROR:
                    paused = await store.pause_for_lapsed_role(pin_s, error=refusal)
                else:
                    await store.refuse(pin_s, refusal)
            if paused:
                await _tell_lapsed_owner(deps, subject, store, platform)
            return
    # Admission gate: the routine's channel budget, after the cap (run_one_tick),
    # balance and pin gates. Keyed on `subject.channel_id`, the channel the fire is
    # billed to (the destination's parent, else where the routine was made).
    if await _refused_over_channel_budget(deps, subject, store, platform=platform):
        return
    # The creator's spend allowance and monthly limit, last, on the agent the fire
    # resolved. A spent monthly limit is `cap_exceeded`, as it was before the limit
    # joined the allowances.
    spent = await spend_block(
        sm,
        settings,
        subject,
        platform=platform,
        check="allowance_exhausted",
        agent_id=resolved_agent_id,
    )
    if spent is not None:
        capped = spent == "cap_exceeded"
        reason = (
            TerminationReason.ADMISSION_CAP_EXCEEDED
            if capped
            else TerminationReason.ADMISSION_DENIED
        )
        if (observation := current_outcome.get()) is not None:
            observation.finish(reason=reason)
        log.info(
            "routine.skipped.cap_exceeded" if capped else "routine.skipped.allowance_exhausted",
            routine_id=str(subject.id),
            tenant_id=str(subject.tenant_id),
        )
        async with sm() as s, s.begin():
            told = await store.fail(s, error=spent, reason=reason)
        store.tell_owner(reason, channel_told=told)
        return
    if resolved_agent_id != subject.agent_id:
        if not await store.heal_agent_id(resolved_agent_id):
            async with sm() as refused_s, refused_s.begin():
                # The owner's own edit raced this fire; the next one runs it: no notice.
                await store.refuse(refused_s, "routine_changed", tell=False)
            return
        subject = replace(subject, agent_id=resolved_agent_id)

    # Mount the agent's assembled .env on the headless turn: derive the
    # tenant-scoped agent UUID and pass tenant/agent/session_factory so
    # run_turn uploads + mounts the credential file.
    agent_uuid = derive_agent_uuid(tenant_id=subject.tenant_id, ma_agent_id=resolved_agent_id)

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
    trigger_message = subject.trigger_message
    if subject.destination_kind is not None:
        trigger_message = (
            render_routine_controls(
                subject,
                platform=platform,
                direct_post=direct_post,
                web=await routine_links(sm, settings, subject, platform),
                held=fire_hold is not None,
            )
            + "\n"
            + subject.trigger_message
        )
    final_state: list[TurnState] = []
    output_sessions: list[str] = []
    # Passed only when held, so an ordinary fire's call stays what it was.
    held_fire: dict[str, Any] = {} if fire_hold is None else {"hold": fire_hold}

    result = await deps.run_turn(
        anthropic=client,
        agent_id=resolved_agent_id,
        environment_id=resolved_env_id,
        trigger_message=trigger_message,
        on_state=final_state.append,
        on_session=output_sessions.append,
        mcp_settings=settings.mcp,
        repo_memory=get_repo_memory_settings(settings),
        account_id=account_id,
        usage_record_factory=usage_record_factory,
        tenant_id=subject.tenant_id,
        agent_uuid=agent_uuid,
        session_factory=sm,
        fernet=fernet,
        github_fallback_pat=github_fallback_pat,
        github_app_id=github_app_id,
        github_app_private_key=github_app_private_key,
        agent_github_app=settings.github_app,
        github_mcp_router=settings.github.mcp_router,
        tool_safety=settings.tool_safety,
        budget_channel_id=subject.channel_id,
        # Stamped like a turn in the destination, so an isolated or sealed
        # channel's routine transcript reads only from inside it.
        origin_place=(
            routine_origin(fire_policy, subject, platform=platform)
            if fire_policy is not None
            else None
        ),
        # Built-in sandbox tools only under an admin's opt-in for the routine or
        # its agent, and only while the run matches what that opt-in pinned.
        sandbox=await store.sandbox(settings, platform=platform),
        # The owner's personal memory, read-only, only for a routine whose
        # result goes to nobody else; decided again from the facts each fire.
        personal_memory=mounts_personal_memory(subject),
        feature_settings=settings,
        platform=platform,
        runs_as_user=subject.created_by_user_id,
        membership_resolver=membership_resolver,
        routine_id=subject.id,
        **held_fire,
    )

    if subject.destination_kind is None:
        await store.finish(FireResult(tail=result))
        return

    # Fallback post: only when the agent did not deliver to the
    # destination itself. The chat adapter for `platform` posts it.
    posted = bool(final_state) and agent_posted_to(final_state[0], subject)
    payload = result
    needs_delivery = not posted
    if output_sessions:
        payload = await collect_routine_files(
            client, session_id=output_sessions[-1], text="" if posted else result
        )
        # A messaging tool can have posted the text while leaving generated
        # outputs behind. Deliver those without repeating the final reply.
        needs_delivery = needs_delivery or bool(payload)
    await store.finish(
        FireResult(
            tail=result,
            payload=payload,
            # The hold this fire ran under travels with its result to delivery.
            hold=delivery_hold_value(fire_hold),
            delivery="pending" if needs_delivery else "skipped",
            note=None if needs_delivery else "agent_posted",
        )
    )
