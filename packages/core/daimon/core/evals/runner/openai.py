"""OpenAI arms: each item's session runs in a self-hosted environment the provider makes for
it, served by `codex exec-server` in the item's pod. The pod starts when the session asks for
its environment, so its environment id is known. The fork is a provider agent whose MCP
tools are function tools the worker answers (`runner.tools`), so nothing dials an MCP server.

The turns' runtime is held here: no operator grant or stored deployment serves an eval
session."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal
from typing import Final

from daimon.core.pricing import provider_prices
from daimon.core.provider_keys import DEFAULT_KEY_ENV
from daimon.core.turn.hosted_codec import OPENAI
from daimon.core.turn.openai_host import OpenAIHostRuntime
from daimon.core.turn.openai_state import OpenAIRecoveryJournal, OpenAIUsageRevisions
from daimon.core.turn.prepare import ProviderPreparationRequest
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import ModelRef, ResourceRef, Revision, Scope
from mux.contracts.resources import AgentSpec, SessionSpec, ToolSpec
from mux.contracts.usage import UsageObservation
from mux.drivers.openai import (
    SELF_HOSTED,
    Context,
    OpenAIAgents,
    SessionControls,
    Transport,
    configured_transport,
)

#: The label eval records' provider refs carry; the key's own project applies.
_PROJECT: Final = "evals"


def _any(_scope: Scope, _kind: str, _id: str | None) -> bool:
    """The worker's own agents: each call names the item's scope."""
    return True


async def _no_infrastructure(_observation: UsageObservation) -> Decimal:
    """The pod is the worker's, priced by the run's sandbox rate."""
    return Decimal(0)


class OpenAIArms:
    """The worker's OpenAI side. `transport` defaults to the deployment's OpenAI key."""

    def __init__(self, transport: Transport | None = None) -> None:
        self._transport = transport

    @property
    def transport(self) -> Transport:
        if self._transport is None:
            key = os.environ.get(DEFAULT_KEY_ENV[OPENAI.provider], "")
            self._transport = configured_transport(api_key=key, project=None)
        return self._transport

    def _agents(self) -> OpenAIAgents:
        return OpenAIAgents(Context(self.transport, _PROJECT, OPENAI.profile, _any))

    async def agent(
        self, scope: Scope, name: str, model: str, system: str | None, tools: Sequence[ToolSpec]
    ) -> str:
        """The item's provider agent: the fork's prompt and the host tools, for eval only."""
        spec = AgentSpec(name=name, model=ModelRef(provider=OPENAI.provider, id=model),
                         system=system, tools=tuple(tools), purpose="eval")  # fmt: skip
        return (await self._agents().create(scope, spec, key=name)).ref.id

    async def delete(self, scope: Scope, agent_id: str) -> None:
        ref = ResourceRef(id=agent_id, kind="agent", provider=OPENAI.provider,
                          account_scope_id=_PROJECT, tenant_id=scope.tenant_id,
                          account_id=scope.account_id)  # fmt: skip
        await self._agents().delete(scope, ref, key=f"{agent_id}:delete")

    def runtime(
        self, revision: ConfigRevision, agent_id: str,
        connect: Callable[[str], Awaitable[None]],
    ) -> OpenAIHostRuntime:  # fmt: skip
        """The item's turns: its agent in a self-hosted environment `connect` serves."""
        tenant = revision.channel.tenant_id
        owned = {("agent", agent_id), ("environment", SELF_HOSTED)}

        async def plan(request: ProviderPreparationRequest) -> SessionSpec:
            def ref(id_: str, kind: str) -> ResourceRef:
                return ResourceRef(id=id_, kind=kind, provider=OPENAI.provider,
                                   account_scope_id=_PROJECT, tenant_id=request.scope.tenant_id,
                                   account_id=request.scope.account_id)  # fmt: skip

            return SessionSpec(agent=ref(agent_id, "agent"), agent_revision=Revision(local=0),
                               environment=ref(SELF_HOSTED, "environment"),
                               config_revision=revision.local)  # fmt: skip

        transport = self.transport
        return OpenAIHostRuntime(
            transport_factory=lambda _config, _scope: transport,
            journal=OpenAIRecoveryJournal(), usage_store=OpenAIUsageRevisions(),
            account_scope_id=_PROJECT, session_plan=plan,
            authorization=lambda scope, kind, id_: scope.tenant_id == tenant
            and (kind == "session" or (kind, id_) in owned),
            controls=SessionControls.model_validate({
                "model": revision.model, "multi_agent_enabled": False,
                "reasoning_effort": revision.effort,
            }),
            price=provider_prices(OPENAI.profile).get((OPENAI.provider, revision.model or "")),
            infrastructure=_no_infrastructure, connect_environment=connect,
        )  # fmt: skip
