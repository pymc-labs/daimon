"""C11 provider evidence: native skill/MCP/file preparation, no invented git mount."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from email import message_from_bytes, policy
from typing import cast

import httpx
import pytest
from mux.contracts.actions import UserMessage
from mux.contracts.events import RequiresActionPayload, TextPart
from mux.contracts.ids import ModelRef, Revision, Scope
from mux.contracts.resources import (
    AgentSpec,
    EnvironmentSpec,
    MCPConnection,
    ResourceBinding,
    SessionSpec,
    SkillUpload,
    SkillUploadFile,
    ToolSpec,
    WorkspaceSource,
)
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.session_controls import SessionControls
from mux.drivers.openai.transport import Object, SDKTransport, object_json
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ContinuityLost, UnsupportedCapability
from mux.testing.drivers.openai.default_capability import decode_upload
from mux.testing.drivers.openai.offline_sdk import refuse_inherited_settings
from mux.testing.isolated_sdk import build_async_openai

SCOPE = Scope(tenant_id="tenant", account_id="account", principal_id="human", authorization_id="go")
BUNDLE = SkillUpload(
    files=(
        SkillUploadFile(
            path="SKILL.md",
            content=b"---\nname: fixture\ndescription: Fixture skill.\n---\nUse fixture.\n",
        ),
    )
)
MCP = MCPConnection(name="daimon", url="https://mcp.example.invalid/mcp")
ACTION: Object = {
    "type": "function_call",
    "call_id": "unavailable",
    "name": "missing_execution",
    "arguments": {},
    "turn_id": "root",
}


class Wire:
    def __init__(self, spend_limit_usd_cents: int | None = 20) -> None:
        self.spend_limit_usd_cents = spend_limit_usd_cents
        self.requests: list[httpx.Request] = []
        self.uploads: list[SkillUpload] = []
        self.files: list[bytes] = []
        self.agent: Object = {}
        self.session: Object = {}
        self.waiting = False

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["OpenAI-Beta"] == "agents=v1"
        path = request.url.path.removeprefix("/v1")
        if path == "/files" and request.method == "POST":
            message = message_from_bytes(
                ("Content-Type: " + request.headers["content-type"] + "\r\n\r\n").encode()
                + request.content,
                policy=policy.default,
            )
            parts = tuple(part for part in message.iter_parts() if part.get_filename())
            assert len(parts) == 1 and parts[0].get_filename() == "repository.tar"
            payload = parts[0].get_payload(decode=True)
            assert isinstance(payload, bytes)
            self.files.append(payload)
            return httpx.Response(
                200,
                json={
                    "id": "repository-archive",
                    "filename": "repository.tar",
                    "bytes": len(payload),
                    "created_at": 0,
                },
            )
        if path == "/skills" and request.method == "POST":
            self.uploads.append(decode_upload(request))
            assert request.headers["Idempotency-Key"] == "skill"
            return httpx.Response(
                200,
                json={
                    "id": "skill",
                    "name": "fixture",
                    "description": "Fixture skill.",
                    "created_at": 0,
                    "latest_version": "1",
                    "default_version": "1",
                },
            )
        if path == "/skills/skill/versions/1":
            return httpx.Response(
                200,
                json={
                    "id": "version",
                    "skill_id": "skill",
                    "version": "1",
                    "name": "fixture",
                    "description": "Fixture skill.",
                    "created_at": 0,
                },
            )
        if path == "/agents" and request.method == "POST":
            self.agent = {
                "id": "agent",
                "created_at": 0,
                **object_json(json.loads(request.content)),
            }
            return httpx.Response(200, json=self.agent)
        if path == "/agents/agent":
            return httpx.Response(200, json=self.agent)
        if path == "/agents/sessions" and request.method == "POST":
            body = object_json(json.loads(request.content))
            if self.spend_limit_usd_cents is None:
                assert "spend_control" not in body
            else:
                assert body["spend_control"] == {"limit": self.spend_limit_usd_cents}
            self.session = {
                "id": "session",
                "agent": {**self.agent, **object_json(body["agent"])},
                "created_at": 0,
                "metadata": body["metadata"],
                "environment": {"id": "environment", **object_json(body["environment"])},
                "status": "idle",
                "required_actions": [],
            }
            if self.spend_limit_usd_cents is not None:
                self.session["spend_control"] = body["spend_control"]
            return httpx.Response(200, json=self.session)
        if path == "/agents/sessions/session":
            return httpx.Response(
                200,
                json={
                    **self.session,
                    "status": "requires_action" if self.waiting else "idle",
                    "required_actions": [ACTION] if self.waiting else [],
                },
            )
        if path == "/agents/sessions/session/turns":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "root",
                            "session_id": "session",
                            "agent_id": "agent",
                            "subagent_id": None,
                            "status": "waiting",
                            "created_at": 0,
                        }
                    ],
                    "has_more": False,
                    "last_id": "root",
                },
            )
        if path == "/agents/sessions/session/events" and request.method == "POST":
            assert request.headers["Idempotency-Key"] == "turn"
            self.waiting = True
            return httpx.Response(202)
        if path == "/agents/sessions/session/events" and request.method == "GET":
            assert request.url.params["stream"] == "true"
            assert request.headers["Accept"] == "text/event-stream"
            event = {
                "event_id": "required",
                "type": "agent.session.requires_action",
                "session": {"id": "session", "required_actions": [ACTION]},
            }
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content="data: " + json.dumps(event) + "\n\n",
            )
        raise AssertionError("unexpected native route")


@asynccontextmanager
async def deployment_adapter(
    *, spend_limit_usd_cents: int | None = 20
) -> AsyncIterator[tuple[OpenAIDriver, Wire]]:
    refuse_inherited_settings()
    wire = Wire(spend_limit_usd_cents)
    async with build_async_openai(
        transport=httpx.MockTransport(wire.handle),
        api_key="offline",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        driver = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            authorization=lambda scope, kind, id_: scope == SCOPE,
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            session_controls=SessionControls(
                model="gpt-6-luna",
                multi_agent_enabled=False,
                container_size="small",
                spend_limit_usd_cents=spend_limit_usd_cents,
            ),
        )
        yield driver, wire


@pytest.fixture(params=(None, 20), ids=("cap-omitted", "explicit-cap"))
async def deployment(request: pytest.FixtureRequest) -> AsyncIterator[tuple[OpenAIDriver, Wire]]:
    async with deployment_adapter(spend_limit_usd_cents=cast(int | None, request.param)) as ports:
        yield ports


@pytest.mark.asyncio
async def test_skill_pin_mcp_and_uploaded_file_are_bound_before_unavailable_execution(
    deployment: tuple[OpenAIDriver, Wire],
) -> None:
    driver, wire = deployment
    skill = await driver.skills.create(SCOPE, BUNDLE, key="skill")
    assert wire.uploads == [BUNDLE]
    assert skill.latest_version is not None
    agent = await driver.agents.create(
        SCOPE,
        AgentSpec(
            name="interaction",
            model=ModelRef(provider="openai", id="gpt-6-luna"),
            skills=(skill.latest_version,),
            mcp_servers=(MCP,),
            tools=(
                ToolSpec(name="missing_execution", kind="custom", input_schema={"type": "object"}),
            ),
        ),
        key="agent",
    )

    # An uploaded archive is a FILE. This is not a git checkout claim.
    async def content():
        yield b"owner-supplied-archive-bytes"

    uploaded = await driver.artifacts.upload(
        SCOPE, content(), filename="repository.tar", media_type="application/x-tar", key="file"
    )
    uploaded_file = uploaded.ref
    assert wire.files == [b"owner-supplied-archive-bytes"]
    session = await driver.sessions.create(
        SCOPE,
        SessionSpec(
            agent=agent.ref,
            agent_revision=Revision(local=0),
            config_revision=1,
            resources=(
                ResourceBinding(
                    id="repository-archive",
                    kind="artifact",
                    resource=uploaded_file,
                    target_path="/workspace/repository.tar",
                ),
            ),
        ),
        key="session",
    )
    saved_agent = await driver.agents.retrieve(SCOPE, agent.ref)
    assert saved_agent.spec.skills == (skill.latest_version,)
    assert saved_agent.spec.mcp_servers == (MCP,)
    native = object_json(wire.session["environment"])
    assert native["skills"] == [{"type": "skill_reference", "skill_id": "skill", "version": "1"}]
    assert native["files"] == [
        {"type": "file_id", "file_id": "repository-archive", "path": "/workspace/repository.tar"}
    ]
    if wire.spend_limit_usd_cents is None:
        assert "spend_control" not in wire.session
    else:
        assert wire.session["spend_control"] == {"limit": wire.spend_limit_usd_cents}
    receipt = await driver.events.send(
        SCOPE,
        session.ref,
        (UserMessage(content=(TextPart(text="Use fixture, then missing_execution."),)),),
        key="turn",
    )
    assert receipt.status == "queued" and not receipt.input_ids
    events = [event async for event in driver.events.stream(SCOPE, session.ref)]
    actions = [event.typed_payload() for event in events if event.type == "session.requires_action"]
    assert len(actions) == 1 and isinstance(actions[0], RequiresActionPayload)
    assert actions[0].actions[0].call_id == "unavailable"
    assert not any(event.type == "session.turn_ended" for event in events)
    current = await driver.sessions.retrieve(SCOPE, session.ref)
    assert current.state == "requires_action" and current.active_root_turn == "root"
    assert current.required_actions == actions[0].actions
    native["skills"] = [{"type": "skill_reference", "skill_id": "skill", "version": "2"}]
    wire.session["environment"] = native
    with pytest.raises(ContinuityLost, match="installed skill pins"):
        await driver.sessions.retrieve(SCOPE, session.ref)


@pytest.mark.asyncio
async def test_c11_distinct_title_and_native_repository_remain_explicit_gaps(
    deployment: tuple[OpenAIDriver, Wire],
) -> None:
    driver, wire = deployment
    with pytest.raises(UnsupportedCapability, match="skill_display_title"):
        await driver.skills.create(
            SCOPE, BUNDLE.model_copy(update={"display_title": "separate title"}), key="title"
        )
    with pytest.raises(UnsupportedCapability, match="workspace_source"):
        await driver.environments.create(
            SCOPE,
            EnvironmentSpec(
                name="native-git",
                sources=(
                    WorkspaceSource(
                        kind="repository",
                        target_path="/workspace/repository",
                        repository_url="https://example.invalid/repository.git",
                        ref="main",
                    ),
                ),
            ),
            key="repository",
        )
    assert wire.requests == []
