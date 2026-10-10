"""Source-pinned native turn fixtures for the frozen QA catalog.

Fixtures supply provider inputs, never assertions or PASS evidence. Setup,
platform, tools, files and clocks still require runner-owned bindings.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

import yaml
from daimon.testing.provider_replay import (
    Backend,
    Object,
    ProviderTape,
    Record,
    SourcePin,
    WireFrame,
    WireReply,
)
from mcp.types import CallToolResult
from mux.contracts.resources import MCPConnection
from pydantic import Field, TypeAdapter, model_validator

TARGET_SHA256 = "fc14eab684b6aa257ca5c01ab113ef154c9847938d263f2739480299c43cc2f4"
_OBJECT = TypeAdapter[Object](Object)


class FixtureGap(Record):
    status: Literal["BLOCKED"] = "BLOCKED"
    code: Literal[
        "MANUAL_TRIGGER",
        "ADMIN_READBACK_ONLY",
        "NATIVE_TOOL_SCHEMA_UNBOUND",
        "ARTIFACT_PROTOCOL_UNBOUND",
        "HOST_GATE",
        "RUNNER_HOOK",
        "ANTHROPIC_SOURCE_UNBOUND",
        "MCP_AUTH_UNBOUND",
        "EXTERNAL_TOOL_SCHEMA_UNBOUND",
    ]
    location: str
    reason: str = Field(min_length=1)


class NativeMCPCall(Record):
    call_id: str = Field(min_length=1)
    server: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: Object
    # Actual JSON-RPC CallToolResult shape, not a claimed host mutation.
    result: Object
    schema_origin: Literal["daimon-registry", "authored-external"] = "daimon-registry"

    @model_validator(mode="after")
    def valid_result(self) -> NativeMCPCall:
        CallToolResult.model_validate(self.result)
        if set(self.result) != {"content", "isError"} or type(self.result["isError"]) is not bool:
            raise ValueError("fixture result requires explicit bounded MCP content/error fields")
        return self


class MCPFixtureBinding(Record):
    connections: tuple[MCPConnection, ...]
    schemas: SourcePin
    auth_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    auth_source: SourcePin
    host_source: SourcePin

    @model_validator(mode="after")
    def closed_connections(self) -> MCPFixtureBinding:
        if not self.connections or len({c.name for c in self.connections}) != len(self.connections):
            raise ValueError("MCP fixture requires distinct, nonempty connections")
        for connection in self.connections:
            policy = connection.tool_policy
            allowed = policy.get("allowed_tools")
            if (
                not connection.credential_ref
                or set(policy) != {"allowed_tools", "required"}
                or policy["required"] is not True
                or not isinstance(allowed, list)
                or not allowed
                or any(not isinstance(name, str) or not name for name in allowed)
                or len(set(str(name) for name in allowed)) != len(allowed)
            ):
                raise ValueError(
                    "MCP fixture requires explicit credential refs and closed tool policies"
                )
        return self


class NativeTurn(Record):
    turn: int = Field(ge=1)
    location: str
    channel: str
    thread: str
    # Fully expanded fixture input; the original template remains in scenario source.
    request_text: str
    session_id: str
    root_id: str
    request: Object
    frames: tuple[Object, ...]
    snapshot: Object
    completion_gate: str | None = None
    mcp_calls: tuple[NativeMCPCall, ...] = ()


class BackendFixture(Record):
    backend: Backend
    profile: str
    model: str
    api_family: Literal["agents-sessions", "interactions"]
    sdk_distribution: str
    sdk_version: str
    codec_source: SourcePin
    codec_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    provenance: Literal["authored-sdk-wire"] = "authored-sdk-wire"
    turns: tuple[NativeTurn, ...]
    gaps: tuple[FixtureGap, ...]
    mcp_binding: MCPFixtureBinding | None = None

    @model_validator(mode="after")
    def turn_integrity(self) -> BackendFixture:
        # Reuse the tape's provider/model/API-family contract validation.
        ProviderTape(
            scenario_id="contract",
            backend=self.backend,
            profile=self.profile,
            model=self.model,
            api_family=self.api_family,
            sdk_distribution=self.sdk_distribution,
            sdk_version=self.sdk_version,
            source=self.codec_source,
            replies=(),
        )
        if len({t.turn for t in self.turns}) != len(self.turns):
            raise ValueError("duplicate catalog turn numbers")
        if len({t.root_id for t in self.turns}) != len(self.turns):
            raise ValueError("duplicate native root identities")
        for t in self.turns:
            if len({c.call_id for c in t.mcp_calls}) != len(t.mcp_calls):
                raise ValueError("duplicate native MCP call identities")
            if t.mcp_calls and self.mcp_binding is None:
                raise ValueError("native MCP calls lack explicit connection intent")
            for call in t.mcp_calls:
                connections = self.mcp_binding.connections if self.mcp_binding else ()
                server = next((c for c in connections if c.name == call.server), None)
                allowed = server.tool_policy.get("allowed_tools") if server else None
                if not isinstance(allowed, list) or call.name not in allowed:
                    raise ValueError("native MCP call is outside fixture connection policy")
                if (
                    call.result.get("isError") is not False
                    and call.result.get("isError") is not True
                ):
                    raise ValueError("native MCP result lacks explicit error disposition")
                if self.backend == "openai":
                    items = [
                        f.get("item")
                        for f in t.frames
                        if f.get("type") == "agent.session.turn.item.done"
                    ]
                    expected: Object = {
                        "id": call.call_id,
                        "type": "mcp_call",
                        "turn_id": t.root_id,
                        "name": call.name,
                        "server_label": call.server,
                        "arguments": call.arguments,
                        "output": call.result,
                        "error": None,
                        "status": "completed",
                    }
                    if items.count(expected) != 1:
                        raise ValueError("OpenAI native MCP wire differs from its script")
                else:
                    steps = t.snapshot.get("steps")
                    use: Object = {
                        "id": call.call_id,
                        "type": "mcp_server_tool_call",
                        "name": call.name,
                        "server_name": call.server,
                        "arguments": call.arguments,
                    }
                    reply: Object = {
                        "id": call.call_id + ":result",
                        "type": "mcp_server_tool_result",
                        "call_id": call.call_id,
                        "result": call.result,
                        "is_error": call.result["isError"],
                    }
                    if (
                        not isinstance(steps, list)
                        or steps.count(use) != 1
                        or steps.count(reply) != 1
                        or steps.index(use) >= steps.index(reply)
                    ):
                        raise ValueError("Gemini native MCP wire differs from its script")
            if self.backend == "openai":
                if t.request != {
                    "events": [
                        {
                            "type": "agent.session.input.message",
                            "input": [
                                {
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": t.request_text}],
                                }
                            ],
                        }
                    ]
                }:
                    raise ValueError("OpenAI wire input differs from the catalog invocation")
                if any(f.get("session_id") != t.session_id for f in t.frames):
                    raise ValueError("OpenAI frame belongs to a foreign session")
                if t.snapshot.get("id") != t.root_id:
                    raise ValueError("OpenAI snapshot belongs to a foreign root")
            else:
                if t.request.get("input") != [{"type": "text", "text": t.request_text}]:
                    raise ValueError("Gemini wire input differs from the catalog invocation")
                if t.snapshot.get("id") != t.root_id:
                    raise ValueError("Gemini snapshot belongs to a foreign interaction")
                config = t.request.get("agent_config")
                if not isinstance(config, dict) or config.get("model") != self.model:
                    raise ValueError("Gemini request uses a foreign model")
        return self


class AnthropicSource(Record):
    pin: SourcePin
    # Existing tapes are read by N9, not converted into provider-neutral Events.
    scenario_id: str
    source_kind: Literal["catalog-authored", "golden-recording"]


class ScenarioFixture(Record):
    scenario_id: str
    source: SourcePin
    # Public copy omits bibliographic source paths; execution fields are unchanged.
    projection: SourcePin
    scenario: Object
    values: dict[str, str]
    assets: tuple[SourcePin, ...]
    # Every source invocation, including context, admin, waits and teardown survives.
    invocations: tuple[Object, ...]
    anthropic: AnthropicSource | None = None
    anthropic_gaps: tuple[FixtureGap, ...] = ()
    providers: tuple[BackendFixture, ...]

    @model_validator(mode="after")
    def no_dropped_source(self) -> ScenarioFixture:
        if self.scenario.get("id") != self.scenario_id:
            raise ValueError("fixture differs from the source scenario identity")
        if len(self.providers) != 2 or {p.backend for p in self.providers} != {"openai", "gemini"}:
            raise ValueError("every scenario needs both explicit native provider bindings")
        if self.anthropic is None and not self.anthropic_gaps:
            raise ValueError("missing Anthropic source must retain a typed gap")
        triggers = {
            str(i["location"]): i for i in self.invocations if i.get("operation") == "host_turn"
        }
        for provider in self.providers:
            represented = {t.location for t in provider.turns}
            represented.update(g.location for g in provider.gaps if g.code != "RUNNER_HOOK")
            if set(triggers) - represented:
                raise ValueError("host trigger silently omitted from the native binding")
            for turn in provider.turns:
                invocation = triggers.get(turn.location)
                if invocation is None or invocation.get("turn") != turn.turn:
                    raise ValueError("native fixture has an invented or renumbered host trigger")
                text = str(invocation["text"])
                for name, value in self.values.items():
                    text = text.replace("{" + name + "}", value)
                if text != turn.request_text:
                    raise ValueError("native fixture input differs from source/fixture values")
        return self


class FixturePack(Record):
    version: Literal[1] = 1
    target: SourcePin
    scenarios: tuple[ScenarioFixture, ...]

    @model_validator(mode="after")
    def frozen_target(self) -> FixturePack:
        ids = tuple(s.scenario_id for s in self.scenarios)
        canonical = ("\n".join(ids) + "\n").encode()
        if (
            len(ids) != 53
            or len(set(ids)) != 53
            or self.target.sha256 != TARGET_SHA256
            or hashlib.sha256(canonical).hexdigest() != TARGET_SHA256
        ):
            raise ValueError("fixture pack differs from frozen TARGET-53")
        return self

    def verify_catalog(self, root: Path) -> None:
        self.target.verify(root)
        for scenario in self.scenarios:
            path = (root / scenario.source.path).resolve()
            if not path.is_relative_to(root.resolve()):
                raise ValueError("source pin escapes its root")
            if scenario.source.path != scenario.projection.path:
                raise ValueError("scenario projection must bind the original source path")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != scenario.source.sha256:
                scenario.projection.verify(root)
            loaded: object = yaml.safe_load(raw)
            document = _OBJECT.validate_python(loaded)
            document.pop("sources", None)
            if document != scenario.scenario:
                raise ValueError("embedded scenario differs from pinned catalog bytes")
            for asset in scenario.assets:
                asset.verify(root)
            for fixture in scenario.providers:
                if fixture.mcp_binding is not None:
                    fixture.mcp_binding.schemas.verify(root)

    def select(self, scenario_id: str, backend: Backend) -> tuple[ScenarioFixture, BackendFixture]:
        scenario = next((s for s in self.scenarios if s.scenario_id == scenario_id), None)
        if scenario is None:
            raise ValueError("scenario absent from frozen fixture pack")
        return scenario, next(p for p in scenario.providers if p.backend == backend)


class FixtureIndex(Record):
    version: Literal[1] = 1
    target: SourcePin
    scenarios: tuple[SourcePin, ...]


def load_provider_fixtures(path: Path, *, catalog_root: Path) -> FixturePack:
    index = FixtureIndex.model_validate_json(path.read_bytes())
    scenarios: list[ScenarioFixture] = []
    for pin in index.scenarios:
        pin.verify(path.parent)
        scenarios.append(ScenarioFixture.model_validate_json((path.parent / pin.path).read_bytes()))
    pack = FixturePack(target=index.target, scenarios=tuple(scenarios))
    pack.verify_catalog(catalog_root)
    return pack


def turn_tape(
    scenario: ScenarioFixture, fixture: BackendFixture, turn: NativeTurn, *, key: str
) -> ProviderTape:
    """Minimal strict native send/stream/read tape for an already owned binding.

    N9 supplies real driver storage and prepares cold resources separately. For
    extra recovery, resource or usage requests it must author exact WireReplies;
    no success router or unlimited snapshot fallback is provided here.
    """
    if fixture not in scenario.providers or turn not in fixture.turns:
        raise ValueError("turn does not belong to this scenario/provider")
    gate = f"turn.{turn.turn}.accepted"
    replies: list[WireReply] = []
    if fixture.backend == "openai":
        path = f"/v1/agents/sessions/{turn.session_id}"
        replies.append(
            WireReply(
                id="session",
                method="GET",
                path=path,
                headers=(("openai-beta", "agents=v1"),),
                response_json={
                    "id": turn.session_id,
                    "agent": {"id": "qa-agent", "model": fixture.model},
                    "created_at": 0,
                    "environment": {"type": "openai_hosted", "id": "qa-environment"},
                    "status": "idle",
                    "required_actions": [],
                    "metadata": {"mux_tenant": "qa-tenant"},
                },
            )
        )
        replies.extend(
            (
                WireReply(
                    id="send",
                    method="POST",
                    path=path + "/events",
                    request_json=turn.request,
                    headers=(("openai-beta", "agents=v1"), ("idempotency-key", key)),
                    status=202,
                    response_json={},
                    after=("session",),
                    releases=(gate,),
                ),
                WireReply(
                    id="stream",
                    method="GET",
                    path=path + "/events",
                    query=(("stream", "true"),),
                    headers=(("openai-beta", "agents=v1"), ("accept", "text/event-stream")),
                    frames=tuple(
                        WireFrame(
                            payload=f,
                            after_gate=turn.completion_gate
                            if turn.completion_gate and index >= 2
                            else gate,
                        )
                        for index, f in enumerate(turn.frames)
                    ),
                ),
            )
        )
    else:
        path = f"/v1beta/interactions/{turn.root_id}"
        replies.extend(
            (
                WireReply(
                    id="send",
                    method="POST",
                    path="/v1beta/interactions",
                    request_json=turn.request,
                    headers=(("api-revision", "2026-05-20"),),
                    response_json={**turn.snapshot, "status": "in_progress", "steps": []},
                    releases=(gate,),
                ),
                WireReply(
                    id="stream",
                    method="GET",
                    path=path,
                    query=(("stream", "true"),),
                    headers=(("api-revision", "2026-05-20"),),
                    after=("send",),
                    frames=tuple(
                        WireFrame(payload=f, after_gate=turn.completion_gate or gate)
                        for f in turn.frames
                    ),
                ),
                WireReply(
                    id="saved",
                    method="GET",
                    path=path,
                    headers=(("api-revision", "2026-05-20"),),
                    response_json=turn.snapshot,
                    after=("send",),
                ),
            )
        )
    return ProviderTape(
        scenario_id=scenario.scenario_id,
        backend=fixture.backend,
        profile=fixture.profile,
        model=fixture.model,
        api_family=fixture.api_family,
        sdk_distribution=fixture.sdk_distribution,
        sdk_version=fixture.sdk_version,
        source=scenario.projection,
        replies=tuple(replies),
    )


def scenario_tape(
    scenario: ScenarioFixture, fixture: BackendFixture, *, keys: dict[int, str]
) -> ProviderTape:
    """Compose exact port-level turns with Gemini's real history GET requests.

    Preparation/resource/accounting requests are deliberately not guessed. N9
    can append explicitly scripted replies for those owned host boundaries.
    Blocked invocations have no reply; attempting to dispatch one is a violation.
    """
    if set(keys) != {t.turn for t in fixture.turns}:
        raise ValueError("send keys must bind every authored turn exactly")
    replies: list[WireReply] = []
    history: dict[str, list[NativeTurn]] = {}
    for turn in fixture.turns:
        tape = turn_tape(scenario, fixture, turn, key=keys[turn.turn])
        prefix = f"turn.{turn.turn}."
        prior = history.setdefault(turn.session_id, [])
        if fixture.backend == "gemini" and prior:
            previous = prior[-1]
            replies.append(
                WireReply(
                    id=prefix + "previous",
                    method="GET",
                    path=f"/v1beta/interactions/{previous.root_id}",
                    headers=(("api-revision", "2026-05-20"),),
                    response_json=previous.snapshot,
                    after=(f"turn.{previous.turn}.saved",),
                )
            )
        for reply in tape.replies:
            after = tuple(prefix + name for name in reply.after)
            if fixture.backend == "gemini" and prior and reply.id == "send":
                after += (prefix + "previous",)
            # Gemini reconcile walks all interactions retained by this session.
            if fixture.backend == "gemini" and reply.id == "saved":
                for old in prior:
                    replies.append(
                        WireReply(
                            id=prefix + f"history.{old.turn}",
                            method="GET",
                            path=f"/v1beta/interactions/{old.root_id}",
                            headers=(("api-revision", "2026-05-20"),),
                            response_json=old.snapshot,
                            after=(prefix + "send",),
                        )
                    )
            replies.append(reply.model_copy(update={"id": prefix + reply.id, "after": after}))
        prior.append(turn)
    return ProviderTape(
        scenario_id=scenario.scenario_id,
        backend=fixture.backend,
        profile=fixture.profile,
        model=fixture.model,
        api_family=fixture.api_family,
        sdk_distribution=fixture.sdk_distribution,
        sdk_version=fixture.sdk_version,
        source=scenario.projection,
        replies=tuple(replies),
    )
