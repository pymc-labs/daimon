"""The model a chat actually answers on, for its header."""

from __future__ import annotations

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.caller import WebCaller
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.channel_backend import BackendUnsupported, effective_thread_backend
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import agent_backends
from daimon.core.stores.domain import AgentBackendRevision, WebThreadRow
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

    A chat bound to a provider keeps it until its next message, so the header shows that
    provider's model, not the agent's newest setting. A binding that can't be read shows
    the agent's own model.
    """
    if agent is None:
        return None
    tenant_id = caller.person.tenant_id
    async with runtime.sessionmaker() as session:
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
