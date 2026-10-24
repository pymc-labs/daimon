"""self-hosted display records for the existing host reducers.

These are display compatibility objects, never provider requests or usage
meters. The accompanying neutral event retains provider provenance. Registration
belongs to the explicit native preparation/runtime module.
"""

from __future__ import annotations

from collections.abc import Sequence

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

PROFILE = "selfhosted.loop"
PROFILES = (PROFILE,)
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
    return "selfhosted:tool:" + call_id


def _action_input(action: RequiredAction) -> dict[str, JsonValue]:
    value = action.payload.get("input")
    if isinstance(value, dict):
        return dict(value)
    return {"value": value}


def confirmation_display_id(action: RequiredAction) -> str:
    if not action.call_id:
        raise UnsupportedCapability(("host_confirmation_call_identity",), PROFILE)
    return tool_display_id(action.call_id)


def display_event(event: Event, *, session_id: str) -> DisplayEvent | None:
    """Decode authoritative root display work without inspecting SDK/native JSON.

    A function result request is not an approval. Until its host executor is
    bound, refuse that action instead of letting AutoApprove answer it. Usage
    travels on the separate UsageObservation seam, never as a model span.
    """
    if event.session_id != session_id or event.native.provider != "selfhosted":
        raise ScopeViolation(session_id, "foreign event in self-hosted host codec")
    if event.authority not in ("record", "reconciled") or event.thread_id is not None:
        return None
    if event.type == "native.selfhosted.checkpoint_warning":
        return BetaManagedAgentsAgentMessageEvent.model_validate(
            {
                "id": "selfhosted:checkpoint-warning:" + session_id,
                "type": "agent.message",
                "processed_at": event.occurred_at or event.observed_at,
                "content": [
                    {
                        "type": "text",
                        "text": "Recent messages and files may be lost after a restart.",
                    }
                ],
            }
        )
    payload = event.typed_payload()
    timestamp = event.occurred_at or event.observed_at
    if isinstance(payload, UserMessagePayload):
        if any(not isinstance(part, TextPart | ImagePart) for part in payload.content):
            raise UnsupportedCapability(("host_input_content",), PROFILE)
        return BetaManagedAgentsUserMessageEvent.model_validate(
            {
                "id": "selfhosted:input:" + payload.input_id,
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
                "id": "selfhosted:turn:" + payload.root_turn_id + ":running",
                "type": "session.status_running",
                "processed_at": timestamp,
            }
        )
    if isinstance(payload, AgentMessagePayload) and payload.complete:
        return BetaManagedAgentsAgentMessageEvent.model_validate(
            {
                "id": "selfhosted:item:" + payload.item_id,
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
        server, separator, name = payload.tool_name.partition("/")
        if payload.mcp_server is not None or (payload.executor == "mcp" and separator):
            raw.update(
                type="agent.mcp_tool_use",
                mcp_server_name=payload.mcp_server or server,
                name=name if separator else payload.tool_name,
            )
            return BetaManagedAgentsAgentMCPToolUseEvent.model_validate(raw)
        return BetaManagedAgentsAgentToolUseEvent.model_validate(raw)
    if isinstance(payload, ToolResultPayload):
        return BetaManagedAgentsAgentToolResultEvent.model_validate(
            {
                "id": "selfhosted:result:" + payload.call_id,
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
                "id": "selfhosted:turn:" + payload.root_turn_id + ":ended",
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
        return BetaManagedAgentsSessionStatusIdleEvent.model_validate(
            {
                "id": event.id,
                "type": "session.status_idle",
                "processed_at": timestamp,
                "stop_reason": {
                    "type": "requires_action",
                    "event_ids": [confirmation_display_id(action) for action in payload.actions],
                },
            }
        )
    if event.type == "session.status_terminated":
        return BetaManagedAgentsSessionStatusTerminatedEvent.model_validate(
            {"id": event.id, "type": "session.status_terminated", "processed_at": timestamp}
        )
    if event.type == "session.error":
        # Usage errors stay in the neutral journal. The terminal outcome drives
        # the existing host failure path; unknown usage is replayed separately.
        return None
    return None


def display_batch(event: Event, *, session_id: str) -> tuple[DisplayEvent, ...]:
    """Show the exact requested tool and input before the approval pause."""
    displayed = display_event(event, session_id=session_id)
    if displayed is None:
        return ()
    payload = event.typed_payload()
    if not isinstance(payload, RequiresActionPayload):
        return (displayed,)
    requests: list[DisplayEvent] = []
    for action in payload.actions:
        qualified = str(action.payload.get("name", "tool"))
        server, separator, name = qualified.partition("/")
        event_class = (
            BetaManagedAgentsAgentMCPToolUseEvent
            if separator
            else BetaManagedAgentsAgentToolUseEvent
        )
        requests.append(
            event_class.model_validate(
                {
                    "id": confirmation_display_id(action),
                    "type": "agent.mcp_tool_use" if separator else "agent.tool_use",
                    **({"mcp_server_name": server} if separator else {}),
                    "processed_at": event.occurred_at or event.observed_at,
                    "name": name if separator else qualified,
                    "input": _action_input(action),
                    "evaluated_permission": "ask",
                }
            )
        )
    return (*requests, displayed)
