"""Typed full resource walks, retaining the native SDK's iterator lifecycle."""

from collections.abc import AsyncIterator
from typing import Protocol

from mux.contracts.ids import Scope
from mux.contracts.resources import Agent, AgentFilter, Environment, EnvironmentFilter
from mux.drivers.anthropic.resources.agents import AnthropicAgents
from mux.drivers.anthropic.resources.environments import AnthropicEnvironments


class ResourceWalk(Protocol):
    def agents(self, scope: Scope, *, filters: AgentFilter) -> AsyncIterator[Agent]: ...
    def environments(
        self, scope: Scope, *, filters: EnvironmentFilter
    ) -> AsyncIterator[Environment]: ...


class AnthropicResourceWalk:
    def __init__(self, agents: AnthropicAgents, environments: AnthropicEnvironments) -> None:
        self._agents = agents
        self._environments = environments

    def agents(self, scope: Scope, *, filters: AgentFilter) -> AsyncIterator[Agent]:
        return self._agents.walk(scope, filters=filters)

    def environments(
        self, scope: Scope, *, filters: EnvironmentFilter
    ) -> AsyncIterator[Environment]:
        return self._environments.walk(scope, filters=filters)
