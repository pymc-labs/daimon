"""The MCP side of the pinned-agent write rule (`daimon.core.agent_pins`).

Every tool that adds to or edits what an agent reaches calls
`require_pin_write_access` once it has resolved the target: the private-form
request tools with the turn origin they were called from, and the direct
configuration tools (`update_agent`, `attach_mcp_server`, `detach_mcp_server`,
`remove_agent_key`) with none, because they take no origin. With no origin a
member is outside every pin, so a pinned agent's direct configuration is an
admin's; a member inside its channels uses the request tools instead.
"""

from __future__ import annotations

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.agent_pins import (
    PIN_WRITE_REFUSAL,
    POLICY_UNREADABLE_REFUSAL,
    agent_pin_names,
    pin_write_refused,
)
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.domain import TurnOriginRow
from fastmcp.exceptions import ToolError


async def require_pin_write_access(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    ma_agent: BetaManagedAgentsAgent,
    origin: TurnOriginRow | None,
) -> None:
    """Raise unless this caller may change ``ma_agent`` from ``origin``."""
    if auth.is_admin:
        return
    async with runtime.session_factory() as session:
        try:
            policy = await load_access_policy(session, tenant_id=auth.tenant_id)
        except AccessPolicyUnreadable as exc:
            raise ToolError(POLICY_UNREADABLE_REFUSAL) from exc
    if pin_write_refused(
        policy,
        is_admin=False,
        agent_names=agent_pin_names(ma_agent.name, ma_agent.metadata),
        parent_channel_id=origin.parent_channel_id if origin is not None else None,
        thread_id=origin.thread_id if origin is not None else None,
    ):
        raise ToolError(
            f"'{ma_agent.name}': {PIN_WRITE_REFUSAL} No card was posted. Tell the caller to "
            "ask in one of that agent's channels, or ask an admin. Do not retry."
        )
