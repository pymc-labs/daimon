from __future__ import annotations

import json

import httpx
import pytest
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart
from mux.contracts.ids import ModelRef, Revision, Scope, SkillRef
from mux.contracts.resources import AgentSpec, SessionSpec, SkillUpload, SkillUploadFile
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai._common import objects
from mux.drivers.openai.transport import Object, SDKTransport, object_json
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ContinuityLost
from openai import AsyncOpenAI

SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="human", authorization_id="grant"
)


class Wire:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.agent: Object = {}
        self.session: Object = {}
        self.version = "1"
        self.turn = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/v1")
        assert request.headers["OpenAI-Beta"] == "agents=v1"
        if path == "/skills" and request.method == "POST":
            assert request.headers["Idempotency-Key"] == "skill"
            return httpx.Response(
                200,
                json={
                    "id": "skill",
                    "name": "fixture",
                    "description": "fixture",
                    "latest_version": "1",
                    "default_version": "1",
                    "created_at": 0,
                },
            )
        if path == "/skills/skill":
            return httpx.Response(200, json={"id": "skill", "default_version": self.version})
        if path == "/agents" and request.method == "POST":
            self.agent = {
                "id": "agent",
                "created_at": 0,
                **object_json(json.loads(request.content)),
            }
            assert "skills" not in self.agent
            return httpx.Response(200, json=self.agent)
        if path == "/agents/agent":
            return httpx.Response(200, json=self.agent)
        if path == "/agents/sessions" and request.method == "POST":
            body = object_json(json.loads(request.content))
            assert object_json(body["environment"])["skills"] == [
                {"type": "skill_reference", "skill_id": "skill", "version": "1"}
            ]
            self.session = {
                "id": "session",
                "agent": {"id": "agent", "model": "fixture"},
                "created_at": 0,
                "status": "idle",
                "required_actions": [],
                "metadata": body["metadata"],
                "environment": {"id": "environment", **object_json(body["environment"])},
            }
            return httpx.Response(200, json=self.session)
        if path == "/agents/sessions/session":
            return httpx.Response(200, json=self.session)
        if path == "/agents/sessions/session/events" and request.method == "POST":
            self.turn += 1
            assert request.headers["Idempotency-Key"] == f"send-{self.turn}"
            return httpx.Response(202)
        if path == "/agents/sessions/session/events" and request.method == "GET":
            assert request.url.params["stream"] == "true"
            assert request.headers["Accept"] == "text/event-stream"
            value: Object = {
                "event_id": f"end-{self.turn}",
                "type": "agent.session.turn.completed",
                "session_id": "session",
                "turn": {
                    "id": f"turn-{self.turn}",
                    "session_id": "session",
                    "subagent_id": None,
                    "agent_id": "agent",
                    "created_at": 0,
                    "status": "completed",
                    "usage": None,
                },
            }
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content="event: agent.session.turn.completed"
                + "\ndata: "
                + json.dumps(value)
                + "\n\n",
            )
        return httpx.Response(404, json={"error": {"message": "fixture path not seeded"}})


@pytest.mark.asyncio
async def test_inline_skill_to_agent_to_hosted_session_pins_survive_two_turns() -> None:
    wire = Wire()
    sdk = AsyncOpenAI(
        api_key="offline-fixture",
        base_url="https://openai.invalid/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)),
    )
    driver = OpenAIDriver(
        SDKTransport(sdk),
        account_scope_id="project",
        authorization=lambda scope, kind, id_: scope == SCOPE,
        journal=MemoryRecoveryJournal(),
        usage_revisions=MemoryUsageRevisions(),
    )
    try:
        skill = await driver.skills.create(
            SCOPE,
            SkillUpload(
                files=(
                    SkillUploadFile(
                        path="SKILL.md",
                        content=(
                            b"---\nname: fixture\ndescription: Fixture skill.\n---\nUse fixture.\n"
                        ),
                    ),
                )
            ),
            key="skill",
        )
        agent = await driver.agents.create(
            SCOPE,
            AgentSpec(
                name="agent",
                model=ModelRef(provider="openai", id="fixture"),
                skills=(SkillRef(id=skill.id),),
            ),
            key="agent",
        )
        session = await driver.sessions.create(
            SCOPE,
            SessionSpec(agent=agent.ref, agent_revision=Revision(local=0), config_revision=1),
            key="session",
        )
        assert session.continuity.workspace == "native_reuse"
        wire.version = "2"
        for number in (1, 2):
            receipt = await driver.events.send(
                SCOPE,
                session.ref,
                (UserMessage(content=(TextPart(text="hello"),)),),
                key=f"send-{number}",
            )
            assert receipt.status == "queued"
            events = [event async for event in driver.events.stream(SCOPE, session.ref)]
            assert len([event for event in events if event.type == "session.turn_ended"]) == 1
            current = await driver.sessions.retrieve(SCOPE, session.ref)
            assert current.binding.native_refs["environment"] == "environment"
            assert objects(object_json(wire.session["environment"])["skills"])[0]["version"] == "1"
        environment = object_json(wire.session["environment"])
        installed = objects(environment["skills"])
        wire.session["environment"] = {**environment, "skills": [{**installed[0], "version": "2"}]}
        with pytest.raises(ContinuityLost, match="installed skill pins"):
            await driver.sessions.retrieve(SCOPE, session.ref)
    finally:
        await sdk.close()
