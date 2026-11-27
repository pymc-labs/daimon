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
from daimon.core.authz import (
    build_agent_ref,
)
from daimon.core.bot_access import bot_access_blocked, bot_access_wait_on
from daimon.core.channel_audience import requester_standing
from daimon.core.config import Settings, get_repo_memory_settings
from daimon.core.defaults.metadata import BUILTIN_AGENT_NAME
from daimon.core.defaults.provisioning import reconcile_tenant_defaults
from daimon.core.defaults.report import ApplyReport
from daimon.core.direct_messages import DirectMessageAccessRevoked, DmRoutineReply, run_dm_routine
from daimon.core.discord_membership import open_discord_dm
from daimon.core.features.model_input import is_isolated
from daimon.core.features.snapshot import FeaturePlace, place_feature_or_none
from daimon.core.fire.admit import (
    AdmitReads,
    Admitted,
    Refused,
    Waiting,
    admit_place,
    checks_ran_agent,
    owner_dm_open,
    ran_agent_refusal,
)
from daimon.core.fire.failure import record_fire_error
from daimon.core.fire.store import FireGuardStore, FireResult, FireStatus, FireStore
from daimon.core.fire.subject import Subject
from daimon.core.github_credentials import build_multifernet
from daimon.core.headless_runner import run_turn
from daimon.core.lead_intake_execution import current_intake_execution
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import (
    ResolverCache,
    resolve_agent,
    resolve_environment,
    retrieve_agent_cached,
)
from daimon.core.mcp_identity import DmRoutineRelay, routine_destination_key
from daimon.core.membership import MembershipResolver
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import retrieve_agent
from daimon.core.observability import capture_exception_with_scope
from daimon.core.pricing import MODEL_PRICING
from daimon.core.provider_routine import (
    ProviderRoutineRun,
    admit_provider_routine,
    refusal_text,
    run_provider_routine,
)
from daimon.core.routine_delivery import (
    DirectPost,
    agent_delivered_itself,
    render_routine_controls,
    routine_knows,
    routine_links,
    split_run_text,
)
from daimon.core.routine_files import collect_routine_files, stage_routine_files
from daimon.core.routine_hold import delivery_hold_value
from daimon.core.routines import mounts_personal_memory
from daimon.core.rule_views import (
    routine_destination_channel,
)
from daimon.core.scope import DeploymentDefault, ScopeContext
from daimon.core.spend_gate import SpendBlock, SpendCheck, SpendRequest, spend_gate_in_new_session
from daimon.core.stores.domain import PlatformPrincipalRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.scoped_config_read import resolve
from daimon.core.turn.admission import Admission, DmSource
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.outcomes import TurnObservation, current_outcome
from daimon.core.turn.state import TurnState
from daimon.core.turn.termination import TerminationReason, termination_reason
from daimon.core.turn_hold import TurnHold
from daimon.core.usage_recording import record_turn_usage
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
    intake_bound: bool,
    settle: Callable[[FireStatus], Awaitable[None]] | None = None,
) -> FireStatus:
    """Run `turn` under the feature gates, the execution grant and the dispatch bound.

    It reads the `routines` feature first; `intake_bound` (bound as lead intake) reads
    `lead_intake` too. A failure is recorded through `store`; `settle` runs
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
                    subject.tenant_id,
                    "",
                    subject.channel_id,
                    agent_id=subject.agent_id,
                )
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
    retrieve_agent_cached: Callable[..., Awaitable[BetaManagedAgentsAgent]] = retrieve_agent_cached
    open_discord_dm: Callable[..., Awaitable[str]] = open_discord_dm
    admit_provider_routine: Callable[..., Awaitable[Admission]] = admit_provider_routine
    run_provider_routine: Callable[..., Awaitable[ProviderRoutineRun]] = run_provider_routine

    @property
    def reads(self) -> AdmitReads:
        """What place admission reads with, from this fire's runtime."""
        return AdmitReads(
            sm=self.sm,
            settings=self.settings,
            membership_resolver=self.membership_resolver,
            get_or_create_platform_principal=self.get_or_create_platform_principal,
            bot_access_wait_on=self.bot_access_wait_on,
            bot_access_blocked=self.bot_access_blocked,
            dm_open=self.dm_open,
            owner_granted=self.owner_granted,
        )


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


