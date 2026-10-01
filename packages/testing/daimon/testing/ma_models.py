"""Canonical builders for Managed Agents SDK response shapes.

One sanctioned builder per SDK shape, each ending in the real keyword
constructor so pydantic validates every field (never `model_construct`).
Keyword names mirror the SDK field names; a test that is *about* one of
these shapes inlines the full constructor instead.

The defaults agree with each other: `ma_session()` freezes the same agent
id and model that `ma_agent()` carries, so a session handed to the turn
pipeline next to the default responder reads as a reuse, not a handoff.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Literal

from anthropic.types.beta import (
    BetaCloudConfig,
    BetaEnvironment,
    BetaManagedAgentsAgent,
    BetaManagedAgentsSession,
    BetaPackages,
    BetaUnrestrictedNetwork,
)
from anthropic.types.beta.beta_environment import Config as EnvironmentConfig
from anthropic.types.beta.beta_managed_agents_agent import Skill as AgentSkill
from anthropic.types.beta.beta_managed_agents_agent import Tool as AgentTool
from anthropic.types.beta.beta_managed_agents_mcp_server_url_definition import (
    BetaManagedAgentsMCPServerURLDefinition,
)
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from anthropic.types.beta.beta_managed_agents_outcome_evaluation_resource import (
    BetaManagedAgentsOutcomeEvaluationResource,
)
from anthropic.types.beta.beta_managed_agents_session_agent import BetaManagedAgentsSessionAgent
from anthropic.types.beta.beta_managed_agents_session_stats import BetaManagedAgentsSessionStats
from anthropic.types.beta.beta_managed_agents_session_usage import BetaManagedAgentsSessionUsage
from anthropic.types.beta.sessions.beta_managed_agents_session_resource import (
    BetaManagedAgentsSessionResource,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from pydantic import TypeAdapter

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_AGENT_ID = "ag_test"
DEFAULT_AGENT_NAME = "test-agent"
DEFAULT_MODEL_ID = "claude-sonnet-4-6"
"""A model `daimon.core.pricing.AGENT_MODEL_PRICING` meters, so a turn faked
with these builders reaches the ledger like a real one would."""
DEFAULT_ENV_ID = "env_test"
DEFAULT_ENV_NAME = "test-env"
DEFAULT_SESSION_ID = "sess_test"

FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)
FIXED_TS_STR = "2026-01-01T00:00:00Z"
"""`BetaEnvironment` carries its timestamps as RFC 3339 strings, the other
shapes as `datetime`; both spellings name the same instant."""

EMPTY_CLOUD_CONFIG = BetaCloudConfig(
    type="cloud",
    networking=BetaUnrestrictedNetwork(type="unrestricted"),
    packages=BetaPackages(apt=[], cargo=[], gem=[], go=[], npm=[], pip=[]),
)

EMPTY_SESSION_STATS = BetaManagedAgentsSessionStats()

EMPTY_SESSION_USAGE = BetaManagedAgentsSessionUsage()

type SessionStatus = Literal["rescheduling", "running", "idle", "terminated"]
"""The SDK inlines `BetaManagedAgentsSession.status` on the model rather than
exporting it as a standalone type, so this is a local alias of the same four
literals."""

type ModelSpeed = Literal["standard", "fast"]

# ---------------------------------------------------------------------------
# Dict-form coercion (pydantic validates; pyright sees the model type)
# ---------------------------------------------------------------------------

_TOOL_ADAPTER: TypeAdapter[AgentTool] = TypeAdapter(AgentTool)
_SKILL_ADAPTER: TypeAdapter[AgentSkill] = TypeAdapter(AgentSkill)
_MCP_SERVER_ADAPTER: TypeAdapter[BetaManagedAgentsMCPServerURLDefinition] = TypeAdapter(
    BetaManagedAgentsMCPServerURLDefinition
)
_RESOURCE_ADAPTER: TypeAdapter[BetaManagedAgentsSessionResource] = TypeAdapter(
    BetaManagedAgentsSessionResource
)


def _validate_each[T](
    adapter: TypeAdapter[T], items: Sequence[T | Mapping[str, object]]
) -> list[T]:
    """Validate dict-form entries through the SDK model; pass models through."""
    return [adapter.validate_python(item) if isinstance(item, Mapping) else item for item in items]


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


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


def ma_model_config(
    id: str = DEFAULT_MODEL_ID, *, speed: ModelSpeed | None = None
) -> BetaManagedAgentsModelConfig:
    return BetaManagedAgentsModelConfig(id=id, speed=speed)


def _as_model_config(model: str | BetaManagedAgentsModelConfig) -> BetaManagedAgentsModelConfig:
    return ma_model_config(model) if isinstance(model, str) else model


def ma_agent(
    *,
    id: str = DEFAULT_AGENT_ID,
    name: str = DEFAULT_AGENT_NAME,
    model: str | BetaManagedAgentsModelConfig = DEFAULT_MODEL_ID,
    tenant_id: uuid.UUID | str | None = None,
    metadata: Mapping[str, str] | None = None,
    description: str | None = None,
    system: str | None = None,
    tools: Sequence[AgentTool | Mapping[str, object]] = (),
    mcp_servers: Sequence[BetaManagedAgentsMCPServerURLDefinition | Mapping[str, object]] = (),
    skills: Sequence[AgentSkill | Mapping[str, object]] = (),
    version: int = 1,
    created_at: datetime = FIXED_TS,
    updated_at: datetime | None = None,
    archived_at: datetime | None = None,
) -> BetaManagedAgentsAgent:
    """A validated `BetaManagedAgentsAgent`.

    `tenant_id=` stamps the tenant/name metadata the resolver matches on;
    `metadata=` merges over it. `tools` / `mcp_servers` / `skills` accept
    the SDK models or their dict form (validated on the way in).
    """
    return BetaManagedAgentsAgent(
        id=id,
        type="agent",
        name=name,
        version=version,
        model=_as_model_config(model),
        metadata=_merge_metadata(tenant_id=tenant_id, name=name, metadata=metadata),
        description=description,
        system=system,
        tools=_validate_each(_TOOL_ADAPTER, tools),
        mcp_servers=_validate_each(_MCP_SERVER_ADAPTER, mcp_servers),
        skills=_validate_each(_SKILL_ADAPTER, skills),
        created_at=created_at,
        updated_at=updated_at if updated_at is not None else created_at,
        archived_at=archived_at,
    )


def ma_environment(
    *,
    id: str = DEFAULT_ENV_ID,
    name: str = DEFAULT_ENV_NAME,
    tenant_id: uuid.UUID | str | None = None,
    metadata: Mapping[str, str] | None = None,
    description: str = "",
    config: EnvironmentConfig = EMPTY_CLOUD_CONFIG,
    created_at: str = FIXED_TS_STR,
    updated_at: str | None = None,
    archived_at: str | None = None,
    scope: Literal["organization", "account"] | None = None,
) -> BetaEnvironment:
    """A validated `BetaEnvironment` (cloud config by default).

    Timestamps are RFC 3339 strings because that is how the SDK types them
    on this shape. `tenant_id=` / `metadata=` behave as in `ma_agent`.
    """
    return BetaEnvironment(
        id=id,
        type="environment",
        name=name,
        config=config,
        metadata=_merge_metadata(tenant_id=tenant_id, name=name, metadata=metadata),
        description=description,
        created_at=created_at,
        updated_at=updated_at if updated_at is not None else created_at,
        archived_at=archived_at,
        scope=scope,
    )


def ma_session_agent(
    *,
    id: str = DEFAULT_AGENT_ID,
    name: str = DEFAULT_AGENT_NAME,
    model: str | BetaManagedAgentsModelConfig = DEFAULT_MODEL_ID,
    description: str | None = None,
    system: str | None = None,
    tools: Sequence[AgentTool | Mapping[str, object]] = (),
    mcp_servers: Sequence[BetaManagedAgentsMCPServerURLDefinition | Mapping[str, object]] = (),
    skills: Sequence[AgentSkill | Mapping[str, object]] = (),
    version: int = 1,
) -> BetaManagedAgentsSessionAgent:
    """The agent snapshot a session freezes at creation time."""
    return BetaManagedAgentsSessionAgent(
        id=id,
        type="agent",
        name=name,
        version=version,
        model=_as_model_config(model),
        description=description,
        system=system,
        tools=_validate_each(_TOOL_ADAPTER, tools),
        mcp_servers=_validate_each(_MCP_SERVER_ADAPTER, mcp_servers),
        skills=_validate_each(_SKILL_ADAPTER, skills),
    )


def ma_session_agent_from(agent: BetaManagedAgentsAgent) -> BetaManagedAgentsSessionAgent:
    """Freeze `agent` the way session creation does: same id, name, model,
    prompt, toolset, and version."""
    return BetaManagedAgentsSessionAgent(
        id=agent.id,
        type="agent",
        name=agent.name,
        version=agent.version,
        model=agent.model,
        description=agent.description,
        system=agent.system,
        tools=list(agent.tools),
        mcp_servers=list(agent.mcp_servers),
        skills=list(agent.skills),
    )


def ma_session(
    *,
    id: str = DEFAULT_SESSION_ID,
    agent: BetaManagedAgentsSessionAgent | BetaManagedAgentsAgent | None = None,
    agent_id: str | None = None,
    model: str | BetaManagedAgentsModelConfig | None = None,
    environment_id: str = DEFAULT_ENV_ID,
    status: SessionStatus = "idle",
    metadata: Mapping[str, str] | None = None,
    title: str | None = None,
    resources: Sequence[BetaManagedAgentsSessionResource | Mapping[str, object]] = (),
    outcome_evaluations: Sequence[BetaManagedAgentsOutcomeEvaluationResource] = (),
    vault_ids: Sequence[str] = (),
    stats: BetaManagedAgentsSessionStats = EMPTY_SESSION_STATS,
    usage: BetaManagedAgentsSessionUsage = EMPTY_SESSION_USAGE,
    created_at: datetime = FIXED_TS,
    updated_at: datetime | None = None,
    archived_at: datetime | None = None,
    deployment_id: str | None = None,
) -> BetaManagedAgentsSession:
    """A validated `BetaManagedAgentsSession`.

    The frozen agent comes from `agent=` (a `BetaManagedAgentsSessionAgent`
    used as-is, or a `BetaManagedAgentsAgent` frozen via
    `ma_session_agent_from`), else from the `agent_id=` / `model=`
    shortcuts over `ma_session_agent`'s defaults. Passing both is an error
    rather than a silent precedence rule.
    """
    if agent is not None and (agent_id is not None or model is not None):
        raise ValueError("ma_session: pass either agent= or agent_id=/model=, not both")
    if isinstance(agent, BetaManagedAgentsAgent):
        frozen_agent = ma_session_agent_from(agent)
    elif agent is not None:
        frozen_agent = agent
    else:
        frozen_agent = ma_session_agent(
            id=agent_id if agent_id is not None else DEFAULT_AGENT_ID,
            model=model if model is not None else DEFAULT_MODEL_ID,
        )
    return BetaManagedAgentsSession(
        id=id,
        type="session",
        status=status,
        agent=frozen_agent,
        environment_id=environment_id,
        metadata=dict(metadata) if metadata is not None else {},
        title=title,
        resources=_validate_each(_RESOURCE_ADAPTER, resources),
        outcome_evaluations=list(outcome_evaluations),
        vault_ids=list(vault_ids),
        stats=stats,
        usage=usage,
        created_at=created_at,
        updated_at=updated_at if updated_at is not None else created_at,
        archived_at=archived_at,
        deployment_id=deployment_id,
    )


def ma_model_usage(
    *,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    speed: ModelSpeed | None = None,
) -> BetaManagedAgentsSpanModelUsage:
    """Token usage for one model request (`span.model_request_end`)."""
    return BetaManagedAgentsSpanModelUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        speed=speed,
    )
