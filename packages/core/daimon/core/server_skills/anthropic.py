"""The Managed Agents target: the agent record the web editor edits, which holds skill pins.

An agent switched to OpenAI or Gemini runs from this record too, so a repin reaches it on
its next turn. Those providers run without custom skills unless an operator's source
representation maps them; self-hosted channels run their own spec. `tests/parity` records
both. Agents the defaults manage are never repinned.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final

import anthropic
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsSkillParams
from daimon.core.agent_editing import system_agent_rejection
from daimon.core.channel_backend import AgentBackendRefused
from daimon.core.defaults.ma_index import download_skill_version, list_agents_by_tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_TENANT
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import list_skill_versions, publish_skill_version, update_agent
from daimon.core.server_skills.gate import AttachedAgent, manifest_digest
from daimon.core.server_skills.sync import TargetError
from daimon.core.skills.ingest import SkillBundle, SkillIngestError, retained_archive_files
from daimon.core.stores.domain import ServerSkillRow
from mux.contracts.errors import ProviderError, ScopeViolation
from mux.contracts.ids import Scope

__all__ = ["PROVIDER", "ManagedAgentsTarget"]

log = structlog.get_logger(__name__)

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

    @property
    def provider(self) -> str:
        return PROVIDER

    async def publish(self, mirror: ServerSkillRow, bundle: SkillBundle) -> str:
        scope = resource_scope(tenant_id=str(mirror.tenant_id))
        if (found := await self._published(mirror, scope)) is not None:
            return found
        try:
            published = await publish_skill_version(
                self.client, mirror.skill_id, data=bundle.zip_bytes, scope=scope
            )
        except _FAILURES as err:
            raise TargetError(f"publish refused ({type(err).__name__})") from err
        if published.version in ("", "latest"):
            raise TargetError("publish returned no concrete version")
        return published.version

    async def _published(self, mirror: ServerSkillRow, scope: Scope) -> str | None:
        """The newest version when it already holds the mirror's files.

        Only the sync publishes to a mirror, under its lock, so that is a publish whose
        record a crash lost; reusing it keeps a retry from publishing the files twice.
        """
        try:
            listed = [
                v
                async for v in list_skill_versions(
                    self.client, mirror.skill_id, limit=100, scope=scope
                )
            ]
            if not listed:
                return None
            newest = max(listed, key=lambda v: v.created_at).version
            data = await download_skill_version(
                self.client, skill_id=mirror.skill_id, version=newest, scope=scope
            )
            files = retained_archive_files(data)
        except (*_FAILURES, SkillIngestError) as err:
            log.warning(
                "server_skill.published_unreadable",
                mirror_id=str(mirror.id),
                err_type=type(err).__name__,
            )
            return None
        held = {path: hashlib.sha256(body).hexdigest() for path, body in files.items()}
        return newest if manifest_digest(held) == mirror.digest else None

    async def attached(self, mirror: ServerSkillRow) -> list[AttachedAgent]:
        try:
            agents = await list_agents_by_tenant(self.client, tenant_id=mirror.tenant_id)
        except _FAILURES as err:
            raise TargetError(f"agents unreadable ({type(err).__name__})") from err
        return [
            AttachedAgent(agent.id, skill.version)
            for agent in agents
            if system_agent_rejection(agent) is None
            for skill in agent.skills
            if skill.type == "custom" and skill.skill_id == mirror.skill_id
        ]

    async def _pin(self, mirror: ServerSkillRow, agent_id: str, version: str) -> None:
        """Pin the agent's copy of the skill to `version`."""
        scope = resource_scope(tenant_id=str(mirror.tenant_id))

        async def apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
            if fresh.archived_at is not None or (fresh.metadata or {}).get(
                MA_METADATA_KEY_TENANT
            ) != str(mirror.tenant_id):
                raise TargetError("agent is archived or outside the tenant")
            if system_agent_rejection(fresh) is not None:
                raise TargetError("agent is managed by settings")
            if not any(s.skill_id == mirror.skill_id for s in fresh.skills):
                return fresh
            return await update_agent(
                self.client,
                fresh.id,
                version=fresh.version,
                payload={"skills": _params(fresh, mirror.skill_id, version)},
                scope=scope,
            )

        await update_agent_with_version_retry(self.client, agent_id, apply, scope=scope)

    async def repin(self, mirror: ServerSkillRow, agent_id: str, version: str) -> None:
        try:
            await self._pin(mirror, agent_id, version)
        except _FAILURES as err:
            raise TargetError(f"repin refused ({type(err).__name__})") from err
