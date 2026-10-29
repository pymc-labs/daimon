"""OpenAI agents: the real `OpenAIDriver` and `OpenAITurnIO` over a scripted sessions wire."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
from daimon.core.turn.openai_io import OpenAITurnIO
from mux.contracts.events import Event
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.transport import SDKTransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from openai import AsyncOpenAI

from .drive import SCOPE, Result, drive, session
from .script import Clock, Script, piece
from .wire import Progress, ScriptedBody, unavailable

PROFILE = "openai.persistent_workspace"
BASE = "/v1/agents/sessions/session"


class Persistence:
    """The host journal, in memory: records, gap markers and claimed mutations."""

    def __init__(self) -> None:
        self.records: list[Event] = []
        self.gaps = 0
        self.stops: list[str] = []

    async def stopped(self, key, session) -> None:
        self.stops.append(key)

    async def gap(self, session) -> None:
        self.gaps += 1

    async def record(self, session, event, *, source_key=None) -> None:
        self.records.append(event)

    async def mutate(self, session, kind, request, deliver, receipt_type, status_of):
        return await deliver("claimed-" + str(uuid4()))


class _Open(httpx.AsyncByteStream):
    """The buffering stream a reconcile opens: open and quiet until it is closed."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        await asyncio.Event().wait()
        yield b""


class SessionsWire:
    def __init__(self, script: Script) -> None:
        self.script = script
        self.clock = Clock(script.stamps)
        self.progress = Progress(script, self.record)
        self.sends = 0
        self.interrupts = 0
        self.reconciling = False
        self.announced = False

    def turn(self, status: str) -> dict[str, object]:
        return {
            "id": "root",
            "session_id": "session",
            "agent_id": "agent",
            "subagent_id": None,
            "status": status,
            "created_at": int(self.clock.create().timestamp()),
            "usage": {
                "input_tokens": 100,
                "input_tokens_details": {"cached_tokens": 10},
                "output_tokens": 20,
                "output_tokens_details": {"reasoning_tokens": 5},
            },
        }

    def item(self, index: int) -> dict[str, object]:
        return {
            "id": f"answer-{index:03d}",
            "turn_id": "root",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": piece(index)}],
        }

    def record(self, index: int) -> dict[str, object]:
        if index < 0:
            return self.event("turn.completed", "done", turn=self.turn("completed"))
        return self.event(
            "turn.item.done", f"item-{index:03d}", turn_id="root", item=self.item(index)
        )

    def event(self, suffix: str, id_: str, **fields: object) -> dict[str, object]:
        return {
            "type": "agent.session." + suffix,
            "event_id": id_,
            "session_id": "session",
            **fields,
        }

    def status(self) -> str:
        return "completed" if self.progress.ended else "in_progress"

    def sse(self) -> httpx.Response:
        headers = {"Content-Type": "text/event-stream"}
        if self.reconciling:
            return httpx.Response(200, stream=_Open(), headers=headers)
        conn, records = self.progress.connect()
        if not self.announced:
            self.announced = True
            start = self.event("turn.in_progress", "running", turn=self.turn("in_progress"))
            records = [start, *records]
        chunks = [("data: " + json.dumps(r) + "\n\n").encode() for r in records]
        return httpx.Response(
            200, stream=ScriptedBody(chunks, conn, self.progress), headers=headers
        )

    def page(self, values: list[dict[str, object]]) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": values,
                "has_more": False,
                "last_id": values[-1]["id"] if values else None,
            },
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == BASE + "/events" and method == "GET":
            return self.sse()
        if path == BASE + "/events":
            body = json.loads(request.content)
            if body["events"][0]["type"] == "agent.session.input.cancel":
                self.interrupts += 1
                self.progress.ended = True
                return httpx.Response(202, json={})
            self.sends += 1
            return httpx.Response(202, json={})
        if path == BASE:
            if self.progress.status_fails():
                return unavailable(request)
            running = self.sends and not self.progress.ended
            return httpx.Response(
                200,
                json={
                    "id": "session",
                    "agent": {"id": "agent", "model": "gpt-6-luna"},
                    "created_at": 0,
                    "environment": {"type": "openai_hosted", "id": "environment"},
                    "status": "in_progress" if running else "idle",
                    "required_actions": [],
                    "metadata": {"mux_tenant": "tenant"},
                },
            )
        if path == BASE + "/turns/root":
            return httpx.Response(
                200, json=self.turn("cancelled" if self.interrupts else self.status())
            )
        if path == BASE + "/turns":
            if self.progress.replay_fails():
                return unavailable(request)
            return self.page([self.turn(self.status())] if self.sends else [])
        if path == BASE + "/items":
            items = [
                {
                    "id": "input",
                    "turn_id": "root",
                    "type": "message",
                    "role": "user",
                    "status": "completed",
                    "content": [{"type": "input_text", "text": "question"}],
                },
                *(
                    r["item"]
                    for r in self.progress.records
                    if r["type"] == "agent.session.turn.item.done"
                ),
            ]
            return self.page(items if self.sends else [])
        raise AssertionError(f"unscripted OpenAI request {method} {path}")


async def run(monkeypatch: pytest.MonkeyPatch, script: Script) -> Result:
    wire = SessionsWire(script)
    async with AsyncOpenAI(
        api_key="offline-placeholder",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)),
    ) as sdk:
        backend = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            authorization=lambda scope, kind, id_: scope == SCOPE,
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
        )
        io = OpenAITurnIO(backend, SCOPE, session("openai"), persistence=Persistence())
        replay = io.replay

        async def reconcile(*args: object, **kwargs: object) -> object:
            # The reconcile's own buffering stream is not one of the script's
            # connections; it stays open and quiet while the pages are read.
            wire.reconciling = True
            try:
                return await replay(*args, **kwargs)  # type: ignore[arg-type]
            finally:
                wire.reconciling = False

        monkeypatch.setattr(io, "replay", reconcile)
        state, lifecycle = await drive(monkeypatch, io, deadline_s=script.deadline_s)
    return Result(
        state,
        lifecycle,
        sends=wire.sends,
        facts={"opens": wire.progress.opens, "interrupts": wire.interrupts},
    )
