"""What the kit needs from the caller, and what each case reports."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol
from uuid import uuid4

import httpx

from mux.drivers.selfhosted.wire import Event, InlineSkill, Limits, MCPServer, TurnRequest

from ..fake_model_server import RecordedRequest, Step
from .client import HarnessClient


class ModelScripter(Protocol):
    """The fake model as the kit drives it; ``FakeModelServer`` satisfies this."""

    def script(self, steps: Sequence[Step], *, conversation: str = ...) -> None: ...

    def requests_for(self, conversation: str | None = None) -> list[RecordedRequest]: ...


RestartHook = Callable[[], Awaitable[str | None]]
"""Stop the harness (abruptly if possible) and start a new one on the same home.
Return the new base URL if it changed."""


def default_mcp_tool_name(server: str, tool: str) -> str:
    """How the model sees an MCP tool (the OpenAI Agents SDK convention)."""
    return f"{server}__{tool}"


def identity_tool_name(name: str) -> str:
    """How the model sees a built-in tool (``bash``, ``read``, ``write``, ``edit``)."""
    return name


@dataclass
class HarnessTarget:
    """A running harness plus the handles the kit uses to drive and observe it."""

    base_url: str
    token: str
    model: ModelScripter
    restart: RestartHook | None = None
    mcp_tool_name: Callable[[str, str], str] = default_mcp_tool_name
    tool_name: Callable[[str], str] = identity_tool_name
    """Maps a built-in tool name to the name the model sees, for engines that prefix them."""
    session_id: str = field(default_factory=lambda: f"conf-{uuid4().hex[:12]}")
    timeout_s: float = 20
    http_transport: httpx.AsyncBaseTransport | None = None
    """Optional transport, for a harness mounted in-process as an ASGI app."""


Status = Literal["pass", "skip", "fail"]


@dataclass(frozen=True)
class CaseResult:
    name: str
    status: Status
    detail: str = ""


class SkipCase(Exception):
    """Raised by a case the target cannot support; the reason is reported, never hidden."""


@dataclass
class TurnRun:
    request: TurnRequest
    conversation: str
    events: list[Event]

    def of(self, event_type: str) -> list[Event]:
        return [event for event in self.events if event.type == event_type]

    @property
    def types(self) -> list[str]:
        return [event.type for event in self.events]

    @property
    def outcome(self) -> str:
        return str(self.of("turn_ended")[-1].data["outcome"])


class CaseContext:
    """Per-case helpers: unique turn ids, a unique fake-model conversation, polling."""

    def __init__(self, target: HarnessTarget, client: HarnessClient, name: str) -> None:
        self.target = target
        self.client = client
        self.name = name
        self.conversation = f"conf-{name}-{uuid4().hex[:8]}"
        self._turns = 0

    @property
    def session_id(self) -> str:
        return self.target.session_id

    def turn_request(
        self,
        text: str = "hello",
        *,
        instructions: str = "You are a conformance test agent.",
        tool_policy: dict[str, Literal["auto", "ask", "deny"]] | None = None,
        skills: list[InlineSkill] | None = None,
        mcp_servers: list[MCPServer] | None = None,
        max_tool_calls: int = 5,
        deadline_s: float = 60,
    ) -> TurnRequest:
        self._turns += 1
        tag = f"{self.conversation}-{self._turns}"
        return TurnRequest.model_validate(
            {
                "session_id": self.session_id,
                "turn_id": f"turn-{tag}",
                "idempotency_key": f"key-{tag}",
                "model": self.conversation,
                "instructions": instructions,
                "input": [{"type": "text", "text": text}],
                "mcp_servers": [server.model_dump() for server in mcp_servers or []],
                "skills": [skill.model_dump() for skill in skills or []],
                "limits": Limits(max_tool_calls=max_tool_calls, deadline_s=deadline_s).model_dump(),
                "tool_policy": tool_policy or {},
            }
        )

    def tool(self, name: str) -> str:
        """The model-visible name of a built-in tool."""
        return self.target.tool_name(name)

    def script(self, *steps: Step) -> None:
        self.target.model.script(steps, conversation=self.conversation)

    def model_requests(self) -> list[RecordedRequest]:
        return self.target.model.requests_for(self.conversation)

    async def start(self, request: TurnRequest) -> None:
        response = await self.client.submit(request)
        check(
            response.status_code == 202, f"POST /v0/turns answered {response.status_code}, not 202"
        )

    async def run_turn(self, request: TurnRequest | None = None) -> TurnRun:
        request = request or self.turn_request()
        await self.start(request)
        events = await self.client.wait_ended(self.session_id, request.turn_id)
        return TurnRun(request, self.conversation, events)

    async def wait_for_model_requests(self, count: int) -> None:
        try:
            async with asyncio.timeout(self.target.timeout_s):
                while len(self.model_requests()) < count:
                    await asyncio.sleep(0.02)
        except TimeoutError:
            raise AssertionError(
                f"model received {len(self.model_requests())} of {count} requests"
            ) from None


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def tool_result(run_events: list[Event], call_id: str) -> Event:
    matches = [e for e in run_events if e.type == "tool_result" and e.data["call_id"] == call_id]
    check(len(matches) == 1, f"expected one tool_result for {call_id}, got {len(matches)}")
    return matches[0]


def text_of(value: object) -> str:
    return json.dumps(value, sort_keys=True)
