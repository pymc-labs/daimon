"""C3 events have stable identities in a session's durable sequence domain."""

from __future__ import annotations

import json

from pydantic import JsonValue

from mux.contracts import events as mux
from mux.contracts.ids import ModelRef, ResourceRef
from mux.contracts.usage import UsageObservation
from mux.drivers.selfhosted import wire
from mux.errors import ProviderError, ScopeViolation


def identity(session_id: str, seq: int) -> str:
    return f"selfhosted:{session_id}:{seq}"


def usage(event: wire.Event, session: ResourceRef) -> UsageObservation:
    if event.type in ("error", "model_request_started"):
        request_id = event.data.get("request_id") or str(event.seq)
        return UsageObservation(
            id=f"selfhosted:{session.id}:{event.turn_id}:request:{request_id}",
            revision=1,
            session=session,
            turn_id=event.turn_id,
            model=ModelRef(provider="selfhosted", id=str(event.data["model"]))
            if "model" in event.data
            else None,
            grain="model_request",
            basis="increment",
            completeness="unknown",
            native_meter=event.data,
            observed_at=event.at,
        )
    data = wire.UsageData.model_validate(event.data)
    if data.cached_input_tokens > data.input_tokens or data.reasoning_tokens > data.output_tokens:
        raise ProviderError("upstream", retryable=False, native_code="invalid_usage_subsets")
    return UsageObservation(
        id=f"selfhosted:{session.id}:{event.turn_id}:request:{data.request_id}",
        revision=2,
        session=session,
        turn_id=event.turn_id,
        model=ModelRef(provider="selfhosted", id=data.model),
        grain="model_request",
        basis="increment",
        completeness="measured",
        input_tokens=data.input_tokens,
        input_cached_tokens=data.cached_input_tokens,
        output_tokens=data.output_tokens,
        output_reasoning_tokens=data.reasoning_tokens,
        native_meter=event.data,
        observed_at=event.at,
    )


def parts(values: list[wire.Part]) -> tuple[mux.ContentPart, ...]:
    return tuple(
        mux.TextPart(text=value.text)
        if isinstance(value, wire.TextPart)
        else mux.ImagePart(media_type=value.media_type, data_base64=value.data_b64)
        for value in values
    )


def object_input(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {"value": value}


def normalize(event: wire.Event, session: ResourceRef) -> mux.Event:
    if event.session_id != session.id:
        raise ScopeViolation(session.id, "harness returned a foreign session event")
    item_id = identity(session.id, event.seq)
    if event.type == "model_request_started":
        return mux.Event(
            id=item_id,
            session_id=session.id,
            sequence=event.seq,
            turn_id=event.turn_id,
            type="native.selfhosted.model_request_started",
            observed_at=event.at,
            occurred_at=event.at,
            authority="record",
            payload=event.data,
            native=mux.NativeProvenance(
                provider="selfhosted",
                api_revision=wire.PROTOCOL,
                event_id=item_id,
                event_type=event.type,
                cursor=str(event.seq),
                ordering_domain=session.id,
            ),
        )
    data = event.typed_data()
    payload: object
    authority: mux.Authority = "record"
    match data:
        case wire.StatusRunningData():
            type_, payload = (
                "session.status_running",
                mux.StatusRunningPayload(root_turn_id=event.turn_id),
            )
        case wire.AgentMessageDeltaData():
            type_, authority = "agent.message.delta", "preview"
            payload = mux.AgentMessageDeltaPayload(
                item_id=str(event.data["message_id"]),
                content_index=0,
                text=data.text,
                preview_sequence=event.seq,
            )
        case wire.AgentMessageData():
            type_, payload = (
                "agent.message",
                mux.AgentMessagePayload(
                    item_id=str(event.data["message_id"]), content=parts(data.parts)
                ),
            )
        case wire.ToolUseData():
            type_, payload = (
                "agent.tool_use",
                mux.ToolUsePayload(
                    call_id=data.call_id,
                    tool_name=data.name,
                    input=object_input(data.input),
                    executor=data.executor,
                ),
            )
        case wire.ToolResultData():
            type_, payload = (
                "agent.tool_result",
                mux.ToolResultPayload(
                    call_id=data.call_id,
                    content=(
                        mux.TextPart(
                            text=data.output
                            if isinstance(data.output, str)
                            else json.dumps(data.output)
                        ),
                    ),
                    is_error=data.is_error,
                ),
            )
        case wire.RequiresActionData():
            type_, payload = (
                "session.requires_action",
                mux.RequiresActionPayload(
                    actions=(
                        mux.RequiredAction(
                            id=data.action_id,
                            kind=data.kind,
                            call_id=data.call_id,
                            payload={"name": data.name, "input": data.input},
                        ),
                    )
                ),
            )
        case wire.UsageData():
            observation = usage(event, session)
            type_, payload = (
                "usage.observed",
                mux.UsageObservedPayload(
                    observation_id=observation.id, revision=observation.revision
                ),
            )
        case wire.TurnEndedData():
            type_, payload = (
                "session.turn_ended",
                mux.TurnEndedPayload(
                    root_turn_id=event.turn_id,
                    outcome=data.outcome,
                    native_reason=data.error.kind if data.error else None,
                ),
            )
        case wire.ErrorData():
            type_, payload = (
                "session.error",
                mux.SessionErrorPayload(
                    category="upstream",
                    retry_status="retrying" if data.retryable else "terminal",
                    native_code=data.kind,
                ),
            )
        case _:
            raise ProviderError("upstream", retryable=False, native_code="unknown_event")
    from mux.contracts._base import Contract

    assert isinstance(payload, Contract)
    return mux.Event(
        id=item_id,
        session_id=session.id,
        sequence=event.seq,
        turn_id=event.turn_id,
        type=type_,
        item_id=str(event.data["message_id"])
        if event.type in ("agent_message", "agent_message_delta")
        else item_id,
        observed_at=event.at,
        occurred_at=event.at,
        authority=authority,
        payload=payload.model_dump(mode="json"),
        native=mux.NativeProvenance(
            provider="selfhosted",
            api_revision=wire.PROTOCOL,
            event_id=item_id,
            event_type=event.type,
            cursor=str(event.seq),
            ordering_domain=session.id,
        ),
    )
