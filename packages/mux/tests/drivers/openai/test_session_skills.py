from __future__ import annotations

import json

import httpx
import pytest
from mux.contracts.ids import ResourceRef, Revision, Scope, SkillRef
from mux.contracts.resources import SessionSpec
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.skill_bindings import encode_metadata
from mux.drivers.openai.transport import Object, SDKTransport, object_json
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import UnsupportedCapability
from mux.testing.isolated_sdk import build_async_openai

SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="human", authorization_id="grant"
)


class Wire:
    def __init__(self, agent_pins: tuple[SkillRef, ...] = ()) -> None:
        self.metadata: Object = {}
        self.session: Object = {}
        self.agent_pins = agent_pins

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1")
        if path.startswith("/skills/"):
            parts = path.split("/")
            if len(parts) == 5:
                return httpx.Response(200, json={"skill_id": parts[2], "version": parts[4]})
            return httpx.Response(200, json={"id": parts[2], "default_version": "3"})
        if path == "/agents/agent":
            agent: Object = {"id": "agent", "created_at": 0, "model": "fixture"}
            if self.agent_pins:
                agent["metadata"] = {**self.metadata}
            return httpx.Response(200, json=agent)
        if path == "/agents/sessions" and request.method == "POST":
            body = object_json(json.loads(request.content))
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
        return httpx.Response(404, json={"error": {"message": "fixture path not seeded"}})


def driver(wire: Wire, profile_id: str) -> tuple[OpenAIDriver, object]:
    sdk = build_async_openai(
        transport=httpx.MockTransport(wire.handle),
        api_key="offline-fixture",
        base_url="https://openai.invalid/v1",
    )
    return (
        OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            authorization=lambda scope, kind, id_: scope == SCOPE,
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            profile_id=profile_id,
        ),
        sdk,
    )


def spec(*skills: SkillRef) -> SessionSpec:
    return SessionSpec(
        agent=ResourceRef(
            provider="openai",
            account_scope_id="project",
            kind="agent",
            id="agent",
            tenant_id="tenant",
            account_id="account",
        ),
        agent_revision=Revision(local=0),
        config_revision=1,
        skills=skills,
    )


@pytest.mark.asyncio
async def test_session_skills_when_agent_has_pins_then_both_attach_once() -> None:
    wire = Wire(agent_pins=(SkillRef(id="agent-skill"),))
    client, sdk = driver(wire, "openai.persistent_workspace")
    wire.metadata = encode_metadata(wire.agent_pins, client.sessions._c)  # pyright: ignore[reportPrivateUsage]
    try:
        session = await client.sessions.create(
            SCOPE,
            spec(SkillRef(id="agent-skill"), SkillRef(id="channel-skill", version="2")),
            key="session",
        )
        installed = object_json(wire.session["environment"])["skills"]
        assert installed == [
            {"type": "skill_reference", "skill_id": "agent-skill", "version": "3"},
            {"type": "skill_reference", "skill_id": "channel-skill", "version": "2"},
        ], "agent pins come first and a repeated id attaches once"
        again = await client.sessions.retrieve(SCOPE, session.ref)
        assert again.ref == session.ref, "stored pins match what the session installed"
    finally:
        await sdk.close()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_session_skills_when_agent_has_none_then_session_installs_them() -> None:
    wire = Wire()
    client, sdk = driver(wire, "openai.persistent_workspace")
    try:
        await client.sessions.create(SCOPE, spec(SkillRef(id="channel-skill")), key="session")
        installed = object_json(wire.session["environment"])["skills"]
        assert installed == [
            {"type": "skill_reference", "skill_id": "channel-skill", "version": "3"}
        ], "a session's own skill attaches without agent pins"
    finally:
        await sdk.close()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_session_skills_when_conversation_only_then_refused() -> None:
    wire = Wire()
    client, sdk = driver(wire, "openai.conversation_only")
    try:
        with pytest.raises(UnsupportedCapability, match="skills_bundle"):
            await client.sessions.create(SCOPE, spec(SkillRef(id="channel-skill")), key="s")
        assert wire.session == {}, "no session is created without its skills"
    finally:
        await sdk.close()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_session_skills_when_source_is_not_native_then_refused() -> None:
    wire = Wire()
    client, sdk = driver(wire, "openai.persistent_workspace")
    try:
        with pytest.raises(UnsupportedCapability, match="session_skill_source"):
            await client.sessions.create(
                SCOPE, spec(SkillRef(id="skill", source="custom")), key="s"
            )
    finally:
        await sdk.close()  # type: ignore[attr-defined]
