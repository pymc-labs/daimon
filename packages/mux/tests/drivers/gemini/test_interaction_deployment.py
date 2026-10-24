"""C11 actual-SDK wire facts, not a fictional native skills upload."""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import pytest
from mux.contracts.actions import UserMessage, UserToolResult
from mux.contracts.events import RequiresActionPayload, TextPart
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, PageRequest, Scope
from mux.contracts.resources import (
    Agent,
    AgentSpec,
    Environment,
    EnvironmentSpec,
    MCPConnection,
    SessionSpec,
    SkillUpload,
    SkillUploadFile,
    ToolSpec,
    WorkspaceSource,
)
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.transport import SDKTransport
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
from mux.state.memory import MemoryStateStore
from mux.testing.drivers.gemini.conformance_hooks import InlineSkillDeployment, prepare_inline_skill
from mux.testing.drivers.gemini.fake import MemoryStorage
from mux.testing.isolated_sdk import build_gemini_client

pytestmark = pytest.mark.usefixtures("gemini_recorded_clock")

SCOPE = Scope(tenant_id="n12", account_id="offline", principal_id="human", authorization_id="auth")
FILES = {
    "SKILL.md": b"---\nname: fixture\ndescription: Offline deployment proof\n---\nUse scripts/run.py.\n",
    "scripts/run.py": b"print('exact original bytes')\n",
    "reference.txt": "Unicode: \u2603\n".encode(),
}
BUNDLE = SkillUpload(
    display_title="fixture",
    files=tuple(SkillUploadFile(path=path, content=body) for path, body in FILES.items()),
)
STAMP = datetime(2026, 10, 10, tzinfo=UTC).isoformat()


@dataclass
class Wire:
    requests: list[httpx.Request] = field(default_factory=list[httpx.Request])
    native_action: bool = False

    def response(self, id_: str = "interaction") -> dict[str, object]:
        return {
            "id": id_,
            "status": "requires_action" if id_ == "interaction" else "completed",
            "created": STAMP,
            "updated": STAMP,
            "environment_id": "workspace",
            "steps": (
                []
                if self.native_action
                else [
                    {
                        "type": "function_call",
                        "id": "call",
                        "name": "unavailable",
                        "arguments": {"path": "repo/file"},
                    }
                ]
            )
            if id_ == "interaction"
            else [],
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET" and request.url.params.get("stream") == "true":
            # Empty provider stream cannot release the occupied required action.
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"")
        if request.method == "GET":
            assert request.url.path.endswith("/interactions/interaction")
            return httpx.Response(200, json=self.response())
        assert request.method == "POST" and request.url.path.endswith("/interactions")
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json=self.response(
                "continuation" if body.get("previous_interaction_id") else "interaction"
            ),
        )


@pytest.fixture
async def sdk() -> AsyncIterator[
    tuple[GeminiManagedAgents, Wire, MemoryStorage, MemoryStateStore, SDKTransport]
]:
    wire, storage, state = Wire(), MemoryStorage(), MemoryStateStore()
    with build_gemini_client(
        transport=httpx.MockTransport(wire), api_key="offline-fixture"
    ) as client:
        transport = SDKTransport(client)
        ma = GeminiManagedAgents(
            transport, storage=storage, state_store=state, account_scope_id="project"
        )
        try:
            yield ma, wire, storage, state, transport
        finally:
            await client.aio.aclose()


