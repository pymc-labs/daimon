"""Stage one of the two-stage turn chokepoint: `admit()`.

A single core call performs identity resolution, the channel -> tenant ->
deployment config cascade, MA resolve + SDK retrieve, the per-tenant balance
gate, and the per-user monthly cap gate -- returning a frozen `Admission` or
raising a typed error. No boolean gate result crosses this boundary.

Gate ORDER is load-bearing and must not be reordered: identity -> invoker
policy -> cascade -> missing-config -> resolve/retrieve -> balance -> cap. A
tenant that is both over-balance and mis-configured must see the config error
(matches both adapters' inline sequences today). The invoker policy runs
before the cascade so a refused user learns nothing about the tenant's
configuration and never reaches an MA call.

Ported verbatim from `bot.py`'s inline pre-turn sequence (the reference
implementation). Both the Discord and Slack adapters now call `admit()` as
their pre-turn gate.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
from daimon.core.access_policy import is_invoker_allowed, is_sealed
from daimon.core.billing import is_over_cap
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

__all__ = ["Admission", "AdmissionDenied", "MissingTurnConfigError", "admit"]


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

    # --- Invoker policy: a tenant may restrict who can start a turn. Only a
    # live ADMIN role passed by the adapter exempts the caller; no role means
    # non-admin, never a stored role the user may have lost. An unreadable
    # policy raised `AccessPolicyUnreadable` above -- refused, never open. ---
    if not is_invoker_allowed(
        policy, external_user_id=external_user_id, is_admin=role is Role.ADMIN
    ):
        raise AdmissionDenied(reason="invoker_not_allowed")

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

    memory_read_only = is_sealed(
        policy, channel_id=thread_id or channel_id, parent_channel_id=channel_id
    ) or (is_dm and policy.dm_memory_read_only)

    return Admission(
        memory_read_only=memory_read_only,
        account_id=principal.account_id,
        agent=agent,
        environment=environment,
        config=config.model_copy(update={"responder_ma_agent_id": agent.id}),
    )
