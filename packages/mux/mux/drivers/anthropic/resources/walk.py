"""Typed full resource walks, retaining the native SDK's iterator lifecycle."""

from collections.abc import AsyncIterator
from typing import Protocol

from mux.contracts.ids import Page, Scope
from mux.contracts.resources import (
    Agent,
    AgentFilter,
    Environment,
    EnvironmentFilter,
    Skill,
    SkillVersion,
)
from mux.drivers.anthropic.resources.agents import AnthropicAgents
from mux.drivers.anthropic.resources.environments import AnthropicEnvironments
from mux.drivers.anthropic.resources.skills import AnthropicSkills, AnthropicSkillVersions


class ResourceWalk(Protocol):
    def agents(self, scope: Scope, *, filters: AgentFilter) -> AsyncIterator[Agent]: ...
    def environments(
        self, scope: Scope, *, filters: EnvironmentFilter
    ) -> AsyncIterator[Environment]: ...

    def skill_pages(self, scope: Scope, *, limit: int) -> AsyncIterator[Page[Skill]]: ...
    def skill_versions(
        self, scope: Scope, skill_id: str, *, limit: int | None = None
    ) -> AsyncIterator[SkillVersion]: ...


class AnthropicResourceWalk:
    def __init__(
        self,
        agents: AnthropicAgents,
        environments: AnthropicEnvironments,
        skills: AnthropicSkills,
        versions: AnthropicSkillVersions,
    ) -> None:
        self._agents = agents
        self._environments = environments
        self._skills = skills
        self._versions = versions

    def agents(self, scope: Scope, *, filters: AgentFilter) -> AsyncIterator[Agent]:
        return self._agents.walk(scope, filters=filters)

    def environments(
        self, scope: Scope, *, filters: EnvironmentFilter
    ) -> AsyncIterator[Environment]:
        return self._environments.walk(scope, filters=filters)

    def skill_pages(self, scope: Scope, *, limit: int) -> AsyncIterator[Page[Skill]]:
        return self._skills.pages(scope, limit=limit)

    def skill_versions(
        self, scope: Scope, skill_id: str, *, limit: int | None = None
    ) -> AsyncIterator[SkillVersion]:
        return self._versions.walk(scope, skill_id, limit=limit)
