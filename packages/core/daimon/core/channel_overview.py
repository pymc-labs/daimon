"""One channel's state as one viewer may see it: its rule, budget, and environment.

A fact is shown only to a viewer who can already learn it through another
read, and otherwise omitted (None, and missing from `ChannelOverview.shown`),
never reported as false:

- whether only its own agents run there: server admins, operator tokens
  reading the tenant (`get_tenant_summary`), and the deployment operator;
- its readers and writers: the same viewers, since any admin may change them;
- budget: those, and anyone the platform shows the channel to
  (`get_channel_budget`);
- environment: those, and anyone on the channel's side of every isolation
  channel kept to its own agents (`explain_agent_resolution`).

Everything here is pure but `load_channel_overview`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from daimon.core.access_policy import ChannelReaders, ChannelWriters, TenantAccessPolicy
from daimon.core.channel_budget import ChannelBudgetStatus, get_channel_budget_status
from daimon.core.permissions import channel_rule, home_of
from daimon.core.scope import DeploymentDefault, ScopeContext
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.scoped_config_read import resolve
from sqlalchemy.ext.asyncio import AsyncSession

OverviewFact = Literal["own_agents", "rule", "budget", "environment"]


@dataclass(frozen=True)
class ChannelViewer:
    """Who reads a channel's overview, and from where."""

    is_server_admin: bool = False
    """A server admin, or an operator token reading the tenant."""
    reads_access_policy: bool = False
    """The deployment operator, who reads the whole access policy."""
    sees_channel: bool = False
    """The platform shows the viewer this channel; the caller has checked."""
    inside_channel_id: str | None = None
    """The channel kept to its own agents the viewer stands in (`RuleViewer.inside_channel_id`)."""


@dataclass(frozen=True)
class ChannelOverview:
    """A fact not in `shown` is omitted; a shown `budget` or `environment_name`
    of None means there is none."""

    channel_id: str
    shown: frozenset[OverviewFact]
    own_agents_only: bool | None = None
    readers: ChannelReaders | None = None
    writers: ChannelWriters | None = None
    budget: ChannelBudgetStatus | None = None
    environment_name: str | None = None


def shown_facts(
    viewer: ChannelViewer, *, policy: TenantAccessPolicy, channel_id: str
) -> frozenset[OverviewFact]:
    """The facts `viewer` may see of `channel_id` (see the module docstring)."""
    wide = viewer.is_server_admin or viewer.reads_access_policy
    facts: set[OverviewFact] = set()
    if wide:
        facts |= {"own_agents", "rule", "budget", "environment"}
    if viewer.sees_channel:
        facts.add("budget")
    if home_of(policy, channel_id) == viewer.inside_channel_id:
        facts.add("environment")
    return frozenset(facts)


def build_channel_overview(
    channel_id: str,
    *,
    viewer: ChannelViewer,
    policy: TenantAccessPolicy,
    environment_name: str | None,
    budget: ChannelBudgetStatus | None,
) -> ChannelOverview:
    """`environment_name` is the one resolved for the channel."""
    shown = shown_facts(viewer, policy=policy, channel_id=channel_id)
    rule = channel_rule(policy, channel_id)
    return ChannelOverview(
        channel_id=channel_id,
        shown=shown,
        own_agents_only=rule.readers == "own" if "own_agents" in shown else None,
        readers=rule.readers if "rule" in shown else None,
        writers=rule.writers if "rule" in shown else None,
        budget=budget if "budget" in shown else None,
        environment_name=environment_name if "environment" in shown else None,
    )


async def load_channel_overview(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    viewer: ChannelViewer,
    default: DeploymentDefault,
    now: datetime,
) -> ChannelOverview:
    """Read one channel's overview, skipping the reads of facts the viewer can't see.

    Raises `AccessPolicyUnreadable` when the access policy can't be read.
    """
    policy = await load_access_policy(session, tenant_id=tenant_id)
    shown = shown_facts(viewer, policy=policy, channel_id=channel_id)
    environment_name = (
        (
            await resolve(
                session,
                context=ScopeContext(tenant_id=tenant_id, channel_id=channel_id),
                default=default,
            )
        ).environment_name
        if "environment" in shown
        else None
    )
    budget = (
        await get_channel_budget_status(
            session, tenant_id=tenant_id, platform=platform, channel_id=channel_id, now=now
        )
        if "budget" in shown
        else None
    )
    return build_channel_overview(
        channel_id,
        viewer=viewer,
        policy=policy,
        environment_name=environment_name,
        budget=budget,
    )


__all__ = [
    "ChannelOverview",
    "ChannelViewer",
    "OverviewFact",
    "build_channel_overview",
    "load_channel_overview",
    "shown_facts",
]
