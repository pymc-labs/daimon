"""Authenticated SDK preparation, native tool scripts and non-verdict controls."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Protocol, cast

import httpx
import pytest
from daimon.testing.outcome_oracle import RunEvidence, TerminalEvidence, TurnEvidence, evaluate
from daimon.testing.provider_fixtures import (
    BackendFixture,
    ScenarioFixture,
    load_provider_fixtures,
    scenario_tape,
)
from daimon.testing.provider_replay import Backend, Object, SourcePin, WireReplay
from jsonschema import Draft202012Validator
from mcp.types import CallToolRequest, CallToolResult
from mux.contracts.actions import UserMessage
from mux.contracts.events import (
    Event,
    TextPart,
    ToolResultPayload,
    ToolUsePayload,
    TurnEndedPayload,
)
from mux.contracts.ids import ModelRef, ResourceRef, Revision, Scope
from mux.contracts.resources import AgentSpec, SessionSpec
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.fake import MemoryStorage
from mux.drivers.gemini.transport import SDKTransport as GeminiTransport
from mux.drivers.gemini.transport import close_iterator
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.transport import SDKTransport, object_json
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
from mux.state.memory import MemoryStateStore
from openai import AsyncOpenAI
from pydantic import TypeAdapter, ValidationError

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "packages/testing/fixtures/target53"
PACK = load_provider_fixtures(DATA / "index.json", catalog_root=DATA / "catalog")
CASES = [(s, p) for s in PACK.scenarios for p in s.providers if p.mcp_binding is not None]
SCOPE = Scope(
    tenant_id="qa-tenant", account_id="account", principal_id="user", authorization_id="qa"
)
OBJECT = TypeAdapter[Object](Object)


class SchemaValidator(Protocol):
    def validate(self, instance: Object, /) -> None: ...


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith(("OPENAI_", "GOOGLE_", "GEMINI_")):
            monkeypatch.delenv(name)


class Preparation:
    """Exactly three native preparation routes, then the strict scenario tape."""

    def __init__(
        self, fixture: BackendFixture, wire: WireReplay, *, fault: str | None = None
    ) -> None:
        assert fixture.mcp_binding is not None
        self.fixture, self.wire, self.fault = fixture, wire, fault
        self.agent: Object | None = None
        self.agent_posts = self.session_posts = self.agent_gets = 0
        self.resolutions: list[tuple[Scope, str, str]] = []
        self.secret = "offline-mcp-fixture"
        self.session_ids = list(dict.fromkeys(t.session_id for t in fixture.turns))

    async def resolve(self, scope: Scope, reference: str, destination: str) -> str:
        self.resolutions.append((scope, reference, destination))
        assert self.fixture.mcp_binding is not None
        allowed = {(c.credential_ref, c.url) for c in self.fixture.mcp_binding.connections}
        assert scope == SCOPE and (reference, destination) in allowed
        if self.fault == "revoked":
            raise RuntimeError("offline-private-resolver-detail")
        return self.secret

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agents" and request.method == "POST":
            assert self.agent_posts == 0
            self.agent_posts += 1
            body = OBJECT.validate_json(request.content)
            assert "authorization" not in request.content.decode()
            assert self.secret not in request.content.decode()
            self.agent = {"id": "qa-agent", "created_at": 0, **body}
            return httpx.Response(200, json=self.agent)
        if request.url.path == "/v1/agents/qa-agent" and request.method == "GET":
            assert self.agent is not None
            self.agent_gets += 1
            body = OBJECT.validate_json(json.dumps(self.agent))
            if self.fault == "destination":
                tools = body["tools"]
                assert isinstance(tools, list) and isinstance(tools[0], dict)
                tools[0]["transport"] = {
                    "type": "http",
                    "server_url": "https://foreign.invalid/mcp",
                }
            return httpx.Response(200, json=body)
        if request.url.path == "/v1/agents/sessions" and request.method == "POST":
            assert self.agent is not None and self.fixture.mcp_binding is not None
            assert self.session_posts < len(self.session_ids)
            body = OBJECT.validate_json(request.content)
            assert body["agent_id"] == "qa-agent"
            override = object_json(body["agent"])
            tools = override["tools"]
            assert isinstance(tools, list)
            assert tools == [
                {
                    "type": "mcp",
                    "server_label": c.name,
                    "allowed_tools": c.tool_policy["allowed_tools"],
                    "required": True,
                    "connection_origin": "service",
                    "transport": {
                        "type": "http",
                        "server_url": c.url,
                        "authorization": "Bearer " + self.secret,
                    },
                }
                for c in self.fixture.mcp_binding.connections
            ]
            session = self.session_ids[self.session_posts]
            self.session_posts += 1
            return httpx.Response(
                200,
                json={
                    "id": session,
                    "agent": {"id": "qa-agent", "model": self.fixture.model},
                    "created_at": 0,
                    "environment": {"type": "openai_hosted", "id": "qa-environment"},
                    "status": "idle",
                    "required_actions": [],
                    "metadata": {"mux_tenant": SCOPE.tenant_id},
                },
            )
        return self.wire.dispatch(request)


def spec(fixture: BackendFixture) -> AgentSpec:
    assert fixture.mcp_binding is not None
    return AgentSpec(
        name="qa-agent",
        model=ModelRef(provider=fixture.backend, id=fixture.model),
        mcp_servers=fixture.mcp_binding.connections,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "fixture"),
    [
        (s, p)
        for s, p in CASES
        if p.backend == "openai" and not any(g.code == "MCP_SERVER_LIMIT" for g in p.gaps)
    ],
    ids=[
        s.scenario_id
        for s, p in CASES
        if p.backend == "openai" and not any(g.code == "MCP_SERVER_LIMIT" for g in p.gaps)
    ],
)
async def test_admitted_recipes_prepare_authenticated_sessions_and_decode_native_calls(
    scenario: ScenarioFixture,
    fixture: BackendFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    keys = {t.turn: f"turn-{t.turn}" for t in fixture.turns}
    wire = WireReplay(scenario_tape(scenario, fixture, keys=keys))
    preparation = Preparation(fixture, wire)
    caplog.set_level(logging.DEBUG, logger="openai")
    events: list[Event] = []
    async with AsyncOpenAI(
        api_key="offline-placeholder",
        base_url="https://offline.invalid/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(preparation.handle)),
    ) as sdk:
        driver = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            authorization=lambda scope, kind, identity: scope == SCOPE,
            mcp_secrets=preparation.resolve,
        )
        agent = await driver.agents.create(SCOPE, spec(fixture), key="prepare-agent")
        sessions: dict[str, ResourceRef] = {}
        for number, session_id in enumerate(preparation.session_ids):
            session = await driver.sessions.create(
                SCOPE,
                SessionSpec(agent=agent.ref, agent_revision=Revision(local=0), config_revision=1),
                key=f"prepare-{number}",
            )
            assert session.ref.id == session_id
            sessions[session_id] = session.ref
        for turn in fixture.turns:
            session = sessions[turn.session_id]
            await driver.events.send(
                SCOPE,
                session,
                (UserMessage(content=(TextPart(text=turn.request_text),)),),
                key=keys[turn.turn],
            )
            stream = await driver.events.open_stream(SCOPE, session)
            events.extend([e async for e in stream])
            await close_iterator(stream)
    wire.assert_consumed()
    assert preparation.agent_posts == 1 and preparation.session_posts == len(
        preparation.session_ids
    )
    assert preparation.agent_gets == len(preparation.session_ids)
    assert fixture.mcp_binding is not None
    assert len(preparation.resolutions) == len(preparation.session_ids) * len(
        fixture.mcp_binding.connections
    )
    assert preparation.secret not in caplog.text
    assert preparation.secret not in json.dumps(preparation.agent)
    calls = {
        p.call_id: p
        for e in events
        if isinstance(p := e.typed_payload(), ToolUsePayload) and p.executor == "mcp"
    }
    results = {
        p.call_id: p for e in events if isinstance(p := e.typed_payload(), ToolResultPayload)
    }
    scripted = {call.call_id: call for turn in fixture.turns for call in turn.mcp_calls}
    assert set(calls) == set(scripted)
    for identity, call in scripted.items():
        assert calls[identity].tool_name == call.name and calls[identity].input == call.arguments
        assert calls[identity].mcp_server == call.server
        assert results[identity].is_error == call.result["isError"]
    terminals = {
        p.root_turn_id: p for e in events if isinstance(p := e.typed_payload(), TurnEndedPayload)
    }
    assert set(terminals) == {t.root_id for t in fixture.turns}
    assert all(t.outcome == "completed" for t in terminals.values())
    recording = RunEvidence(
        scenario_id=scenario.scenario_id,
        backend="openai",
        turns=tuple(
            TurnEvidence(
                turn=t.turn,
                slot_id="qa-slot",
                session_id=t.session_id,
                root_turn_id=t.root_id,
                started_s=0.0,
                terminal_capture_complete=True,
                terminals=(
                    TerminalEvidence(
                        evidence_id=t.root_id + ":terminal",
                        session_id=t.session_id,
                        root_turn_id=t.root_id,
                        authority="record",
                        outcome="completed",
                        observed_s=1.0,
                    ),
                ),
            )
            for t in fixture.turns
        ),
    )
    assertions = scenario.scenario.get("assert", [])
    assert isinstance(assertions, list)
    # Real SDK terminal evidence alone lacks actual adapter/DB/tool effects.
    # Never turn a consumed native/auth tape into a full scenario PASS.
    assert evaluate(recording, [OBJECT.validate_python(a) for a in assertions]).status != "PASS"


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver_enabled", (True, False))
async def test_new17_two_server_intent_refuses_before_resolution_or_native_io(
    resolver_enabled: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scenario, fixture = PACK.select("QA-NEW17-MCP-USABLE-NEXT-MESSAGE", "openai")
    assert fixture.mcp_binding is not None
    assert {c.name for c in fixture.mcp_binding.connections} == {"daimon-mcp", "deepwiki"}
    assert any(g.code == "MCP_SERVER_LIMIT" and g.status == "BLOCKED" for g in fixture.gaps)
    wire = WireReplay(
        scenario_tape(scenario, fixture, keys={t.turn: f"turn-{t.turn}" for t in fixture.turns})
    )
    preparation = Preparation(fixture, wire)
    caplog.set_level(logging.DEBUG)
    async with AsyncOpenAI(
        api_key="offline-placeholder",
        base_url="https://offline.invalid/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(preparation.handle)),
    ) as sdk:
        driver = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            authorization=lambda scope, kind, identity: scope == SCOPE,
            mcp_secrets=preparation.resolve if resolver_enabled else None,
        )
        with pytest.raises(UnsupportedCapability) as caught:
            await driver.agents.create(SCOPE, spec(fixture), key="refuse-two-servers")
    assert caught.value.missing == ("single_bound_mcp_server",)
    assert preparation.agent_posts == preparation.session_posts == preparation.agent_gets == 0
    assert preparation.resolutions == [] and wire.requests == []
    assert preparation.secret not in caplog.text
    # Preserve the source scenario and both hypothetical tool calls. Neither
    # frame consumption nor tool execution is claimed for refused preparation.
    assert {c.name for t in fixture.turns for c in t.mcp_calls} == {
        "attach_mcp_server",
        "ask_question",
    }
    assert len(fixture.turns) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("no_resolver", "revoked", "destination", "foreign_scope"))
async def test_authentication_faults_refuse_before_session_or_tool_dispatch(fault: str) -> None:
    scenario, fixture = PACK.select("QA-D5-UNBOUND-AGENT-EDIT-REFUSED", "openai")
    wire = WireReplay(scenario_tape(scenario, fixture, keys={1: "turn-1"}))
    preparation = Preparation(fixture, wire, fault=fault)
    async with AsyncOpenAI(
        api_key="offline-placeholder",
        base_url="https://offline.invalid/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(preparation.handle)),
    ) as sdk:
        driver = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            authorization=lambda scope, kind, identity: scope == SCOPE,
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            mcp_secrets=None if fault == "no_resolver" else preparation.resolve,
        )
        agent = await driver.agents.create(SCOPE, spec(fixture), key="prepare-agent")
        scope = (
            SCOPE.model_copy(update={"tenant_id": "foreign"}) if fault == "foreign_scope" else SCOPE
        )
        with pytest.raises((ProviderError, ScopeViolation, UnsupportedCapability)):
            await driver.sessions.create(
                scope,
                SessionSpec(agent=agent.ref, agent_revision=Revision(local=0), config_revision=1),
                key="refuse",
            )
    assert preparation.session_posts == 0 and not wire.requests
    assert len(preparation.resolutions) == (1 if fault == "revoked" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fixture",
    [p for _, p in CASES if p.backend == "gemini"],
    ids=[s.scenario_id for s, p in CASES if p.backend == "gemini"],
)
async def test_gemini_authentication_gap_is_real_not_an_anonymous_substitution(
    fixture: BackendFixture,
) -> None:
    class NoIO:
        def __getattr__(self, name: str) -> object:
            raise AssertionError("unsupported authenticated intent must not reach transport")

    driver = GeminiManagedAgents(
        cast(GeminiTransport, NoIO()),
        storage=MemoryStorage(),
        state_store=MemoryStateStore(),
        account_scope_id="project",
    )
    with pytest.raises(UnsupportedCapability) as caught:
        await driver.agents.create(SCOPE, spec(fixture), key="refuse")
    assert caught.value.missing == ("mcp_credentials_or_policy",)
    assert any(g.code == "MCP_AUTH_UNBOUND" for g in fixture.gaps)
    for turn in fixture.turns:
        tools = turn.request["tools"]
        assert isinstance(tools, list) and tools
        assert all(
            isinstance(t, dict)
            and t.get("headers") == {"Authorization": "Bearer offline-mcp-fixture"}
            for t in tools
        )


def test_frozen_recipe_sources_schema_pins_and_every_call_are_retained() -> None:
    assert len(CASES) == 20 and len(PACK.scenarios) == 53
    schema = OBJECT.validate_json((DATA / "catalog/fixtures/mcp-tools.json").read_bytes())
    for _scenario, fixture in CASES:
        assert fixture.mcp_binding is not None
        fixture.mcp_binding.auth_source.verify(ROOT)
        fixture.mcp_binding.host_source.verify(ROOT)
        assert fixture.mcp_binding.auth_commit == "9cf23f6c1eaf1d269ed9cab3f281ddd0cc9a6b82"
        assert not any(g.code == "NATIVE_TOOL_SCHEMA_UNBOUND" for g in fixture.gaps)
        assert any(g.code == "RUNNER_HOOK" and g.location == "mcp" for g in fixture.gaps)
        for turn in fixture.turns:
            for call in turn.mcp_calls:
                CallToolRequest.model_validate(
                    {
                        "method": "tools/call",
                        "params": {"name": call.name, "arguments": call.arguments},
                    }
                )
                CallToolResult.model_validate(call.result)
                if call.schema_origin == "daimon-registry":
                    document = OBJECT.validate_python(schema[call.name])
                    SourcePin.model_validate(document["source"]).verify(ROOT)
                    validator = cast(
                        SchemaValidator,
                        Draft202012Validator(OBJECT.validate_python(document["inputSchema"])),
                    )
                    validator.validate(call.arguments)
                else:
                    assert any(g.code == "EXTERNAL_TOOL_SCHEMA_UNBOUND" for g in fixture.gaps)


@pytest.mark.parametrize("backend", ("openai", "gemini"))
@pytest.mark.parametrize("fault", ("policy", "wrong_call_id", "wrong_result", "no_binding"))
def test_mutated_wire_or_connection_policy_is_rejected(backend: Backend, fault: str) -> None:
    _, fixture = PACK.select("QA-D5-UNBOUND-AGENT-EDIT-REFUSED", backend)
    document = fixture.model_dump(mode="json")
    if fault == "no_binding":
        document["mcp_binding"] = None
    elif fault == "policy":
        document["mcp_binding"]["connections"][0]["tool_policy"]["allowed_tools"] = ["unrelated"]
    elif backend == "openai":
        item = next(
            f["item"]
            for f in document["turns"][0]["frames"]
            if f.get("item", {}).get("type") == "mcp_call"
        )
        item["id" if fault == "wrong_call_id" else "output"] = (
            "foreign" if fault == "wrong_call_id" else {}
        )
    else:
        step = document["turns"][0]["snapshot"]["steps"][0 if fault == "wrong_call_id" else 1]
        step["id" if fault == "wrong_call_id" else "result"] = (
            "foreign" if fault == "wrong_call_id" else {}
        )
    with pytest.raises(ValidationError):
        BackendFixture.model_validate_json(json.dumps(document))


def test_gemini_result_before_call_is_rejected() -> None:
    _, fixture = PACK.select("QA-D5-UNBOUND-AGENT-EDIT-REFUSED", "gemini")
    document = fixture.model_dump(mode="json")
    steps = document["turns"][0]["snapshot"]["steps"]
    steps[0], steps[1] = steps[1], steps[0]
    with pytest.raises(ValidationError, match="Gemini native MCP wire"):
        BackendFixture.model_validate_json(json.dumps(document))


@pytest.mark.parametrize("fault", ("no_ref", "empty_policy", "duplicate", "open_policy"))
def test_mcp_intent_cannot_silently_downgrade_auth_or_policy(fault: str) -> None:
    _, fixture = PACK.select("QA-D5-UNBOUND-AGENT-EDIT-REFUSED", "openai")
    document = fixture.model_dump(mode="json")
    connection = document["mcp_binding"]["connections"][0]
    if fault == "no_ref":
        connection["credential_ref"] = None
    elif fault == "empty_policy":
        connection["tool_policy"] = {}
    elif fault == "duplicate":
        document["mcp_binding"]["connections"].append(connection)
    else:
        connection["tool_policy"]["unknown"] = True
    with pytest.raises(ValidationError):
        BackendFixture.model_validate_json(json.dumps(document))


@pytest.mark.parametrize("fault", ("remove_gap", "wrong_location", "remove_server"))
def test_two_server_refusal_cannot_be_removed_or_misbound(fault: str) -> None:
    _, fixture = PACK.select("QA-NEW17-MCP-USABLE-NEXT-MESSAGE", "openai")
    document = fixture.model_dump(mode="json")
    if fault == "remove_gap":
        document["gaps"] = [g for g in document["gaps"] if g["code"] != "MCP_SERVER_LIMIT"]
    elif fault == "wrong_location":
        next(g for g in document["gaps"] if g["code"] == "MCP_SERVER_LIMIT")["location"] = "foreign"
    else:
        document["mcp_binding"]["connections"].pop()
    with pytest.raises(ValidationError, match="MCP server limit refusal"):
        BackendFixture.model_validate_json(json.dumps(document))
