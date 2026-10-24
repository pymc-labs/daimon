"""Tenant-stamped Managed Agents builders for Daimon suites.

The canonical SDK-shape builders live in `mux.testing.drivers.anthropic.ma_models`.
`ma_agent` and `ma_environment` here add one Daimon convenience on top of them:
`tenant_id=` stamps the `daimon_tenant` / `daimon_name` metadata the resolver
matches on. Every other builder and constant is imported from mux.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal, TypedDict, Unpack

from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent, BetaManagedAgentsSession
from anthropic.types.beta.beta_environment import Config as EnvironmentConfig
from anthropic.types.beta.beta_managed_agents_agent import Skill as AgentSkill
from anthropic.types.beta.beta_managed_agents_agent import Tool as AgentTool
from anthropic.types.beta.beta_managed_agents_mcp_server_url_definition import (
    BetaManagedAgentsMCPServerURLDefinition,
)
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from mux.testing.drivers.anthropic import ma_models as mux_ma_models
from mux.testing.drivers.anthropic.ma_models import (
    DEFAULT_AGENT_NAME,
    DEFAULT_ENV_NAME,
    ma_session_agent,
)


def tenant_metadata(tenant_id: uuid.UUID | str, name: str, **extra: str) -> dict[str, str]:
    """The `daimon_tenant` / `daimon_name` stamps the resolver reads off MA
    resources, plus any extra keys."""
    return {MA_METADATA_KEY_TENANT: str(tenant_id), MA_METADATA_KEY_NAME: name, **extra}


def _merge_metadata(
    *,
    tenant_id: uuid.UUID | str | None,
    name: str,
    metadata: Mapping[str, str] | None,
) -> dict[str, str]:
    """`tenant_id=` is a convenience for `tenant_metadata`; an explicit
    `metadata=` merges over it, so a caller can override either stamp."""
    merged: dict[str, str] = tenant_metadata(tenant_id, name) if tenant_id is not None else {}
    if metadata is not None:
        merged.update(metadata)
    return merged


class _AgentFields(TypedDict, total=False):
    id: str
    model: str | BetaManagedAgentsModelConfig
    description: str | None
    system: str | None
    tools: Sequence[AgentTool | Mapping[str, object]]
    mcp_servers: Sequence[BetaManagedAgentsMCPServerURLDefinition | Mapping[str, object]]
    skills: Sequence[AgentSkill | Mapping[str, object]]
    version: int
    created_at: datetime
    updated_at: datetime | None
    archived_at: datetime | None


class _EnvironmentFields(TypedDict, total=False):
    id: str
    description: str
    config: EnvironmentConfig
    created_at: str
    updated_at: str | None
    archived_at: str | None
    scope: Literal["organization", "account"] | None


def ma_agent(
    *,
    name: str = DEFAULT_AGENT_NAME,
    tenant_id: uuid.UUID | str | None = None,
    metadata: Mapping[str, str] | None = None,
    **fields: Unpack[_AgentFields],
) -> BetaManagedAgentsAgent:
    """`mux_ma_models.ma_agent` with `tenant_id=` stamping the resolver metadata;
    `metadata=` merges over it."""
    return mux_ma_models.ma_agent(
        name=name,
        metadata=_merge_metadata(tenant_id=tenant_id, name=name, metadata=metadata),
        **fields,
    )


def ma_environment(
    *,
    name: str = DEFAULT_ENV_NAME,
    tenant_id: uuid.UUID | str | None = None,
    metadata: Mapping[str, str] | None = None,
    **fields: Unpack[_EnvironmentFields],
) -> BetaEnvironment:
    """`mux_ma_models.ma_environment` with `tenant_id=` / `metadata=` as in `ma_agent`."""
    return mux_ma_models.ma_environment(
        name=name,
        metadata=_merge_metadata(tenant_id=tenant_id, name=name, metadata=metadata),
        **fields,
    )


def with_coordinator(session: BetaManagedAgentsSession) -> BetaManagedAgentsSession:
    """`session` with a coordinator roster of one child that may do anything.

    The child is a full agent definition, as the provider resolves one: the
    root's tools and servers plus a built-in toolset and an MCP write, all on
    `always_allow`. Validated against the SDK's real shape.
    """
    allow = {"enabled": True, "permission_policy": {"type": "always_allow"}}
    child = ma_session_agent(
        id="agent_child",
        tools=[
            {
                "type": "agent_toolset_20260401",
                "default_config": allow,
                "configs": [{"name": name, **allow} for name in ("bash", "write", "edit")],
            },
            {
                "type": "mcp_toolset",
                "mcp_server_name": "child_linear",
                "default_config": allow,
                "configs": [{"name": "create_issue", **allow}],
            },
        ],
        mcp_servers=[{"name": "child_linear", "type": "url", "url": "https://linear.example/mcp"}],
    ).model_dump(mode="json", exclude={"multiagent"})
    dumped = session.model_dump(mode="json")
    dumped["agent"]["multiagent"] = {"type": "coordinator", "agents": [child]}
    return BetaManagedAgentsSession.model_validate(dumped)


def with_hidden_capability(session: BetaManagedAgentsSession) -> BetaManagedAgentsSession:
    """`session` carrying an unreviewed field in the agent capability container."""
    dumped = session.model_dump(mode="json")
    dumped["agent"]["future_capability"] = True
    return BetaManagedAgentsSession.model_validate(dumped)
