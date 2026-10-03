"""Stage one of the two-stage turn chokepoint: `admit()`.

A single core call performs identity resolution, the channel -> tenant ->
deployment config cascade, MA resolve + SDK retrieve, the per-tenant balance
gate, the per-user monthly cap gate and the channel budget gate -- returning a
frozen `Admission` or raising a typed error. No boolean gate result crosses this boundary.

Gate ORDER is load-bearing and must not be reordered: identity -> channel
protection -> invoker policy -> external participant -> cascade -> external in
setup -> missing-config -> resolve/retrieve -> agent pin -> channel isolation ->
balance -> cap -> channel budget. A tenant that is both over-balance and
mis-configured must see the config error (matches both adapters' inline
sequences today). The access policy gates run before the cascade so a refused turn learns nothing
about the tenant's configuration and never reaches an MA call.

Ported verbatim from `bot.py`'s inline pre-turn sequence (the reference
implementation). Both the Discord and Slack adapters now call `admit()` as
their pre-turn gate.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Literal

import structlog
from anthropic import APIStatusError
from anthropic.types.beta import (
    BetaEnvironment,
    BetaManagedAgentsAgent,
    BetaManagedAgentsCustomSkill,
)
from daimon.core.access_policy import (
    TenantAccessPolicy,
)
from daimon.core.authz import (
    Action,
    AgentRef,
    Place,
    Subject,
    Surface,
    authorize,
    build_agent_ref,
    build_subject,
    build_turn_place,
)
from daimon.core.billing import is_over_cap
from daimon.core.channel_admins import ChannelAdminCaller, load_administered_channel_ids
from daimon.core.channel_budget import is_over_channel_budget
from daimon.core.channel_budget_notice import spawn_budget_notice
from daimon.core.channel_skills import turn_channel_skills
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.defaults.provisioning import reconcile_tenant_defaults
from daimon.core.ma_resolver import MAResolverMissError, resolve_agent, resolve_environment
from daimon.core.named_agent import matching_agent
from daimon.core.permissions import (
    agent_permissions,
    at_home,
    channel_permissions,
    confidential_channel_of,
    dm_source_sealed,
    seal_ids_at,
)
from daimon.core.scope import ResolvedConfig, ScopeContext
from daimon.core.setup_conversations import get_setup_agent, get_setup_responder
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.accounts import (
    get_external,
    set_external,
    set_platform_role_ids,
    set_role,
)
from daimon.core.stores.domain import Role
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.scoped_config_read import resolve as resolve_config
from daimon.core.stores.scoped_config_write import clear_agent_references
from daimon.core.tenant_balance import is_over_balance
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import (
    AdmissionDenied,
    DmSourceSealedError,
    MissingTurnConfigError,
    NamedAgentRefused,
    SessionBusyError,
)
from daimon.core.turn.outcomes import TurnObservation, current_outcome

__all__ = [
    "Admission",
    "AdmissionDenied",
    "AdmissionGrant",
    "DmSource",
    "MissingTurnConfigError",
    "admit",
    "reauthorize",
]

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DmSource:
    """Where a DM conversation was started from, re-checked by `reauthorize`."""

    channel_id: str | None
    thread_id: str | None
    thread_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExternalFinding:
    """An adapter's live finding on whether the caller is from another organisation.

    `is_external` is how this turn treats them. `is_known` says it rests on
    positive evidence (a home tenant id, ours or another's, or a verified 1:1
    chat); only such a finding is stored. One without it, such as a fail-closed
    guess, applies to this turn alone.
    """

    is_external: bool
    is_known: bool


@dataclass(frozen=True)
class AdmissionGrant:
    """The facts an admission was decided on, kept so it can be decided again.

    `admit()` decides at admission time; the session is built, reused,
    replaced or recovered later -- after a compatibility check, a workspace
    transfer, or a dead-session recovery minutes on. `reauthorize` re-reads
    the policy and asks `authorize` the same questions at that moment, so a
    pin, seal, protection or invoker change made in between applies to the
    turn that is about to run (the model's decide-then-do race).
    """

    tenant_id: uuid.UUID
    subject: Subject
    surface: Surface
    turn_place: Place
    agent: AgentRef
    run_place: Place
    channel_id: str
    thread_id: str | None
    is_dm: bool
    # Set by the DM path: the conversation's source, whose seal is re-checked.
    dm_source: DmSource | None = None
    # The caller is from another organisation: answered only in an isolated channel.
    is_external: bool = False


@dataclass(frozen=True)
class Admission:
    """Everything `run_prepared_turn` needs to start a turn, resolved once."""

    account_id: uuid.UUID
    agent: BetaManagedAgentsAgent
    environment: BetaEnvironment
    config: ResolvedConfig
    # Turn from a sealed channel (or a thread under one), or from a DM when the
    # tenant asks for it: memory mounts must be read-only.
    memory_read_only: bool = False
    # The channel or thread itself is sealed (DM memory policy aside). Callers
    # that copy content out of the channel, such as /dm, must refuse.
    source_sealed: bool = False
    # Only private Slack orchestration assigns this signed, execution-specific grant.
    slack_turn_context_id: uuid.UUID | None = None
    # The caller is from another organisation (`admit`'s `is_external`, or the stored
    # flag when the adapter could not tell): never an admin, whatever its role.
    is_external: bool = False
    private_dm_id: str | None = None
    # The channel and thread the turn runs in, and every sealed id that seals
    # them (the channel and a thread sealed on its own): stamped on the session
    # so the transcript tools can apply the seal to it
    # (`daimon.core.session_seal.origin_stamp`).
    origin_channel_id: str | None = None
    origin_thread_id: str | None = None
    origin_seal_ids: frozenset[str] = frozenset()
    # Parent channel the turn's spend is attributed to; in a DM, the channel it
    # was moved from, or None. Apart from `origin_channel_id`, which drives the
    # seal: a DM is budgeted to its source channel but never runs there.
    budget_channel_id: str | None = None
    # The channel's extra skills (`daimon.core.channel_skills`), decided once
    # so creating the session and checking it for drift add the same ones.
    channel_skills: tuple[BetaManagedAgentsCustomSkill, ...] = ()
    # What admission was decided on; `reauthorize` decides it again at the
    # moment the session is built. None only for hand-built test admissions.
    grant: AdmissionGrant | None = field(default=None, compare=False, repr=False)
    observation: TurnObservation | None = field(default=None, compare=False, repr=False)


async def admit(
    deps: TurnDeps,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    external_user_id: str,
    channel_id: str,
    now: datetime,
    thread_id: str | None = None,
    role: Role | None = None,
    platform_role_ids: Sequence[str] | None = None,
    is_dm: bool = False,
    dm_source_channel_id: str | None = None,
    category_id: str | None = None,
    category_unresolved: bool = False,
    external: ExternalFinding | None = None,
    requested_agent_name: str | None = None,
    requested_agent_id: str | None = None,
) -> Admission:
    observation = current_outcome.get() or TurnObservation(
        deps.sessionmaker, tenant_id, platform, channel_id, thread_id
    )
    observation.channel_id = channel_id
    observation.thread_id = thread_id
    try:
        with observation.activate():
            result = await admit_impl(
                deps,
                tenant_id=tenant_id,
                platform=platform,
                external_user_id=external_user_id,
                channel_id=channel_id,
                now=now,
                thread_id=thread_id,
                role=role,
                platform_role_ids=platform_role_ids,
                is_dm=is_dm,
                dm_source_channel_id=dm_source_channel_id,
                category_id=category_id,
                category_unresolved=category_unresolved,
                external=external,
                requested_agent_name=requested_agent_name,
                requested_agent_id=requested_agent_id,
            )
    except BaseException as exc:
        observation.finish(error=exc)
        raise
    observation.account_id = result.account_id
    observation.agent_id = result.agent.id
    return replace(result, observation=observation)


async def admit_impl(
    deps: TurnDeps,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    external_user_id: str,
    channel_id: str,
    now: datetime,
    thread_id: str | None = None,
    role: Role | None = None,
    platform_role_ids: Sequence[str] | None = None,
    is_dm: bool = False,
    dm_source_channel_id: str | None = None,
    category_id: str | None = None,
    category_unresolved: bool = False,
    external: ExternalFinding | None = None,
    requested_agent_name: str | None = None,
    requested_agent_id: str | None = None,
) -> Admission:
    """Run the full pre-turn gate sequence; raise instead of returning bool.

    `external` is the adapter's live finding about the caller's organisation
    (`ExternalFinding`); None (an adapter that cannot tell) uses the stored flag.
    """
    started = last_stage = perf_counter()
    stage_ms: dict[str, float] = {}

    def mark(stage: str) -> None:
        nonlocal last_stage
        current = perf_counter()
        stage_ms[stage] = round((current - last_stage) * 1000, 1)
        last_stage = current

    # --- Identity resolution ---
    async with deps.sessionmaker() as session:
        principal = await get_or_create_platform_principal(
            session,
            tenant_id=tenant_id,
            platform=platform,
            external_id=external_user_id,
        )
        # Only positive evidence is stored, before the role: marking demotes, and
        # an external account is never promoted. A finding without it holds
        # for this turn alone and leaves the stored flag, role and groups be.
        if external is not None and external.is_known:
            await set_external(session, principal.account_id, external.is_external)
            stored = external.is_external
        else:
            stored = await get_external(session, principal.account_id)
        is_external = stored or (external is not None and external.is_external)
        store_identity = stored or not is_external
        if is_external:
            # Nor a channel admin: no stored group (a team they own) matches a grant.
            role, platform_role_ids = Role.USER, ()
        if role is not None and store_identity:
            await set_role(session, principal.account_id, role)
        # Kept like the role so MCP calls can match channel admin role grants.
        if platform_role_ids is not None and store_identity:
            await set_platform_role_ids(session, principal.account_id, platform_role_ids)
        await session.commit()
        mark("identity")
        # Read after the role commit, so a refused turn still records the role.
        policy = await load_access_policy(session, tenant_id=tenant_id)
        # Matched against the live role ids; a server admin needs no grant.
        administered = (
            frozenset[str]()
            # An external caller administers nothing, whatever a grant names.
            if role is Role.ADMIN or is_external
            else await load_administered_channel_ids(
                session,
                tenant_id=tenant_id,
                platform=platform,
                caller=ChannelAdminCaller(
                    platform_user_id=external_user_id,
                    role_ids=frozenset(platform_role_ids or ()),
                ),
            )
        )
    mark("policy_and_admins")

    # --- Channel protection, first of the policy gates: the turn's reply
    # would land in its thread or channel, so a protected target refuses the
    # turn itself -- before any thread, reply or upload exists, and before the
    # invoker gate, whose refusal the adapters would otherwise post there.
    # Admins get no exemption. `category_id` is the Discord category the
    # channel sits in; `category_unresolved` says the adapter couldn't look it
    # up, which fails closed when any category is protected. ---
    # --- Invoker policy: a tenant may restrict who can start a turn. Only a
    # live ADMIN role passed by the adapter exempts the caller; no role means
    # non-admin, never a stored role the user may have lost. An unreadable
    # policy raised `AccessPolicyUnreadable` above -- refused, never open.
    # Both gates are `authorize(START_TURN)`, protection first. ---
    subject = build_subject(
        is_admin=role is Role.ADMIN,
        platform_user_id=external_user_id,
        administered_channel_ids=administered,
    )
    turn_place = build_turn_place(
        channel_id=channel_id,
        thread_id=thread_id,
        category_id=category_id,
        category_unresolved=category_unresolved,
    )
    _require_turn_start(policy, subject, turn_place)
    # --- External participant: someone from another organisation (a Teams
    # shared channel's B2B direct connect participant) is answered only
    # inside an isolated channel, its threads included, never in a DM. ---
    _require_external_inside_isolation(
        policy, is_external=is_external, is_dm=is_dm, channel_id=channel_id, thread_id=thread_id
    )
    mark("start_policy")

    if (observation := current_outcome.get()) is not None:
        observation.account_id = principal.account_id

    # --- Config resolution (per turn) ---
    scope = ScopeContext(
        account_id=principal.account_id,
        tenant_id=tenant_id,
        channel_id=channel_id,
        platform=platform,
        thread_id=thread_id,
    )
    async with deps.sessionmaker() as session:
        config = await resolve_config(session, context=scope, default=deps.deployment_default)
        parent_config = (
            await resolve_config(
                session,
                context=scope.model_copy(update={"thread_id": None}),
                default=deps.deployment_default,
            )
            if (requested_agent_name is not None or requested_agent_id is not None)
            and thread_id is not None
            else config
        )
    named_agent = None
    if requested_agent_name is not None or requested_agent_id is not None:
        roster = await list_agents_by_tenant(deps.anthropic, tenant_id=tenant_id)
        named_agent = (
            next((agent for agent in roster if agent.id == requested_agent_id), None)
            if requested_agent_id is not None
            else matching_agent(roster, requested_agent_name or "")
        )
        if named_agent is None and requested_agent_id is not None:
            raise NamedAgentRefused(
                "That agent is unavailable. Ask an admin to refresh agent roles."
            )
        if named_agent is not None:
            if config.thread_binding_id is not None and (
                config.responder_ma_agent_id != named_agent.id
            ):
                raise NamedAgentRefused(
                    "This thread belongs to another agent. Start a new thread or ask for a handoff."
                )
            inside = confidential_channel_of(policy, thread_id, channel_id)
            named_permissions = agent_permissions(
                policy, (named_agent.name, named_agent.metadata.get("daimon_name"))
            )
            here = channel_permissions(
                policy,
                channel_id=thread_id or channel_id,
                parent_channel_id=channel_id if thread_id is not None else None,
            )
            if inside is not None and not at_home(named_permissions, here):
                own = parent_config.agent_name or "this channel's agent"
                raise NamedAgentRefused(
                    f"This is {own}'s confidential channel. Name that agent here instead."
                )
            config = config.model_copy(
                update={
                    "agent_name": named_agent.metadata.get("daimon_name") or named_agent.name,
                    "agent_name_tier": "named",
                    "responder_ma_agent_id": named_agent.id,
                }
            )
    mark("config")

    # --- An external caller is never answered in a setup conversation, even
    # one in their isolated channel: its built-in agent is not the channel's
    # own and changes the agent's setup. Before any MA call. ---
    if is_external and config.thread_binding_kind == "setup":
        raise AdmissionDenied(reason="external_participant")

    # --- Missing config check (before any MA call) ---
    if config.agent_name is None or config.environment_name is None:
        missing: tuple[Literal["agent", "environment"], ...]
        if config.agent_name is None and config.environment_name is None:
            missing = ("agent", "environment")
        elif config.agent_name is None:
            missing = ("agent",)
        else:
            missing = ("environment",)
        raise MissingTurnConfigError(
            missing=missing,
            agent_name_tier=config.agent_name_tier,
            environment_name_tier=config.environment_name_tier,
        )

    async def _apply() -> object:
        return await reconcile_tenant_defaults(
            deps.anthropic,
            deps.sessionmaker,
            deps.defaults_root,
            tenant_id=tenant_id,
            public_url=deps.public_url,
        )

    # --- Resolve agent/environment via ma_resolver (self-heals on
    # archive/recreate); MAResolverMissError propagates unwrapped. This path
    # always passes cached_id=None, so the resolver's own liveness step never
    # runs here, and its short TTL cache can hand back an id for an agent
    # archived since it was cached -- the retrieve below is what settles it. ---
    agent_id = config.responder_ma_agent_id
    if agent_id is None:
        agent_id = await resolve_agent(
            deps.anthropic,
            tenant_id=tenant_id,
            daimon_tag=config.agent_name,
            cached_id=None,
            apply_callable=_apply,
            cache=deps.resolver_cache,
        )
    env_id = await resolve_environment(
        deps.anthropic,
        tenant_id=tenant_id,
        daimon_tag=config.environment_name,
        cached_id=None,
        apply_callable=_apply,
        cache=deps.resolver_cache,
    )

    # A bound thread resolves its responder by concrete id, never by name: a
    # recreated namesake must not inherit the conversation. Which concrete
    # lookup depends on why the thread is bound -- a setup conversation must
    # answer as the built-in Daimon (`get_setup_responder` asserts that), while
    # a thread whose task was handed to another agent answers as that agent,
    # which is an ordinary tenant-and-archive-checked retrieve.
    if named_agent is not None:
        agent = await get_setup_agent(
            deps.anthropic, tenant_id=tenant_id, ma_agent_id=named_agent.id
        )
    elif config.thread_binding_kind in ("handoff", "opened"):
        agent = await get_setup_agent(deps.anthropic, tenant_id=tenant_id, ma_agent_id=agent_id)
    elif config.thread_binding_id is not None:
        agent = await get_setup_responder(deps.anthropic, tenant_id=tenant_id, ma_agent_id=agent_id)
    else:
        try:
            agent = await deps.anthropic.beta.agents.retrieve(agent_id)
        except APIStatusError as err:
            if err.status_code not in (400, 404):
                raise
            deps.resolver_cache.pop((tenant_id, "agent", config.agent_name), None)
            raise MAResolverMissError(
                kind="agent", tenant_id=tenant_id, daimon_tag=config.agent_name
            ) from err
    if (observation := current_outcome.get()) is not None:
        observation.agent_id = agent.id
    try:
        environment = await deps.anthropic.beta.environments.retrieve(env_id)
    except APIStatusError as err:
        if err.status_code not in (400, 404):
            raise
        deps.resolver_cache.pop((tenant_id, "environment", config.environment_name), None)
        raise MAResolverMissError(
            kind="environment", tenant_id=tenant_id, daimon_tag=config.environment_name
        ) from err
    mark("agent_environment")

    # --- Liveness check on the already-retrieved agent: it was archived out of
    # band since the resolver cached/looked up its id. Self-heal the scope
    # rows naming it (tenant-wide) before raising, so the next mention resolves
    # the next cascade tier instead of re-hitting this same dead binding, and
    # raise the existing resolver-miss error so the friendly copy at the four
    # adapter catch sites renders unchanged -- no new error taxonomy. ---
    if agent.archived_at is not None:
        deps.resolver_cache.pop((tenant_id, "agent", config.agent_name), None)
        async with deps.sessionmaker() as session, session.begin():
            await clear_agent_references(session, tenant_id=tenant_id, agent_name=config.agent_name)
        raise MAResolverMissError(kind="agent", tenant_id=tenant_id, daimon_tag=config.agent_name)
    if environment.archived_at is not None:
        deps.resolver_cache.pop((tenant_id, "environment", config.environment_name), None)
        raise MAResolverMissError(
            kind="environment", tenant_id=tenant_id, daimon_tag=config.environment_name
        )

    # --- Agent pin: an operator can tie an agent to named channels because of
    # what its credentials reach. It runs after the cascade because it depends
    # on which agent answers, and it checks both the cascade's name and the
    # agent's own, so a handed-off thread (resolved by id) is covered too. A DM
    # has no channel, so it is outside every pin. Admins are trusted and exempt
    # only in a DM, where the reply reaches no one else; in a channel or
    # thread other members would see it, so the pin holds for them too. The
    # role is the adapter's live one, never a stored role. ---
    grant = AdmissionGrant(
        tenant_id=tenant_id,
        subject=subject,
        surface=Surface.DM if is_dm else Surface.CHANNEL,
        turn_place=turn_place,
        agent=build_agent_ref(agent.name, agent.metadata, config.agent_name),
        run_place=Place()
        if is_dm
        else replace(
            build_turn_place(channel_id=channel_id, thread_id=thread_id),
            setup_thread=config.thread_binding_kind == "setup",
        ),
        channel_id=channel_id,
        thread_id=thread_id,
        is_dm=is_dm,
        is_external=is_external,
    )
    _require_run_agent(policy, grant)
    mark("agent_policy")

    # --- Admission gate: per-tenant balance -- independent of Stripe config ---
    if await is_over_balance(sessionmaker=deps.sessionmaker, tenant_id=tenant_id):
        raise AdmissionDenied(reason="balance_depleted")
    mark("balance")

    # --- Admission gate: monthly usage cap ---
    if await is_over_cap(
        billing_config=deps.billing_config,
        sessionmaker=deps.sessionmaker,
        tenant_id=tenant_id,
        user_id=external_user_id,
        now=now,
    ):
        raise AdmissionDenied(reason="cap_exceeded")
    mark("user_cap")

    # --- Admission gate: channel budget; a DM counts toward the channel it came from,
    # and an isolated channel's own agent toward that channel wherever an exempt
    # caller (an admin, or that channel's admin) runs it ---
    budget_channel_id = agent_permissions(policy, grant.agent.names).budget_channel or (
        dm_source_channel_id if is_dm else channel_id
    )
    if await is_over_channel_budget(
        sessionmaker=deps.sessionmaker,
        tenant_id=tenant_id,
        platform=platform,
        channel_id=budget_channel_id,
        now=now,
    ):
        if tenant_id not in deps.budget_notices_off:
            spawn_budget_notice(
                sessionmaker=deps.sessionmaker,
                notifier=deps.budget_notifier,
                tenant_id=tenant_id,
                platform=platform,
                channel_id=budget_channel_id,
                now=now,
                group_members=deps.group_members,
            )
        raise AdmissionDenied(reason="channel_budget_exceeded")
    mark("channel_budget")

    # Every id that seals the turn: its channel, and the thread sealed on its
    # own (a Discord thread by id, a Slack one as channel_id:thread_ts). All of
    # them are recorded, so unsealing one later leaves the others holding.
    seal_ids = seal_ids_at(policy, channel_id=channel_id, thread_id=thread_id)
    source_sealed = bool(seal_ids)
    memory_read_only = (source_sealed and not _is_own_agent(policy, grant)) or (
        is_dm and policy.dm_memory_read_only
    )

    channel_skills = (
        ()
        if is_dm
        else await turn_channel_skills(
            deps.sessionmaker,
            deps.anthropic,
            tenant_id=tenant_id,
            platform=platform,
            channel_id=channel_id,
            agent=agent,
            agent_names=grant.agent.names,
        )
    )

    mark("channel_skills")

    result = Admission(
        memory_read_only=memory_read_only,
        source_sealed=source_sealed,
        origin_channel_id=channel_id,
        origin_thread_id=thread_id,
        origin_seal_ids=seal_ids,
        # Every DM-admitted session is private: the transcript tools never open
        # it to anyone but its own execution grant, admins included. /dm
        # replaces this with its execution-specific grant.
        private_dm_id=(thread_id or channel_id) if is_dm else None,
        account_id=principal.account_id,
        agent=agent,
        environment=environment,
        config=config.model_copy(update={"responder_ma_agent_id": agent.id}),
        budget_channel_id=budget_channel_id,
        channel_skills=channel_skills,
        grant=grant,
        is_external=is_external,
    )
    mark("result")
    _log.info(
        "turn.admission_timing",
        tenant_id=str(tenant_id),
        platform=platform,
        channel_id=channel_id,
        thread_id=thread_id,
        total_ms=round((perf_counter() - started) * 1000, 1),
        stage_ms=stage_ms,
    )
    return result


def _require_turn_start(policy: TenantAccessPolicy, subject: Subject, place: Place) -> None:
    """Protection, then the invoker allowlist; any denial refuses the turn."""
    decision = authorize(policy, subject=subject, action=Action.START_TURN, place=place)
    if not decision:
        raise AdmissionDenied(
            reason="invoker_not_allowed"
            if decision.reason == "invoker_not_allowed"
            else "channel_protected"
        )


def _require_external_inside_isolation(
    policy: TenantAccessPolicy,
    *,
    is_external: bool,
    is_dm: bool,
    channel_id: str,
    thread_id: str | None,
    setup_thread: bool = False,
) -> None:
    """Refuse an external caller anywhere but an isolated channel or a thread in one,
    and in a setup conversation anywhere."""
    if not is_external:
        return
    place = channel_permissions(
        policy,
        channel_id=thread_id or channel_id,
        parent_channel_id=channel_id if thread_id is not None else None,
    )
    if is_dm or setup_thread or not place.answers_external:
        raise AdmissionDenied(reason="external_participant")


def _require_run_agent(policy: TenantAccessPolicy, grant: AdmissionGrant) -> None:
    """The agent pin, then channel isolation; any denial refuses the turn."""
    decision = authorize(
        policy,
        subject=grant.subject,
        action=Action.RUN_AGENT,
        surface=grant.surface,
        agent=grant.agent,
        place=grant.run_place,
    )
    if not decision:
        raise AdmissionDenied(
            reason="channel_isolated"
            if decision.reason == "channel_isolated"
            else "agent_pinned_elsewhere"
        )


def _is_own_agent(policy: TenantAccessPolicy, grant: AdmissionGrant) -> bool:
    """An isolated channel's own agent at work there, whose memory stays writable."""
    here = channel_permissions(
        policy,
        channel_id=grant.run_place.channel_id,
        parent_channel_id=grant.run_place.parent_channel_id,
    )
    return at_home(agent_permissions(policy, grant.agent.names), here)


async def reauthorize(deps: TurnDeps, admission: Admission) -> Admission:
    """Decide an admission again, on the policy as it is now.

    Called at the moment a session is built, reused, replaced or recovered:
    protection, the invoker allowlist and the agent pin are re-asked of
    `authorize` with a fresh policy read, and a turn that would now be
    refused raises `AdmissionDenied` before any session runs it. A seal added
    since admission joins the turn's seal ids (they only grow), so the
    session is stamped with it and memory turns read-only. An unreadable
    policy raises `AccessPolicyUnreadable` -- refused, never open.

    A hand-built admission with no recorded grant (tests only) is returned
    unchanged.
    """
    grant = admission.grant
    if grant is None:
        return admission
    async with deps.sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=grant.tenant_id)
    _require_turn_start(policy, grant.subject, grant.turn_place)
    _require_external_inside_isolation(
        policy,
        is_external=grant.is_external,
        is_dm=grant.is_dm,
        channel_id=grant.channel_id,
        thread_id=grant.thread_id,
        setup_thread=grant.run_place.setup_thread,
    )
    _require_run_agent(policy, grant)
    if grant.dm_source is not None and dm_source_sealed(
        policy,
        source_channel_id=grant.dm_source.channel_id,
        source_thread_id=grant.dm_source.thread_id,
        source_thread_keys=grant.dm_source.thread_keys,
    ):
        raise DmSourceSealedError("dm_source_sealed")
    seal_ids = admission.origin_seal_ids | seal_ids_at(
        policy, channel_id=grant.channel_id, thread_id=grant.thread_id
    )
    # Memory posture is decided from the policy as it is now, not only from
    # new seals: a DM turn picks up `dm_memory_read_only` switched on since
    # admission. Read-only never relaxes back to writable here.
    memory_read_only = (
        admission.memory_read_only
        or (bool(seal_ids) and not _is_own_agent(policy, grant))
        or (grant.is_dm and policy.dm_memory_read_only)
    )
    if seal_ids == admission.origin_seal_ids and memory_read_only == admission.memory_read_only:
        return admission
    return replace(
        admission,
        origin_seal_ids=seal_ids,
        source_sealed=admission.source_sealed or bool(seal_ids),
        memory_read_only=memory_read_only,
    )