async def _has_daimon_tools(deps: FireDeps, subject: Subject, agent_id: str | None) -> bool:
    """Whether the agent that runs this fire has Daimon's tools; an isolated one has none.
    Unreadable: the fire's controls carry no hint about them."""
    if agent_id is None:
        return False
    try:
        agent = await deps.retrieve_agent_cached(
            deps.client, deps.resolver_cache, subject.tenant_id, agent_id
        )
    except Exception:
        log.warning("routine.agent_unreadable", routine_id=str(subject.id), exc_info=True)
        return False
    return not is_isolated(agent)


def _dm_routine_relay(
    deps: FireDeps, subject: Subject, owner: str
) -> tuple[DmRoutineRelay | None, Callable[[], Awaitable[str]] | None]:
    """What this fire binds into its turn's relay credential, and how to open the owner's
    DM, when a server signs DM routines: the routine, its owner, this fire's execution and
    its revision now. (None, None) otherwise: today's turn."""
    execution = current_intake_execution.get()
    discord = deps.settings.discord
    held = execution is not None and execution[0] == subject.id
    if (
        not deps.settings.mcp_identity.names_dm_routine
        or discord is None
        or execution is None
        or not held
        or subject.revision is None
    ):
        return None, None
    relay = DmRoutineRelay(
        routine_id=subject.id,
        owner=owner,
        execution=execution[1],
        revision=subject.revision,
    )
    token = discord.bot_token.get_secret_value()
    return relay, functools.partial(deps.open_discord_dm, token, owner)


async def _tell_lapsed_owner(
    deps: FireDeps, subject: Subject, store: FireStore, platform: str
) -> None:
    """DM the owner once, after the lapsed-role pause committed; a failure is logged."""
    owner = subject.created_by_user_id
    async with deps.sm() as s:
        if (
            owner is None
            or deps.send_dm is None
            or not await owner_dm_open(deps.reads, s, subject, platform)
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
            knows=await routine_knows(sm, settings, subject, "discord"),
            daimon_tools=await _has_daimon_tools(deps, subject, subject.agent_id),
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
        relay, open_dm = _dm_routine_relay(deps, subject, owner_user_id)
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
            relay=relay,
            open_dm=open_dm,
        )
    except (AdmissionDenied, DirectMessageAccessRevoked) as error:
        log.info(
            "routine.skipped.invoker_policy",
            routine_id=str(subject.id),
            tenant_id=str(subject.tenant_id),
            reason=error.reason,
        )
        async with sm() as s, s.begin():
            await store.refuse(
                s,
                error.reason,
                error=refusal_text(error) if isinstance(error, AdmissionDenied) else None,
                fence="destination",
            )
        return
    run_text = split_run_text(reply.text)
    payload = (
        await collect_routine_files(deps.client, session_id=reply.session_id, text=run_text.post)
        if reply.on_managed_agents
        else await stage_routine_files(
            sm, reply.outputs, tenant_id=subject.tenant_id, text=run_text.post
        )
    )
    await store.finish(
        FireResult(
            tail=run_text.record,
            payload=payload,
            delivery="pending",
            producing_agent_id=reply.producing_agent_id,
        )
    )


async def _record_refusal(
    deps: FireDeps, subject: Subject, store: FireStore, verdict: Refused | Waiting
) -> None:
    """Record what admission refused; a lapsed role pauses the routine and DMs its owner."""
    lapsed_on: str | None = None
    async with deps.sm() as s, s.begin():
        if isinstance(verdict, Waiting):
            # A wait on the platform or the bot's access clears by itself: no notice.
            await store.refuse(
                s, verdict.reason, error=verdict.reason, fault=verdict.fault, tell=False
            )
        elif verdict.lapsed:
            if await store.pause_for_lapsed_role(s, error=verdict.reason):
                lapsed_on = verdict.platform
        else:
            await store.refuse(
                s, verdict.reason, error=verdict.error, fault=verdict.fault, tell=verdict.tell
            )
    if lapsed_on is not None:
        await _tell_lapsed_owner(deps, subject, store, lapsed_on)


