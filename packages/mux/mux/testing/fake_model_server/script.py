"""Scripted steps, request records and the per-conversation queue."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Sequence
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_CONVERSATION = "default"
CONVERSATION_HEADER = "x-fake-conversation"


class Usage(BaseModel):
    """Token counts reported with one scripted response."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=10, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=5, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)


class ToolCall(BaseModel):
    """One function call the scripted model asks the engine to run."""

    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str | None = None


class Reply(BaseModel):
    """A successful model response: optional text, then optional tool calls."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["reply"] = "reply"
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list[ToolCall])
    usage: Usage = Field(default_factory=Usage)
    delay_s: float = Field(default=0, ge=0)


class Fail(BaseModel):
    """An HTTP error response in the shape of the API being called."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["fail"] = "fail"
    status: int = Field(default=500, ge=400, le=599)
    message: str = "scripted failure"
    error_type: str | None = None
    retry_after_s: float | None = None
    delay_s: float = Field(default=0, ge=0)


Step = Annotated[Reply | Fail, Field(discriminator="kind")]


def say(text: str, *, usage: Usage | None = None, delay_s: float = 0) -> Reply:
    return Reply(text=text, usage=usage or Usage(), delay_s=delay_s)


def call_tool(
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    call_id: str | None = None,
    text: str = "",
    usage: Usage | None = None,
    delay_s: float = 0,
) -> Reply:
    return Reply(
        text=text,
        tool_calls=[ToolCall(name=name, arguments=arguments or {}, call_id=call_id)],
        usage=usage or Usage(),
        delay_s=delay_s,
    )


def fail(
    status: int = 500,
    message: str = "scripted failure",
    *,
    error_type: str | None = None,
    retry_after_s: float | None = None,
    delay_s: float = 0,
) -> Fail:
    return Fail(
        status=status,
        message=message,
        error_type=error_type,
        retry_after_s=retry_after_s,
        delay_s=delay_s,
    )


class RecordedRequest(BaseModel):
    """What the engine sent, normalised just enough to assert on."""

    api: Literal["responses", "chat_completions", "anthropic_messages"]
    conversation: str
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: dict[str, Any]
    stream: bool
    answered_with: Literal["reply", "fail", "exhausted"]

    @property
    def instructions(self) -> str:
        """System prompt text, whichever field the API carries it in."""
        body = self.body
        if self.api == "responses":
            # An engine may send its system prompt as `instructions` or as a leading
            # developer/system input message; both are the same thing to a test.
            leading = [as_dict(item) for item in as_list(body.get("input"))]
            parts = [str(body.get("instructions") or "")] + [
                _content_text(item.get("content"))
                for item in leading
                if item.get("role") in {"system", "developer"}
            ]
            return "\n".join(part for part in parts if part)
        if self.api == "anthropic_messages":
            system = body.get("system") or ""
            if isinstance(system, str):
                return system
            return "\n".join(str(as_dict(block).get("text", "")) for block in as_list(system))
        messages = [as_dict(message) for message in as_list(body.get("messages"))]
        return "\n".join(
            _content_text(message.get("content"))
            for message in messages
            if message.get("role") in {"system", "developer"}
        )

    @property
    def tool_names(self) -> list[str]:
        """Names of the tools offered to the model, in request order."""
        names: list[str] = []
        for entry in as_list(self.body.get("tools")):
            tool = as_dict(entry)
            name = as_dict(tool.get("function")).get("name") or tool.get("name")
            if name is None:
                name = tool.get("server_label") or tool.get("type")
            names.append(str(name))
        return names

    @property
    def history(self) -> list[Any]:
        """The conversation items or messages sent with the request."""
        items = self.body.get("input") if self.api == "responses" else self.body.get("messages")
        if isinstance(items, str):
            return [{"role": "user", "content": items}]
        return as_list(items)

    def text(self) -> str:
        """The whole request body as JSON text, for substring assertions."""
        return json.dumps(self.body, sort_keys=True)


def as_dict(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def as_list(value: object) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    return "".join(str(as_dict(part).get("text", "")) for part in as_list(content))


class ScriptBook:
    """Step queues keyed by conversation, plus the log of every request."""

    def __init__(self) -> None:
        self._queues: dict[str, deque[Step]] = {}
        self.requests: list[RecordedRequest] = []

    def script(self, steps: Sequence[Step], *, conversation: str = DEFAULT_CONVERSATION) -> None:
        """Append steps to a conversation's queue."""
        self._queues.setdefault(conversation, deque()).extend(steps)

    def has(self, conversation: str) -> bool:
        return conversation in self._queues

    def next_step(self, conversation: str) -> Step | None:
        queue = self._queues.get(conversation)
        return queue.popleft() if queue else None

    def remaining(self, conversation: str = DEFAULT_CONVERSATION) -> int:
        return len(self._queues.get(conversation, ()))

    def requests_for(self, conversation: str | None = None) -> list[RecordedRequest]:
        if conversation is None:
            return list(self.requests)
        return [request for request in self.requests if request.conversation == conversation]

    def reset(self) -> None:
        self._queues.clear()
        self.requests.clear()

    async def wait_for_requests(
        self, count: int, *, conversation: str | None = None, timeout_s: float = 10
    ) -> list[RecordedRequest]:
        """Block until at least ``count`` requests arrived; raise on timeout."""
        async with asyncio.timeout(timeout_s):
            while len(self.requests_for(conversation)) < count:
                await asyncio.sleep(0.01)
        return self.requests_for(conversation)