async def prepare(
    ma: GeminiManagedAgents, *, bundle: SkillUpload = BUNDLE
) -> InlineSkillDeployment:
    def session_spec(agent: Agent, env: Environment) -> SessionSpec:
        return SessionSpec(
            agent=agent.ref,
            agent_revision=agent.revision,
            environment=env.ref,
            config_revision=1,
            extensions={
                "gemini.session": ExtensionConfig(
                    namespace="gemini.session",
                    version=1,
                    value={
                        "binding_id": "binding",
                        "thread": {
                            "channel": {
                                "tenant_id": "n12",
                                "platform": "test",
                                "channel_id": "channel",
                            },
                            "thread_id": "thread",
                        },
                    },
                )
            },
        )

    return await prepare_inline_skill(
        ma,
        SCOPE,
        bundle,
        agent=AgentSpec(
            name="deployment",
            model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
            mcp_servers=(MCPConnection(name="public", url="https://offline.invalid/mcp"),),
            tools=(ToolSpec(name="unavailable", kind="custom", input_schema={"type": "object"}),),
        ),
        environment=EnvironmentSpec(
            name="repository",
            sources=(
                WorkspaceSource(
                    kind="repository",
                    repository_url="https://offline.invalid/repo.git",
                    target_path="repo",
                ),
            ),
        ),
        session_spec=session_spec,
        key="deployment",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("native_action", [False, True])
async def test_inline_pinned_bundle_mcp_repository_and_unavailable_action_wire_proof(
    sdk: tuple[GeminiManagedAgents, Wire, MemoryStorage, MemoryStateStore, SDKTransport],
    native_action: bool,
) -> None:
    ma, wire, storage, state, transport = sdk
    wire.native_action = native_action
    deployment = await prepare(ma)
    spec = deployment.desired
    assert wire.requests == []  # Local catalogue creation is not an upstream upload.
    agent = await ma.agents.retrieve(SCOPE, spec.agent)
    assert agent.spec.skills is not None
    pin = agent.spec.skills[0]
    skill = await ma.skills.retrieve(SCOPE, pin.id)
    assert skill.latest_version == pin and pin.version == pin.digest
    assert isinstance(skill.native, dict)
    assert skill.native["provider_upload"] is False
    session = deployment.session
    # A restart and a newer catalogue version cannot swap the already-pinned bytes.
    ma = GeminiManagedAgents(
        transport, storage=storage, state_store=state.restart(), account_scope_id="project"
    )
    changed = BUNDLE.model_copy(
        update={
            "files": (
                SkillUploadFile(path="SKILL.md", content=b"---\nname: newer\n---\nNew bytes\n"),
            )
        }
    )
    newer = await ma.skills.publish_version(SCOPE, pin.id, changed, key="new-version")
    assert newer.ref.version != pin.version
    receipt = await ma.events.send(
        SCOPE,
        session.ref,
        (UserMessage(content=(TextPart(text="use the fixture skill"),)),),
        key="interaction",
    )
    assert receipt.status == "queued"
    body = json.loads(wire.requests[0].content)
    assert body["agent_config"]["model"] == "gemini-3.8-flash"
    assert body["background"] is True and body["store"] is True
    assert body["tools"] == [
        {
            "type": "function",
            "name": "unavailable",
            "description": "",
            "parameters": {"type": "object"},
        },
        {"type": "mcp_server", "name": "public", "url": "https://offline.invalid/mcp"},
    ]
    sources = body["environment"]["sources"]
    assert body["environment"]["type"] == "remote"
    assert sources[0] == {
        "type": "repository",
        "source": "https://offline.invalid/repo.git",
        "target": "repo",
    }
    # Decode retained request.body bytes independently, compare every original byte/file.
    actual = {
        source["target"].removeprefix(f".agents/skills/{pin.id}/"): source["content"].encode(
            "utf-8"
        )
        for source in sources[1:]
    }
    assert actual == FILES
    assert all(source["type"] == "inline" for source in sources[1:])
    assert len({source["target"] for source in sources}) == len(sources)
    assert json.dumps(FILES["SKILL.md"].decode()).encode() in wire.requests[0].content
    assert [e async for e in ma.events.stream(SCOPE, session.ref)] == []
    journal = await ma.events.list(SCOPE, session.ref, page=PageRequest())
    actions = [e.typed_payload() for e in journal.data if e.type == "session.requires_action"]
    assert actions and all(isinstance(a, RequiresActionPayload) and a.actions for a in actions)
    assert not any(e.type == "session.turn_ended" for e in journal.data)
    waiting = await ma.sessions.retrieve(SCOPE, session.ref)
    assert waiting.state == "requires_action" and waiting.active_root_turn == receipt.turn_id
    assert waiting.required_actions
    posts = sum(r.method == "POST" for r in wire.requests)
    with pytest.raises(UnsupportedCapability):
        await ma.events.send(
            SCOPE, session.ref, (UserMessage(content=(TextPart(text="new turn"),)),), key="blocked"
        )
    if native_action:
        with pytest.raises(UnsupportedCapability):
            await ma.events.send(
                SCOPE,
                session.ref,
                (
                    UserToolResult(
                        action_id=waiting.required_actions[0].id,
                        content=(TextPart(text="not executed"),),
                    ),
                ),
                key="refuse-native-action",
            )
        assert sum(r.method == "POST" for r in wire.requests) == posts
    else:
        result = await ma.events.send(
            SCOPE,
            session.ref,
            (
                UserToolResult(
                    action_id=waiting.required_actions[0].id,
                    content=(TextPart(text="typed result"),),
                ),
            ),
            key="continuation",
        )
        assert result.turn_id == receipt.turn_id
        continuation = json.loads([r for r in wire.requests if r.method == "POST"][-1].content)
        assert continuation["previous_interaction_id"] == "interaction"
        assert continuation["environment"] == "workspace"
        assert continuation["tools"] == body["tools"]
        assert continuation["input"] == [
            {
                "type": "function_result",
                "name": "unavailable",
                "call_id": "call",
                "result": "typed result",
            }
        ]


@pytest.mark.asyncio
async def test_tampered_skill_pin_or_foreign_catalogue_refuses_before_provider_io(
    sdk: tuple[GeminiManagedAgents, Wire, MemoryStorage, MemoryStateStore, SDKTransport],
) -> None:
    ma, wire, _, _, _ = sdk
    deployment = await prepare(ma)
    spec = deployment.desired
    agent = await ma.agents.retrieve(SCOPE, spec.agent)
    assert agent.spec.skills is not None
    pin = agent.spec.skills[0]
    for pin_field, error in (("digest", ProviderError), ("id", ScopeViolation)):
        with pytest.raises(error):
            await ma.agents.create(
                SCOPE,
                agent.spec.model_copy(
                    update={"skills": (pin.model_copy(update={pin_field: "foreign"}),)}
                ),
                key=pin_field,
            )
    assert not wire.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["agent", "agent_revision", "environment"])
async def test_host_callback_cannot_substitute_prepared_resources(
    sdk: tuple[GeminiManagedAgents, Wire, MemoryStorage, MemoryStateStore, SDKTransport],
    changed: str,
) -> None:
    ma, wire, _, _, _ = sdk

    def wrong_session(agent: Agent, env: Environment) -> SessionSpec:
        spec = SessionSpec(
            agent=agent.ref, agent_revision=agent.revision, environment=env.ref, config_revision=1
        )
        if changed == "agent":
            return spec.model_copy(update={"agent": agent.ref.model_copy(update={"id": "foreign"})})
        if changed == "agent_revision":
            return spec.model_copy(
                update={"agent_revision": agent.revision.model_copy(update={"local": 99})}
            )
        return spec.model_copy(update={"environment": None})

    with pytest.raises(ScopeViolation, match="changed the pinned deployment"):
        await prepare_inline_skill(
            ma,
            SCOPE,
            BUNDLE,
            agent=AgentSpec(name="proof", model=ModelRef(provider="gemini", id="gemini-3.8-flash")),
            environment=EnvironmentSpec(name="proof"),
            session_spec=wrong_session,
            key="proof",
        )
    assert not wire.requests
