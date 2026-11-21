"""Harness arms: each item's harness runs in its own pod, and the worker drives it at the pod's
address with a token made for the item. The fork's MCP tools are host tools the harness pauses
on and the worker answers (`runner.tools`), so the pod never dials an MCP server.

The item's agent and environment are records of its turns alone, held here: no operator grant
or stored harness record serves an eval session."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final, cast

import httpx
from daimon.core.config import load_billing_settings
from daimon.core.mux_backend import TurnRuntime
from daimon.core.selfhosted_usage import selfhosted_prices
from daimon.core.turn.prepare import ProviderPreparationRequest
from daimon.core.turn.runtimes import RuntimeFactory
from daimon.core.turn.selfhosted_host import SelfHostedHostRuntime
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import ModelRef, ResourceRef, Revision, Scope
from mux.contracts.ports import Agents, Environments
from mux.contracts.resources import (
    Agent,
    AgentSpec,
    Environment,
    EnvironmentSpec,
    SessionSpec,
    ToolSpec,
)
from mux.drivers.selfhosted import DirectTransport
from mux.errors import ScopeViolation

PORT: Final = 8088
"""Where the pod's harness listens: the only port the sandbox opens inbound."""
PROVIDER: Final = "selfhosted"
_BUILTINS: Final = tuple(
    ToolSpec(name=n, kind="builtin") for n in ("bash", "read", "write", "edit")
)


class _Record[T: Agent | Environment]:
    """One agent or environment, served to the item's turns only."""

    def __init__(self, record: T) -> None:
        self._record = record

    async def retrieve(self, scope: Scope, ref: ResourceRef) -> T:
        if ref != self._record.ref or scope.tenant_id != ref.tenant_id:
            raise ScopeViolation(ref.id, "not the eval item's harness record")
        return self._record


class HarnessArms:
    """The worker's harness side. `client` makes each turn's HTTP client."""

    def __init__(self, client: Callable[[], httpx.AsyncClient] = httpx.AsyncClient) -> None:
        self._client = client

    def runtime(
        self, revision: ConfigRevision, name: str, system: str | None,
        tools: Sequence[ToolSpec], *, address: str, token: str,
    ) -> RuntimeFactory:  # fmt: skip
        """The item's turns: the fork's prompt, the harness's tools and the host tools."""
        model, tenant = revision.model, revision.channel.tenant_id
        assert model is not None
        price = selfhosted_prices(load_billing_settings().selfhosted).get((PROVIDER, model))
        if price is None:
            raise LookupError(f"the harness model {model!r} has no self-hosted price")
        now = datetime.now(UTC)

        def ref(kind: str) -> ResourceRef:
            return ResourceRef(id=f"sh_{kind}_{name}", kind=kind, provider=PROVIDER,
                               account_scope_id=tenant, tenant_id=tenant,
                               account_id=None)  # fmt: skip

        agent = Agent(ref=ref("agent"), revision=Revision(local=1), created_at=now, spec=AgentSpec(
            name=name, model=ModelRef(provider=PROVIDER, id=model), system=system,
            tools=(*_BUILTINS, *tools), purpose="eval",
        ))  # fmt: skip
        environment = Environment(ref=ref("environment"), revision=Revision(local=1),
                                  created_at=now, spec=EnvironmentSpec(
            name=name, execution="self_hosted", purpose="eval"))  # fmt: skip
        owned = (agent.ref.id, environment.ref.id)

        async def plan(_request: ProviderPreparationRequest) -> SessionSpec:
            return SessionSpec(agent=agent.ref, agent_revision=agent.revision,
                               environment=environment.ref,
                               config_revision=revision.local)  # fmt: skip

        def build(request: ProviderPreparationRequest) -> TurnRuntime:
            client = self._client()
            transport = DirectTransport(client, base_url=f"http://{address}:{PORT}", token=token,
                                        plain_http=True)  # fmt: skip
            return SelfHostedHostRuntime(
                transport_factory=lambda _config, _scope: transport, journal=None,
                usage_store=None, account_scope_id=tenant, session_plan=plan,
                agents=cast(Agents, _Record(agent)),
                environments=cast(Environments, _Record(environment)),
                authorization=lambda scope, kind, id_: scope.tenant_id == tenant
                and (kind == "session" or id_ in owned),
                model=model, price=price, infrastructure_usd=Decimal(0), finalize=client.aclose,
                deadline_s=max(1.0, (request.deadline - request.now()).total_seconds()),
            )  # fmt: skip

        return build