async def fire_turn(subject: Subject, store: FireStore, deps: FireDeps) -> None:
    """One fire's turn: place admission (`admit_place`), the spend checks, then the turn.

    ``run_turn`` owns the daimon-mcp vault attach (via ``ensure_mcp_vault``) for the
    owner's account admission resolved. On success, the result is recorded through
    ``store.finish``. Errors propagate to the guard (`fire`), which records them.
    """
    client, sm, settings = deps.client, deps.sm, deps.settings
    verdict = await admit_place(subject, deps.reads)
    if not isinstance(verdict, Admitted):
        await _record_refusal(deps, subject, store, verdict)
        return
    assert subject.created_by_user_id is not None  # admission refuses an ownerless fire
    platform, account_id, fire_hold = verdict.platform, verdict.account_id, verdict.hold

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
        await _fire_dm(
            deps, subject, store, subject.created_by_user_id, direct_post=verdict.direct_post
        )
        return
    if verdict.native is not None:
        await _fire_native(deps, subject, store, verdict)
        return

    # Bind routine context now; headless_runner calls the factory once
    # (session_id, model_id) are known after create_session.
    platform_user_id = subject.created_by_user_id

    def usage_record_factory(session_id: str, model_id: str) -> Callable[..., Awaitable[object]]:
        record = functools.partial(
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
        return record

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
    if checks_ran_agent(verdict):
        ran = await deps.retrieve_agent(
            client,
            resolved_agent_id,
            scope=resource_scope(tenant_id=str(subject.tenant_id)),
            records=sm,
        )
        ran_ref = build_agent_ref(ran.name, ran.metadata, subject.agent_name)
        if (refused := ran_agent_refusal(verdict, subject, ran_ref)) is not None:
            await _record_refusal(deps, subject, store, refused)
            return
    # Admission gate: the routine's channel budget, after the cap (run_one_tick),
    # balance and pin gates, then the creator's spend on the agent the fire resolved.
    healed = await _gated_and_healed(
        deps, subject, store, platform=platform, agent_id=resolved_agent_id
    )
    if healed is None:
        return
    subject = healed

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
                direct_post=verdict.direct_post,
                web=await routine_links(sm, settings, subject, platform),
                knows=await routine_knows(sm, settings, subject, platform),
                held=fire_hold is not None,
                daimon_tools=await _has_daimon_tools(deps, subject, resolved_agent_id),
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
        origin_place=verdict.origin_place,
        # Built-in sandbox tools only under an admin's opt-in for the routine or
        # its agent, and only while the run matches what that opt-in pinned.
        sandbox=await store.sandbox(settings, platform=platform),
        # The owner's personal memory, read-only, only for a routine whose
        # result goes to nobody else; decided again from the facts each fire.
        personal_memory=mounts_personal_memory(subject),
        feature_settings=settings,
        platform=platform,
        runs_as_user=subject.created_by_user_id,
        membership_resolver=deps.membership_resolver,
        routine_id=subject.id,
        # Where this run posts, fixed now: a later edit moves no claim its relay calls
        # sign (`daimon.core.mcp_identity.routine_destination_key`).
        routine_destination=routine_destination_key(subject),
        **held_fire,
    )

    await _record_run(
        sm,
        subject,
        store,
        result,
        final_state[0] if final_state else None,
        files=(
            lambda text, sid=output_sessions[-1]: collect_routine_files(
                client, session_id=sid, text=text
            )
        )
        if output_sessions
        else None,
        hold=fire_hold,
    )


