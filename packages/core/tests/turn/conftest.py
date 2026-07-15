"""Factories for building SDK Managed Agents events in turn tests.

Kept test-only and minimal — promoting these to a package-level helper
would re-introduce a translation shim between test and production code.
Only the shapes the reducer tests actually need.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from anthropic.types.beta.sessions.beta_managed_agents_agent_custom_tool_use_event import (
    BetaManagedAgentsAgentCustomToolUseEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_agent_mcp_tool_result_event import (
    BetaManagedAgentsAgentMCPToolResultEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_agent_mcp_tool_use_event import (
    BetaManagedAgentsAgentMCPToolUseEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_agent_message_event import (
    BetaManagedAgentsAgentMessageEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_agent_tool_result_event import (
    BetaManagedAgentsAgentToolResultEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_agent_tool_result_event import (
    Content as ToolResultContent,
)
from anthropic.types.beta.sessions.beta_managed_agents_agent_tool_use_event import (
    BetaManagedAgentsAgentToolUseEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_model_overloaded_error import (
    BetaManagedAgentsModelOverloadedError,
)
from anthropic.types.beta.sessions.beta_managed_agents_model_rate_limited_error import (
    BetaManagedAgentsModelRateLimitedError,
)
from anthropic.types.beta.sessions.beta_managed_agents_retry_status_terminal import (
    BetaManagedAgentsRetryStatusTerminal,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_end_turn import (
    BetaManagedAgentsSessionEndTurn,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_error_event import (
    BetaManagedAgentsSessionErrorEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_error_event import (
    Error as SessionError,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_requires_action import (
    BetaManagedAgentsSessionRequiresAction,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_retries_exhausted import (
    BetaManagedAgentsSessionRetriesExhausted,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import (
    BetaManagedAgentsSessionStatusIdleEvent,
    StopReason,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_status_terminated_event import (
    BetaManagedAgentsSessionStatusTerminatedEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_text_block import (
    BetaManagedAgentsTextBlock,
)
from anthropic.types.beta.sessions.beta_managed_agents_user_custom_tool_result_event import (
    BetaManagedAgentsUserCustomToolResultEvent,
)

_T = datetime(2026, 1, 1, tzinfo=UTC)


def make_agent_message(*, event_id: str, text: str) -> BetaManagedAgentsAgentMessageEvent:
    return BetaManagedAgentsAgentMessageEvent(
        id=event_id,
        type="agent.message",
        content=[BetaManagedAgentsTextBlock(type="text", text=text)],
        processed_at=_T,
    )


def make_tool_use(
    *,
    event_id: str,
    name: str,
    input: dict[str, object] | None = None,
    evaluated_permission: Literal["allow", "ask", "deny"] | None = None,
) -> BetaManagedAgentsAgentToolUseEvent:
    return BetaManagedAgentsAgentToolUseEvent(
        id=event_id,
        type="agent.tool_use",
        name=name,
        input=input or {},
        evaluated_permission=evaluated_permission,
        processed_at=_T,
    )


def make_custom_tool_use(
    *, event_id: str, name: str, input: dict[str, object] | None = None
) -> BetaManagedAgentsAgentCustomToolUseEvent:
    return BetaManagedAgentsAgentCustomToolUseEvent(
        id=event_id,
        type="agent.custom_tool_use",
        name=name,
        input=input or {},
        processed_at=_T,
    )


def make_mcp_tool_use(
    *,
    event_id: str,
    name: str,
    mcp_server_name: str,
    input: dict[str, object] | None = None,
    evaluated_permission: Literal["allow", "ask", "deny"] | None = None,
) -> BetaManagedAgentsAgentMCPToolUseEvent:
    return BetaManagedAgentsAgentMCPToolUseEvent(
        id=event_id,
        type="agent.mcp_tool_use",
        name=name,
        input=input or {},
        mcp_server_name=mcp_server_name,
        evaluated_permission=evaluated_permission,
        processed_at=_T,
    )


def make_tool_result(
    *,
    event_id: str,
    tool_use_id: str,
    text: str | None = None,
    content: list[ToolResultContent] | None = None,
    is_error: bool | None = False,
) -> BetaManagedAgentsAgentToolResultEvent:
    if content is None:
        content = [BetaManagedAgentsTextBlock(type="text", text=text or "")]
    return BetaManagedAgentsAgentToolResultEvent(
        id=event_id,
        type="agent.tool_result",
        tool_use_id=tool_use_id,
        content=content,
        is_error=is_error,
        processed_at=_T,
    )


def make_mcp_tool_result(
    *,
    event_id: str,
    mcp_tool_use_id: str,
    text: str | None = None,
    content: list[ToolResultContent] | None = None,
    is_error: bool | None = False,
) -> BetaManagedAgentsAgentMCPToolResultEvent:
    if content is None:
        content = [BetaManagedAgentsTextBlock(type="text", text=text or "")]
    return BetaManagedAgentsAgentMCPToolResultEvent(
        id=event_id,
        type="agent.mcp_tool_result",
        mcp_tool_use_id=mcp_tool_use_id,
        content=content,
        is_error=is_error,
        processed_at=_T,
    )


def make_custom_tool_result(
    *,
    event_id: str,
    custom_tool_use_id: str,
    text: str | None = None,
    content: list[ToolResultContent] | None = None,
    is_error: bool | None = False,
) -> BetaManagedAgentsUserCustomToolResultEvent:
    if content is None:
        content = [BetaManagedAgentsTextBlock(type="text", text=text or "")]
    return BetaManagedAgentsUserCustomToolResultEvent(
        id=event_id,
        type="user.custom_tool_result",
        custom_tool_use_id=custom_tool_use_id,
        content=content,
        is_error=is_error,
        processed_at=_T,
    )


def make_status_idle(
    *,
    event_id: str,
    stop_reason: StopReason | None = None,
) -> BetaManagedAgentsSessionStatusIdleEvent:
    if stop_reason is None:
        stop_reason = BetaManagedAgentsSessionEndTurn(type="end_turn")
    return BetaManagedAgentsSessionStatusIdleEvent(
        id=event_id,
        type="session.status_idle",
        stop_reason=stop_reason,
        processed_at=_T,
    )


def make_requires_action(*, event_ids: list[str]) -> BetaManagedAgentsSessionRequiresAction:
    return BetaManagedAgentsSessionRequiresAction(type="requires_action", event_ids=event_ids)


def make_retries_exhausted() -> BetaManagedAgentsSessionRetriesExhausted:
    return BetaManagedAgentsSessionRetriesExhausted(type="retries_exhausted")


def make_end_turn() -> BetaManagedAgentsSessionEndTurn:
    return BetaManagedAgentsSessionEndTurn(type="end_turn")


def make_status_terminated(*, event_id: str) -> BetaManagedAgentsSessionStatusTerminatedEvent:
    return BetaManagedAgentsSessionStatusTerminatedEvent(
        id=event_id, type="session.status_terminated", processed_at=_T
    )


def make_session_error(
    *, event_id: str, error: SessionError | None = None, message: str = "rate limited"
) -> BetaManagedAgentsSessionErrorEvent:
    if error is None:
        error = BetaManagedAgentsModelRateLimitedError(
            type="model_rate_limited_error",
            message=message,
            retry_status=BetaManagedAgentsRetryStatusTerminal(type="terminal"),
        )
    return BetaManagedAgentsSessionErrorEvent(
        id=event_id, type="session.error", error=error, processed_at=_T
    )


def make_overloaded_error(*, message: str = "overloaded") -> BetaManagedAgentsModelOverloadedError:
    return BetaManagedAgentsModelOverloadedError(
        type="model_overloaded_error",
        message=message,
        retry_status=BetaManagedAgentsRetryStatusTerminal(type="terminal"),
    )
