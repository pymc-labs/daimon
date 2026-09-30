"""Add one checked skill to one agent: fetch it, upload it agent-scoped, attach it.

The shell half of `daimon.core.skills.ingest`. An added skill is always titled
`{t8}-{agent}/{name}`, so it can never push a version onto a seeded or shared
library skill (`{t8}-{name}`); a name that would mount beside one of those is
refused. Its `user_skills` row is `source="upload"` under the agent's derived
identity, so a later skill-repo sync keeps it attached but never replaces it.

Who may add a skill is the caller's decision (`operation_policy`'s
`skill_add`); this module only refuses what no caller may do.
"""

from __future__ import annotations

import asyncio
import io
import shutil
import uuid
from pathlib import Path
from typing import Literal

import httpx
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsSkillParams
from daimon.core.constants import AGENT_SKILL_CAP
from daimon.core.defaults.ma_index import (
    find_attach_mount_collision,
    find_conflicting_skill_mount,
    find_skill_by_display_title,
)
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.defaults.spec_merge import merge_skills_with_ma
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.skill_zip import MAX_FILES, MAX_UNCOMPRESSED_BYTES
from daimon.core.skills.fetch import fetch_repo
from daimon.core.skills.ingest import SkillBundle, SkillIngestError, bundle_from_files
from daimon.core.specs import merge_default_agent_toolset
from daimon.core.stores.user_skills import load_user_skill, upsert_user_skill
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["SkillAddResult", "add_agent_skill", "fetch_attachment", "fetch_repo_skill"]

_log = structlog.get_logger(__name__)


class SkillAddResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    skill_id: str
    version: str | None
    action: Literal["created", "updated", "unchanged"]
    newly_attached: bool


async def fetch_attachment(http_client: httpx.AsyncClient, url: str) -> bytes:
    """Download a chat attachment the caller already allowlisted, capped, no redirects."""
    async with http_client.stream("GET", url, follow_redirects=False) as response:
        response.raise_for_status()
        body = io.BytesIO()
        async for chunk in response.aiter_bytes():
            if body.tell() + len(chunk) > MAX_UNCOMPRESSED_BYTES:
                raise SkillIngestError(f"A skill may hold at most {MAX_UNCOMPRESSED_BYTES} bytes.")
            body.write(chunk)
    return body.getvalue()


async def fetch_repo_skill(
    http_client: httpx.AsyncClient,
    *,
    url: str,
    branch: str,
    path: str,
    token: str | None,
    max_tarball_bytes: int,
    max_tarball_decompressed_bytes: int,
) -> SkillBundle:
    """One skill from a GitHub repo: the folder at `path`, or the repo's only skill."""
    fetched = await fetch_repo(
        http_client,
        url,
        branch=branch,
        token=token,
        max_tarball_bytes=max_tarball_bytes,
        max_tarball_decompressed_bytes=max_tarball_decompressed_bytes,
    )
    try:
        files = await asyncio.to_thread(_read_repo_skill, fetched.path, path)
    finally:
        shutil.rmtree(fetched.cleanup_dir, ignore_errors=True)
    return bundle_from_files(files)


def _read_repo_skill(repo_root: Path, path: str) -> dict[str, bytes]:
    root = (repo_root / path).resolve() if path else repo_root.resolve()
    if not root.is_relative_to(repo_root.resolve()) or not root.is_dir():
        raise SkillIngestError(f"{path!r} is not a folder in that repository.")
    if not (root / "SKILL.md").is_file():
        found = sorted(p.parent for p in root.rglob("SKILL.md") if not p.is_symlink())
        if len(found) != 1:
            listed = ", ".join(p.relative_to(root).as_posix() for p in found[:5])
            raise SkillIngestError(
                f"No SKILL.md under {path or 'the repository root'}."
                if not found
                else f"That holds {len(found)} skills ({listed}); pass the path of one."
            )
        root = found[0]
    files: dict[str, bytes] = {}
    total = 0
    for file in sorted(root.rglob("*")):
        rel = file.relative_to(root).as_posix()
        if file.is_symlink():
            raise SkillIngestError(f"{rel} is a symlink; skills may not hold links.")
        if not file.is_file():
            continue
        total += file.stat().st_size
        if len(files) >= MAX_FILES or total > MAX_UNCOMPRESSED_BYTES:
            raise SkillIngestError(
                f"A skill may hold at most {MAX_FILES} files and {MAX_UNCOMPRESSED_BYTES} bytes."
            )
        files[rel] = file.read_bytes()
    return files


