"""What must hold before a run starts or a step spends: feature, backend, run-as, grants,
budget and the spend gate. A refusal names its reason; nothing is created or spent.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final, Literal

import httpx
from daimon.core.channel_backend import channel_provider
from daimon.core.channel_member_check import fetch_member_reads
from daimon.core.config import Settings
from daimon.core.features.snapshot import FeaturePlace, resolve_features
from daimon.core.github_credentials import build_multifernet
from daimon.core.permissions import agent_permissions, effective_channel_rule
from daimon.core.spend_gate import (
    SpendPerson,
    SpendRequest,
    spend_gate,
    spend_person_of_account,
)
from daimon.core.stores import accounts, identity, tenants, workflow_runs
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.workflows import WorkflowRow
from daimon.core.workflows.delivery import target_for
from daimon.core.workflows.features import KEYS, workflow_features
from daimon.core.workflows.spec import (
    AgentNode,
    CodeNode,
    FinishNode,
    ForEachNode,
    Spec,
    node_hash,
    slug,
)
from daimon.core.workflows.validate import validate_spec
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GATE_REASONS: Final = frozenset(
    {"balance_depleted", "budget_exceeded", "allowance_exhausted", "cap_exceeded"}
)
DM_TARGETS: Final = ("owner", "run_as")
_GATE_MESSAGES: Final = {
    "balance_depleted": "The team's balance is used up. Ask an admin to add funds.",
    "budget_exceeded": "The home channel's budget is used up. Ask an admin to raise it.",
    "allowance_exhausted": "The person this runs as has no allowance left. Ask an admin.",
    "cap_exceeded": "The person this runs as reached their monthly limit. Ask an admin.",
}


@dataclass(frozen=True)
class Refusal:
    reason: str
    message: str

    @property
    def gate(self) -> bool:
        """A spend gate refusal: a step waits `blocked` on it instead of failing."""
        return self.reason in GATE_REASONS


@dataclass(frozen=True)
class Cleared:
    """Preflight passed; the workspace's default run budget for a new run."""

    run_budget_usd: Decimal


@dataclass(frozen=True)
class Place:
    """Where a run spends: its workspace, home channel and run-as account."""

    tenant_id: uuid.UUID
    workflow: WorkflowRow
    spec: Spec
    version: int
    mode: Literal["live", "test"]
    run_as_account_id: uuid.UUID | None


def has_code(spec: Spec) -> bool:
    """Any Code step, a For each's included."""
    return any(
        isinstance(n, CodeNode) or (isinstance(n, ForEachNode) and isinstance(n.do, CodeNode))
        for n in spec.nodes.values()
    )


def spend_user_id(person: SpendPerson, account_id: uuid.UUID) -> str:
    """The id the run-as person's step spend is gated and recorded under. An allowance
    counts what they spent under any of their ids, so any one of them serves."""
    return min(person.identities, default=str(account_id))


async def check(
    sm: async_sessionmaker[AsyncSession],
    settings: Settings,
    place: Place,
    *,
    now: datetime,
    budget_usd: Decimal | None = None,
    cost_usd: Decimal = Decimal(0),
    may_read: MayRead | None = None,
) -> Refusal | Cleared:
    """The first rule that stops this run or step. Steps pass the run's budget and spend."""
    async with sm() as session:
        tenant = await tenants.get_tenant(session, place.tenant_id)
        if tenant is None:
            return Refusal("workflows_off", "This team is gone.")
        channel = place.workflow.home_channel_id
        features = workflow_features(
            await resolve_features(
                sm, settings, FeaturePlace(tenant.id, tenant.platform, channel), keys=KEYS
            )
        )
        if not features.enabled:
            return Refusal("workflows_off", "Workflows are off here. Ask an admin to turn them on.")
        if not features.code and has_code(place.spec):
            return Refusal("code_off", "Code steps are off here. Ask an admin to turn them on.")
        if channel is not None:
            provider = await channel_provider(
                session, tenant_id=tenant.id, platform=tenant.platform, channel_id=channel
            )
            if provider not in (None, "anthropic"):
                return Refusal(
                    "backend_unsupported",
                    "Workflows run only on Anthropic. Switch the home channel back to Anthropic.",
                )
        account_id = place.run_as_account_id
        account = None if account_id is None else await accounts.get_account(session, account_id)
        if account_id is None or account is None or account.tenant_id != tenant.id:
            return Refusal("run_as_not_allowed", "The person this runs as is not on the team.")
        reads = may_read or member_reads(sm, settings, tenant.id)
        if (private := await private_reach(session, place, account_id, reads)) is not None:
            return Refusal(
                "private_channel",
                f"This workflow reaches {private}, which the person it runs as cannot read. "
                "Ask an admin.",
            )
        default = features.run_budget_usd
        if place.mode == "live" and (
            missing := await missing_grant(
                session, place.workflow.id, place.version, place.spec, float(default)
            )
        ):
            return Refusal("grant_needs_review", f"{missing} needs approval. Ask the owner.")
        if budget_usd is not None and cost_usd >= budget_usd:
            return Refusal("over_budget", f"The run spent its ${budget_usd} budget.")
        person = await spend_person_of_account(session, tenant_id=tenant.id, account_id=account_id)
        block = await spend_gate(
            session,
            request=SpendRequest(
                tenant_id=tenant.id,
                platform_user_id=spend_user_id(person, account_id),
                platform=tenant.platform,
                channel_id=channel,
                agent_id=None,
                person=person,
            ),
            markup=settings.billing.markup,
            now=now,
        )
        return Cleared(default) if block is None else Refusal(block, _GATE_MESSAGES[block])


