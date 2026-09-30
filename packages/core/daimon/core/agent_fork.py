"""Copy an agent under a new name, for callers outside one adapter's own fork path.

The same copy the chat `fork_agent` tool makes: prompt, model, skills, tools
and MCP definitions from the source's live MA state, the default daimon MCP
server and base toolset guaranteed, the credential guidance applied, and the
source's GitHub credential, repo binding and MCP tokens re-keyed onto the
copy (`agent_lifecycle.copy_credential_and_repo_binding`). API keys are
not copied. Channel isolation uses it to give a channel its own agent.
"""

from __future__ import annotations

import uuid
from typing import cast

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.agent_create_params import Tool
from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)
from cryptography.fernet import MultiFernet
from daimon.core import agent_lifecycle
from daimon.core.agent_guidance import apply_credential_guidance
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag, find_agents_by_daimon_tag
from daimon.core.defaults.mcp_merge import merge_default_mcp_server, merge_default_mcp_toolset
from daimon.core.defaults.metadata import MA_METADATA_KEY_ISOLATED, build_metadata
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.specs import merge_default_agent_toolset
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_FORK_COPY_FIELDS = frozenset(
    {"name", "model", "description", "system", "tools", "mcp_servers", "skills", "metadata"}
)


async def fork_agent(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    source_name: str,
    new_name: str,
    public_url: str | None,
    fernet: MultiFernet,
    oauth_scopes: tuple[str, ...],
) -> BetaManagedAgentsAgent:
    """Create `new_name` as a copy of `source_name`; raise `DaimonError` if either is wrong."""
    if await find_agents_by_daimon_tag(anthropic, tenant_id=tenant_id, name=new_name):
        raise DaimonError(f"An agent named {new_name} already exists. Pick another name.")
    source = await find_agent_by_daimon_tag(anthropic, tenant_id=tenant_id, name=source_name)
    if source is None:
        raise DaimonError(f"There is no agent named {source_name} to copy.")
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
    tools = merge_default_mcp_toolset(
        cast("list[Tool] | None", fork_params.get("tools")), public_url
    )
    fork_params["tools"] = merge_default_agent_toolset(tools)
    # An isolated reader mounts no secrets; the guidance would send it looking for them.
    if source_ma.metadata.get(MA_METADATA_KEY_ISOLATED) != "true":
        fork_params["system"] = apply_credential_guidance(str(fork_params.get("system") or ""))
    created = await anthropic.beta.agents.create(**fork_params)  # type: ignore[arg-type]  # a validated copy of the source's own create fields
    await agent_lifecycle.copy_credential_and_repo_binding(
        anthropic=anthropic,
        sessionmaker=sessionmaker,
        fernet=fernet,
        oauth_scopes=oauth_scopes,
        tenant_id=tenant_id,
        source_agent_uuid=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(source.id)),
        fork_agent_uuid=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(created.id)),
    )
    return created


__all__ = ["fork_agent"]
