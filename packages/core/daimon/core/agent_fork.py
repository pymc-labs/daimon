"""Copy an agent under a new name: the one copy behind every `fork_agent`.

The copy takes the source's prompt, model, tools, MCP definitions and skills
from its live MA state, with the default daimon MCP server and base toolset
guaranteed and the credential guidance applied. It starts with no credentials
(`agent_lifecycle.strip_credentialed_mcp_servers`). Skills scoped to the
source are uploaded again under the copy's own name, so the two never share a
skill id; skills scoped to another agent, and any copy that fails, are left
off and named. Its face starts rendering as soon as it exists, so its first
post has it. Whether it may be copied is `authorize(FORK)`'s: an admin's
call, and never a pinned agent. The chat `fork_agent` tool, the CLI and
channel isolation use it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

import structlog
from anthropic import APIError, AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.agent_create_params import Tool
from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)
from daimon.core import agent_lifecycle
from daimon.core.agent_guidance import apply_credential_guidance
from daimon.core.agent_identity import queue_agent_face
from daimon.core.authz import Action, Subject, authorize, build_agent_ref
from daimon.core.defaults.ma_index import (
    download_skill_version,
    find_agent_by_daimon_tag,
    find_agents_by_daimon_tag,
    list_agents_by_tenant,
    list_skills_strict,
)
from daimon.core.defaults.mcp_merge import merge_default_mcp_server, merge_default_mcp_toolset
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ISOLATED,
    MA_METADATA_KEY_NAME,
    build_metadata,
    skill_owner_candidates,
    strip_tenant_prefix,
)
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import create_agent, retrieve_agent
from daimon.core.skills.add import add_agent_skill
from daimon.core.skills.ingest import bundle_from_upload
from daimon.core.specs import merge_default_agent_toolset
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.domain import UserSkillRow
from daimon.core.stores.user_skills import list_user_skills_for_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_FORK_COPY_FIELDS = frozenset(
    {"name", "model", "description", "system", "tools", "mcp_servers", "skills", "metadata"}
)


_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class AgentCopy:
    agent: BetaManagedAgentsAgent
    dropped_skills: tuple[str, ...]
    """Skills left off the copy: another agent's, or the source's own that failed to copy."""
    copied_skills: tuple[str, ...] = ()
    """The source's own skills, uploaded again under the copy's name."""


@dataclass(frozen=True)
class _OwnSkill:
    skill_id: str
    body: str
    version: str | None
    upload: UserSkillRow | None


@dataclass(frozen=True)
class _SkillSplit:
    kept: list[dict[str, object]]
    own: list[_OwnSkill]
    dropped: list[str]


async def _split_scoped_skills(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    source_name: str,
    skills: list[dict[str, object]],
) -> _SkillSplit:
    """Split off the skills scoped to one agent: a copy must not reach into another's."""
    if not any(skill.get("type") == "custom" for skill in skills):
        return _SkillSplit(skills, [], [])
    rows = {row.id: row for row in await list_skills_strict(anthropic)}
    body_by_id = {
        skill_id: body
        for skill_id, row in rows.items()
        if (body := strip_tenant_prefix(tenant_id=tenant_id, display_title=row.display_title or ""))
    }
    async with sessionmaker() as session:
        uploads = await list_user_skills_for_tenant(session, tenant_id=tenant_id)
    upload_by_id = {row.anthropic_id: row for row in uploads if row.anthropic_id}
    agent_names = [
        agent.metadata.get(MA_METADATA_KEY_NAME) or agent.name
        for agent in await list_agents_by_tenant(anthropic, tenant_id=tenant_id)
    ]
    split = _SkillSplit([], [], [])
    for skill in skills:
        skill_id = str(skill.get("skill_id"))
        body = body_by_id.get(skill_id)
        upload = upload_by_id.get(skill_id)
        owners = skill_owner_candidates(
            body or "",
            stored_owner=upload.agent_name if upload is not None else None,
            agent_names=agent_names,
        )
        if not owners:
            split.kept.append(skill)
        elif owners == frozenset({source_name}) and body is not None:
            pinned = skill.get("version")
            latest = rows[skill_id].latest_version
            version = str(pinned) if pinned and pinned != "latest" else latest
            split.own.append(_OwnSkill(skill_id, body, version, upload))
        else:
            split.dropped.append(body or skill_id)
    return split


async def _no_recheck(_agent: BetaManagedAgentsAgent) -> None:
    """The copy was authorized as a whole and no channel reaches it yet."""


async def _copy_own_skills(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    source_name: str,
    copy: BetaManagedAgentsAgent,
    new_name: str,
    own: list[_OwnSkill],
) -> tuple[list[str], list[str]]:
    """Upload each of the source's own skills again as `copy`'s: (copied, failed)."""
    copied: list[str] = []
    failed: list[str] = []
    for skill in own:
        try:
            if skill.version is None:
                raise DaimonError(f"{skill.body} has no version to copy.")
            data = await download_skill_version(
                anthropic,
                skill_id=skill.skill_id,
                version=skill.version,
                scope=resource_scope(tenant_id=str(tenant_id)),
            )
            # A new skill under the copy's title, never a share of the source's id.
            added = await add_agent_skill(
                anthropic,
                sessionmaker,
                tenant_id=tenant_id,
                agent=await retrieve_agent(
                    anthropic, copy.id, scope=resource_scope(tenant_id=str(tenant_id))
                ),
                agent_name=new_name,
                bundle=bundle_from_upload(data, filename="SKILL.zip"),
                origin=skill.upload.origin if skill.upload else f"copied from {source_name}",
                added_by_account_id=skill.upload.added_by_account_id if skill.upload else None,
                recheck=_no_recheck,
            )
        except (DaimonError, APIError) as exc:
            _log.warning(
                "agent_fork.skill_copy_failed",
                tenant_id=str(tenant_id),
                skill_id=skill.skill_id,
                error=type(exc).__name__,
            )
            failed.append(skill.body)
            continue
        copied.append(f"{new_name}/{added.name}")
    return copied, failed


