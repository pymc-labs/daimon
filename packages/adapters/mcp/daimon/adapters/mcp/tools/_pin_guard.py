"""The MCP side of the pinned-agent write rule (`daimon.core.agent_pins`).

Every tool that adds to or edits what an agent reaches calls
`require_pin_write_access` once it has resolved the target: the private-form
request tools with the turn origin they were called from, and the direct
configuration tools (`update_agent`, `attach_mcp_server`, `detach_mcp_server`,
`remove_agent_key`) with none, because they take no origin. With no origin a
member is outside every pin, so a pinned agent's direct configuration is an
admin's, or a channel admin's who runs every channel it is pinned to; a member
inside its channels uses the request tools instead.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.reachability import channel_admin_caller
from daimon.core.agent_pins import (
    PIN_WRITE_REFUSAL,
    POLICY_UNREADABLE_REFUSAL,
    agent_pin_names,
    is_pin_administered,
    pin_write_refused,
)
from daimon.core.channel_admins import load_administered_channel_ids
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.domain import TurnOriginRow
from fastmcp.exceptions import ToolError


def _trusted_admin(auth: AuthIdentity) -> bool:
    """An admin this guard trusts to configure a pinned agent.

    - A chat turn's credential (``chat_agent_id``): minted for one turn whose
      admission recorded the adapter's live platform role as the account's
      stored role, which ``is_admin`` reads -- current as of that turn. The
      same vault token is reused by that person's later hub and routine
      sessions, so there it is as current as their last platform turn.
    - The deployment operator's own token (no agent, no chat turn, no
      platform user; ``is_admin`` comes from the internal claim or the stored
      role), which already controls the deployment.

    An agent-scoped key (``agent_id``) is never trusted, whoever minted it:
    it is long-lived and its account role may be stale.
    """
    if not auth.is_admin or auth.agent_id is not None:
        return False
    if auth.chat_agent_id is not None:
        return auth.platform_user_id is not None
    return auth.platform_user_id is None


async def require_pin_write_access(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    ma_agent: BetaManagedAgentsAgent | Callable[[], Awaitable[BetaManagedAgentsAgent]],
    origin: TurnOriginRow | None,
) -> None:
    """Raise unless this caller may change ``ma_agent`` from ``origin``.

    ``ma_agent`` may be a resolver, called only when the tenant pins anything,
    for paths that don't otherwise look the agent up.

    Admins are trusted to configure an agent (`_trusted_admin`); agent
    keys never are.
    """
    if _trusted_admin(auth):
        return
    async with runtime.session_factory() as session:
        try:
            policy = await load_access_policy(session, tenant_id=auth.tenant_id)
        except AccessPolicyUnreadable as exc:
            raise ToolError(POLICY_UNREADABLE_REFUSAL) from exc
    if not policy.agent_channel_pins:
        return
    if callable(ma_agent):
        ma_agent = await ma_agent()
    names = agent_pin_names(ma_agent.name, ma_agent.metadata)
    if not pin_write_refused(
        policy,
        is_admin=False,
        agent_names=names,
        parent_channel_id=origin.parent_channel_id if origin is not None else None,
        thread_id=origin.thread_id if origin is not None else None,
    ):
        return
    async with runtime.session_factory() as session:
        administered = await load_administered_channel_ids(
            session,
            tenant_id=auth.tenant_id,
            platform=auth.platform or "",
            caller=channel_admin_caller(auth),
        )
    if not is_pin_administered(policy, agent_names=names, administered_channel_ids=administered):
        raise ToolError(
            f"'{ma_agent.name}': {PIN_WRITE_REFUSAL} No card was posted. Tell the caller to "
            "ask in one of that agent's channels, or ask an admin. Do not retry."
        )
