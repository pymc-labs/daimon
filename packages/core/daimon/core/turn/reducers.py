"""Pure reducer for the turn pipeline.

Contract:

1. `apply(state, event)` returns a new `TurnState`. Never mutates `state`.
2. If `event.id` is already in `state.seen_event_ids`, returns `state`
   unchanged, except that a tool permission may become stricter.
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
from anthropic.types.beta.sessions.beta_managed_agents_mcp_authentication_failed_error import (
    BetaManagedAgentsMCPAuthenticationFailedError,
)
from anthropic.types.beta.sessions.beta_managed_agents_mcp_connection_failed_error import (
    BetaManagedAgentsMCPConnectionFailedError,
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
from daimon.core.pricing import LONG_CONTEXT_PROMPT_TOKENS
from daimon.core.turn.state import (
    ContentBlock,
    McpServerFailure,
    TextBlock,
    ToolUseBlock,
    TurnState,
    UsageTotals,
)
from daimon.core.turn.status_log import status_log_enabled
from daimon.core.turn.termination import TerminationReason
from mux.contracts.usage import UsageObservation
from pydantic import BaseModel


def apply_usage_observation(state: TurnState, usage: UsageObservation) -> TurnState:
    """Fold a real neutral usage snapshot independently of display/native events."""
    return dataclasses.replace(state, usage_totals=state.usage_totals.add_observation(usage))


def apply(
    state: TurnState, event: SessionEvent, *, usage: UsageObservation | None = None
) -> TurnState:
    """Fold a single SDK session event into `state` and return a new
    `TurnState`. Unhandled `event.type` values fall through to a dedup-only
    update — see contract point 4."""
    if event.id in state.seen_event_ids:
        if isinstance(
            event,
            BetaManagedAgentsAgentToolUseEvent
            | BetaManagedAgentsAgentCustomToolUseEvent
            | BetaManagedAgentsAgentMCPToolUseEvent,
        ):
            permission = getattr(event, "evaluated_permission", None)
            rank = {None: 0, "allow": 1, "ask": 2, "deny": 3}
            content = [
                dataclasses.replace(block, evaluated_permission=permission)
                if isinstance(block, ToolUseBlock)
                and block.id == event.id
                and rank[permission] > rank[block.evaluated_permission]
                else block
                for block in state.content
            ]
            if content != state.content:
                return dataclasses.replace(state, content=content)
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
            refusal = _refusal(event)
            if refusal is not None:
                # A refusal idles as a plain `end_turn`, usually with no text, which
                # would otherwise finalize as an empty success.
                return dataclasses.replace(
                    state,
                    stop_reason=event.stop_reason,
                    error=refusal,
                    termination=TerminationReason.REFUSED,
                    seen_event_ids=seen,
                )
            return dataclasses.replace(state, stop_reason=event.stop_reason, seen_event_ids=seen)
        case "session.error":
            assert isinstance(event, BetaManagedAgentsSessionErrorEvent)
            return _apply_session_error(state, event.error, seen)
        case "session.status_terminated":
            assert isinstance(event, BetaManagedAgentsSessionStatusTerminatedEvent)
            return dataclasses.replace(
                state,
                error=TurnError(kind="upstream", message="session terminated by MA"),
                termination=TerminationReason.SESSION_TERMINATED,
                seen_event_ids=seen,
            )
        case "span.model_request_end":
            assert isinstance(event, BetaManagedAgentsSpanModelRequestEndEvent)
            if usage is not None:
                if usage.id != event.id:
                    raise ValueError("usage observation does not belong to this event")
                return dataclasses.replace(
                    state,
                    usage_totals=state.usage_totals.add_observation(usage),
                    seen_event_ids=seen,
                )
            u = event.model_usage
            t = state.usage_totals
            new_totals = UsageTotals(
                input_tokens=t.input_tokens + u.input_tokens,
                cache_creation_input_tokens=t.cache_creation_input_tokens
                + u.cache_creation_input_tokens,
                cache_read_input_tokens=t.cache_read_input_tokens + u.cache_read_input_tokens,
                output_tokens=t.output_tokens + u.output_tokens,
            )
            long_totals = state.long_prompt_usage_totals
            if (
                u.input_tokens + u.cache_creation_input_tokens + u.cache_read_input_tokens
                > LONG_CONTEXT_PROMPT_TOKENS
            ):
                long_totals = UsageTotals(
                    input_tokens=long_totals.input_tokens + u.input_tokens,
                    cache_creation_input_tokens=long_totals.cache_creation_input_tokens
                    + u.cache_creation_input_tokens,
                    cache_read_input_tokens=long_totals.cache_read_input_tokens
                    + u.cache_read_input_tokens,
                    output_tokens=long_totals.output_tokens + u.output_tokens,
                )
            return dataclasses.replace(
                state,
                usage_totals=new_totals,
                long_prompt_usage_totals=long_totals,
                seen_event_ids=seen,
            )
        case _:
            return dataclasses.replace(state, seen_event_ids=seen)


def _apply_agent_message(
    state: TurnState,
    event: BetaManagedAgentsAgentMessageEvent,
    seen: frozenset[str],
) -> TurnState:
    """Fold the message into the turn's text.

    With `turn.status_log` on, the message is its own `TextBlock`: an
    `agent.message` event is a whole message (Managed Agents streams its deltas
    as separate previews that never reach this reducer), so consecutive messages
    stay apart and a short status line written just before the answer is not
    glued to the answer's first word. Off, the text is concatenated onto the last
    `TextBlock`, or opens a new one if the last block is a tool use.
    """
    incoming = "".join(part.text for part in event.content)
    if not incoming:
        return dataclasses.replace(state, seen_event_ids=seen)

    if not status_log_enabled() and state.content and isinstance(state.content[-1], TextBlock):
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
    The call's id joins `finished_tool_ids` the first time it gets a result.
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
    finished = state.finished_tool_ids
    if matched.id not in finished:
        finished = (*finished, matched.id)
    return dataclasses.replace(
        state, content=new_content, finished_tool_ids=finished, seen_event_ids=seen
    )


_MCP_FAILURE_TYPES = ("mcp_connection_failed_error", "mcp_authentication_failed_error")


class _StopDetails(BaseModel):
    type: str
    category: str | None = None


def _refusal(event: BetaManagedAgentsSessionStatusIdleEvent) -> TurnError | None:
    """The API's refusal, read from `stop_details`, which the pinned SDK keeps as an extra."""
    raw: object = (event.model_extra or {}).get("stop_details")
    if raw is None:
        return None
    details = _StopDetails.model_validate(raw)
    if details.type != "refusal":
        return None
    return TurnError(
        kind="refused",
        message=f"the model refused the request ({details.category or 'no category'})",
    )


