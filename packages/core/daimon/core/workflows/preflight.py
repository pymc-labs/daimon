"""What must hold before a run starts or a step spends: feature, backend, run-as, grants,
budget and the spend gate. A refusal names its reason; nothing is created or spent.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final, Literal

from daimon.core.channel_backend import channel_provider
from daimon.core.config import Settings
from daimon.core.features.snapshot import FeaturePlace, resolve_features
from daimon.core.spend_gate import (
    SpendPerson,
    SpendRequest,
    spend_gate,
    spend_person_of_account,
)
from daimon.core.stores import accounts, tenants, workflow_runs
from daimon.core.stores.workflows import WorkflowRow
from daimon.core.workflows.features import KEYS, workflow_features
from daimon.core.workflows.spec import CodeNode, ForEachNode, Spec, node_hash
from daimon.core.workflows.validate import validate_spec
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GATE_REASONS: Final = frozenset(
    {"balance_depleted", "budget_exceeded", "allowance_exhausted", "cap_exceeded"}
)
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
        default = features.run_budget_usd
        if place.mode == "live" and (missing := await _missing_grant(session, place, default)):
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


async def _missing_grant(session: AsyncSession, place: Place, budget: Decimal) -> str | None:
    """The first grant the spec needs that this version has no live, matching row for."""
    needed = validate_spec(place.spec, run_budget_usd=float(budget)).grants
    if not needed:
        return None
    held = await workflow_runs.live_grants(
        session, workflow_id=place.workflow.id, version=place.version
    )
    for grant in needed:
        pinned = node_hash(place.spec.nodes[grant.node]) if grant.node in place.spec.nodes else None
        if not any(
            g.kind == grant.kind
            and g.node_id == grant.node
            and (not g.pins or g.pins.get("node_hash") in (None, pinned))
            for g in held
        ):
            return place.spec.nodes[grant.node].title or grant.node if grant.node else grant.kind
    return None
