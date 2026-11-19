"""OpenAI and self-hosted display records for the existing host reducers.

These are display compatibility objects, never provider requests or usage
meters. The accompanying neutral event retains provider provenance. The two
backends differ only in how an approval is shown: OpenAI asks for browser
origin access, self-hosted asks for the exact tool and input.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

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


@dataclass(frozen=True)
class HostedCodec:
    """One hosted backend's identity: its provider, profile and wording."""

    provider: Literal["openai", "selfhosted"]
    profile: str
    #: Names the provider in transport errors.
    label: str
    #: Names the provider in codec errors.
    codec_label: str

    @property
    def profiles(self) -> tuple[str, ...]:
        return (self.profile,)

    def tool_display_id(self, call_id: str) -> str:
        """Results and approvals refer to the same invocation across live/replay."""
        return self.provider + ":tool:" + call_id


OPENAI = HostedCodec("openai", "openai.persistent_workspace", "OpenAI", "OpenAI")
SELFHOSTED = HostedCodec("selfhosted", "selfhosted.loop", "SelfHosted", "self-hosted")

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


def _text_content(parts: Sequence[ContentPart], codec: HostedCodec) -> list[dict[str, str]]:
    if any(not isinstance(part, TextPart) for part in parts):
        raise UnsupportedCapability(("host_output_content",), codec.profile)
    return [{"type": "text", "text": part.text} for part in parts if isinstance(part, TextPart)]


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
        raise UnsupportedCapability(("host_required_action_executor",), OPENAI.profile)
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


def confirmation_display_id(action: RequiredAction, codec: HostedCodec) -> str:
    """OpenAI shows an approval under its request id; self-hosted under its tool call."""
    if codec is OPENAI:
        return codec.tool_display_id(action.id)
    if not action.call_id:
        raise UnsupportedCapability(("host_confirmation_call_identity",), codec.profile)
    return codec.tool_display_id(action.call_id)


def _approval_request(action: RequiredAction, event: Event, codec: HostedCodec) -> DisplayEvent:
    timestamp = event.occurred_at or event.observed_at
    if codec is OPENAI:
        return BetaManagedAgentsAgentToolUseEvent.model_validate(
            {
                "id": confirmation_display_id(action, codec),
                "type": "agent.tool_use",
                "processed_at": timestamp,
                "name": "browser_origin_access",
                "input": _origin_input(action, event.turn_id),
                "evaluated_permission": "ask",
            }
        )
    value = action.payload.get("input")
    qualified = str(action.payload.get("name", "tool"))
    server, separator, name = qualified.partition("/")
    raw: dict[str, object] = {
        "id": confirmation_display_id(action, codec),
        "type": "agent.mcp_tool_use" if separator else "agent.tool_use",
        **({"mcp_server_name": server} if separator else {}),
        "processed_at": timestamp,
        "name": name if separator else qualified,
        "input": dict(value) if isinstance(value, dict) else {"value": value},
        "evaluated_permission": "ask",
    }
    if separator:
        return BetaManagedAgentsAgentMCPToolUseEvent.model_validate(raw)
    return BetaManagedAgentsAgentToolUseEvent.model_validate(raw)


def _tool_use(payload: ToolUsePayload, raw: dict[str, object], codec: HostedCodec) -> DisplayEvent:
    if codec is OPENAI:
        if payload.executor == "mcp":
            if payload.mcp_server is None:
                raise ValueError("MCP invocation lacks its server identity")
            return BetaManagedAgentsAgentMCPToolUseEvent.model_validate(
                {**raw, "type": "agent.mcp_tool_use", "mcp_server_name": payload.mcp_server}
            )
        return BetaManagedAgentsAgentToolUseEvent.model_validate(raw)
    server, separator, name = payload.tool_name.partition("/")
    if payload.mcp_server is not None or (payload.executor == "mcp" and separator):
        raw.update(
            type="agent.mcp_tool_use",
            mcp_server_name=payload.mcp_server or server,
            name=name if separator else payload.tool_name,
        )
        return BetaManagedAgentsAgentMCPToolUseEvent.model_validate(raw)
    return BetaManagedAgentsAgentToolUseEvent.model_validate(raw)


