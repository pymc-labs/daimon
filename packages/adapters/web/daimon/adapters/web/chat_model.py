"""The model a chat actually answers on, for its header."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.caller import WebCaller
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.channel_backend import BackendUnsupported, effective_thread_backend
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import agent_backends
from daimon.core.stores.domain import AgentBackendRevision, WebThreadRow
from daimon.core.stores.thread_models import get_thread_choice, get_thread_choices
from daimon.core.web_identity import place_of, thread_key
from mux.contracts.config import DEFAULT_BACKEND
from mux.contracts.ids import Provider
from mux.errors import ScopeViolation
from mux.profiles import backend_selection


def model_on(
    agent: BetaManagedAgentsAgent, provider: Provider, revision: AgentBackendRevision | None
) -> str:
    """The model id the agent answers with on `provider`. Pure.

    Claude is the agent's own record. Another provider runs the model the agent was last
    pointed at there, else that provider's default.
    """
    if provider == DEFAULT_BACKEND:
        return agent.model.id
    if revision is not None and revision.backend == provider and revision.model:
        return revision.model
    return backend_selection(provider).model or ""


async def thread_model(
    runtime: WebRuntime,
    caller: WebCaller,
    thread: WebThreadRow,
    agent: BetaManagedAgentsAgent | None,
) -> str | None:
    """The model this chat's next message runs on, or None for an agent that is gone.

    A model someone picked for this chat comes first. A chat bound to a provider keeps it
    until its next message, so the header shows that provider's model, not the agent's
    newest setting. A binding that can't be read shows the agent's own model.
    """
    if agent is None:
        return None
    tenant_id = caller.person.tenant_id
    async with runtime.sessionmaker() as session:
        chosen = await get_thread_choice(
            session, tenant_id=tenant_id, platform="web", thread_id=thread_key(thread.id)
        )
        if chosen is not None:
            return chosen.id
        try:
            provider = await effective_thread_backend(
                session,
                tenant_id=tenant_id,
                platform="web",
                channel_id=place_of(caller.person.id),
                thread_id=thread_key(thread.id),
                caller_id=str(caller.person.id),
            )
        except (ScopeViolation, BackendUnsupported):
            return agent.model.id
        if provider == DEFAULT_BACKEND:
            return agent.model.id
        revisions = await agent_backends.current_many(
            session, tenant_id, [derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.id)]
        )
    return model_on(agent, provider, next(iter(revisions.values()), None))


async def listed_models(
    runtime: WebRuntime,
    caller: WebCaller,
    threads: Sequence[WebThreadRow],
    agents: Mapping[uuid.UUID, BetaManagedAgentsAgent | None],
) -> dict[uuid.UUID, str | None]:
    """`thread_model` for a page of chats in two reads, for the list.

    A pick comes first, else the model its agent is pointed at now; a chat still bound to
    an earlier provider shows that one once it is opened (`thread_model`).
    """
    tenant_id = caller.person.tenant_id
    live = [a for a in agents.values() if a is not None]
    async with runtime.sessionmaker() as session:
        picks = await get_thread_choices(
            session,
            tenant_id=tenant_id,
            platform="web",
            thread_ids=[thread_key(t.id) for t in threads],
        )
        revisions = await agent_backends.current_many(
            session,
            tenant_id,
            [derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=a.id) for a in live],
        )
    models: dict[uuid.UUID, str | None] = {}
    for thread in threads:
        agent = agents[thread.id]
        if agent is None:
            models[thread.id] = None
        elif (pick := picks.get(thread_key(thread.id))) is not None:
            models[thread.id] = pick.id
        else:
            revision = revisions.get(derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.id))
            models[thread.id] = model_on(
                agent, (revision and revision.backend) or DEFAULT_BACKEND, revision
            )
    return models