async def restrict_inherited_memory(
    deps: TurnDeps, admission: Admission, seals: frozenset[str]
) -> Admission:
    """Historical work retains read-only memory after its channel is unsealed."""
    if not seals or admission.memory_read_only:
        return admission
    grant = admission.grant
    own = False
    if grant is not None:
        async with deps.sessionmaker() as db:
            policy = await load_access_policy(db, tenant_id=grant.tenant_id)
        own_channel = agent_permissions(policy, grant.agent.names).own_channel
        own = _is_own_agent(policy, grant) and all(
            seal
            in {
                own_channel,
                grant.thread_id,
                f"{own_channel}:{grant.thread_id}" if grant.thread_id is not None else None,
            }
            or confidential_channel_of(policy, seal) == own_channel
            for seal in seals
        )
    return admission if own else replace(admission, memory_read_only=True)


def decide_before_send(deps: TurnDeps, admission: Admission) -> Callable[[], Awaitable[None]]:
    """A last decision to run right before a message is sent into a session.

    Opening the stream and building the message await network calls after the
    session's last decision. A pin or protection added since refuses the send
    (`AdmissionDenied`); a seal or read-only change the session wasn't built
    with makes the turn wait (`SessionBusyError`) so the next one is prepared
    with it, rather than writing into a session mounted for the old decision.
    """

    async def decide() -> None:
        current = await reauthorize(deps, admission)
        if (
            current.origin_seal_ids != admission.origin_seal_ids
            or current.memory_read_only != admission.memory_read_only
        ):
            raise SessionBusyError(
                pending_reasons=("seal",), retry_after=datetime.now(UTC) + timedelta(seconds=1)
            )

    return decide