async def add_agent_skill(
    client: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent: BetaManagedAgentsAgent,
    agent_name: str,
    bundle: SkillBundle,
    origin: str,
    added_by_account_id: uuid.UUID | None,
) -> SkillAddResult:
    """Upload `bundle` as `agent`'s own skill and attach it. Re-adding updates it.

    Everything that can refuse runs before the upload, so a refusal changes
    nothing. An attach that still fails afterwards leaves the upload recorded;
    adding the same skill again attaches it without uploading twice.
    """
    preview = bundle.preview
    ledger_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.id)
    async with sessionmaker() as session:
        existing = await load_user_skill(
            session,
            tenant_id=tenant_id,
            principal_id=ledger_id,
            agent_name=agent_name,
            name=preview.name,
        )
    if existing is not None and existing.source == "repo":
        raise SkillIngestError(
            f"'{preview.name}' on {agent_name} comes from the skill repo "
            f"{existing.source_repo_url}; change it there, or rename this one."
        )
    attached_ids = {skill.skill_id for skill in agent.skills}
    known_id = existing.anthropic_id if existing is not None else None
    if known_id not in attached_ids and len(agent.skills) >= AGENT_SKILL_CAP:
        raise SkillIngestError(
            f"{agent_name} already has {len(agent.skills)} skills, the most an agent may "
            "have. Remove one first."
        )

    action: Literal["created", "updated", "unchanged"]
    if (
        existing is not None
        and known_id is not None
        and existing.content_hash == preview.content_hash
    ):
        skill_id, version, action = known_id, existing.anthropic_latest_version, "unchanged"
    else:
        skill_id = known_id or await _find_own_skill(
            client, tenant_id=tenant_id, agent_name=agent_name, name=preview.name
        )
        if skill_id is None:
            created = await client.beta.skills.create(
                display_title=tenant_scoped_display_title(
                    tenant_id=tenant_id, name=preview.name, agent_name=agent_name
                ),
                files=[("SKILL.zip", io.BytesIO(bundle.zip_bytes), "application/zip")],
            )
            skill_id, version, action = created.id, created.latest_version, "created"
        else:
            pushed = await client.beta.skills.versions.create(
                skill_id=skill_id,
                files=[("SKILL.zip", io.BytesIO(bundle.zip_bytes), "application/zip")],
            )
            version, action = pushed.version, "updated"

    async with sessionmaker.begin() as session:
        await upsert_user_skill(
            session,
            tenant_id=tenant_id,
            principal_id=ledger_id,
            agent_name=agent_name,
            name=preview.name,
            source_repo_url="",
            source_repo_branch="",
            source_path="",
            content_hash=preview.content_hash,
            anthropic_id=skill_id,
            anthropic_latest_version=version,
            source="upload",
            origin=origin,
            added_by_account_id=added_by_account_id,
        )
    newly_attached = await _attach(
        client, tenant_id=tenant_id, agent_id=agent.id, skill_id=skill_id
    )
    _log.info(
        "skill_upload.added",
        tenant_id=str(tenant_id),
        agent_name=agent_name,
        name=preview.name,
        action=action,
        newly_attached=newly_attached,
    )
    return SkillAddResult(
        name=preview.name,
        skill_id=skill_id,
        version=version,
        action=action,
        newly_attached=newly_attached,
    )


async def _find_own_skill(
    client: AsyncAnthropic, *, tenant_id: uuid.UUID, agent_name: str, name: str
) -> str | None:
    """This agent's existing skill of that name, after refusing a shared-name clash.

    A shared or seeded skill with the same name would mount at the same path
    and break the agent's sessions. An agent-scoped skill of the same title is
    this agent's own (an earlier upload whose row was removed), so it is
    versioned rather than duplicated.
    """
    conflict = await find_conflicting_skill_mount(
        client, tenant_id=tenant_id, name=name, agent_name=agent_name
    )
    if conflict is not None:
        raise SkillIngestError(
            f"'{name}' is already a shared or built-in skill here, and the two would "
            f"clash on the agent. Rename this one (e.g. {name}-2)."
        )
    title = tenant_scoped_display_title(tenant_id=tenant_id, name=name, agent_name=agent_name)
    own = await find_skill_by_display_title(client, title, on_truncation="raise")
    return own.id if own is not None else None


async def _attach(
    client: AsyncAnthropic, *, tenant_id: uuid.UUID, agent_id: str, skill_id: str
) -> bool:
    attached = False

    async def _apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
        nonlocal attached
        if any(skill.skill_id == skill_id for skill in fresh.skills):
            return fresh
        wanted: list[BetaManagedAgentsSkillParams] = [{"type": "custom", "skill_id": skill_id}]
        merged = merge_skills_with_ma(wanted, fresh)
        if len(merged) > AGENT_SKILL_CAP:
            raise SkillIngestError(
                f"Uploaded, but not attached: the agent would have {len(merged)} skills and "
                f"the limit is {AGENT_SKILL_CAP}. Remove one, then add this again."
            )
        collision = await find_attach_mount_collision(client, tenant_id=tenant_id, skills=merged)
        if collision is not None:
            raise SkillIngestError(f"Uploaded, but not attached: {collision}")
        attached = True
        if any(tool.type == "agent_toolset_20260401" for tool in fresh.tools):
            return await client.beta.agents.update(fresh.id, version=fresh.version, skills=merged)
        # A skill needs the base toolset's read tool, or sessions fail to start.
        tools = merge_default_agent_toolset(
            [tool.model_dump(mode="json", exclude_none=True) for tool in fresh.tools]  # type: ignore[arg-type]  # dumped dicts satisfy the Tool TypedDict shape
        )
        return await client.beta.agents.update(
            fresh.id,
            version=fresh.version,
            skills=merged,
            tools=tools,  # type: ignore[arg-type]  # list[dict] satisfies list[Tool] at runtime
        )

    await update_agent_with_version_retry(client, agent_id, _apply)
    return attached
