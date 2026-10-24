"""A watch turn runs only on a session whose every capability-bearing field is
in an explicit allowlist. Any field that is set but not allowlisted (skills,
multiagent, or anything added later) refuses the turn before any provider
mutation. Tools are kept by allowlist (reviewed, explicitly named read tools),
not removed by denylist. Unlisted tools are disabled, and every retained tool
must pause for the current read/report decision."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from anthropic.types.beta import BetaManagedAgentsSession
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.stores.turn_origins import get_active_origin
from daimon.core.stores.watches import get_firing, get_watch, watch_seed
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall, ToolSafetyPolicy, classify_tool
from daimon.core.turn import unattended_capabilities as capabilities
from daimon.core.turn.admission import Admission
from daimon.core.turn.approvals import RuleLookup, unattended_decider
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.posture import PolicyApproval, ToolConfirmationResult
from daimon.core.turn_origin import current_execution_origin_id
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def read_report_policy(policy: ToolSafetyPolicy) -> ToolSafetyPolicy:
    # Keep denials and write pins; discard grants that widen watch authority.
    return ToolSafetyPolicy(
        enabled=True,
        agent_rules=policy.agent_rules,
        denied=policy.denied,
        effects={key: value for key, value in policy.effects.items() if value == "write"},
    )


def watch_posture(
    policy: ToolSafetyPolicy, *, rules_for: RuleLookup | None = None
) -> PolicyApproval:
    strict = read_report_policy(policy)
    decide = unattended_decider(strict, rules_for=rules_for)

    async def read_only(call: ToolCall) -> ToolConfirmationResult:
        if call.server_name in (DAIMON_SERVER_NAME, "sandbox"):
            return ToolConfirmationResult(allow=False, deny_message="Watches only read and report.")
        assert call.server_name is not None  # Shared capability_posture refused native calls.
        if classify_tool(strict, server_name=call.server_name, tool_name=call.tool_name) != "read":
            return ToolConfirmationResult(allow=False, deny_message="Watches refuse writes.")
        return await decide(call)

    return capabilities.capability_posture(PolicyApproval(decide=read_only))


async def require_watch_send(
    sessions: async_sessionmaker[AsyncSession],
    row: TaskContinuationRow,
    admission: Admission,
    *,
    session_id: str,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
) -> None:
    origin_id = current_execution_origin_id.get()
    if origin_id is None:
        raise AdmissionDenied(reason="invoker_not_allowed")
    async with sessions() as db:
        seed = await watch_seed(db, row, now=datetime.now(UTC))
        firing = await get_firing(db, row.idempotency_key)
        watch = None if firing is None else await get_watch(db, firing.watch_id)
        origin = await get_active_origin(
            db,
            origin_id=origin_id,
            tenant_id=tenant_id,
            account_id=admission.account_id,
            platform=platform,
            now=datetime.now(UTC),
        )
        live = await get_live_thread_session(
            db,
            tenant_id=tenant_id,
            platform=platform,
            thread_id=thread_id,
            account_id=admission.account_id,
        )

    if (
        seed is None
        or origin is None
        or origin.attended
        or not origin.chat_session_id
        or live is None
        or live.ma_session_id != session_id
        or live.effective_config is None
        or live.effective_config.chat_session_id != origin.chat_session_id
        or (origin.parent_channel_id, origin.thread_id, origin.responder_ma_agent_id)
        != (row.parent_channel_id, thread_id, admission.agent.id)
        or watch is None
        or watch.session_id != session_id
        or (
            row.tenant_id,
            row.platform,
            row.thread_id,
            row.requester_account_id,
            row.target_ma_agent_id,
        )
        != (tenant_id, platform, thread_id, admission.account_id, admission.agent.id)
    ):
        raise AdmissionDenied(reason="invoker_not_allowed")


def validate_watch_snapshot(snapshot: BetaManagedAgentsSession) -> None:
    """Shared watch policy plus refusal of duplicate server toolsets."""
    allowed = capabilities.capability_policy("watch")
    if allowed.allow_builtins or allowed.allow_skills:
        raise AdmissionDenied(reason="backend_unsupported")
    capabilities.require_allowed_capabilities(snapshot, allow_skills=allowed.allow_skills)
    servers = [tool.mcp_server_name for tool in snapshot.agent.tools if tool.type == "mcp_toolset"]
    if len(servers) != len(set(servers)):
        raise AdmissionDenied(reason="backend_unsupported")


async def watch_capabilities(
    snapshot: BetaManagedAgentsSession,
    policy: ToolSafetyPolicy,
    *,
    public_url: str | None,
    sessions: async_sessionmaker[AsyncSession] | None,
    tenant_id: uuid.UUID,
) -> capabilities.ConstrainedSession:
    """Prepare enabled named reads through the shared policy, before mutation."""
    validate_watch_snapshot(snapshot)
    strict = read_report_policy(policy)
    servers = {server.name for server in snapshot.agent.mcp_servers}
    allowlist = frozenset(
        capabilities.ServerTool(tool.mcp_server_name, config.name)
        for tool in snapshot.agent.tools
        if tool.type == "mcp_toolset" and tool.mcp_server_name in servers
        for config in tool.configs
        if config.enabled
        and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", config.name)
        and classify_tool(strict, server_name=tool.mcp_server_name, tool_name=config.name) == "read"
    )
    constrained = await capabilities.constrain_capabilities(
        snapshot,
        strict,
        public_url=public_url,
        session_factory=sessions,
        tenant_id=tenant_id,
        allow_skills=capabilities.capability_policy("watch").allow_skills,
        tool_allowlist=allowlist,
    )
    send = capabilities.SessionToolConfig(
        tools=[tool for tool in constrained.send.tools if tool["type"] == "mcp_toolset"],
        mcp_servers=constrained.send.mcp_servers,
    )
    return capabilities.ConstrainedSession(
        send=send, original=constrained.original, changed=send != constrained.original
    )
