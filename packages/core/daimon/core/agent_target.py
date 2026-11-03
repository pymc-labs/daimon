"""One agent as an actor sees it: its live record, ownership and how far setup is shown."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import anthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.agent_editing import (
    AgentActor,
    AgentEditDeps,
    AgentEditError,
    system_agent_rejection,
)
from daimon.core.agent_ownership import owns_agent
from daimon.core.defaults.metadata import MA_METADATA_KEY_SETUP_HIDDEN, MA_METADATA_KEY_TENANT
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mux_compat import retrieve_agent
from mux.contracts.errors import ScopeViolation
from sqlalchemy.ext.asyncio import AsyncSession

#: Runs inside a change's own transaction with the agent's name, so the audit row lands with it.
ChangeRecorder = Callable[[AsyncSession, str], Awaitable[None]]


@dataclass(frozen=True)
class AgentTarget:
    """A live tenant agent and what the actor is to it."""

    agent: BetaManagedAgentsAgent
    agent_id: uuid.UUID
    owned: bool
    managed: bool
    # Admins, and owners unless the agent is a member's copy with its setup withheld.
    sees_setup: bool


async def load_target(deps: AgentEditDeps, *, actor: AgentActor, ma_agent_id: str) -> AgentTarget:
    """The tenant's live agent ``ma_agent_id`` for ``actor``; not found when missing or foreign."""
    try:
        agent = await retrieve_agent(deps.client, ma_agent_id, scope=actor.scope())
    except (anthropic.NotFoundError, ScopeViolation) as exc:
        raise AgentEditError("No such agent.", kind="not_found") from exc
    if agent.archived_at is not None or agent.metadata.get(MA_METADATA_KEY_TENANT) != str(
        actor.tenant_id
    ):
        raise AgentEditError("No such agent.", kind="not_found")
    async with deps.session_factory() as session:
        owned = await owns_agent(
            session, tenant_id=actor.tenant_id, ma_agent_id=agent.id, account_id=actor.account_id
        )
    sees = actor.subject.is_admin or (
        owned and agent.metadata.get(MA_METADATA_KEY_SETUP_HIDDEN) != "true"
    )
    return AgentTarget(
        agent=agent,
        agent_id=derive_agent_uuid(tenant_id=actor.tenant_id, ma_agent_id=agent.id),
        owned=owned,
        managed=system_agent_rejection(agent) is not None,
        sees_setup=sees,
    )


def require_setup(target: AgentTarget, what: str) -> None:
    """Refuse unless the actor sees the agent's setup (an admin, or its owner)."""
    if not target.sees_setup:
        raise AgentEditError(f"{what} is for the agent's owner or an admin.", kind="forbidden")
