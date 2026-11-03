"""Administering an agent as a server admin: who owns it, and retiring or copying it."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from daimon.core import agent_ownership
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.agent_target import AgentTarget, ChangeRecorder, load_target
from daimon.core.authz import Action, Surface, authorize


def require_admin(actor: AgentActor, what: str) -> None:
    """Refuse unless the actor is a server admin (`Action.MANAGE_AGENTS`)."""
    decision = authorize(
        TenantAccessPolicy(),
        subject=actor.subject,
        action=Action.MANAGE_AGENTS,
        surface=actor.surface,
    )
    if not decision:
        raise AgentEditError(f"{what} needs an admin.", kind="forbidden")


async def load_admin_target(
    deps: AgentEditDeps, *, actor: AgentActor, ma_agent_id: str, what: str
) -> AgentTarget:
    """The agent, for an admin only."""
    require_admin(actor, what)
    return await load_target(deps, actor=actor, ma_agent_id=ma_agent_id)


async def set_owners(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    ma_agent_id: str,
    owner_account_id: uuid.UUID | None,
    co_owner_account_ids: Sequence[uuid.UUID],
    audited: ChangeRecorder | None = None,
) -> bool:
    """Make the owner and co-owners exactly these; whether anything changed."""
    target = await load_admin_target(
        deps, actor=actor, ma_agent_id=ma_agent_id, what="Changing owners"
    )
    try:
        async with deps.session_factory() as session, session.begin():
            changed = await agent_ownership.replace_agent_owners(
                session,
                tenant_id=actor.tenant_id,
                agent_id=target.agent_id,
                owner_account_id=owner_account_id,
                co_owner_account_ids=co_owner_account_ids,
                by_account_id=actor.account_id,
            )
            if changed and audited is not None:
                await audited(session, target.agent.name)
    except LookupError as exc:
        raise AgentEditError("That person is not in this workspace.", kind="invalid") from exc
    except agent_ownership.ForeignOwnerError as exc:
        raise AgentEditError(
            "A person from another organisation cannot own an agent.", kind="invalid"
        ) from exc
    return changed
