"""The SDK-independent HTTP/JSON contract for ``daimon-harness/0``.

Wire parts intentionally differ from mux content parts: images are always inline.
An event validates its data against its type before entering a durable journal.
"""

from __future__ import annotations

from typing import Annotated, Literal, cast

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

PROTOCOL = "daimon-harness/0"


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResponseModel(WireModel):
    """Readers tolerate fields added by newer harness versions."""

    model_config = ConfigDict(extra="ignore")


class TextPart(WireModel):
    type: Literal["text"] = "text"
    text: str


class ImagePart(WireModel):
    type: Literal["image"] = "image"
    media_type: str
    data_b64: str


Part = Annotated[TextPart | ImagePart, Field(discriminator="type")]


class MCPServer(WireModel):
    name: str
    url: str
    headers_env: dict[str, str] = Field(default_factory=dict)


class InlineSkill(WireModel):
    name: str
    files: dict[str, str]


class URLSkill(WireModel):
    name: str
    url: str


Skill = InlineSkill | URLSkill


class Limits(WireModel):
    max_tool_calls: int = Field(ge=0)
    deadline_s: float = Field(gt=0, allow_inf_nan=False)


class TurnRequest(WireModel):
    session_id: str
    turn_id: str
    idempotency_key: str
    model: str
    instructions: str
    input: list[Part]
    mcp_servers: list[MCPServer]
    skills: list[Skill]
    limits: Limits


class ActionRequest(WireModel):
    action_id: str
    decision: Literal["allow", "deny", "result"]
    result: JsonValue = None

    @model_validator(mode="after")
    def validate_decision(self) -> ActionRequest:
        has_result = "result" in self.model_fields_set
        if (self.decision == "result") != has_result:
            raise ValueError("result must be present exactly when decision is result")
        return self

    @model_serializer(mode="wrap")
    def serialize_decision(self, handler: SerializerFunctionWrapHandler) -> dict[str, JsonValue]:
        data = cast(dict[str, JsonValue], handler(self))
        if self.decision != "result":
            data.pop("result", None)
        return data


class CancelResponse(ResponseModel):
    cancelled: bool


class HealthResponse(ResponseModel):
    ok: Literal[True] = True
    protocol: Literal["daimon-harness/0"] = PROTOCOL
    version: str


class StatusRunningData(ResponseModel):
    pass


class AgentMessageDeltaData(ResponseModel):
    text: str


class AgentMessageData(ResponseModel):
    parts: list[Part]


class ToolUseData(ResponseModel):
    call_id: str
    name: str
    input: JsonValue
    executor: Literal["agent", "mcp"]


class ToolResultData(ResponseModel):
    call_id: str
    output: JsonValue
    is_error: bool


class RequiresActionData(ResponseModel):
    action_id: str
    kind: Literal["tool_confirmation"] = "tool_confirmation"
    call_id: str
    name: str
    input: JsonValue


class ModelRequestStartedData(ResponseModel):
    request_id: str
    model: str


class UsageData(ResponseModel):
    request_id: str
    model: str
    input_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(ge=0)


class TurnError(ResponseModel):
    kind: str
    message: str


class TurnEndedData(ResponseModel):
    outcome: Literal["completed", "interrupted", "errored"]
    error: TurnError | None = None

    @model_validator(mode="after")
    def require_error(self) -> TurnEndedData:
        if self.outcome == "errored" and self.error is None:
            raise ValueError("errored turn requires error details")
        return self


class ErrorData(TurnError):
    retryable: bool
    request_id: str | None = None


EventType = Literal[
    "status_running",
    "model_request_started",
    "agent_message_delta",
    "agent_message",
    "tool_use",
    "tool_result",
    "requires_action",
    "usage",
    "turn_ended",
    "error",
]
EventData = (
    StatusRunningData
    | AgentMessageDeltaData
    | AgentMessageData
    | ToolUseData
    | ToolResultData
    | RequiresActionData
    | ModelRequestStartedData
    | UsageData
    | TurnEndedData
    | ErrorData
)
DATA_MODELS: dict[EventType, type[WireModel]] = {
    "status_running": StatusRunningData,
    "agent_message_delta": AgentMessageDeltaData,
    "agent_message": AgentMessageData,
    "tool_use": ToolUseData,
    "tool_result": ToolResultData,
    "requires_action": RequiresActionData,
    "model_request_started": ModelRequestStartedData,
    "usage": UsageData,
    "turn_ended": TurnEndedData,
    "error": ErrorData,
}


class Event(ResponseModel):
    seq: int = Field(ge=0)
    session_id: str
    turn_id: str
    at: AwareDatetime
    type: EventType
    data: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_data(self) -> Event:
        DATA_MODELS[self.type].model_validate(self.data)
        return self

    def typed_data(self) -> WireModel:
        return DATA_MODELS[self.type].model_validate(self.data)


class EventsResponse(ResponseModel):
    events: list[Event]
    complete: bool


class InvocationRequest(WireModel):
    method: Literal["GET", "POST"]
    path: str
    query: dict[str, JsonValue] = Field(default_factory=dict)
    body: dict[str, JsonValue] | None = None


class InvocationEnvelope(WireModel):
    c3: InvocationRequest


class TurnAccepted(ResponseModel):
    accepted: Literal[True] = True
    turn_id: str
