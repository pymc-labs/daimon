"""The tenant access policy as the channel tools see it.

One write guard for Discord and Slack: each platform resolves its target to a
channel id (plus parent channel and category where it has them) and calls
`require_channel_writable` after its own caller-permission check, so the
policy never reveals a channel the caller could not see anyway. Protection
applies to admins too.
"""

from __future__ import annotations

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.access_policy import TenantAccessPolicy, is_write_protected
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from fastmcp.exceptions import ToolError

_PROTECTED_MSG = (
    "this channel is protected: the workspace does not let daimon post there. "
    "Tell the caller and offer to post somewhere else. Do not retry."
)
_UNREADABLE_MSG = "this workspace's access policy could not be read, so daimon won't post anywhere"


async def load_channel_policy(runtime: McpRuntime, auth: AuthIdentity) -> TenantAccessPolicy:
    """Load the caller's tenant policy; an unreadable one refuses the call."""
    try:
        async with runtime.session_factory() as session:
            return await load_access_policy(session, tenant_id=auth.tenant_id)
    except AccessPolicyUnreadable as exc:
        raise ToolError(_UNREADABLE_MSG) from exc


async def require_channel_writable(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    parent_channel_id: str | None = None,
    category_id: str | None = None,
) -> None:
    """Raise ToolError when the tenant policy protects the target from agent writes."""
    policy = await load_channel_policy(runtime, auth)
    if is_write_protected(
        policy,
        channel_id=channel_id,
        parent_channel_id=parent_channel_id,
        category_id=category_id,
    ):
        raise ToolError(_PROTECTED_MSG)
