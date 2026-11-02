"""Self-hosted harness: the real `SelfHostedDriver` and `SelfHostedTurnIO` over a scripted
C3 harness.

The harness is polled, not streamed, so each poll after the send is one script
connection: it returns that connection's new records, and a drop is the next
poll failing. A polled journal has no stream to close, so `eof` and `hold` are
both a poll with nothing new, and a withheld `turn_ended` is the stuck case.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
import pytest
from daimon.core.turn.selfhosted_io import SelfHostedTurnIO
from mux.contracts.ids import ModelRef, ResourceRef, Revision
from mux.contracts.ports import Agents, Environments
from mux.contracts.resources import Agent, AgentSpec, Environment, EnvironmentSpec, SessionSpec
from mux.drivers.selfhosted import SelfHostedDriver
from mux.drivers.selfhosted.transport import DirectTransport

from .drive import SCOPE, Result, drive
from .openai_fake import Persistence
from .script import EARLIER, Clock, End, Script, piece
from .wire import Progress, unavailable

PROFILE = "selfhosted.loop"


class HarnessWire:
    def __init__(self, script: Script) -> None:
        self.script = script
        self.clock = Clock(script.stamps)
        self.progress = Progress(script, self.record)
        self.progress.polled = True
        self.request: dict[str, object] | None = None
        self.sends = 0
        self.drop: End | None = None
        self.session_id = ""

    def stamped(self, seq: int, type_: str, data: dict[str, object], at: datetime) -> dict:
        assert self.request is not None
        return {
            "seq": seq,
            "session_id": self.request["session_id"],
            "turn_id": self.request["turn_id"],
            "at": at.isoformat(),
            "type": type_,
            "data": data,
        }

    def record(self, index: int) -> dict[str, object]:
        if index < 0:
            return {"type": "turn_ended", "data": {"outcome": "completed"}}
        return {
            "type": "agent_message",
            "data": {
                "message_id": f"message-{index:03d}",
                "parts": [{"type": "text", "text": piece(index)}],
            },
        }

    def earlier(self) -> list[dict]:
        """A finished turn from before the host restarted."""
        if not self.script.after_restart:
            return []
        at = (self.clock.created - timedelta(minutes=5)).isoformat()
        return [
            {
                "seq": seq,
                "session_id": self.session_id,
                "turn_id": "earlier-turn",
                "at": at,
                "type": type_,
                "data": data,
            }
            for seq, (type_, data) in enumerate(
                [
                    ("status_running", {}),
                    (
                        "agent_message",
                        {
                            "message_id": "earlier-message",
                            "parts": [{"type": "text", "text": EARLIER}],
                        },
                    ),
                    ("turn_ended", {"outcome": "completed"}),
                ],
                start=1,
            )
        ]

    def journal(self) -> list[dict]:
        values = self.earlier()
        if self.request is None:
            return values
        values += [
            self.stamped(len(values) + 1, "status_running", {}, self.clock.create()),
            self.stamped(
                len(values) + 2,
                "usage",
                {
                    "request_id": "request-1",
                    "model": "gpt-6-luna",
                    "input_tokens": 10,
                    "cached_input_tokens": 0,
                    "output_tokens": 5,
                    "reasoning_tokens": 0,
                },
                self.clock.create(),
            ),
        ]
        for record in self.progress.records:
            values.append(
                self.stamped(
                    len(values) + 1,
                    cast(str, record["type"]),
                    cast(dict, record["data"]),
                    self.clock.read(),
                )
            )
        if self.progress.interrupts and not self.progress.ended:
            values.append(
                self.stamped(
                    len(values) + 1, "turn_ended", {"outcome": "interrupted"}, self.clock.read()
                )
            )
        return values

    def poll(self, request: httpx.Request) -> httpx.Response:
        if self.request is not None:
            if self.drop is not None:
                drop, self.drop = self.drop, None
                if drop == "read_error":
                    raise httpx.ReadError("connection reset mid-turn", request=request)
                raise httpx.RemoteProtocolError("peer closed mid-chunk", request=request)
            if self.progress.read_fails():
                return unavailable(request)
            # Each poll after the send is one script step.
            conn, _ = self.progress.connect()
            if conn.end in ("read_error", "protocol_error"):
                self.drop = conn.end
            if conn.end != "terminal":
                self.progress.quiet = True
        after = int(
            request.url.params.get("replay_after_seq", request.url.params.get("after_seq", "0"))
        )
        values = self.journal()
        return httpx.Response(
            200,
            json={
                "events": [v for v in values if v["seq"] > after],
                "complete": bool(values) and values[-1]["type"] == "turn_ended",
            },
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/events"):
            return self.poll(request)
        body = json.loads(request.content) if request.content else {}
        if path == "/v0/turns":
            if self.request is None or self.request["idempotency_key"] != body["idempotency_key"]:
                self.request = body
                self.sends += 1
            return httpx.Response(202, json={"accepted": True, "turn_id": body["turn_id"]})
        if path.endswith("/cancel"):
            self.progress.interrupted()
            return httpx.Response(200, json={"cancelled": True})
        raise AssertionError(f"unscripted harness request {request.method} {path}")


class _Resources:
    def __init__(self, value: object) -> None:
        self.value = value

    async def retrieve(self, scope: object, ref: object) -> object:
        return self.value


def _ref(kind: str) -> ResourceRef:
    return ResourceRef(
        id=kind,
        kind=kind,
        provider="selfhosted",
        account_scope_id="tenant",
        tenant_id=SCOPE.tenant_id,
        account_id=None,
    )


async def run(monkeypatch: pytest.MonkeyPatch, script: Script) -> Result:
    wire = HarnessWire(script)
    agent = Agent(
        ref=_ref("agent"),
        revision=Revision(local=1),
        created_at=datetime.now(UTC),
        spec=AgentSpec(name="contract", model=ModelRef(provider="selfhosted", id="gpt-6-luna")),
    )
    environment = Environment(
        ref=_ref("environment"),
        revision=Revision(local=1),
        created_at=datetime.now(UTC),
        spec=EnvironmentSpec(name="contract", execution="self_hosted"),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)) as client:
        backend = SelfHostedDriver(
            DirectTransport(client, base_url="https://harness", token="offline"),
            account_scope_id="tenant",
            agents=cast(Agents, _Resources(agent)),
            environments=cast(Environments, _Resources(environment)),
            authorization=lambda scope, kind, id_: scope == SCOPE,
            deployment="gpt-6-luna",
        )
        created = await backend.sessions.create(
            SCOPE,
            SessionSpec(
                agent=agent.ref,
                agent_revision=agent.revision,
                environment=environment.ref,
                config_revision=1,
            ),
            key="session",
        )
        wire.session_id = created.ref.id
        io = SelfHostedTurnIO(backend, SCOPE, created.ref, persistence=Persistence())
        state, lifecycle = await drive(
            monkeypatch, io, wire.progress, deadline_s=script.deadline_s, session_id=created.ref.id
        )
    return Result(
        state,
        lifecycle,
        sends=wire.sends,
        facts=wire.progress.facts(),
    )
