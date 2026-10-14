"""Immutable observations of an owned, offline platform post window.

Body/card text, controls and notification/accessibility fallbacks stay separate.
The runner binds identity to independent native evidence; capture is not a turn
completion certificate and never extracts provider or headless return strings.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

type Platform = Literal["slack", "teams"]
type Operation = Literal["send", "edit", "delete"]


class SurfaceModel(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid", allow_inf_nan=False)


class PlatformMessageCapture(SurfaceModel):
    message_id: str
    destination_id: str
    thread_id: str | None
    order: int
    observed_s: float
    body_texts: tuple[str, ...]
    card_texts: tuple[str, ...]
    control_labels: tuple[str, ...]
    fallback_text: str
    opaque_text: bool
    payload_json: str


class PlatformPostEvent(SurfaceModel):
    evidence_id: str
    operation: Operation
    message: PlatformMessageCapture
    request_json: str


class PlatformSurfaceCapture(SurfaceModel):
    boundary: Literal["platform_adapter_posts"] = "platform_adapter_posts"
    platform: Platform
    evidence_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    root_turn_id: str = Field(min_length=1)
    started_s: float = Field(ge=0)
    observed_s: float = Field(ge=0)
    events: tuple[PlatformPostEvent, ...]
    messages: tuple[PlatformMessageCapture, ...]
    post_capture_complete: bool
    text_capture_complete: bool
    delivery_failures: int = Field(ge=0)

    @model_validator(mode="after")
    def owned_window(self) -> PlatformSurfaceCapture:
        if self.observed_s < self.started_s or any(
            not self.started_s <= event.message.observed_s <= self.observed_s
            for event in self.events
        ):
            raise ValueError("platform post outside owned capture window")
        if self.text_capture_complete and (
            not self.post_capture_complete
            or any(event.message.opaque_text for event in self.events)
            or any(message.opaque_text for message in self.messages)
        ):
            raise ValueError("complete text requires complete posts without opaque text")
        if self.post_capture_complete and self.delivery_failures:
            raise ValueError("failed delivery cannot complete capture")
        return self


class SurfaceRecorder:
    """Only accepting fake gateways call record; serialize before retaining state."""

    def __init__(
        self,
        *,
        evidence_id: str,
        destination_id: str,
        thread_id: str | None,
        clock: Callable[[], float],
        decode: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        self.evidence_id = evidence_id
        self.destination_id = destination_id
        self.thread_id = thread_id
        self.clock = clock
        self.decode = decode
        self.events: list[PlatformPostEvent] = []
        self.delivery_failures = 0
        self._messages: dict[str, PlatformMessageCapture] = {}
        self._next_order = 0

    def payload(self, message_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self._messages[message_id].payload_json))

    def record(
        self,
        operation: Operation,
        message_id: str,
        request: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        encoded = json.dumps(payload, ensure_ascii=False)
        decoded = cast(dict[str, Any], json.loads(encoded))
        old = self._messages.get(message_id)
        if operation != "send" and old is None:
            raise ValueError("platform message is not owned by this capture")
        if operation == "send" and old is not None:
            raise ValueError("duplicate platform message")
        if operation == "delete":
            assert old is not None
            message = old.model_copy(update={"observed_s": self.clock()})
            del self._messages[message_id]
        else:
            order = self._next_order if old is None else old.order
            if old is None:
                self._next_order += 1
            message = PlatformMessageCapture(
                message_id=message_id,
                destination_id=self.destination_id,
                thread_id=self.thread_id,
                order=order,
                observed_s=self.clock(),
                payload_json=encoded,
                **self.decode(decoded),
            )
            self._messages[message_id] = message
        self.events.append(
            PlatformPostEvent(
                evidence_id=f"{self.evidence_id}:post:{len(self.events)}",
                operation=operation,
                message=message,
                request_json=json.dumps(request, ensure_ascii=False),
            )
        )

    def surviving_messages(self) -> tuple[PlatformMessageCapture, ...]:
        return tuple(self._messages.values())


class SurfaceWindow:
    """One invocation; terminal count is observed from the real adapter hooks."""

    def __init__(
        self,
        *,
        platform: Platform,
        evidence_id: str,
        session_id: str,
        root_turn_id: str,
        recorder: SurfaceRecorder,
    ) -> None:
        self.platform: Platform = platform
        self.evidence_id = evidence_id
        self.session_id = session_id
        self.root_turn_id = root_turn_id
        self.recorder = recorder
        self.started_s = recorder.clock()
        self._ran = False
        self._closed = False
        self._receipt: PlatformSurfaceCapture | None = None
        self._snapshot()  # identity/clock validation before any adapter dispatch

    async def run(
        self,
        invoke: Callable[[], Awaitable[object]],
        terminal_count: Callable[[], int],
    ) -> PlatformSurfaceCapture:
        if self._ran:
            raise ValueError("platform surface harness is single-use")
        self._ran = True
        try:
            await invoke()
            self._closed = terminal_count() == 1 and not self.recorder.delivery_failures
        finally:
            self._receipt = self._snapshot()
        return self._receipt

    def snapshot(self) -> PlatformSurfaceCapture:
        return self._receipt if self._receipt is not None else self._snapshot()

    def _snapshot(self) -> PlatformSurfaceCapture:
        messages = self.recorder.surviving_messages()
        return PlatformSurfaceCapture(
            platform=self.platform,
            evidence_id=self.evidence_id,
            session_id=self.session_id,
            root_turn_id=self.root_turn_id,
            started_s=self.started_s,
            observed_s=self.recorder.clock(),
            events=tuple(self.recorder.events),
            messages=messages,
            post_capture_complete=self._closed,
            delivery_failures=self.recorder.delivery_failures,
            text_capture_complete=self._closed
            and not any(event.message.opaque_text for event in self.recorder.events),
        )
