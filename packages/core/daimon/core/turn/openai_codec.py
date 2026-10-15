"""OpenAI display records for the existing host reducers.

These are display compatibility objects, never provider requests or usage
meters. The accompanying neutral event retains provider provenance. Registration
belongs to the explicit native preparation/runtime module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from anthropic.types.beta.sessions import (
    BetaManagedAgentsAgentMCPToolUseEvent,
    BetaManagedAgentsAgentMessageEvent,
    BetaManagedAgentsAgentToolResultEvent,
    BetaManagedAgentsAgentToolUseEvent,
    BetaManagedAgentsSessionStatusIdleEvent,
    BetaManagedAgentsSessionStatusRunningEvent,
    BetaManagedAgentsSessionStatusTerminatedEvent,
    BetaManagedAgentsUserMessageEvent,
)
from mux.contracts.events import (
    AgentMessagePayload,
    ContentPart,
    Event,
    ImagePart,
    RequiredAction,
    RequiresActionPayload,
    StatusRunningPayload,
    TextPart,
    ToolResultPayload,
    ToolUsePayload,
    TurnEndedPayload,
    UserMessagePayload,
)
from mux.errors import ScopeViolation, UnsupportedCapability
from pydantic import JsonValue

PROFILE = "openai.persistent_workspace"
type DisplayEvent = (
    BetaManagedAgentsAgentMessageEvent
    | BetaManagedAgentsAgentToolUseEvent
    | BetaManagedAgentsAgentMCPToolUseEvent
    | BetaManagedAgentsAgentToolResultEvent
    | BetaManagedAgentsSessionStatusIdleEvent
    | BetaManagedAgentsSessionStatusRunningEvent
    | BetaManagedAgentsSessionStatusTerminatedEvent
    | BetaManagedAgentsUserMessageEvent
)


def _text_content(parts: Sequence[ContentPart]) -> list[dict[str, str]]:
    if any(not isinstance(part, TextPart) for part in parts):
        raise UnsupportedCapability(("host_output_content",), PROFILE)
    return [{"type": "text", "text": part.text} for part in parts if isinstance(part, TextPart)]


def tool_display_id(call_id: str) -> str:
    """Results and approvals refer to the same invocation across live/replay."""
    return "openai:tool:" + call_id


def _origin_input(action: RequiredAction, turn_id: str | None) -> dict[str, JsonValue]:
    request = action.payload.get("request")
    if (
        action.native_type != "computer_use_approval_request"
        or action.payload.get("type") != "computer_use_approval_request"
        or action.id != action.payload.get("request_id")
        or turn_id is None
        or action.payload.get("turn_id") != turn_id
        or not isinstance(request, Mapping)
        or request.get("type") != "browser_origin_access"
    ):
        raise UnsupportedCapability(("host_required_action_executor",), PROFILE)
    origin, reason = request.get("origin"), request.get("reason")
    if (
        not isinstance(origin, str)
        or not origin
        or (reason is not None and not isinstance(reason, str))
    ):
        raise ValueError("invalid origin approval display")
    result: dict[str, JsonValue] = {"origin": origin}
    if reason is not None:
        result["reason"] = reason
    return result


def display_event(event: Event, *, session_id: str) -> DisplayEvent | None:
    """Decode authoritative root display work without inspecting SDK/native JSON.

    A function result request is not an approval. Until its host executor is
    bound, refuse that action instead of letting AutoApprove answer it. Usage
    travels on the separate UsageObservation seam, never as a model span.
    """
    if event.session_id != session_id or event.native.provider != "openai":
        raise ScopeViolation(session_id, "foreign event in OpenAI host codec")
    if event.authority not in ("record", "reconciled") or event.thread_id is not None:
        return None
    payload = event.typed_payload()
    timestamp = event.occurred_at or event.observed_at
    if isinstance(payload, UserMessagePayload):
        if any(not isinstance(part, TextPart | ImagePart) for part in payload.content):
            raise UnsupportedCapability(("host_input_content",), PROFILE)
        return BetaManagedAgentsUserMessageEvent.model_validate(
            {
                "id": "openai:input:" + payload.input_id,
                "type": "user.message",
                "processed_at": timestamp,
                "content": [
                    {"type": "text", "text": part.text}
                    if isinstance(part, TextPart)
                    else {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": part.media_type,
                            "data": part.data_base64,
                        },
                    }
                    for part in payload.content
                    if isinstance(part, TextPart | ImagePart)
                ],
            }
        )
    if isinstance(payload, StatusRunningPayload):
        if event.turn_id != payload.root_turn_id:
            raise ValueError("running event lacks matching root identity")
        return BetaManagedAgentsSessionStatusRunningEvent.model_validate(
            {
                "id": "openai:turn:" + payload.root_turn_id + ":running",
                "type": "session.status_running",
                "processed_at": timestamp,
            }
        )
    if isinstance(payload, AgentMessagePayload) and payload.complete:
        return BetaManagedAgentsAgentMessageEvent.model_validate(
            {
                "id": "openai:item:" + payload.item_id,
                "type": "agent.message",
                "processed_at": timestamp,
                "content": _text_content(payload.content),
            }
        )
    if isinstance(payload, ToolUsePayload):
        raw = {
            "id": tool_display_id(payload.call_id),
            "type": "agent.tool_use",
            "processed_at": timestamp,
            "name": payload.tool_name,
            "input": dict(payload.input),
            "evaluated_permission": "ask" if payload.permission == "ask" else "allow",
        }
        if payload.executor == "mcp":
            if payload.mcp_server is None:
                raise ValueError("MCP invocation lacks its server identity")
            return BetaManagedAgentsAgentMCPToolUseEvent.model_validate(
                {**raw, "type": "agent.mcp_tool_use", "mcp_server_name": payload.mcp_server}
            )
        return BetaManagedAgentsAgentToolUseEvent.model_validate(raw)
    if isinstance(payload, ToolResultPayload):
        return BetaManagedAgentsAgentToolResultEvent.model_validate(
            {
                "id": "openai:result:" + payload.call_id,
                "type": "agent.tool_result",
                "processed_at": timestamp,
                "tool_use_id": tool_display_id(payload.call_id),
                "content": _text_content(payload.content),
                "is_error": payload.is_error,
            }
        )
    if isinstance(payload, TurnEndedPayload):
        if event.turn_id != payload.root_turn_id:
            raise ValueError("terminal event lacks matching root identity")
        return BetaManagedAgentsSessionStatusIdleEvent.model_validate(
            {
                "id": "openai:turn:" + payload.root_turn_id + ":ended",
                "type": "session.status_idle",
                "processed_at": timestamp,
                "stop_reason": {
                    "type": "retries_exhausted" if payload.outcome == "errored" else "end_turn"
                },
            }
        )
    if isinstance(payload, RequiresActionPayload):
        if not payload.actions or any(
            action.kind != "tool_confirmation" for action in payload.actions
        ):
            raise UnsupportedCapability(("host_required_action_executor",), PROFILE)
        for action in payload.actions:
            _origin_input(action, event.turn_id)
        return BetaManagedAgentsSessionStatusIdleEvent.model_validate(
            {
                "id": event.id,
                "type": "session.status_idle",
                "processed_at": timestamp,
                "stop_reason": {
                    "type": "requires_action",
                    "event_ids": [tool_display_id(action.id) for action in payload.actions],
                },
            }
        )
    if event.type == "session.status_terminated":
        return BetaManagedAgentsSessionStatusTerminatedEvent.model_validate(
            {"id": event.id, "type": "session.status_terminated", "processed_at": timestamp}
        )
    if event.type == "session.error":
        # Do not disguise a provider error as an Anthropic SDK error variant.
        raise UnsupportedCapability(("host_neutral_error_reducer",), PROFILE)
    return None


def display_batch(event: Event, *, session_id: str) -> tuple[DisplayEvent, ...]:
    """A native approval supplies its visible origin before the pause.

    These are display records for the observed request, not a claim that a
    command executed. Authentication fields are never rendered as a tool call.
    """
    displayed = display_event(event, session_id=session_id)
    if displayed is None:
        return ()
    payload = event.typed_payload()
    if not isinstance(payload, RequiresActionPayload):
        return (displayed,)
    requests: list[DisplayEvent] = []
    for action in payload.actions:
        requests.append(
            BetaManagedAgentsAgentToolUseEvent.model_validate(
                {
                    "id": tool_display_id(action.id),
                    "type": "agent.tool_use",
                    "processed_at": event.occurred_at or event.observed_at,
                    "name": "browser_origin_access",
                    "input": _origin_input(action, event.turn_id),
                    "evaluated_permission": "ask",
                }
            )
        )
    return (*requests, displayed)
