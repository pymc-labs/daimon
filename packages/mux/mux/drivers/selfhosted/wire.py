"""The SDK-independent HTTP/JSON contract for ``daimon-harness/0``.

Wire parts intentionally differ from mux content parts: images are always inline.
An event validates its data against its type before entering a durable journal.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping
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


SKILL_MAX_BYTES = 8 * 1024 * 1024
TURN_SKILLS_MAX_BYTES = 24 * 1024 * 1024


def skill_sha256(name: str, files: Mapping[str, bytes]) -> str:
    """Hash UTF-8 compact sorted JSON {name, files: {path: canonical base64}}."""
    canonical = json.dumps(
        {
            "name": name,
            "files": {path: base64.b64encode(data).decode("ascii") for path, data in files.items()},
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_skill_path(path: str) -> None:
    if (
        not path
        or "\\" in path
        or "\x00" in path
        or any(part in ("", ".", "..") for part in path.split("/"))
    ):
        raise ValueError("skill files require canonical relative POSIX paths")


class InlineSkill(WireModel):
    name: str
    files: dict[str, str]
    binary_files: dict[str, str] = Field(default_factory=dict)
    sha256: str = ""

    def decoded_files(self) -> dict[str, bytes]:
        if self.files.keys() & self.binary_files.keys():
            raise ValueError("skill path occurs in both text and binary files")
        decoded = {path: text.encode("utf-8") for path, text in self.files.items()}
        for path, data in self.binary_files.items():
            if len(data) > ((SKILL_MAX_BYTES + 2) // 3) * 4:
                raise ValueError("skill exceeds 8 MiB decoded bytes")
            try:
                decoded[path] = base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error):
                raise ValueError("skill binary file requires valid base64") from None
        if sum(map(len, decoded.values())) > SKILL_MAX_BYTES:
            raise ValueError("skill exceeds 8 MiB decoded bytes")
        for path in decoded:
            validate_skill_path(path)
            parts = path.split("/")
            if any("/".join(parts[:i]) in decoded for i in range(1, len(parts))):
                raise ValueError("skill path is both a file and a directory")
        return decoded

    @model_validator(mode="after")
    def validate_bundle(self) -> InlineSkill:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", self.name):
            raise ValueError("invalid skill name")
        digest = skill_sha256(self.name, self.decoded_files())
        if "sha256" in self.model_fields_set and self.sha256 != digest:
            raise ValueError("skill sha256 does not match content")
        self.sha256 = digest
        return self


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
    tool_policy: dict[str, Literal["auto", "ask"]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_skills(self) -> TurnRequest:
        if len({skill.name for skill in self.skills}) != len(self.skills):
            raise ValueError("duplicate skill names")
        size = sum(
            sum(map(len, skill.decoded_files().values()))
            for skill in self.skills
            if isinstance(skill, InlineSkill)
        )
        if size > TURN_SKILLS_MAX_BYTES:
            raise ValueError("turn skills exceed 24 MiB decoded bytes")
        return self


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
    message_id: str
    text: str


class AgentMessageData(ResponseModel):
    message_id: str
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