async def copy_agent(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    source: BetaManagedAgentsAgent,
    new_name: str,
    public_url: str | None,
    subject: Subject,
    default_agent_name: str | None,
    extra_metadata: Mapping[str, str] | None = None,
) -> AgentCopy:
    """Create `new_name` as a copy of `source`; raise `DaimonError` if `subject` may not.

    `extra_metadata` is stamped on the copy too (an isolation copy's channel).
    `default_agent_name` is the deployment default, which gets no face.
    """
    async with sessionmaker() as session:
        try:
            policy = await load_access_policy(session, tenant_id=tenant_id)
        except AccessPolicyUnreadable as exc:
            raise DaimonError("The access policy can't be read.") from exc
    source_name = source.metadata.get(MA_METADATA_KEY_NAME) or source.name
    decision = authorize(
        policy,
        subject=subject,
        action=Action.FORK,
        agent=build_agent_ref(source.name, source.metadata),
    )
    if decision.reason == "agent_has_rule":
        # The copy would run anywhere with the prompt and skills of one that may not.
        raise DaimonError(
            f"{source_name} has an agent rule limiting where it runs, so it can't be copied."
        )
    if not decision:
        raise DaimonError("Only a workspace or server admin can copy an agent.")
    source_ma = await retrieve_agent(
        anthropic, source.id, scope=resource_scope(tenant_id=str(tenant_id))
    )
    params = source_ma.model_dump(mode="json")
    fork_params: dict[str, object] = {k: params[k] for k in _FORK_COPY_FIELDS if k in params}
    fork_params["name"] = new_name
    fork_params["metadata"] = build_metadata(
        tenant_id=tenant_id, name=new_name, account_id=derive_guild_account_uuid(tenant_id)
    ) | dict(extra_metadata or {})
    fork_params["mcp_servers"] = merge_default_mcp_server(
        cast("list[BetaManagedAgentsURLMCPServerParams] | None", fork_params.get("mcp_servers")),
        public_url,
    )
    toolset = merge_default_mcp_toolset(
        cast("list[Tool] | None", fork_params.get("tools")), public_url
    )
    # A fork copies raw MA state past `dump_agent_spec`, so the base toolset is guaranteed here.
    fork_params["tools"] = merge_default_agent_toolset(toolset)
    # An isolated reader mounts no secrets; the guidance would send it looking for them.
    if source_ma.metadata.get(MA_METADATA_KEY_ISOLATED) != "true":
        fork_params["system"] = apply_credential_guidance(str(fork_params.get("system") or ""))
    servers, tools = await agent_lifecycle.strip_credentialed_mcp_servers(
        sessionmaker=sessionmaker,
        tenant_id=tenant_id,
        source_agent_uuid=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(source.id)),
        mcp_servers=cast("list[dict[str, object]] | None", fork_params.get("mcp_servers")),
        tools=cast("list[dict[str, object]] | None", fork_params.get("tools")),
    )
    fork_params["mcp_servers"], fork_params["tools"] = servers, tools
    split = await _split_scoped_skills(
        anthropic,
        sessionmaker,
        tenant_id=tenant_id,
        source_name=source_name,
        skills=cast("list[dict[str, object]]", fork_params.get("skills") or []),
    )
    if "skills" in fork_params:
        fork_params["skills"] = split.kept
    created = await create_agent(
        anthropic, fork_params, scope=resource_scope(tenant_id=str(tenant_id))
    )
    queue_agent_face(
        sessionmaker,
        tenant_id=tenant_id,
        agent_name=new_name,
        metadata=created.metadata,
        default_agent_name=default_agent_name,
    )
    if not split.own:
        return AgentCopy(created, tuple(split.dropped))
    copied, failed = await _copy_own_skills(
        anthropic,
        sessionmaker,
        tenant_id=tenant_id,
        source_name=source_name,
        copy=created,
        new_name=new_name,
        own=split.own,
    )
    try:
        agent = await retrieve_agent(
            anthropic, created.id, scope=resource_scope(tenant_id=str(tenant_id))
        )
    except APIError as exc:
        # The copy exists: raising would leave it orphaned and unreported, so
        # return the create's snapshot; `copied_skills` names what it lacks.
        _log.warning(
            "agent_fork.reread_failed",
            tenant_id=str(tenant_id),
            agent_id=created.id,
            error=type(exc).__name__,
        )
        agent = created
    return AgentCopy(agent, (*split.dropped, *failed), tuple(copied))


async def fork_agent(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    source_name: str,
    new_name: str,
    public_url: str | None,
    subject: Subject,
    default_agent_name: str | None,
    extra_metadata: Mapping[str, str] | None = None,
) -> AgentCopy:
    """Copy the agent named `source_name` to `new_name`; raise `DaimonError` if either is wrong."""
    if await find_agents_by_daimon_tag(anthropic, tenant_id=tenant_id, name=new_name):
        raise DaimonError(f"An agent named {new_name} already exists. Pick another name.")
    source = await find_agent_by_daimon_tag(anthropic, tenant_id=tenant_id, name=source_name)
    if source is None:
        raise DaimonError(f"There is no agent named {source_name} to copy.")
    return await copy_agent(
        anthropic,
        sessionmaker,
        tenant_id=tenant_id,
        source=source,
        new_name=new_name,
        public_url=public_url,
        subject=subject,
        default_agent_name=default_agent_name,
        extra_metadata=extra_metadata,
    )


__all__ = ["AgentCopy", "copy_agent", "fork_agent"]
