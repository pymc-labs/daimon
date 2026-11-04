"""The Managed Agents target: the agent record the web editor edits, which holds skill pins.

An agent switched to OpenAI or Gemini runs from this record under a consent pinned to
it, so its repin goes through `ConsentKeeper`: renewed when the declared drops stay the
same, held otherwise. Those providers get the skill only through an operator's source
representation; self-hosted channels run their own spec. `tests/parity` records both.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import anthropic
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsSkillParams
from daimon.core.channel_backend import AgentBackendRefused
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_TENANT
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import publish_skill_version, update_agent
from daimon.core.server_skills.consent import ConsentKeeper
from daimon.core.server_skills.gate import AttachedAgent
from daimon.core.server_skills.sync import TargetError
from daimon.core.skills.ingest import SkillBundle
from daimon.core.stores.domain import ServerSkillRow
from mux.contracts.errors import ProviderError, ScopeViolation

__all__ = ["PROVIDER", "ManagedAgentsTarget"]

PROVIDER: Final = "managed_agents"
# An agent the backend service can't read is gone or moved: retried, never held.
_FAILURES = (anthropic.APIError, ProviderError, ScopeViolation, AgentBackendRefused)


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
    consents: ConsentKeeper | None = None
    """None only where no agent can carry a backend consent (backend choice is off)."""

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
            consented = (
                await self.consents.check(mirror, agent_id, version)
                if self.consents is not None
                else None
            )
            await update_agent_with_version_retry(self.client, agent_id, apply, scope=scope)
            if consented is not None and self.consents is not None:
                await self.consents.renew(mirror, agent_id, consented)
        except _FAILURES as err:
            raise TargetError(f"repin refused ({type(err).__name__})") from err