async def _gated_and_healed(
    deps: FireDeps, subject: Subject, store: FireStore, *, platform: str, agent_id: str
) -> Subject | None:
    """The subject to run, its agent id healed to `agent_id`, or None when a gate refused.

    The channel budget is keyed on `subject.channel_id`, the channel the fire is billed
    to (the destination's parent, else where the routine was made). The creator's spend
    allowance and monthly limit come last, on the agent the fire resolved; a spent
    monthly limit is `cap_exceeded`, as it was before the limit joined them.
    """
    if await _refused_over_channel_budget(deps, subject, store, platform=platform):
        return None
    spent = await spend_block(
        deps.sm,
        deps.settings,
        subject,
        platform=platform,
        check="allowance_exhausted",
        agent_id=agent_id,
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
        async with deps.sm() as s, s.begin():
            told = await store.fail(s, error=spent, reason=reason)
        store.tell_owner(reason, channel_told=told)
        return None
    if agent_id == subject.agent_id:
        return subject
    if not await store.heal_agent_id(agent_id):
        async with deps.sm() as refused_s, refused_s.begin():
            # The owner's own edit raced this fire; the next one runs it: no notice.
            await store.refuse(refused_s, "routine_changed", tell=False)
        return None
    return replace(subject, agent_id=agent_id)


async def _record_run(
    sm: async_sessionmaker[AsyncSession],
    subject: Subject,
    store: FireStore,
    result: str,
    state: TurnState | None,
    *,
    files: Callable[[str], Awaitable[str]] | None,
    hold: TurnHold | None,
) -> None:
    """Record a finished fire and queue its delivery, on every provider alike.

    `files` turns the text to post into the outbox payload with the fire's files.
    """
    if subject.destination_kind is None:
        await store.finish(FireResult(tail=result))
        return
    # Fallback post: only when the agent did not deliver to the
    # destination itself. The chat adapter for `platform` posts it.
    posted = state is not None and await agent_delivered_itself(sm, state, subject)
    run_text = split_run_text(result)
    payload = run_text.post
    needs_delivery = not posted
    if files is not None:
        payload = await files("" if posted else run_text.post)
        # A messaging tool can have posted the text while leaving generated
        # outputs behind. Deliver those without repeating the final reply.
        needs_delivery = needs_delivery or bool(payload)
    await store.finish(
        FireResult(
            tail=run_text.record,
            payload=payload,
            # The hold this fire ran under travels with its result to delivery.
            hold=delivery_hold_value(hold),
            delivery="pending" if needs_delivery else "skipped",
            note=None if needs_delivery else "agent_posted",
        )
    )


async def _fire_native(
    deps: FireDeps, subject: Subject, store: FireStore, admitted: Admitted
) -> None:
    """Run a routine whose place is on OpenAI or Gemini as an unattended chat-path turn.

    The same gates, controls, quiet-run rule and delivery as a Managed Agents fire;
    admission resolves the agent through its own records and meters the turn.
    """
    assert subject.created_by_user_id is not None  # admission refused an ownerless fire
    sm, settings, owner = deps.sm, deps.settings, subject.created_by_user_id
    platform, hold = admitted.platform, admitted.hold
    turn_deps = deps.turn_deps()
    try:
        admission = await deps.admit_provider_routine(
            turn_deps,
            subject,
            platform=platform,
            owner=owner,
            subject=admitted.invoker,
            sandbox=await store.sandbox(settings, platform=platform),
            hold=hold,
            origin_place=admitted.origin_place,
        )
    except AdmissionDenied as error:
        log.info(
            "routine.skipped.admission",
            routine_id=str(subject.id),
            tenant_id=str(subject.tenant_id),
            reason=error.reason,
        )
        async with sm() as s, s.begin():
            await store.refuse(s, error.reason, error=refusal_text(error))
        return
    ran = admission.agent
    if checks_ran_agent(admitted) and (
        refused := ran_agent_refusal(
            admitted, subject, build_agent_ref(ran.name, ran.metadata, subject.agent_name)
        )
    ):
        await _record_refusal(deps, subject, store, refused)
        return
    healed = await _gated_and_healed(deps, subject, store, platform=platform, agent_id=ran.id)
    if healed is None:
        return
    subject = healed
    prompt = (
        render_routine_controls(
            subject,
            platform=platform,
            direct_post=admitted.direct_post,
            web=await routine_links(sm, settings, subject, platform),
            knows=await routine_knows(sm, settings, subject, platform),
            held=hold is not None,
            daimon_tools=await _has_daimon_tools(deps, subject, ran.id),
        )
        + "\n"
        + subject.trigger_message
        if subject.destination_kind is not None
        else subject.trigger_message
    )
    try:
        run = await deps.run_provider_routine(
            turn_deps,
            admission,
            routine_id=subject.id,
            tenant_id=subject.tenant_id,
            platform=platform,
            owner=owner,
            prompt=prompt,
        )
    except AdmissionDenied as error:
        # An unattended rule refused the turn before its first message.
        async with sm() as s, s.begin():
            await store.refuse(s, error.reason, error=refusal_text(error))
        return

    async def files(text: str) -> str:
        return await stage_routine_files(sm, run.outputs, tenant_id=subject.tenant_id, text=text)

    await _record_run(sm, subject, store, run.text, run.state, files=files, hold=hold)
