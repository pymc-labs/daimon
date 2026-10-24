"""Explicit self-hosted driver composition; the host supplies resource ports.

`__all__` is the host-facing API. The `daimon-harness/0` wire format stays a
public submodule, `mux.drivers.selfhosted.wire`, with its own `__all__`: the
host, the harness and the conformance kit all build and read those models, and
re-exporting them here would give each a second path.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from mux.contracts.admission import Admission, admit
from mux.contracts.config import ConfigRevision
from mux.contracts.ports import Agents, Artifacts, Environments, ManagedAgents, Skills
from mux.contracts.profile import Profile
from mux.drivers.selfhosted.config import (
    SESSION_POOL_AUDIENCE,
    HarnessConfig,
    validate_resource_spec,
)
from mux.drivers.selfhosted.context import Authorization, BindingLookup, Context
from mux.drivers.selfhosted.events import (
    HighWater,
    SelfHostedEvents,
    SkillResolver,
    ToolsResolver,
    TurnTools,
    agent_tool_policy,
)
from mux.drivers.selfhosted.models import SelfHostedModels
from mux.drivers.selfhosted.sessions import SelfHostedSessions
from mux.drivers.selfhosted.transport import (
    DirectTransport,
    FoundryInvocationsTransport,
    TokenProvider,
    Transport,
)
from mux.drivers.selfhosted.usage import SelfHostedUsage
from mux.errors import UnsupportedCapability
from mux.profiles.selfhosted import LOOP

__all__ = [
    "SESSION_POOL_AUDIENCE",
    "Authorization",
    "BindingLookup",
    "Context",
    "DirectTransport",
    "FoundryInvocationsTransport",
    "HarnessConfig",
    "HighWater",
    "SelfHostedDriver",
    "SelfHostedEvents",
    "SelfHostedModels",
    "SelfHostedSessions",
    "SelfHostedUsage",
    "SkillResolver",
    "TokenProvider",
    "ToolsResolver",
    "Transport",
    "TurnTools",
    "agent_tool_policy",
    "validate_resource_spec",
]


class SelfHostedDriver:
    """One authorized harness environment and injected, tenant-scoped resources."""

    def __init__(
        self,
        transport: Transport,
        *,
        account_scope_id: str,
        agents: Agents,
        environments: Environments,
        authorization: Authorization,
        binding_lookup: BindingLookup | None = None,
        model: str | None = None,
        deployment: str | None = None,
        max_tool_calls: int = 100,
        deadline_s: float = 300,
        skill_resolver: SkillResolver | None = None,
        tools_resolver: ToolsResolver | None = None,
        high_water: HighWater | None = None,
    ) -> None:
        self._profile = LOOP
        self.agents, self.environments = agents, environments
        context = Context(
            transport, account_scope_id, agents, environments, authorization, binding_lookup
        )
        self.sessions = SelfHostedSessions(context)
        self.events = SelfHostedEvents(
            context,
            self.sessions,
            model=model,
            deployment=deployment,
            max_tool_calls=max_tool_calls,
            deadline_s=deadline_s,
            skill_resolver=skill_resolver,
            tools_resolver=tools_resolver,
            high_water=high_water,
        )
        self.sessions.events = self.events
        self.usage = SelfHostedUsage(self.events)
        self.models = SelfHostedModels((deployment or model or "gpt-6.1-sol",))

    @property
    def artifacts(self) -> Artifacts:
        raise UnsupportedCapability(("artifacts",), LOOP.profile_id)

    @property
    def skills(self) -> Skills:
        raise UnsupportedCapability(("skills_bundle",), LOOP.profile_id)

    def capabilities(self) -> Profile:
        return self._profile

    def admit(self, config: ConfigRevision) -> Admission:
        return admit(config, self.capabilities())

    def extension[T](self, port: type[T], *, namespace: str, version: int) -> T:
        raise UnsupportedCapability((namespace,), LOOP.profile_id)

    async def aclose(self) -> None:
        await self.events.aclose()


if TYPE_CHECKING:
    _checked_factory: Callable[..., ManagedAgents] = SelfHostedDriver
