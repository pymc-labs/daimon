"""Gemini arms: each item runs in a self-hosted environment of its own, on a runtime serving
its fork. The provider shows an environment's key once, at creation, so the environment is
made before the item's pod, which polls it with that key; the session runs there."""

from __future__ import annotations

import os
from collections.abc import Sequence
from decimal import Decimal
from types import MappingProxyType
from typing import Final, Protocol, cast

from daimon.core import pricing
from daimon.core.mux_backend import TurnRuntime
from daimon.core.provider_keys import DEFAULT_KEY_ENV
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.gemini import (
    PROFILE,
    GeminiDeployment,
    GeminiJournal,
    GeminiTransportRuntime,
    GeminiUsageRuntime,
)
from daimon.core.turn.gemini_state import PostgresFlashAccounting, PostgresGeminiStorage
from daimon.core.turn.prepare import ProviderPreparationRequest
from daimon.core.turn.runtimes import RuntimeFactory
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import ModelRef, Scope
from mux.contracts.resources import AgentSpec, Environment, EnvironmentSpec, ToolSpec
from mux.drivers.gemini import (
    EnvironmentTransport,
    GeminiManagedAgents,
    SelfHostedTransport,
    Transport,
    configured_transport,
    self_hosted_id,
)
from mux.profiles import get_profile
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: The provider project eval records are kept under.
_PROJECT: Final = "evals"
_PROVIDER: Final = get_profile(PROFILE).provider
#: Held per model attempt, and the most an item's attempts may hold at once; each run's
#: cap bounds what items spend.
_HOLD_USD, _CAP_USD = Decimal("0.05"), Decimal("1")


class EvalTransport(Transport, EnvironmentTransport, SelfHostedTransport, Protocol):
    """A transport that also makes self-hosted environments and deletes them."""


class GeminiArms:
    """The worker's Gemini side. `transport` defaults to the deployment's Gemini key."""

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], transport: EvalTransport | None = None
    ) -> None:
        self._sessions, self._transport = sessions, transport

    @property
    def transport(self) -> EvalTransport:
        if self._transport is None:
            key = os.environ.get(DEFAULT_KEY_ENV[_PROVIDER], "")
            self._transport = cast(EvalTransport, configured_transport(api_key=key))
        return self._transport

    def _storage(self, scope: Scope) -> PostgresGeminiStorage:
        return PostgresGeminiStorage(self._sessions, scope=scope, project=_PROJECT)

    async def environment(self, scope: Scope, name: str) -> tuple[Environment, str]:
        """A new self-hosted eval environment of `scope` (the requester's), and its key."""
        backend = GeminiManagedAgents(
            self.transport, storage=self._storage(scope),
            state_store=PostgresStateStore(self._sessions), account_scope_id=_PROJECT,
        )  # fmt: skip
        spec = EnvironmentSpec(name=name, execution="self_hosted", purpose="eval")
        made = await backend.environments.create(scope, spec, key=name)
        native = made.native if isinstance(made.native, dict) else {}
        return made.model_copy(update={"native": {"id": native["id"]}}), str(
            native["environment_key"]
        )

    async def delete(self, environment: Environment) -> None:
        await self.transport.delete_environment(self_hosted_id(environment))

    def runtime(
        self, revision: ConfigRevision, name: str, system: str | None,
        tools: Sequence[ToolSpec], environment: Environment,
    ) -> RuntimeFactory:  # fmt: skip
        """The item's turns: the fork's prompt and tools, run in its environment."""
        assert revision.model is not None
        deployment = GeminiDeployment(
            revision.digest, revision.local, _PROJECT,
            AgentSpec(name=name, model=ModelRef(provider=_PROVIDER, id=revision.model),
                      system=system, tools=tuple(tools), purpose="eval"),
            environment.spec, self_hosted=environment,
        )  # fmt: skip
        prices = MappingProxyType(
            {model: price for (_, model), price in pricing.provider_prices(PROFILE).items()}
        )
        transport = GeminiTransportRuntime(self.transport)

        def build(request: ProviderPreparationRequest) -> TurnRuntime:
            assert request.deps.state_store is not None, "channel runtimes carry a state store"
            accounting = PostgresFlashAccounting(
                self._sessions, scope=request.scope, project=_PROJECT,
                hold_usd=_HOLD_USD, cap_usd=_CAP_USD,
            )  # fmt: skip
            return TurnRuntime(
                transport_factory=lambda _config, _scope: transport,
                journal=GeminiJournal(
                    self._storage(request.scope), request.deps.state_store, deployment
                ),  # fmt: skip
                usage_store=GeminiUsageRuntime(accounting, prices, Decimal(0)),
            )

        return build
