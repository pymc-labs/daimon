"""Administering an agent as a server admin: who owns it, and retiring or copying it."""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Sequence

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core import agent_ownership
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_editing import (
    AgentActor,
    AgentEditDeps,
    AgentEditError,
    reject_name_taken,
    system_agent_rejection,
)
from daimon.core.agent_fork import AgentCopy, copy_agent
from daimon.core.agent_lifecycle import archive_memory_store_best_effort, forget_archived_agent
from daimon.core.agent_ownership import agent_name_lock
from daimon.core.agent_target import AgentTarget, ChangeRecorder, load_target
from daimon.core.authz import Action, authorize
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mux_compat import archive_agent
from daimon.core.specs import AgentSpec
from daimon.core.stores import agent_backends
from mux.contracts.config import DEFAULT_BACKEND
from pydantic import ValidationError


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
    except agent_ownership.InactiveOwnerError as exc:
        raise AgentEditError("That person is not an active member.", kind="invalid") from exc
    return changed


async def archive(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    ma_agent_id: str,
    audited: ChangeRecorder | None = None,
) -> str:
    """Retire an agent and clear every place that named it; its name.

    A defaults-managed agent is refused, admins included.
    """
    target = await load_admin_target(
        deps, actor=actor, ma_agent_id=ma_agent_id, what="Archiving an agent"
    )
    agent = target.agent
    if system_agent_rejection(agent) is not None:
        raise AgentEditError(
            f"{agent.name} is managed by the server and can't be archived.", kind="forbidden"
        )
    name = _display_name(agent)
    await archive_agent(deps.client, agent.id, scope=actor.scope())
    await archive_memory_store_best_effort(
        anthropic=deps.client,
        sessionmaker=deps.session_factory,
        tenant_id=actor.tenant_id,
        agent_id=derive_agent_uuid(tenant_id=actor.tenant_id, ma_agent_id=agent.id),
        log_context={"tenant_id": str(actor.tenant_id), "agent_name": name},
    )
    await forget_archived_agent(
        deps.session_factory, tenant_id=actor.tenant_id, name=name, agent=agent
    )
    if audited is not None:
        async with deps.session_factory() as session, session.begin():
            await audited(session, name)
    return name


async def copy(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    ma_agent_id: str,
    new_name: str,
    audited: ChangeRecorder | None = None,
) -> AgentCopy:
    """Make a copy of an agent that its admin owns; one on another provider is refused."""
    target = await load_admin_target(
        deps, actor=actor, ma_agent_id=ma_agent_id, what="Copying an agent"
    )
    try:
        spec = AgentSpec(name=new_name, model=target.agent.model.id)
    except ValidationError as exc:
        raise AgentEditError("That name can't be used.", kind="invalid") from exc
    async with deps.session_factory() as session:
        revision = await agent_backends.current(session, actor.tenant_id, target.agent_id)
    if revision is not None and revision.backend not in (None, DEFAULT_BACKEND):
        raise AgentEditError(
            f"{target.agent.name} runs on another provider. Copy it from a chat.", kind="invalid"
        )
    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(
            agent_name_lock(deps.session_factory, tenant_id=actor.tenant_id, name=spec.name)
        )
        await reject_name_taken(deps, tenant_id=actor.tenant_id, name=spec.name)
        try:
            made = await copy_agent(
                deps.client,
                deps.session_factory,
                tenant_id=actor.tenant_id,
                source=target.agent,
                new_name=spec.name,
                public_url=deps.public_url,
                subject=actor.subject,
                default_agent_name=deps.deployment_default.agent_name,
                owner=actor.account_id,
            )
        except AgentEditError:
            raise
        except DaimonError as exc:
            raise AgentEditError(str(exc), kind="invalid") from exc
    if audited is not None:
        async with deps.session_factory() as session, session.begin():
            await audited(session, spec.name)
    return made


def _display_name(agent: BetaManagedAgentsAgent) -> str:
    return agent.metadata.get(MA_METADATA_KEY_NAME) or agent.name
