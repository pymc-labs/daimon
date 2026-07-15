"""Pure reducer for the turn pipeline.

Contract:

1. `apply(state, event)` returns a new `TurnState`. Never mutates `state`.
2. If `event.id` is already in `state.seen_event_ids`, returns `state`
   unchanged (dedup).
3. Otherwise, records the id and folds the event into state.
4. Event types outside the dispatched set fall through to a dedup-only
   update, so the driver may forward everything it receives.

The reducer has no clock, no randomness, no I/O. State produced by a fold
over `GET /v1/sessions/{id}/events` is bit-identical to the state produced
by folding over the live SSE stream.
"""

from __future__ import annotations

import dataclasses

from anthropic.types.beta.sessions import BetaManagedAgentsSessionEvent as SessionEvent
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
from anthropic.types.beta.sessions.beta_managed_agents_agent_tool_use_event import (
    BetaManagedAgentsAgentToolUseEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_error_event import (
    BetaManagedAgentsSessionErrorEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_error_event import (
    Error as SessionError,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import (
    BetaManagedAgentsSessionStatusIdleEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_status_terminated_event import (
    BetaManagedAgentsSessionStatusTerminatedEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_user_custom_tool_result_event import (
    BetaManagedAgentsUserCustomToolResultEvent,
)
from daimon.core.errors import TurnError
from daimon.core.turn.state import (
    ContentBlock,
    TextBlock,
    ToolUseBlock,
    TurnState,
    UsageTotals,
)


def apply(state: TurnState, event: SessionEvent) -> TurnState:
    """Fold a single SDK session event into `state` and return a new
    `TurnState`. Unhandled `event.type` values fall through to a dedup-only
    update — see contract point 4."""
    if event.id in state.seen_event_ids:
        return state
    seen = state.seen_event_ids | {event.id}

    match event.type:
        case "agent.message":
            assert isinstance(event, BetaManagedAgentsAgentMessageEvent)
            return _apply_agent_message(state, event, seen)
        case "agent.tool_use" | "agent.custom_tool_use" | "agent.mcp_tool_use":
            assert isinstance(
                event,
                BetaManagedAgentsAgentToolUseEvent
                | BetaManagedAgentsAgentCustomToolUseEvent
                | BetaManagedAgentsAgentMCPToolUseEvent,
            )
            return _apply_tool_use(state, event, seen)
        case "agent.tool_result" | "agent.mcp_tool_result" | "user.custom_tool_result":
            assert isinstance(
                event,
                BetaManagedAgentsAgentToolResultEvent
                | BetaManagedAgentsAgentMCPToolResultEvent
                | BetaManagedAgentsUserCustomToolResultEvent,
            )
            return _apply_tool_result(state, event, seen)
        case "session.status_idle":
            assert isinstance(event, BetaManagedAgentsSessionStatusIdleEvent)
            return dataclasses.replace(state, stop_reason=event.stop_reason, seen_event_ids=seen)
        case "session.error":
            assert isinstance(event, BetaManagedAgentsSessionErrorEvent)
            return dataclasses.replace(
                state, error=_to_turn_error(event.error), seen_event_ids=seen
            )
        case "session.status_terminated":
            assert isinstance(event, BetaManagedAgentsSessionStatusTerminatedEvent)
            return dataclasses.replace(
                state,
                error=TurnError(kind="upstream", message="session terminated by MA"),
                seen_event_ids=seen,
            )
        case "span.model_request_end":
            assert isinstance(event, BetaManagedAgentsSpanModelRequestEndEvent)
            u = event.model_usage
            t = state.usage_totals
            new_totals = UsageTotals(
                input_tokens=t.input_tokens + u.input_tokens,
                cache_creation_input_tokens=t.cache_creation_input_tokens
                + u.cache_creation_input_tokens,
                cache_read_input_tokens=t.cache_read_input_tokens + u.cache_read_input_tokens,
                output_tokens=t.output_tokens + u.output_tokens,
            )
            return dataclasses.replace(state, usage_totals=new_totals, seen_event_ids=seen)
        case _:
            return dataclasses.replace(state, seen_event_ids=seen)


def _apply_agent_message(
    state: TurnState,
    event: BetaManagedAgentsAgentMessageEvent,
    seen: frozenset[str],
) -> TurnState:
    """Concatenate `agent.message` text onto the last `TextBlock`, or open
    a new one if the last block is a tool use."""
    incoming = "".join(part.text for part in event.content)
    if not incoming:
        return dataclasses.replace(state, seen_event_ids=seen)

    if state.content and isinstance(state.content[-1], TextBlock):
        last = state.content[-1]
        grown = TextBlock(kind="text", text=last.text + incoming)
        new_content: list[ContentBlock] = [*state.content[:-1], grown]
    else:
        new_content = [*state.content, TextBlock(kind="text", text=incoming)]

    return dataclasses.replace(state, content=new_content, seen_event_ids=seen)


def _apply_tool_use(
    state: TurnState,
    event: BetaManagedAgentsAgentToolUseEvent
    | BetaManagedAgentsAgentCustomToolUseEvent
    | BetaManagedAgentsAgentMCPToolUseEvent,
    seen: frozenset[str],
) -> TurnState:
    """Append a pending `ToolUseBlock`. The block's `id` is the originating
    event id — the matching tool-result event references it by tool_use_id
    (or mcp_tool_use_id / custom_tool_use_id)."""
    evaluated_permission = getattr(event, "evaluated_permission", None)
    mcp_server_name = getattr(event, "mcp_server_name", None)
    block = ToolUseBlock(
        kind="tool_use",
        id=event.id,
        type=event.type,
        name=event.name,
        input=event.input,
        evaluated_permission=evaluated_permission,
        mcp_server_name=mcp_server_name,
    )
    return dataclasses.replace(state, content=[*state.content, block], seen_event_ids=seen)


def _apply_tool_result(
    state: TurnState,
    event: BetaManagedAgentsAgentToolResultEvent
    | BetaManagedAgentsAgentMCPToolResultEvent
    | BetaManagedAgentsUserCustomToolResultEvent,
    seen: frozenset[str],
) -> TurnState:
    """Complete or fail the `ToolUseBlock` whose id matches the result's
    pairing field. Pairing field differs by variant:

    - `agent.tool_result` → `event.tool_use_id`
    - `agent.mcp_tool_result` → `event.mcp_tool_use_id`
    - `user.custom_tool_result` → `event.custom_tool_use_id`

    Orphan results (no matching block — possible if a partial replay
    starts mid-pair) are recorded as dedup-only and synthesize no block.
    """
    if isinstance(event, BetaManagedAgentsAgentMCPToolResultEvent):
        pairing_id = event.mcp_tool_use_id
    elif isinstance(event, BetaManagedAgentsUserCustomToolResultEvent):
        pairing_id = event.custom_tool_use_id
    else:
        pairing_id = event.tool_use_id

    match_index: int | None = None
    for i, block in enumerate(state.content):
        if isinstance(block, ToolUseBlock) and block.id == pairing_id:
            match_index = i
            break

    if match_index is None:
        return dataclasses.replace(state, seen_event_ids=seen)

    matched = state.content[match_index]
    assert isinstance(matched, ToolUseBlock)

    is_error = bool(event.is_error)
    updated = dataclasses.replace(
        matched,
        status="failed" if is_error else "complete",
        result_content=event.content,
        is_error=is_error,
    )
    new_content: list[ContentBlock] = [
        *state.content[:match_index],
        updated,
        *state.content[match_index + 1 :],
    ]
    return dataclasses.replace(state, content=new_content, seen_event_ids=seen)


def _to_turn_error(sdk_error: SessionError) -> TurnError:
    """Wrap an SDK `Error` discriminated-union value into a `TurnError`.

    The reducer does not pattern-match the variant; downstream consumers
    (driver retry logic, renderer) inspect `cause` themselves. `message` is
    the SDK error's own `message` when present, otherwise the discriminator
    `type` so logs always carry something readable.
    """
    message = getattr(sdk_error, "message", None) or sdk_error.type
    return TurnError(kind="upstream", message=message, cause=sdk_error)
