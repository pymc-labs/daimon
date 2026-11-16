"""Direct message access: the I/O around `dm_routing.resolve`.

Loads grants, rules, the workspace default and a person's pick, reads which
agents take DMs and whether the person may run them, and returns one decision.
Group membership comes from the caller's `Subject` (`verified_membership` None
means the platform could not say, so group-based checks fail closed).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.anthropic_guard import anthropic_disabled
from daimon.core.authz import Action, AgentRef, Place, Subject, Surface, authorize, build_agent_ref
from daimon.core.config import Settings
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_READER_OF
from daimon.core.dm_routing import (
    Candidate,
    DmDecision,
    DmGrant,
    DmRefused,
    DmRuleFact,
    DmStart,
    Person,
    grant_admits,
    resolve,
)
from daimon.core.features.snapshot import FeaturePlace, feature_in
from daimon.core.stores import dm_access as store
from daimon.core.stores.direct_messages import dm_enabled, get_dm_agent_name
from daimon.core.stores.selfhosted_resources import list_live_agent_facts
from daimon.core.turn.deps import TurnDeps
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class DmAgent:
    """An agent a DM may reach: its config name (grants and rules use it) and MA identity."""

    name: str
    ma_agent_id: str
    ref: AgentRef


@dataclass(frozen=True)
class DmRouting:
    """A workspace's stored access: grants by agent name, ordered rules, the default agent."""

    grants: dict[str, DmGrant]
    rules: list[DmRuleFact]
    default: str | None


def dm_agent_of(agent: BetaManagedAgentsAgent, config_name: str | None = None) -> DmAgent:
    """The DM view of an MA agent record; `config_name` is the name an admission resolved by."""
    name = config_name or agent.metadata.get(MA_METADATA_KEY_NAME) or agent.name
    return DmAgent(
        name=name,
        ma_agent_id=agent.id,
        ref=build_agent_ref(agent.name, agent.metadata, config_name),
    )


async def list_dm_agents(
    deps: TurnDeps, session: AsyncSession, *, tenant_id: uuid.UUID
) -> list[DmAgent]:
    """The workspace's own agents by name; published report readers are never DM agents."""
    agents = (
        await list_live_agent_facts(session, tenant_id=tenant_id)
        if anthropic_disabled()
        else await list_agents_by_tenant(
            deps.anthropic, tenant_id=tenant_id, records=deps.sessionmaker
        )
    )
    return [dm_agent_of(a) for a in agents if MA_METADATA_KEY_READER_OF not in a.metadata]


def person_of(subject: Subject) -> Person:
    """The sender as routing sees them; groups are their roles when membership was verified."""
    user_id = subject.platform_user_id or ""
    groups = frozenset(subject.role_ids) if subject.verified_membership is True else None
    return Person(user_id, groups)


async def load_routing(session: AsyncSession, *, tenant_id: uuid.UUID) -> DmRouting:
    """Read the grants, rules and default of one workspace."""
    grants = {
        name: DmGrant(row.who, frozenset(row.user_ids), frozenset(row.group_ids))
        for name, row in (await store.list_grants(session, tenant_id=tenant_id)).items()
    }
    rules = [
        DmRuleFact(r.subject, r.subject_id, r.agent_name, r.locked)
        for r in await store.list_rules(session, tenant_id=tenant_id)
    ]
    return DmRouting(grants, rules, await get_dm_agent_name(session, tenant_id=tenant_id))


async def _candidate(
    session: AsyncSession,
    settings: Settings | None,
    policy: TenantAccessPolicy,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    agent: DmAgent,
    subject: Subject,
) -> Candidate:
    place = FeaturePlace(tenant_id, platform, agent_id=agent.ma_agent_id)
    dm_on = (await feature_in(session, settings, place, "direct_messages")).on
    if not dm_on:
        return Candidate(agent.name, False, False)
    # An agent's pins and isolation apply to a DM without an admin exemption.
    runner = replace(subject, is_admin=False)
    may_run = all(
        bool(
            authorize(
                policy,
                subject=runner,
                action=action,
                surface=Surface.DM,
                agent=agent.ref,
                place=Place(),
                recipient_id=subject.platform_user_id,
            )
        )
        for action in (Action.DIRECT_MESSAGE, Action.RUN_AGENT)
    )
    return Candidate(agent.name, True, may_run)


async def decide_dm(
    session: AsyncSession,
    settings: Settings | None,
    policy: TenantAccessPolicy,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    agents: Sequence[DmAgent],
    subject: Subject,
    account_id: uuid.UUID | None,
) -> DmDecision:
    """Which of `agents` a person's DM reaches, from grants, rules, their pick and the default.

    A workspace with DMs off admits nobody. Pass every agent to choose among them, or one
    to ask whether the person may use it: the decision is then `DmStart` for that agent.
    """
    if not await dm_enabled(session, tenant_id=tenant_id):
        return DmDecision(DmRefused("no_agent"))
    routing = await load_routing(session, tenant_id=tenant_id)
    picked = (
        None
        if account_id is None
        else await store.get_pick(session, tenant_id=tenant_id, account_id=account_id)
    )
    candidates = [
        await _candidate(
            session,
            settings,
            policy,
            tenant_id=tenant_id,
            platform=platform,
            agent=agent,
            subject=subject,
        )
        for agent in agents
    ]
    return resolve(
        person_of(subject),
        candidates,
        routing.grants,
        routing.rules,
        default=routing.default,
        pick=None if picked is None else picked.agent_name,
    )


async def permits_dm(
    session: AsyncSession,
    settings: Settings | None,
    policy: TenantAccessPolicy,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    agent: DmAgent,
    subject: Subject,
) -> bool:
    """Whether this person may DM this agent: on, runnable, granted, and no lock names another."""
    decision = await decide_dm(
        session,
        settings,
        policy,
        tenant_id=tenant_id,
        platform=platform,
        agents=[agent],
        subject=subject,
        account_id=None,
    )
    return isinstance(decision.route, DmStart) and decision.route.agent == agent.name


async def agent_dm_admits(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_name: str | None,
    user_id: str,
    groups: frozenset[str] | None = None,
) -> bool:
    """Whether the sending agent's grant admits `user_id` to an agent-sent DM.

    Locks only decide who answers, so they do not apply. A message with no sending
    agent (a budget notice) uses the workspace default agent's grant, and none when
    no default is set. Groups not known where the grant needs them: not admitted.
    """
    name = agent_name or await get_dm_agent_name(session, tenant_id=tenant_id)
    if name is None:
        return True
    grants = (await load_routing(session, tenant_id=tenant_id)).grants
    return grant_admits(grants.get(name, DmGrant()), Person(user_id, groups)) is True