def display_event(event: Event, *, session_id: str, codec: HostedCodec) -> DisplayEvent | None:
    """Decode authoritative root display work without inspecting SDK/native JSON.

    An OpenAI function result request is not an approval. Until its host executor
    is bound, refuse that action instead of letting AutoApprove answer it. Usage
    travels on the separate UsageObservation seam, never as a model span.
    """
    if event.session_id != session_id or event.native.provider != codec.provider:
        raise ScopeViolation(session_id, f"foreign event in {codec.codec_label} host codec")
    if event.authority not in ("record", "reconciled") or event.thread_id is not None:
        return None
    prefix = codec.provider + ":"
    timestamp = event.occurred_at or event.observed_at
    if codec is SELFHOSTED and event.type == "native.selfhosted.checkpoint_warning":
        return BetaManagedAgentsAgentMessageEvent.model_validate(
            {
                "id": "selfhosted:checkpoint-warning:" + session_id,
                "type": "agent.message",
                "processed_at": timestamp,
                "content": [
                    {
                        "type": "text",
                        "text": "Recent messages and files may be lost after a restart.",
                    }
                ],
            }
        )
    payload = event.typed_payload()
    if isinstance(payload, UserMessagePayload):
        if any(not isinstance(part, TextPart | ImagePart) for part in payload.content):
            raise UnsupportedCapability(("host_input_content",), codec.profile)
        return BetaManagedAgentsUserMessageEvent.model_validate(
            {
                "id": prefix + "input:" + payload.input_id,
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
                "id": prefix + "turn:" + payload.root_turn_id + ":running",
                "type": "session.status_running",
                "processed_at": timestamp,
            }
        )
    if isinstance(payload, AgentMessagePayload) and payload.complete:
        return BetaManagedAgentsAgentMessageEvent.model_validate(
            {
                "id": prefix + "item:" + payload.item_id,
                "type": "agent.message",
                "processed_at": timestamp,
                "content": _text_content(payload.content, codec),
            }
        )
    if isinstance(payload, ToolUsePayload):
        raw: dict[str, object] = {
            "id": codec.tool_display_id(payload.call_id),
            "type": "agent.tool_use",
            "processed_at": timestamp,
            "name": payload.tool_name,
            "input": dict(payload.input),
            "evaluated_permission": "ask" if payload.permission == "ask" else "allow",
        }
        return _tool_use(payload, raw, codec)
    if isinstance(payload, ToolResultPayload):
        return BetaManagedAgentsAgentToolResultEvent.model_validate(
            {
                "id": prefix + "result:" + payload.call_id,
                "type": "agent.tool_result",
                "processed_at": timestamp,
                "tool_use_id": codec.tool_display_id(payload.call_id),
                "content": _text_content(payload.content, codec),
                "is_error": payload.is_error,
            }
        )
    if isinstance(payload, TurnEndedPayload):
        if event.turn_id != payload.root_turn_id:
            raise ValueError("terminal event lacks matching root identity")
        return BetaManagedAgentsSessionStatusIdleEvent.model_validate(
            {
                "id": prefix + "turn:" + payload.root_turn_id + ":ended",
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
            raise UnsupportedCapability(("host_required_action_executor",), codec.profile)
        if codec is OPENAI:
            for action in payload.actions:
                _origin_input(action, event.turn_id)
        return BetaManagedAgentsSessionStatusIdleEvent.model_validate(
            {
                "id": event.id,
                "type": "session.status_idle",
                "processed_at": timestamp,
                "stop_reason": {
                    "type": "requires_action",
                    "event_ids": [
                        confirmation_display_id(action, codec) for action in payload.actions
                    ],
                },
            }
        )
    if event.type == "session.status_terminated":
        return BetaManagedAgentsSessionStatusTerminatedEvent.model_validate(
            {"id": event.id, "type": "session.status_terminated", "processed_at": timestamp}
        )
    # A self-hosted usage error stays in the neutral journal: the terminal outcome
    # drives the host failure path and unknown usage is replayed separately.
    if event.type == "session.error" and codec is OPENAI:
        # Do not disguise a provider error as an Anthropic SDK error variant.
        raise UnsupportedCapability(("host_neutral_error_reducer",), codec.profile)
    return None


def display_batch(event: Event, *, session_id: str, codec: HostedCodec) -> tuple[DisplayEvent, ...]:
    """Show what an approval asks for before its pause.

    These are display records for the observed request, not a claim that a
    command executed. Authentication fields are never rendered as a tool call.
    """
    displayed = display_event(event, session_id=session_id, codec=codec)
    if displayed is None:
        return ()
    payload = event.typed_payload()
    if not isinstance(payload, RequiresActionPayload):
        return (displayed,)
    return (*(_approval_request(action, event, codec) for action in payload.actions), displayed)
