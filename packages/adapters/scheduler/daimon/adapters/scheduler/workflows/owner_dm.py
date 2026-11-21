"""`owner_dm` steps: a moved DM routine's fire, as a fresh private DM turn of the workflow's owner.

The same rules as the routine's own fire (`dm_open`, `owner_granted`, then `run_dm_routine`'s
admission and `owner_busy` fence); a busy owner skips the slot. Built-ins stay off until the
workflow's grant carries them. Discord only, as DM routines are.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from daimon.core.channel_audience import requester_standing
from daimon.core.config import Settings, env_admits
from daimon.core.direct_messages import DirectMessageAccessRevoked, run_dm_routine
from daimon.core.dm_access import roles_by_membership, sender_admits
from daimon.core.membership import MembershipResolver
from daimon.core.stores import workflows as store
from daimon.core.stores.direct_messages import dm_enabled
from daimon.core.stores.domain import TenantRow
from daimon.core.stores.identity import platform_external_ids
from daimon.core.stores.tenants import get_tenant
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.workflows import engine
from daimon.core.workflows.agent_step import writes_of
from daimon.core.workflows.lanes import agent_of
from daimon.core.workflows.runs import StepWork
from daimon.core.workflows.spec import AgentNode
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CLOSED = "Routine DMs are off here, or the owner may not use them."
#: The DM turn writes under the operator's routine policy, which only `writes: routine` grants.
ROUTINE_ONLY = "An owner DM step writes as a routine does, so it needs writes set to routine."


async def dm_open(
    session: AsyncSession, settings: Settings, *, tenant_id: uuid.UUID, platform: str, owner: str
) -> bool:
    """Whether a routine may DM its owner: Discord, routine DMs on, owner allowed."""
    return (
        platform == "discord"
        and env_admits(settings.direct_message_policies, tenant_id, owner)
        and await dm_enabled(session, tenant_id=tenant_id)
    )


async def owner_granted(
    sm: async_sessionmaker[AsyncSession],
    resolver: MembershipResolver | None,
    *,
    tenant: TenantRow,
    agent_name: str,
    owner: str,
) -> bool:
    """The agent's DM grant admits the owner, by their current server roles."""
    return await sender_admits(
        sm,
        tenant_id=tenant.id,
        agent_name=agent_name,
        user_id=owner,
        groups=roles_by_membership(resolver, tenant_id=tenant.id, guild_id=tenant.external_id),
    )


@dataclass(frozen=True)
class OwnerDmTurn:
    sm: async_sessionmaker[AsyncSession]
    settings: Settings
    deps: TurnDeps

    async def __call__(self, work: StepWork) -> engine.Outcome:
        node, tenant_id = work.node, work.run.tenant_id
        assert isinstance(node, AgentNode), "owner DM steps are agent nodes"
        if work.run.mode == "test":
            return engine.Succeeded(summary="Skipped: a test run sends no DM.", early=True)
        if writes_of(work) != "routine":
            return engine.Failed("fatal", ROUTINE_ONLY, reason="writes_refused")
        agent = agent_of(work.spec, node)
        async with self.sm() as s:
            tenant = await get_tenant(s, tenant_id)
            workflow = await store.get_workflow(
                s, tenant_id=tenant_id, workflow_id=work.run.workflow_id
            )
            account = None if workflow is None else workflow.owner_account_id
            if tenant is None or workflow is None or account is None or agent is None:
                return engine.Failed("fatal", CLOSED, reason="dm_closed")
            ids = await platform_external_ids(
                s, tenant_id=tenant_id, platform="discord", account_ids=[account]
            )
            owner = ids.get(account)
            if owner is None or not await dm_open(
                s, self.settings, tenant_id=tenant_id, platform=tenant.platform, owner=owner
            ):
                return engine.Failed("fatal", CLOSED, reason="dm_closed")
        resolver = self.deps.membership_resolver
        if not await owner_granted(self.sm, resolver, tenant=tenant, agent_name=agent, owner=owner):
            return engine.Failed("fatal", CLOSED, reason="dm_closed")
        try:
            async with self.sm() as s:
                standing = await requester_standing(
                    s, tenant_id=tenant_id, user_id=owner, is_dm=True, resolver=resolver
                )
            reply = await run_dm_routine(
                self.deps,
                requester_org=standing,
                tenant_id=tenant_id,
                owner_user_id=owner,
                agent_name=agent,
                prompt=node.instructions,
                source=None,
                budget_channel_id=workflow.home_channel_id,
            )
        except (AdmissionDenied, DirectMessageAccessRevoked) as error:
            if error.reason == "owner_busy":
                return engine.Succeeded(summary="Skipped: the owner was busy.", early=True)
            return engine.Failed("fatal", CLOSED, reason=str(error.reason))
        return engine.Succeeded(output=reply.text, summary=reply.text)