def _apply_session_error(
    state: TurnState, sdk_error: SessionError, seen: frozenset[str]
) -> TurnState:
    """Fold one `session.error` by what MA says the client should do next.

    Every variant carries `retry_status`. `retrying` means MA will fire the
    same error again as `exhausted` or `terminal` once its budget runs out, so
    an early copy must not brand the turn failed while the model is still
    working. An MCP connection or authentication failure that is not
    `terminal` is the degraded case: MA drops that server and keeps the
    session running, the model answers with the rest of its tools, and the
    session idles cleanly — so it lands in `mcp_failures`, newest status per
    server, and never in `error`. Everything else is the turn's error, as
    before.
    """
    retry_status = sdk_error.retry_status.type
    if sdk_error.type in _MCP_FAILURE_TYPES and retry_status != "terminal":
        assert isinstance(
            sdk_error,
            BetaManagedAgentsMCPAuthenticationFailedError
            | BetaManagedAgentsMCPConnectionFailedError,
        )
        failure = McpServerFailure(
            server_name=sdk_error.mcp_server_name,
            error_type=sdk_error.type,
            message=sdk_error.message,
            retry_status=retry_status,
        )
        kept = tuple(f for f in state.mcp_failures if f.server_name != failure.server_name)
        return dataclasses.replace(state, mcp_failures=(*kept, failure), seen_event_ids=seen)
    if retry_status == "retrying":
        # Kept aside, not dropped: should the stream end with no settled
        # copy and no output, the finalizer surfaces it rather than a blank.
        return dataclasses.replace(
            state, retrying_error=_to_turn_error(sdk_error), seen_event_ids=seen
        )
    return dataclasses.replace(state, error=_to_turn_error(sdk_error), seen_event_ids=seen)


def _to_turn_error(sdk_error: SessionError) -> TurnError:
    """Wrap an SDK `Error` discriminated-union value into a `TurnError`.

    The reducer does not pattern-match the variant; downstream consumers
    (driver retry logic, renderer) inspect `cause` themselves. `message` is
    the SDK error's own `message` when present, otherwise the discriminator
    `type` so logs always carry something readable.
    """
    message = getattr(sdk_error, "message", None) or sdk_error.type
    return TurnError(kind="upstream", message=message, cause=sdk_error)
