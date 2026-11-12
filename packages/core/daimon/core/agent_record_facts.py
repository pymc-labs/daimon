"""Project Daimon-owned agent and environment records into the SDK shapes policy reads.

Admission, routing and billing still read Managed Agents shapes. Self-hosted and
Daimon-owned (OpenAI, Gemini) records share these projections, so both kinds
expose the same tools, skills, MCP servers and model as an MA record would.
"""

from __future__ import annotations

from typing import get_args

from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_agent_tool_config import (
    BetaManagedAgentsAgentToolConfig,
)
from daimon.core.ma_metadata_keys import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from mux.contracts.resources import Agent, Environment, ToolSpec
from mux.profiles.anthropic import BUILTIN_SKILL_SOURCE
from pydantic import JsonValue

# Toolset entries outside the SDK's closed name set have no MA-shaped equivalent.
_TOOLSET_NAMES: frozenset[str] = frozenset(
    get_args(BetaManagedAgentsAgentToolConfig.model_fields["name"].annotation)
)


def _policy(tool: ToolSpec) -> dict[str, JsonValue]:
    return {"type": "always_ask" if tool.permission == "ask" else "always_allow"}


def project_tools(tools: tuple[ToolSpec, ...]) -> list[dict[str, JsonValue]]:
    """Builtins group into one toolset entry, as the MA driver compiles them."""
    builtin: list[JsonValue] = [
        {"name": tool.name, "enabled": True, "permission_policy": _policy(tool)}
        for tool in tools
        if tool.kind == "builtin" and tool.name in _TOOLSET_NAMES
    ]
    projected: list[dict[str, JsonValue]] = []
    if builtin:
        projected.append(
            {
                "type": "agent_toolset_20260401",
                "default_config": {"enabled": False, "permission_policy": {"type": "always_ask"}},
                "configs": builtin,
            }
        )
    for tool in tools:
        if tool.kind == "custom":
            projected.append(
                {
                    "type": "custom",
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": dict(tool.input_schema or {"type": "object"}),
                }
            )
        elif tool.kind == "mcp_toolset":
            projected.append(
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": tool.name,
                    "default_config": {"enabled": True, "permission_policy": _policy(tool)},
                    "configs": [],
                }
            )
    return projected


def agent_facts(agent: Agent, *, tenant_id: str) -> BetaManagedAgentsAgent:
    """Reserved metadata comes from the record's tenant and name, never caller aliases."""
    spec = agent.spec
    return BetaManagedAgentsAgent.model_validate(
        {
            "id": agent.ref.id,
            "type": "agent",
            "name": spec.name,
            "version": agent.revision.local,
            # `provider` rides as an extra field so a Daimon record keeps its provider.
            "model": {"id": spec.model.id, "provider": spec.model.provider},
            "system": spec.system,
            "description": spec.description,
            "metadata": {
                **dict(spec.metadata or {}),
                MA_METADATA_KEY_NAME: spec.name,
                MA_METADATA_KEY_TENANT: tenant_id,
            },
            "tools": project_tools(spec.tools or ()),
            "mcp_servers": [
                {"type": "url", "name": server.name, "url": server.url}
                for server in spec.mcp_servers or ()
            ],
            "skills": [
                {
                    "type": BUILTIN_SKILL_SOURCE
                    if skill.source == BUILTIN_SKILL_SOURCE
                    else "custom",
                    "skill_id": skill.id,
                    "version": skill.version or "latest",
                }
                for skill in spec.skills or ()
            ],
            "created_at": agent.created_at,
            "updated_at": agent.updated_at or agent.created_at,
            "archived_at": agent.archived_at,
        }
    )


def environment_facts(environment: Environment, *, tenant_id: str) -> BetaEnvironment:
    """Daimon-owned environments run outside Anthropic, which MA calls self-hosted."""
    spec = environment.spec
    updated = environment.updated_at or environment.created_at
    return BetaEnvironment.model_validate(
        {
            "id": environment.ref.id,
            "type": "environment",
            "name": spec.name,
            "description": spec.description or "",
            "metadata": {
                **dict(spec.metadata or {}),
                MA_METADATA_KEY_NAME: spec.name,
                MA_METADATA_KEY_TENANT: tenant_id,
            },
            "config": {"type": "self_hosted"},
            "created_at": environment.created_at.isoformat(),
            "updated_at": updated.isoformat(),
            "archived_at": environment.archived_at.isoformat() if environment.archived_at else None,
        }
    )