def _reached(spec: Spec, home_channel_id: str | None) -> tuple[list[str], set[str]]:
    """The channels a workflow posts to (home and delivery targets) and its step agents."""
    home = spec.home.channel if spec.home is not None else home_channel_id
    channels = [home] if home else []
    agents = {spec.defaults.agent} if spec.defaults and spec.defaults.agent else set[str]()
    for node in spec.nodes.values():
        inner = node.do if isinstance(node, ForEachNode) else node
        if isinstance(inner, AgentNode) and inner.agent:
            agents.add(inner.agent)
        if isinstance(node, FinishNode) and node.deliver and node.deliver.to not in DM_TARGETS:
            target = target_for(node.deliver.to, home=home, mode="live", run_as=None, owner=None)
            if isinstance(target.get("channel_id"), str):
                channels.append(target["channel_id"])
    return channels, agents


#: Whether an account can read a channel (or thread) on the workspace's platform.
MayRead = Callable[[uuid.UUID, str], Awaitable[bool]]


def member_reads(
    sm: async_sessionmaker[AsyncSession], settings: Settings, tenant_id: uuid.UUID
) -> MayRead:
    """The platform's own answer, asked live with the account's id on the workspace's platform."""

    async def may_read(account_id: uuid.UUID, channel_id: str) -> bool:
        async with sm() as session:
            tenant = await tenants.get_tenant(session, tenant_id)
            ids = await identity.list_platform_principals_for_account(
                session, account_id=account_id
            )
        user_id = next(
            (p.external_id for p in ids if tenant and p.platform == tenant.platform), None
        )
        if tenant is None or user_id is None:
            return False
        keys = tuple(k.get_secret_value() for k in settings.crypto.keys)
        async with httpx.AsyncClient(timeout=10) as http:
            return await fetch_member_reads(
                http,
                settings=settings,
                sessionmaker=sm,
                fernet=build_multifernet(keys) if keys else None,
                platform=tenant.platform,
                workspace_id=tenant.external_id,
                user_id=user_id,
                channel_id=channel_id,
            )

    return may_read


async def private_reach(
    session: AsyncSession, place: Place, account_id: uuid.UUID, may_read: MayRead
) -> str | None:
    """The first channel with limited readers (inside or own, a narrow room included) that the
    workflow posts to or whose own agent it runs, when `account_id` cannot read it on the
    platform; None when there is none. The seal rule: admins are not exempt."""
    channels, agents = _reached(place.spec, place.workflow.home_channel_id)
    policy = await load_access_policy(session, tenant_id=place.tenant_id)
    names = [n for n in policy.agent_rules if slug(n) in agents or n in agents]
    sealed = [c for c in channels if effective_channel_rule(policy, c).readers != "any"]
    if (home := agent_permissions(policy, names).home) is not None:
        sealed.append(home)
    for channel in dict.fromkeys(sealed):
        if not await may_read(account_id, channel):
            return channel
    return None


async def missing_grant(
    session: AsyncSession, workflow_id: uuid.UUID, version: int, spec: Spec, budget: float
) -> str | None:
    """The first grant the spec needs that this version has no live, matching row for."""
    needed = validate_spec(spec, run_budget_usd=budget).grants
    if not needed:
        return None
    held = await workflow_runs.live_grants(session, workflow_id=workflow_id, version=version)
    for grant in needed:
        pinned = node_hash(spec.nodes[grant.node]) if grant.node in spec.nodes else None
        if not any(
            g.kind == grant.kind
            and g.node_id == grant.node
            and (not g.pins or g.pins.get("node_hash") in (None, pinned))
            for g in held
        ):
            return spec.nodes[grant.node].title or grant.node if grant.node else grant.kind
    return None
