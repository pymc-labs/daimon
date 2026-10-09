"""Normalized events: content parts, the `Event` envelope and its payloads.

Event names stay recognizable from Anthropic's, but the payload contracts
below define what they mean. `Event` checks the payload of every fixed type
against its model; `agent.thread.*` and `native.*` events carry a free-form
payload so subagent provenance and unknown native features survive.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from mux.contracts._base import Contract, FrozenMap, Tagged
from mux.contracts.errors import ProviderErrorCategory
from mux.contracts.ids import Provider, ResourceRef


class TextPart(Tagged):
    type: Literal["text"] = "text"
    text: str


class ImagePart(Tagged):
    """An image, inline (base64) or as a stored artifact. Exactly one of the two."""

    type: Literal["image"] = "image"
    media_type: str
    data_base64: str | None = None
    artifact: ResourceRef | None = None

    @model_validator(mode="after")
    def _one_source(self) -> ImagePart:
        if (self.data_base64 is None) == (self.artifact is None):
            raise ValueError("an image needs exactly one of data_base64 or artifact")
        return self


class ArtifactPart(Tagged):
    type: Literal["artifact_ref"] = "artifact_ref"
    artifact: ResourceRef
    filename: str | None = None
    media_type: str | None = None


class NativePart(Tagged):
    type: Literal["native"] = "native"
    namespace: str
    version: int = Field(ge=1)
    payload: FrozenMap[str, JsonValue]


ContentPart = Annotated[
    TextPart | ImagePart | ArtifactPart | NativePart, Field(discriminator="type")
]

Authority = Literal["record", "preview", "reconciled", "gap"]
"""How far an event can be trusted. Previews never bill or complete a turn."""

TurnOutcome = Literal["completed", "interrupted", "errored", "terminated"]
RetryStatus = Literal["retrying", "exhausted", "terminal"]


class NativeProvenance(Contract):
    """Where an event came from upstream. `raw_ref` points at a scoped store, never a secret."""

    provider: Provider
    api_revision: str
    event_id: str | None = None
    event_type: str | None = None
    cursor: str | None = None
    ordering_domain: str | None = None
    raw_ref: str | None = None
    record: JsonValue | None = None
    """Opaque native event, read only by the temporary host compatibility edge."""


# Payloads of the fixed event types.


class UserMessagePayload(Contract):
    input_id: str
    content: tuple[ContentPart, ...]
    mode: Literal["new_turn", "steer"] = "new_turn"


class AgentMessagePayload(Contract):
    """Authoritative message content. Upserted by `item_id`, never appended twice."""

    item_id: str
    content: tuple[ContentPart, ...]
    revision: int = Field(default=1, ge=1)
    complete: bool = True
    phase: str | None = None


class AgentMessageDeltaPayload(Contract):
    """A streamed fragment for display only: not history, not billing."""

    item_id: str
    content_index: int = Field(ge=0)
    text: str
    preview_sequence: int = Field(ge=0)


class ToolUsePayload(Contract):
    call_id: str
    tool_name: str
    input: FrozenMap[str, JsonValue]
    executor: Literal["agent", "host", "mcp"]
    permission: Literal["auto", "ask"] = "auto"
    mcp_server: str | None = None


class ToolResultPayload(Contract):
    call_id: str
    content: tuple[ContentPart, ...]
    is_error: bool = False


class StatusRunningPayload(Contract):
    root_turn_id: str


class TurnEndedPayload(Contract):
    """The one authoritative outcome of a root turn.

    It carries no revision: a corrected outcome is a new `session.turn_ended`
    event with `authority="reconciled"` for the same `root_turn_id`, and the
    latest reconciled event wins.
    """

    root_turn_id: str
    outcome: TurnOutcome
    native_reason: str | None = None
    cancel_receipt: str | None = None


class SessionErrorPayload(Contract):
    category: ProviderErrorCategory
    retry_status: RetryStatus
    native_code: str | None = None
    operation_id: str | None = None
    message: str | None = None


class StatusTerminatedPayload(Contract):
    reason: str
    native_lifecycle: str | None = None


class ToolServerDegradedPayload(Contract):
    server: str
    error_type: str
    retry_status: RetryStatus


class UsageObservedPayload(Contract):
    observation_id: str
    revision: int = Field(ge=1)


class ReconciledPayload(Contract):
    snapshot_ref: str
    coverage: str
    gaps: tuple[str, ...] = ()


class HistoryGapPayload(Contract):
    domain: str
    after: str | None = None
    before: str | None = None
    recoverable: bool


class RequiredAction(Contract):
    """Something the session waits on before the turn can continue."""

    id: str
    kind: Literal["tool_confirmation", "function_result", "environment_connection", "native"]
    call_id: str | None = None
    payload: FrozenMap[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    native_type: str | None = None


class RequiresActionPayload(Contract):
    """The session paused for these actions. Pausing is not completing the turn."""

    actions: tuple[RequiredAction, ...]


PAYLOAD_MODELS: Mapping[str, type[Contract]] = {
    "user.message": UserMessagePayload,
    "agent.message": AgentMessagePayload,
    "agent.message.delta": AgentMessageDeltaPayload,
    "agent.tool_use": ToolUsePayload,
    "agent.tool_result": ToolResultPayload,
    "session.status_running": StatusRunningPayload,
    "session.requires_action": RequiresActionPayload,
    "session.turn_ended": TurnEndedPayload,
    "session.error": SessionErrorPayload,
    "session.status_terminated": StatusTerminatedPayload,
    "tool_server.degraded": ToolServerDegradedPayload,
    "usage.observed": UsageObservedPayload,
    "session.reconciled": ReconciledPayload,
    "session.history_gap": HistoryGapPayload,
}
"""Every fixed event type and the model its payload must satisfy."""

OPEN_PREFIXES: tuple[str, ...] = ("agent.thread.", "native.")
"""Event-type prefixes whose payload is provider-shaped and not checked."""


class Event(Contract):
    """One journal entry. `sequence` is local commit order, not provider order."""

    id: str
    session_id: str
    sequence: int = Field(ge=0)
    type: str
    turn_id: str | None = None
    thread_id: str | None = None
    item_id: str | None = None
    caused_by: tuple[str, ...] = ()
    observed_at: datetime
    occurred_at: datetime | None = None
    authority: Authority
    payload: FrozenMap[str, JsonValue]
    native: NativeProvenance

    @model_validator(mode="after")
    def _check_payload(self) -> Event:
        model = PAYLOAD_MODELS.get(self.type)
        if model is not None:
            model.model_validate(self.payload)
        elif not self.type.startswith(OPEN_PREFIXES):
            raise ValueError(f"unknown event type {self.type!r}")
        if self.type == "agent.message.delta" and self.authority != "preview":
            raise ValueError("agent.message.delta is preview-only")
        return self

    def typed_payload(self) -> Contract | None:
        """The payload parsed into its model, or None for an open type."""
        model = PAYLOAD_MODELS.get(self.type)
        return None if model is None else model.model_validate(self.payload)
