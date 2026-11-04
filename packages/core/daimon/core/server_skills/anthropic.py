"""The Managed Agents target: every Daimon agent's source record, which holds its skill pins.

OpenAI, Gemini and self-hosted runs derive their skills from the pinned source version
at turn start, so pinning it here is what moves them too.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import anthropic
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsSkillParams
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_TENANT
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import publish_skill_version, update_agent
from daimon.core.server_skills.gate import AttachedAgent
from daimon.core.server_skills.sync import TargetError
from daimon.core.skills.ingest import SkillBundle
from daimon.core.stores.domain import ServerSkillRow
from mux.contracts.errors import ProviderError, ScopeViolation

__all__ = ["PROVIDER", "ManagedAgentsTarget"]

PROVIDER: Final = "managed_agents"
_FAILURES = (anthropic.APIError, ProviderError, ScopeViolation)


def _params(
    agent: BetaManagedAgentsAgent, skill_id: str, version: str
) -> list[BetaManagedAgentsSkillParams]:
    out: list[BetaManagedAgentsSkillParams] = []
    for skill in agent.skills:
        if skill.type == "custom":
            pin = version if skill.skill_id == skill_id else skill.version
            out.append({"type": "custom", "skill_id": skill.skill_id, "version": pin})
        else:
            out.append({"type": skill.type, "skill_id": skill.skill_id, "version": skill.version})
    return out


@dataclass(frozen=True)
class ManagedAgentsTarget:
    client: AsyncAnthropic

    @property
    def provider(self) -> str:
        return PROVIDER

    async def publish(self, mirror: ServerSkillRow, bundle: SkillBundle) -> str:
        try:
            published = await publish_skill_version(
                self.client,
                mirror.skill_id,
                data=bundle.zip_bytes,
                scope=resource_scope(tenant_id=str(mirror.tenant_id)),
            )
        except _FAILURES as err:
            raise TargetError(f"publish refused ({type(err).__name__})") from err
        if published.version in ("", "latest"):
            raise TargetError("publish returned no concrete version")
        return published.version

    async def attached(self, mirror: ServerSkillRow) -> list[AttachedAgent]:
        try:
            agents = await list_agents_by_tenant(self.client, tenant_id=mirror.tenant_id)
        except _FAILURES as err:
            raise TargetError(f"agents unreadable ({type(err).__name__})") from err
        return [
            AttachedAgent(agent.id, skill.version)
            for agent in agents
            for skill in agent.skills
            if skill.type == "custom" and skill.skill_id == mirror.skill_id
        ]

    async def repin(self, mirror: ServerSkillRow, agent_id: str, version: str) -> None:
        scope = resource_scope(tenant_id=str(mirror.tenant_id))

        async def apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
            if fresh.archived_at is not None or (fresh.metadata or {}).get(
                MA_METADATA_KEY_TENANT
            ) != str(mirror.tenant_id):
                raise TargetError("agent is archived or outside the tenant")
            if not any(s.skill_id == mirror.skill_id for s in fresh.skills):
                return fresh
            return await update_agent(
                self.client,
                fresh.id,
                version=fresh.version,
                payload={"skills": _params(fresh, mirror.skill_id, version)},
                scope=scope,
            )

        try:
            await update_agent_with_version_retry(self.client, agent_id, apply, scope=scope)
        except _FAILURES as err:
            raise TargetError(f"repin refused ({type(err).__name__})") from err
