"""Copy an agent under a new name: the one copy behind every `fork_agent`.

The copy takes the source's prompt, model, tools, MCP definitions and skills
from its live MA state, with the default daimon MCP server and base toolset
guaranteed and the credential guidance applied. It starts with no credentials
(`agent_lifecycle.strip_credentialed_mcp_servers`) and no skills scoped to
another agent, which are left off and named. An agent pinned to channels is
not copied. The chat `fork_agent` tool and channel isolation both use it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import cast

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.agent_create_params import Tool
from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)
from daimon.core import agent_lifecycle
from daimon.core.agent_guidance import apply_credential_guidance
from daimon.core.defaults.ma_index import (
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
from daimon.core.specs import merge_default_agent_toolset
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.user_skills import list_user_skills_for_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_FORK_COPY_FIELDS = frozenset(
    {"name", "model", "description", "system", "tools", "mcp_servers", "skills", "metadata"}
)


@dataclass(frozen=True)
class AgentCopy:
    agent: BetaManagedAgentsAgent
    dropped_skills: tuple[str, ...]
    """Skills scoped to an agent (``agent/skill``), left off the copy."""


async def _drop_scoped_skills(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    skills: list[dict[str, object]],
) -> tuple[list[dict[str, object]], tuple[str, ...]]:
    """Split off the skills scoped to one agent: a copy must not reach into another's."""
    if not any(skill.get("type") == "custom" for skill in skills):
        return skills, ()
    body_by_id = {
        row.id: body
        for row in await list_skills_strict(anthropic)
        if (body := strip_tenant_prefix(tenant_id=tenant_id, display_title=row.display_title or ""))
    }
    async with sessionmaker() as session:
        uploads = await list_user_skills_for_tenant(session, tenant_id=tenant_id)
    owner_by_id = {row.anthropic_id: row.agent_name for row in uploads if row.anthropic_id}
    agent_names = [
        agent.metadata.get(MA_METADATA_KEY_NAME) or agent.name
        for agent in await list_agents_by_tenant(anthropic, tenant_id=tenant_id)
    ]
    kept: list[dict[str, object]] = []
    dropped: list[str] = []
    for skill in skills:
        skill_id = str(skill.get("skill_id"))
        body = body_by_id.get(skill_id)
        owners = skill_owner_candidates(
            body or "", stored_owner=owner_by_id.get(skill_id), agent_names=agent_names
        )
        if owners:
            dropped.append(body or skill_id)
        else:
            kept.append(skill)
    return kept, tuple(dropped)


async def copy_agent(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    source: BetaManagedAgentsAgent,
    new_name: str,
    public_url: str | None,
) -> AgentCopy:
    """Create `new_name` as a copy of `source`; raise `DaimonError` if it may not be copied."""
    async with sessionmaker() as session:
        try:
            policy = await load_access_policy(session, tenant_id=tenant_id)
        except AccessPolicyUnreadable as exc:
            raise DaimonError("The access policy can't be read.") from exc
    source_name = source.metadata.get(MA_METADATA_KEY_NAME) or source.name
    if any(name in policy.agent_channel_pins for name in {source.name, source_name}):
        # The copy would be an unpinned agent with the pinned one's prompt and skills.
        raise DaimonError(f"{source_name} is pinned to specific channels, so it can't be copied.")
    source_ma = await anthropic.beta.agents.retrieve(source.id)
    params = source_ma.model_dump(mode="json")
    fork_params: dict[str, object] = {k: params[k] for k in _FORK_COPY_FIELDS if k in params}
    fork_params["name"] = new_name
    fork_params["metadata"] = build_metadata(
        tenant_id=tenant_id, name=new_name, account_id=derive_guild_account_uuid(tenant_id)
    )
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
    skills, dropped = await _drop_scoped_skills(
        anthropic,
        sessionmaker,
        tenant_id=tenant_id,
        skills=cast("list[dict[str, object]]", fork_params.get("skills") or []),
    )
    if "skills" in fork_params:
        fork_params["skills"] = skills
    created = await anthropic.beta.agents.create(**fork_params)  # type: ignore[arg-type]  # a validated copy of the source's own create fields
    return AgentCopy(created, dropped)


async def fork_agent(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    source_name: str,
    new_name: str,
    public_url: str | None,
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
    )


__all__ = ["AgentCopy", "copy_agent", "fork_agent"]
