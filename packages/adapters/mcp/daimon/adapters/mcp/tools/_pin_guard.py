"""The MCP side of the agent rule's write check (`daimon.core.agent_pins`).

Every tool that adds to or edits what an agent reaches calls
`require_pin_write_access` once it has resolved the target: the private-form
request tools with the turn origin they were called from, and the direct
configuration tools (`update_agent`, `attach_mcp_server`, `detach_mcp_server`,
`remove_agent_key`) with none: their `origin_context_id` only finds the agent. With no origin a
member is outside every rule's channels, so the direct configuration of an
agent with a rule needs an admin; a member inside its channels uses request tools.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import mcp_subject
from daimon.core.admin_actions import DelegatedAction
from daimon.core.agent_pins import (
    PIN_WRITE_REFUSAL,
    POLICY_UNREADABLE_REFUSAL,
)
from daimon.core.authz import (
    Action,
    Place,
    Surface,
    authorize,
    build_agent_ref,
)
from daimon.core.permissions import any_agent_rules
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.domain import TurnOriginRow
from fastmcp.exceptions import ToolError


def _trusted_credential(auth: AuthIdentity) -> bool:
    """The request's verified platform/grant verdict applies to configuration only.

    External keys cannot inherit an owner's authority. Personal user and
    verified chat credentials use the same live configuration check.
    """
    return not auth.is_external_agent and not auth.is_operator


async def require_pin_write_access(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    ma_agent: BetaManagedAgentsAgent | Callable[[], Awaitable[BetaManagedAgentsAgent]],
    origin: TurnOriginRow | None,
) -> None:
    """Raise unless this caller may change ``ma_agent`` from ``origin``.

    ``ma_agent`` may be a resolver, called only when the tenant has agent rules,
    for paths that don't otherwise look the agent up.

    A trusted admin credential may write from anywhere; agent keys never are trusted.
    """
    trusted = _trusted_credential(auth)
    if trusted and auth.admin_for(DelegatedAction.AGENT_SETTINGS):
        return
    async with runtime.session_factory() as session:
        try:
            policy = await load_access_policy(session, tenant_id=auth.tenant_id)
        except AccessPolicyUnreadable as exc:
            raise ToolError(POLICY_UNREADABLE_REFUSAL) from exc
    if not any_agent_rules(policy):
        return
    if callable(ma_agent):
        ma_agent = await ma_agent()
    subject = mcp_subject(auth)
    if not authorize(
        policy,
        subject=subject,
        action=Action.CONFIGURE,
        surface=Surface.CONFIG,
        agent=build_agent_ref(ma_agent.name, ma_agent.metadata),
        place=(
            Place.from_origin(
                parent_channel_id=origin.parent_channel_id, thread_id=origin.thread_id
            )
            if origin is not None
            else Place()
        ),
    ):
        raise ToolError(
            f"'{ma_agent.name}': {PIN_WRITE_REFUSAL} No card was posted. Tell the caller to "
            "ask in one of that agent's channels, or ask an admin. Do not retry."
        )
