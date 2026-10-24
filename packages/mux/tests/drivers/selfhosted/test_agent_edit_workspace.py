"""An agent edit keeps the harness session: the real harness app still has the workspace file.

The mux driver talks to the harness application over loopback HTTP with a
scripted model adapter, so no model service or credential is involved.
"""

from __future__ import annotations

import asyncio
import hashlib
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import httpx
import pytest
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart
from mux.contracts.ids import ModelRef, ResourceRef, Revision, Scope
from mux.contracts.ports import Agents, Environments
from mux.contracts.resources import Agent, AgentSpec, Environment, EnvironmentSpec, SessionSpec
from mux.drivers.selfhosted import SelfHostedDriver
from mux.drivers.selfhosted.transport import DirectTransport
from pydantic import JsonValue

if TYPE_CHECKING:
    from daimon_harness.runtime import TurnContext

# The harness app and its server are workspace packages, not mux dependencies:
# a mux-only install skips this module instead of failing to collect it.
uvicorn = pytest.importorskip("uvicorn")
create_app = pytest.importorskip("daimon_harness.service").create_app

SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="person", authorization_id="grant"
)


class ScriptedModel:
    """Turn one writes a file; turn two reports what the harness still holds."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.seen: list[dict[str, object]] = []

    def workspace(self, session_id: str) -> Path:
        digest = hashlib.sha256(session_id.encode()).hexdigest()[:32]
        return self.home / "sessions" / digest / "work"

    async def run(self, context: TurnContext, history: list[JsonValue]) -> list[JsonValue]:
        request = context.request
        text = request.input[0]
        assert text.type == "text"
        self.seen.append(
            {
                "session_id": request.session_id,
                "instructions": request.instructions,
                "file": (self.workspace(request.session_id) / "result.txt").exists(),
            }
        )
        if text.text == "write":

            async def execute() -> JsonValue:
                return context.write("result.txt", "durable")

            await context.tool(
                "call-1", "write", {"path": "result.txt", "content": "durable"}, execute
            )
        await context.emit(
            "usage",
            {
                "request_id": "req-" + request.turn_id,
                "model": request.model,
                "input_tokens": 1,
                "cached_input_tokens": 0,
                "output_tokens": 1,
                "reasoning_tokens": 0,
            },
        )
        message_id = "msg-" + request.turn_id
        await context.emit("agent_message", {"message_id": message_id, "parts": []})
        return [*history, {"role": "user", "content": text.text}]


def ref(kind: str) -> ResourceRef:
    return ResourceRef(
        id=kind,
        kind=kind,
        provider="selfhosted",
        account_scope_id="tenant",
        tenant_id=SCOPE.tenant_id,
        account_id=None,
    )


class Resource:
    def __init__(self, value) -> None:
        self.value = value

    async def retrieve(self, scope, resource):
        assert scope == SCOPE and resource == self.value.ref
        return self.value


async def served(app, listener):
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    async with asyncio.timeout(10):
        while not server.started:
            await asyncio.sleep(0.01)
    return server, task


async def test_workspace_file_survives_an_agent_edit(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_TEST_MODE", "1")
    monkeypatch.setenv("HARNESS_CONFIRM_TOOLS", "")
    model = ScriptedModel(tmp_path)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server, task = await served(
        create_app(home=tmp_path, token="test-token", adapter=model), listener
    )
    agents = Resource(
        Agent(
            ref=ref("agent"),
            revision=Revision(local=1),
            created_at=datetime.now(UTC),
            spec=AgentSpec(
                name="edits",
                system="original instructions",
                model=ModelRef(provider="selfhosted", id="gpt-6-luna"),
            ),
        )
    )
    environments = Resource(
        Environment(
            ref=ref("environment"),
            revision=Revision(local=1),
            created_at=datetime.now(UTC),
            spec=EnvironmentSpec(name="edits", execution="self_hosted"),
        )
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{listener.getsockname()[1]}", timeout=10
        ) as client:
            driver = SelfHostedDriver(
                DirectTransport(client, base_url=str(client.base_url), token="test-token"),
                account_scope_id="tenant",
                agents=cast(Agents, agents),
                environments=cast(Environments, environments),
                authorization=lambda scope, kind, id_: scope == SCOPE,
                deadline_s=10,
            )
            session = await driver.sessions.create(
                SCOPE,
                SessionSpec(
                    agent=ref("agent"),
                    agent_revision=Revision(local=1),
                    environment=ref("environment"),
                    config_revision=1,
                ),
                key="create",
            )

            async def send(text: str, key: str) -> None:
                await driver.events.send(
                    SCOPE, session.ref, [UserMessage(content=(TextPart(text=text),))], key=key
                )
                async with asyncio.timeout(10):
                    events = [
                        event
                        async for event in await driver.events.open_stream(
                            SCOPE, session.ref, after=None
                        )
                    ]
                assert events[-1].payload["outcome"] == "completed"

            await send("write", "first")
            agents.value = agents.value.model_copy(
                update={
                    "revision": Revision(local=2),
                    "spec": agents.value.spec.model_copy(update={"system": "edited instructions"}),
                }
            )
            desired = (await driver.sessions.spec(SCOPE, session.ref)).model_copy(
                update={"agent_revision": Revision(local=2), "config_revision": 2}
            )
            plan = await driver.sessions.plan_update(SCOPE, session.ref, desired)
            await driver.sessions.apply_update(
                SCOPE, plan, expected=plan.expected_revision, key="u"
            )
            await send("check", "second")
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        listener.close()
    first, second = model.seen
    assert (first["instructions"], first["file"]) == ("original instructions", False)
    assert (second["instructions"], second["file"]) == ("edited instructions", True)
    assert first["session_id"] == second["session_id"] == session.ref.id
    assert (model.workspace(session.ref.id) / "result.txt").read_text() == "durable"
