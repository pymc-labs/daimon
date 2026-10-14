"""Provider-independent Add-skill orchestration.

A host edge owns authorization, provider resource access and its upload ledger.
The service preserves the guard/read/write order: refusals precede upload, and
an upload is recorded before attachment so a retry need not upload it again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from daimon.core.constants import AGENT_SKILL_CAP
from daimon.core.skills.ingest import SkillBundle, SkillIngestError


@dataclass(frozen=True)
class StoredSkillUpload:
    skill_id: str | None
    version: str | None
    content_hash: str
    source: str
    source_repo_url: str


@dataclass(frozen=True)
class UploadedSkill:
    id: str
    version: str | None


@dataclass(frozen=True)
class AddedSkill:
    name: str
    skill_id: str
    version: str | None
    action: Literal["created", "updated", "unchanged"]
    newly_attached: bool


class SkillAddHost[AgentT](Protocol):
    """An authorized, tenant-bound resource edge; never inferred from native ids.

    Implementations preserve provider-specific read/write fidelity and recheck
    the caller's policy on each fresh agent. Ledger fields are provider-neutral;
    only a concrete provider edge may translate them to legacy storage columns.
    """

    def attached_ids(self, agent: AgentT) -> frozenset[str]: ...
    def skill_count(self, agent: AgentT) -> int: ...
    async def load_upload(self, name: str) -> StoredSkillUpload | None: ...
    async def find_own_skill(self, name: str) -> str | None: ...
    async def refuse_mount_clash(self, agent: AgentT, name: str, own_id: str | None) -> None: ...
    async def retrieve_agent(self) -> AgentT: ...
    async def recheck(self, agent: AgentT) -> None: ...
    async def refuse_shared(self, agent: AgentT, name: str, skill_id: str) -> None: ...
    async def create_skill(self, bundle: SkillBundle) -> UploadedSkill: ...
    async def publish_skill(self, skill_id: str, bundle: SkillBundle) -> UploadedSkill: ...
    async def save_upload(self, bundle: SkillBundle, uploaded: UploadedSkill) -> None: ...
    async def attach(self, skill_id: str) -> bool: ...


async def add_skill[AgentT](
    host: SkillAddHost[AgentT], *, agent: AgentT, agent_name: str, bundle: SkillBundle
) -> AddedSkill:
    """Upload one checked bundle, then attach it with the host's fresh policy gate."""
    preview = bundle.preview
    existing = await host.load_upload(preview.name)
    if existing is not None and existing.source == "repo":
        raise SkillIngestError(
            f"'{preview.name}' on {agent_name} comes from the skill repo "
            f"{existing.source_repo_url}; change it there, or rename this one."
        )
    known_id = existing.skill_id if existing is not None else None
    count = host.skill_count(agent)
    if known_id not in host.attached_ids(agent) and count >= AGENT_SKILL_CAP:
        raise SkillIngestError(
            f"{agent_name} already has {count} skills, the most an agent may "
            "have. Remove one first."
        )
    action: Literal["created", "updated", "unchanged"]
    if (
        existing is not None
        and known_id is not None
        and existing.content_hash == preview.content_hash
    ):
        uploaded = UploadedSkill(known_id, existing.version)
        action = "unchanged"
    else:
        skill_id = known_id or await host.find_own_skill(preview.name)
        await host.refuse_mount_clash(agent, preview.name, skill_id)
        fresh = await host.retrieve_agent()
        await host.recheck(fresh)
        if skill_id is not None:
            await host.refuse_shared(fresh, preview.name, skill_id)
        if skill_id is None:
            uploaded = await host.create_skill(bundle)
            action = "created"
        else:
            uploaded = await host.publish_skill(skill_id, bundle)
            action = "updated"
    await host.save_upload(bundle, uploaded)
    attached = await host.attach(uploaded.id)
    return AddedSkill(preview.name, uploaded.id, uploaded.version, action, attached)
