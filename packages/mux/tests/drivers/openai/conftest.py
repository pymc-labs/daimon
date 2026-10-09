"""Synthetic replies derived from official Agents API docs; no network."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from copy import deepcopy
from dataclasses import dataclass, field

import pytest
from mux.contracts.ids import ResourceRef, Scope
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.transport import Method, Object
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions

SCOPE = Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="grant")
REF = ResourceRef(
    id="s",
    kind="session",
    provider="openai",
    account_scope_id="project",
    tenant_id="t",
    account_id="a",
)


def native_session(status: str = "idle") -> Object:
    return {
        "id": "s",
        "agent": {"id": "a", "model": "fixture"},
        "created_at": 0,
        "environment": {"type": "openai_hosted", "id": "env"},
        "status": status,
        "required_actions": [],
        "metadata": {"mux_tenant": "t"},
    }


def turn(status: str = "completed", *, id_: str = "root", child: str | None = None) -> Object:
    return {
        "id": id_,
        "session_id": "s",
        "agent_id": "a",
        "subagent_id": child,
        "status": status,
        "created_at": 0,
        "usage": None,
    }


def event(kind: str, id_: str, **values: object) -> Object:
    from mux.drivers.openai.transport import object_json

    return object_json(
        {"type": "agent.session." + kind, "event_id": id_, "session_id": "s", **values}
    )


def required_action_event(id_: str = "action") -> Object:
    """Official streaming schema: no outer session_id or turn_id fields."""
    session = native_session("requires_action")
    session["required_actions"] = [
        {
            "type": "function_call",
            "call_id": "call",
            "turn_id": "root",
            "name": "tool",
            "arguments": {},
        }
    ]
    return {"type": "agent.session.requires_action", "event_id": id_, "session": session}


def message(text: str = "done", *, id_: str = "item", status: str = "completed") -> Object:
    return {
        "id": id_,
        "turn_id": "root",
        "type": "message",
        "role": "assistant",
        "status": status,
        "phase": "final_answer",
        "content": [{"type": "output_text", "text": text}],
    }


def page(*values: Object, more: bool = False) -> Object:
    return {"data": list(values), "has_more": more, "last_id": values[-1]["id"] if values else None}


class Source(AsyncIterator[Object]):
    def __init__(self, values: list[Object], *, hold: bool = False) -> None:
        self.values = iter(deepcopy(values))
        self.hold = hold
        self.closed = False
        self.wait = asyncio.Event()

    def __aiter__(self) -> Source:
        return self

    async def __anext__(self) -> Object:
        try:
            return next(self.values)
        except StopIteration:
            if self.hold:
                await self.wait.wait()
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self.closed = True
        self.wait.set()


@dataclass
class FakeTransport:
    responses: dict[tuple[Method, str], Object | Exception] = field(
        default_factory=lambda: dict[tuple[Method, str], Object | Exception]()
    )
    stream_values: list[Object] = field(default_factory=lambda: list[Object]())
    calls: list[tuple[Method, str, Object | None, Mapping[str, str | int] | None]] = field(
        default_factory=lambda: list[
            tuple[Method, str, Object | None, Mapping[str, str | int] | None]
        ]()
    )
    sources: list[Source] = field(default_factory=lambda: list[Source]())
    hold: bool = False
    request_keys: list[str | None] = field(default_factory=lambda: list[str | None]())

    async def request(
        self,
        method: Method,
        path: str,
        *,
        body: Object | None = None,
        query: Mapping[str, str | int] | None = None,
        key: str | None = None,
    ) -> Object:
        self.calls.append((method, path, deepcopy(body), query))
        self.request_keys.append(key)
        value = self.responses.get((method, path), {})
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)

    async def open_stream(self, path: str) -> AsyncIterator[Object]:
        self.calls.append(("GET", path, None, None))
        self.request_keys.append(None)
        source = Source(self.stream_values, hold=self.hold)
        self.sources.append(source)
        return source


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport(
        responses={
            ("GET", "/agents/sessions/s"): native_session(),
            ("GET", "/agents/sessions/s/turns"): page(turn()),
            ("GET", "/agents/sessions/s/turns/root"): turn(),
            ("GET", "/agents/sessions/s/items"): page(message()),
        }
    )


@pytest.fixture
def driver(transport: FakeTransport) -> OpenAIDriver:
    return OpenAIDriver(
        transport,
        account_scope_id="project",
        journal=MemoryRecoveryJournal(),
        usage_revisions=MemoryUsageRevisions(),
        authorization=lambda s, k, i: s == SCOPE,
    )
