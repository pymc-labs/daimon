"""Gemini interactions: the real `GeminiManagedAgents` and `GeminiTurnIO` over a scripted
interactions transport.

Gemini's stream carries only status events; the answer is the interaction's saved
steps, which a read returns once the interaction leaves `in_progress`. So a
connection's `deliver` count is status updates (each one a read), and every stamp
the script describes is on the interaction object a read returns.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from copy import deepcopy

import httpx
import pytest
from daimon.core.turn.gemini import PROFILE, GeminiTurnIO
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ModelRef, ThreadRef
from mux.contracts.resources import AgentSpec, EnvironmentSpec, SessionSpec
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.transport import Object, OwnedStream, normalize_error
from mux.state.memory import MemoryStateStore
from mux.testing.drivers.gemini.fake import FakeTransport, MemoryStorage
from pydantic import JsonValue

from .drive import SCOPE, Result, drive
from .script import Clock, Script, piece
from .wire import Progress

THREAD = ThreadRef(
    channel=ChannelRef(tenant_id=SCOPE.tenant_id, platform="test", channel_id="channel"),
    thread_id="thread",
)
INTERACTION = "interaction-1"


class InteractionsWire(FakeTransport):
    def __init__(self, script: Script) -> None:
        super().__init__()
        self.script = script
        self.clock = Clock(script.stamps)
        self.progress = Progress(script, lambda index: {"index": index})
        self.sends = 0
        self.interrupts = 0
        self.failed_reads = 0

    def interaction(self, *, read: bool) -> Object:
        status = (
            "cancelled"
            if self.interrupts
            else ("completed" if self.progress.ended else "in_progress")
        )
        created = self.clock.create().isoformat()
        stamp = self.clock.read().isoformat() if read else created
        steps: list[JsonValue] = (
            [
                {"type": "model_output", "content": [{"type": "text", "text": piece(i)}]}
                for i in range(self.script.pieces)
            ]
            if status == "completed"
            else []
        )
        return {
            "id": INTERACTION,
            "status": status,
            "created": created,
            "updated": stamp,
            "environment_id": "workspace",
            "steps": steps,
            "usage": {
                "total_input_tokens": 64,
                "total_cached_tokens": 16,
                "total_output_tokens": 8 if status == "completed" else 1,
                "total_thought_tokens": 3,
            },
        }

    async def create(self, request: Mapping[str, JsonValue]) -> Object:
        self.requests.append(deepcopy(dict(request)))
        self.sends += 1
        return self.interaction(read=False)

    async def get(self, interaction_id: str) -> Object:
        self.read_requests.append(interaction_id)
        if self.progress.quiet and self.failed_reads < (
            self.script.status_failures + self.script.replay_failures
        ):
            self.failed_reads += 1
            raise normalize_error(httpx.ConnectError("unavailable"))
        return self.interaction(read=True)

    async def cancel(self, interaction_id: str) -> Object:
        self.cancelled.append(interaction_id)
        self.interrupts += 1
        return self.interaction(read=True)

    async def open_stream(
        self, interaction_id: str, *, after: str | None = None
    ) -> AsyncIterator[Object]:
        conn, _ = self.progress.connect()
        updates = conn.deliver

        async def events() -> AsyncIterator[Object]:
            for n in range(updates):
                yield {"event_type": "interaction.status_update", "event_id": f"s{n}"}
            await asyncio.sleep(0)
            if conn.end == "terminal" and self.progress.ended:
                yield {"event_type": "interaction.completed", "event_id": "done"}
                return
            self.progress.quiet = True
            if conn.end == "eof" or conn.end == "terminal":
                return
            request = httpx.Request("GET", "https://contract.invalid/stream")
            if conn.end == "read_error":
                raise normalize_error(httpx.ReadError("connection reset", request=request))
            if conn.end == "protocol_error":
                raise normalize_error(httpx.RemoteProtocolError("peer closed", request=request))
            await asyncio.Event().wait()

        async def close() -> None:
            self.stream_closed = True

        return OwnedStream(events(), close)


async def run(monkeypatch: pytest.MonkeyPatch, script: Script) -> Result:
    wire = InteractionsWire(script)
    backend = GeminiManagedAgents(
        wire, storage=MemoryStorage(), state_store=MemoryStateStore(), account_scope_id="project"
    )
    agent = await backend.agents.create(
        SCOPE,
        AgentSpec(name="contract", model=ModelRef(provider="gemini", id="gemini-3.8-flash")),
        key="agent",
    )
    environment = await backend.environments.create(
        SCOPE, EnvironmentSpec(name="inline"), key="env"
    )
    created = await backend.sessions.create(
        SCOPE,
        SessionSpec(
            agent=agent.ref,
            agent_revision=agent.revision,
            environment=environment.ref,
            config_revision=1,
            extensions={
                "gemini.session": ExtensionConfig(
                    namespace="gemini.session",
                    version=1,
                    value={"binding_id": "binding", "thread": THREAD.model_dump(mode="json")},
                )
            },
        ),
        key="session",
    )
    io = GeminiTurnIO(backend, SCOPE, created.ref)
    state, lifecycle = await drive(
        monkeypatch, io, deadline_s=script.deadline_s, session_id=created.ref.id
    )
    return Result(
        state,
        lifecycle,
        sends=wire.sends,
        facts={"opens": wire.progress.opens, "interrupts": wire.interrupts},
    )


__all__ = ["PROFILE", "run"]
