"""Stage one of the two-stage turn chokepoint: `admit()`.

A single core call performs identity resolution, the channel -> tenant ->
deployment config cascade, MA resolve + SDK retrieve, the per-tenant balance
gate, the per-user monthly cap gate and the channel budget gate -- returning a
frozen `Admission` or raising a typed error. No boolean gate result crosses this boundary.

Gate ORDER is load-bearing and must not be reordered: identity -> channel
protection -> invoker policy -> cascade -> missing-config -> resolve/retrieve
-> agent pin -> balance -> cap -> channel budget. A tenant that is both
over-balance and mis-configured must see the config error (matches both
adapters' inline sequences today). The
access policy gates run before the cascade so a refused turn learns nothing
about the tenant's configuration and never reaches an MA call.

Ported verbatim from `bot.py`'s inline pre-turn sequence (the reference
implementation). Both the Discord and Slack adapters now call `admit()` as
their pre-turn gate.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal

from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
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
    build_turn_place,
)
from daimon.core.billing import is_over_cap
from daimon.core.channel_budget import is_over_channel_budget
from daimon.core.defaults.provisioning import reconcile_tenant_defaults
from daimon.core.ma_resolver import MAResolverMissError, resolve_agent, resolve_environment
from daimon.core.scope import ResolvedConfig, ScopeContext
from daimon.core.setup_conversations import get_setup_agent, get_setup_responder
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.scoped_config_read import resolve as resolve_config
from daimon.core.stores.scoped_config_write import clear_agent_references
from daimon.core.tenant_balance import is_over_balance
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import AdmissionDenied, MissingTurnConfigError
from daimon.core.turn.outcomes import TurnObservation, current_outcome

__all__ = [
    "Admission",
    "AdmissionDenied",
    "AdmissionGrant",
    "MissingTurnConfigError",
    "admit",
    "reauthorize",
]


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
    is_dm: bool = False,
    dm_source_channel_id: str | None = None,
    category_id: str | None = None,
    category_unresolved: bool = False,
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
                is_dm=is_dm,
                dm_source_channel_id=dm_source_channel_id,
                category_id=category_id,
                category_unresolved=category_unresolved,
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
    is_dm: bool = False,
    dm_source_channel_id: str | None = None,
    category_id: str | None = None,
    category_unresolved: bool = False,
) -> Admission:
    """Run the full pre-turn gate sequence; raise instead of returning bool."""
    # --- Identity resolution ---
    async with deps.sessionmaker() as session:
        principal = await get_or_create_platform_principal(
            session,
            tenant_id=tenant_id,
            platform=platform,
            external_id=external_user_id,
        )
        if role is not None:
            await set_role(session, principal.account_id, role)
        await session.commit()
        # Read after the role commit, so a refused turn still records the role.
        policy = await load_access_policy(session, tenant_id=tenant_id)

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
    subject = build_subject(is_admin=role is Role.ADMIN, platform_user_id=external_user_id)
    turn_place = build_turn_place(
        channel_id=channel_id,
        thread_id=thread_id,
        category_id=category_id,
        category_unresolved=category_unresolved,
    )
    _require_turn_start(policy, subject, turn_place)

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
    if config.thread_binding_kind == "handoff":
        agent = await get_setup_agent(deps.anthropic, tenant_id=tenant_id, ma_agent_id=agent_id)
    elif config.thread_binding_id is not None:
        agent = await get_setup_responder(deps.anthropic, tenant_id=tenant_id, ma_agent_id=agent_id)
    else:
        agent = await deps.anthropic.beta.agents.retrieve(agent_id)
    if (observation := current_outcome.get()) is not None:
        observation.agent_id = agent.id
    environment = await deps.anthropic.beta.environments.retrieve(env_id)

    # --- Liveness check on the already-retrieved agent: it was archived out of
    # band since the resolver cached/looked up its id. Self-heal the scope
    # rows naming it (tenant-wide) before raising, so the next mention resolves
    # the next cascade tier instead of re-hitting this same dead binding, and
    # raise the existing resolver-miss error so the friendly copy at the four
    # adapter catch sites renders unchanged -- no new error taxonomy. ---
    if agent.archived_at is not None:
        async with deps.sessionmaker() as session, session.begin():
            await clear_agent_references(session, tenant_id=tenant_id, agent_name=config.agent_name)
        raise MAResolverMissError(kind="agent", tenant_id=tenant_id, daimon_tag=config.agent_name)

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
        else build_turn_place(channel_id=channel_id, thread_id=thread_id),
        channel_id=channel_id,
        thread_id=thread_id,
        is_dm=is_dm,
    )
    _require_run_agent(policy, grant)

    # --- Admission gate: per-tenant balance -- independent of Stripe config ---
    if await is_over_balance(sessionmaker=deps.sessionmaker, tenant_id=tenant_id):
        raise AdmissionDenied(reason="balance_depleted")

    # --- Admission gate: monthly usage cap ---
    if await is_over_cap(
        billing_config=deps.billing_config,
        sessionmaker=deps.sessionmaker,
        tenant_id=tenant_id,
        user_id=external_user_id,
        now=now,
    ):
        raise AdmissionDenied(reason="cap_exceeded")

    # --- Admission gate: channel budget; a DM counts toward the channel it came from ---
    budget_channel_id = dm_source_channel_id if is_dm else channel_id
    if await is_over_channel_budget(
        sessionmaker=deps.sessionmaker,
        tenant_id=tenant_id,
        platform=platform,
        channel_id=budget_channel_id,
        now=now,
    ):
        raise AdmissionDenied(reason="channel_budget_exceeded")

    # Every id that seals the turn: its channel, and the thread sealed on its
    # own (a Discord thread by id, a Slack one as channel_id:thread_ts). All of
    # them are recorded, so unsealing one later leaves the others holding.
    seal_ids = _seal_ids(policy, channel_id=channel_id, thread_id=thread_id)
    source_sealed = bool(seal_ids)
    memory_read_only = source_sealed or (is_dm and policy.dm_memory_read_only)

    return Admission(
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
        grant=grant,
    )


def _require_turn_start(policy: TenantAccessPolicy, subject: Subject, place: Place) -> None:
    """Protection, then the invoker allowlist; any denial refuses the turn."""
    decision = authorize(policy, subject=subject, action=Action.START_TURN, place=place)
    if not decision:
        raise AdmissionDenied(
            reason="invoker_not_allowed"
            if decision.reason == "invoker_not_allowed"
            else "channel_protected"
        )


def _require_run_agent(policy: TenantAccessPolicy, grant: AdmissionGrant) -> None:
    """The agent pin; any denial refuses the turn."""
    decision = authorize(
        policy,
        subject=grant.subject,
        action=Action.RUN_AGENT,
        surface=grant.surface,
        agent=grant.agent,
        place=grant.run_place,
    )
    if not decision:
        raise AdmissionDenied(reason="agent_pinned_elsewhere")


def _seal_ids(
    policy: TenantAccessPolicy, *, channel_id: str, thread_id: str | None
) -> frozenset[str]:
    """Every id that seals a turn: its channel and a thread sealed on its own.

    A Discord thread is sealed by its id, a Slack one as channel_id:thread_ts.
    All of them are recorded, so unsealing one later leaves the others holding.
    """
    return frozenset(
        candidate
        for candidate in (
            channel_id,
            thread_id,
            f"{channel_id}:{thread_id}" if thread_id is not None else None,
        )
        if candidate is not None and candidate in policy.sealed_channel_ids
    )


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
    _require_run_agent(policy, grant)
    seal_ids = admission.origin_seal_ids | _seal_ids(
        policy, channel_id=grant.channel_id, thread_id=grant.thread_id
    )
    if seal_ids == admission.origin_seal_ids:
        return admission
    return replace(
        admission,
        origin_seal_ids=seal_ids,
        source_sealed=True,
        memory_read_only=True,
    )
