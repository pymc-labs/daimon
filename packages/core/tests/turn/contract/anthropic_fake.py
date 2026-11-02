"""Claude Managed Agents: the real mux driver and `MuxTurnIO` over a scripted MA wire."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.core.turn.io import MuxTurnIO
from mux.drivers.anthropic import AnthropicManagedAgents
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.testing.drivers.anthropic.ma import list_response, session_response

from .drive import SCOPE, Result, drive, session
from .script import EARLIER, Clock, Script, piece
from .wire import Progress, ScriptedBody, unavailable

PROFILE = "anthropic.managed_agents"
BASE = "/v1/sessions/session"


def earlier(clock: Clock) -> list[dict[str, object]]:
    """A finished turn from before the host restarted."""
    stamp = (clock.created - timedelta(minutes=5)).isoformat()
    return [
        {
            "id": "sevt_earlier_user",
            "type": "user.message",
            "content": [{"type": "text", "text": "earlier question"}],
            "processed_at": stamp,
        },
        {
            "id": "sevt_earlier_answer",
            "type": "agent.message",
            "content": [{"type": "text", "text": EARLIER}],
            "processed_at": stamp,
        },
        {
            "id": "sevt_earlier_end",
            "type": "session.status_idle",
            "stop_reason": {"type": "end_turn"},
            "processed_at": stamp,
        },
    ]


class MAWire:
    def __init__(self, script: Script) -> None:
        self.script = script
        self.clock = Clock(script.stamps)
        self.progress = Progress(script, self.record)
        self.log: list[dict[str, object]] = earlier(self.clock) if script.after_restart else []
        self.sends = 0

    def record(self, index: int) -> dict[str, object]:
        stamp = self.clock.read().isoformat()
        if index < 0:
            return {
                "id": "sevt_end",
                "type": "session.status_idle",
                "stop_reason": {"type": "end_turn"},
                "processed_at": stamp,
            }
        return {
            "id": f"sevt_{index:03d}",
            "type": "agent.message",
            "content": [{"type": "text", "text": piece(index)}],
            "processed_at": stamp,
        }

    def history(self) -> list[dict[str, object]]:
        return [*self.log, *self.progress.records]

    def sse(self) -> httpx.Response:
        conn, records = self.progress.connect()
        chunks = [f"event: {r['type']}\ndata: {json.dumps(r)}\n\n".encode() for r in records]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ScriptedBody(chunks, conn, self.progress),
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == BASE + "/events/stream":
            return self.sse()
        if path == BASE + "/events" and method == "POST":
            body = json.loads(request.content)
            kinds = [event["type"] for event in body["events"]]
            if kinds == ["user.interrupt"]:
                self.progress.interrupted()
                self.progress.ended = True
                self.progress.records.append(
                    {
                        "id": "sevt_interrupted",
                        "type": "session.status_idle",
                        "stop_reason": {"type": "end_turn"},
                        "processed_at": self.clock.read().isoformat(),
                    }
                )
                return httpx.Response(200, json={"data": []})
            assert kinds == ["user.message"], kinds
            self.sends += 1
            self.log.append(
                {
                    "id": f"sevt_user_{self.sends}",
                    "type": "user.message",
                    "content": body["events"][0]["content"],
                    "processed_at": self.clock.create().isoformat(),
                }
            )
            return httpx.Response(200, json={"data": []})
        if path == BASE + "/events" and method == "GET":
            if self.progress.read_fails():
                return unavailable(request)
            return list_response(self.history())
        if path == BASE and method == "GET":
            if self.progress.read_fails():
                return unavailable(request)
            return session_response(
                session_id="session", status="idle" if self.progress.ended else "running"
            )
        if path == BASE + "/archive":
            return session_response(session_id="session", status="idle")
        raise AssertionError(f"unscripted MA request {method} {path}")


async def run(monkeypatch: pytest.MonkeyPatch, script: Script) -> Result:
    wire = MAWire(script)
    async with AsyncAnthropic(
        api_key="offline",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)),
    ) as client:
        backend = AnthropicManagedAgents(
            client,
            account_scope_id="project",
            authorization=ResourceAuthorization(SCOPE, frozenset({("session", "session")})),
        )
        io = MuxTurnIO(backend, SCOPE, session("anthropic"))
        state, lifecycle = await drive(monkeypatch, io, wire.progress, deadline_s=script.deadline_s)
    return Result(
        state,
        lifecycle,
        sends=wire.sends,
        facts=wire.progress.facts(),
    )
