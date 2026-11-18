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
from dataclasses import replace

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import mcp_subject, origin_proves_presence
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
    origin_ids,
)
from daimon.core.permissions import SEALED_REFUSAL, any_agent_rules, any_own_readers
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
    action: DelegatedAction | None = DelegatedAction.AGENT_SETTINGS,
) -> None:
    """Raise unless this caller may change ``ma_agent`` from ``origin``.

    ``ma_agent`` may be a resolver, called only when the tenant has agent rules,
    for paths that don't otherwise look the agent up.

    A trusted admin credential may write from anywhere; agent keys never are trusted.
    """
    admin = _trusted_credential(auth) and auth.admin_for(action)
    async with runtime.session_factory() as session:
        try:
            policy = await load_access_policy(session, tenant_id=auth.tenant_id)
        except AccessPolicyUnreadable as exc:
            raise ToolError(POLICY_UNREADABLE_REFUSAL) from exc
    # A trusted admin may write from anywhere, except to a private channel's own agent.
    if admin and not any_own_readers(policy):
        return
    if not admin and not any_agent_rules(policy):
        return
    if callable(ma_agent):
        ma_agent = await ma_agent()
    subject = mcp_subject(auth, is_admin=admin)
    if origin is not None and (auth.is_external_agent or not origin_proves_presence(auth, origin)):
        origin = None  # an unattended or out-of-hold origin proves nothing
    if origin is not None:
        subject = replace(
            subject, origin_channel_ids=origin_ids(origin.parent_channel_id, origin.thread_id)
        )
    decision = authorize(
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
    )
    if not decision:
        if decision.reason == "sealed_channel":
            raise ToolError(
                f"'{ma_agent.name}': {SEALED_REFUSAL} Nothing was changed. Do not retry."
            )
        raise ToolError(
            f"'{ma_agent.name}': {PIN_WRITE_REFUSAL} No card was posted. Tell the caller to "
            "ask in one of that agent's channels, or ask an admin. Do not retry."
        )
