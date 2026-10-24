"""OpenAI agents for the MCP setup tools, behind the operator's resource-host capability.

A tool asks for a host only when the deployment opted in (`McpRuntime.resource_hosts`),
the turn's channel is freshly configured for the OpenAI profile, and the operator's
deployment manifest authorizes exactly that channel revision for the calling account
(`daimon.core.turn.openai_deployment.authorized_deployments`). Any other channel keeps
the existing Anthropic path; one on another non-Anthropic backend is refused. The
returned host must be bound to exactly that selection before it is used.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._native_resources import (
    MCP_AUTHORIZATION_ID as MCP_AUTHORIZATION_ID,
)
from daimon.adapters.mcp.tools._native_resources import (
    bind_native_host,
    check_skill_origin,
    check_skill_revision,
)
from daimon.core.channel_backend import channel_ref, current_backend
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.stores.domain import TurnOriginRow
from fastmcp.exceptions import ToolError
from mux.profiles.openai import PERSISTENT_WORKSPACE

if TYPE_CHECKING:
    from anthropic.types.beta import BetaManagedAgentsAgent
    from daimon.core.neutral_resources import HostAgentView, NeutralResourceHost
    from mux.contracts.resources import Agent

OPENAI_PROFILE = "openai.persistent_workspace"
ANTHROPIC_PROFILE = "anthropic.managed_agents"


def _principal(auth: AuthIdentity) -> str:
    return f"mcp:{auth.account_id}"


async def openai_skill_host(
    runtime: McpRuntime,
    auth: AuthIdentity,
    origin: TurnOriginRow | None,
    *,
    operation: str,
) -> NeutralResourceHost | None:
    """The bound OpenAI host for this turn's channel, or None for the existing path."""
    factory = runtime.resource_hosts
    if factory is None or origin is None:
        return None
    check_skill_origin(auth, origin)
    async with runtime.session_factory() as session:
        revision = await current_backend(
            session,
            channel_ref(auth.tenant_id, origin.platform, origin.parent_channel_id),
        )
    if revision is None or revision.profile == ANTHROPIC_PROFILE:
        return None
    if revision.profile != OPENAI_PROFILE:
        raise ToolError(
            "This channel's model provider can't change an agent's skills from chat yet. "
            "Nothing was changed. Do not retry."
        )
    from daimon.core.turn.runtimes import authorized_channel_deployments

    authorized = [
        deployment
        for deployment in authorized_channel_deployments(OPENAI_PROFILE, str(auth.tenant_id))
        if deployment.platform == origin.platform
        and deployment.channel_id == origin.parent_channel_id
        and deployment.config_digest == revision.digest
        and deployment.account_id == str(auth.account_id)
        and deployment.project == factory.account_scope_id
    ]
    if len(authorized) != 1:
        return None
    check_skill_revision(revision, PERSISTENT_WORKSPACE)
    return bind_host(
        factory(
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            principal_id=_principal(auth),
            authorization_id=MCP_AUTHORIZATION_ID,
            operation_key=f"mcp-{operation}:{uuid.uuid4()}",
        ),
        auth=auth,
        workspace=authorized[0].project,
    )


def bind_host(host: object, *, auth: AuthIdentity, workspace: str) -> NeutralResourceHost:
    """Select the native OpenAI catalog for the shared scope binder."""
    from mux.drivers.openai import OpenAIAgents

    return bind_native_host(
        host,
        auth=auth,
        workspace=workspace,
        profile=PERSISTENT_WORKSPACE,
        catalog_type=OpenAIAgents,
    )


@dataclass(frozen=True)
class _GateAgent:
    """The facts the shared skill and pin gates read, from a neutral agent."""

    id: str
    name: str
    metadata: Mapping[str, str]


def gate_view(agent: Agent) -> BetaManagedAgentsAgent:
    """A neutral agent as the shared gates read it: its native id, Daimon name and tags.

    The gates read only `id`, `name` and `metadata`; the same policy decides for
    both backends.
    """
    metadata = dict(agent.spec.metadata or {})
    return cast(
        "BetaManagedAgentsAgent",
        _GateAgent(
            id=agent.ref.id,
            name=metadata.get(MA_METADATA_KEY_NAME, agent.spec.name),
            metadata=metadata,
        ),
    )


async def resolve_host_agent(
    runtime: McpRuntime,
    auth: AuthIdentity,
    host: NeutralResourceHost,
    *,
    name: str,
    expected_ma_agent_id: str | None,
    location_channel_id: str | None,
) -> HostAgentView:
    """`resolve_setup_agent`'s rules for an agent behind the host, provider-neutrally.

    A chat call must name the agent it means (`expected_ma_agent_id`, the native
    id); an agent the caller's channel isolation hides resolves as missing; and
    the named id must be the current agent. Same refusals, before any change.
    """
    from daimon.adapters.mcp.tools._rule_view import load_isolation_view
    from daimon.core.stores.domain import CHAT_PLATFORMS

    if auth.platform in CHAT_PLATFORMS and expected_ma_agent_id is None:
        raise ToolError(
            "Pass expected_ma_agent_id from the selected target or list_agents before acting. "
            "Which current agent should I configure? Nothing was changed."
        )
    view = await host.find(name)
    caller = await load_isolation_view(runtime, auth, location_channel_id=location_channel_id)
    seen = view if view is not None and caller.sees_agent(gate_view(view.revision.agent)) else None
    if expected_ma_agent_id is not None:
        if seen is not None and seen.id == expected_ma_agent_id:
            return seen
        raise ToolError(
            f"The selected agent '{name}' ({expected_ma_agent_id}) is missing or changed. "
            "Which current agent should I configure? Nothing was changed."
        )
    if seen is None:
        raise ToolError(
            f"agent '{name}' not found. Which current agent should I configure? "
            "Nothing was changed."
        )
    return seen
