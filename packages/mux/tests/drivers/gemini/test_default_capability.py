"""Real default inputs and driver limits: honest F1 deferral before I/O."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from mux.conformance.default_capability import (
    BASH_RESULT,
    DEFAULT_SKILLS,
    DEFAULT_TOOLS,
    FINAL_MESSAGE,
    PROMPT,
    DefaultCapabilityReplayEvents,
    DefaultManifest,
    NamedSkill,
    check_turn,
    request_metadata,
    skill_text,
)
from mux.conformance.recording import Recorder, Replay, Tape
from mux.conformance.runner import (
    ConformanceFailure,
    PendingKind,
    replay_default_capability,
    run_default_capability,
)
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart, ToolUsePayload
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ModelRef, ThreadRef
from mux.contracts.resources import (
    AgentSpec,
    MCPConnection,
    SessionSpec,
    SkillUpload,
    SkillUploadFile,
    ToolSpec,
)
from mux.drivers.gemini.bundles import MAX_BUNDLE_BYTES
from mux.drivers.gemini.core import compile_agent
from mux.drivers.gemini.default_capability import (
    BUILTIN_MAPPING,
    PENDING,
    PendingTransport,
    adapter,
)
from mux.drivers.gemini.transport import Object
from mux.errors import UnsupportedCapability
from pydantic import BaseModel, JsonValue

ROOT = Path(__file__).resolve().parents[5]


class SkillName(BaseModel):
    skill_id: str


class ToolName(BaseModel):
    name: str


class Toolset(BaseModel):
    configs: tuple[ToolName, ...]


class AuthoredDefault(BaseModel):
    name: str
    system: str
    skills: tuple[SkillName, ...]
    tools: tuple[Toolset, ...]


@pytest.fixture
def manifest() -> DefaultManifest:
    authored = AuthoredDefault.model_validate(
        yaml.safe_load((ROOT / "defaults/agents/daimon.yaml").read_text())
    )
    skills: list[NamedSkill] = []
    for ref in authored.skills:
        directory = ROOT / "defaults/skills" / ref.skill_id
        skills.append(
            NamedSkill(
                ref.skill_id,
                SkillUpload(
                    files=tuple(
                        SkillUploadFile(
                            path=str(path.relative_to(directory)), content=path.read_bytes()
                        )
                        for path in sorted(directory.rglob("*"))
                        if path.is_file()
                    )
                ),
            )
        )
    return DefaultManifest(
        name=authored.name,
        system=authored.system,
        skills=tuple(skills),
        builtin_tools=tuple(tool.name for toolset in authored.tools for tool in toolset.configs),
        mcp=MCPConnection(
            name="daimon-mcp",
            url="https://daimon.invalid/mcp",
            credential_ref="f1-account-mcp",
        ),
    )


@pytest.mark.asyncio
async def test_full_authored_default_is_pending_before_provisioning(
    manifest: DefaultManifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert tuple(skill.name for skill in manifest.skills) == DEFAULT_SKILLS
    assert manifest.builtin_tools == DEFAULT_TOOLS
    assert skill_text(manifest) == (ROOT / "defaults/skills/file-handling/SKILL.md").read_text()
    instance = adapter()

    async def forbidden_provision(*args: object, **kwargs: object) -> None:
        raise AssertionError("pending F1 reached skill provisioning")

    monkeypatch.setattr(instance.driver.skills, "create", forbidden_provision)
    result = await run_default_capability(manifest, instance)
    assert result.fixture_id == "F1" and result.status == "pending"
    assert result.pending_reason == PENDING
    assert result.pending_reason is not None
    assert result.pending_reason.kind == PendingKind.ADAPTER_DEPENDENCY
    assert isinstance(instance.transport, PendingTransport)
    instance.transport.assert_consumed()
    assert instance.transport.skill_uploads == ()
    with pytest.raises(ConformanceFailure, match="no upstream default deployment"):
        _ = instance.transport.deployed_agent


@pytest.mark.asyncio
async def test_removing_pending_does_not_fake_a_default_pass(manifest: DefaultManifest) -> None:
    instance = adapter()
    # A complete mapping alone cannot unblock the full binary/authenticated
    # default. The other genuine preflight/provisioning gaps remain explicit.
    result = await run_default_capability(
        manifest,
        replace(instance, pending=None),
    )
    assert result.status == "fail"
    assert result.evidence == ("probe raised ValueError",)
    instance.transport.assert_consumed()


@pytest.mark.asyncio
async def test_full_binary_default_skill_is_rejected_without_provider_io(
    manifest: DefaultManifest,
) -> None:
    instance = adapter()
    bundle = next(s.upload for s in manifest.skills if s.name == "pymc-artifact-style")
    assert sum(len(file.content) for file in bundle.files) > MAX_BUNDLE_BYTES
    assert any(file.path.endswith(".ttf") for file in bundle.files)
    with pytest.raises(ValueError, match="inline size limit"):
        await instance.driver.skills.create(instance.scope, bundle, key="binary-default")
    instance.transport.assert_consumed()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [name for name in DEFAULT_SKILLS if name != "pymc-artifact-style"])
async def test_complete_text_default_skill_is_feasible_offline(
    manifest: DefaultManifest, name: str
) -> None:
    instance = adapter()
    bundle = next(s.upload for s in manifest.skills if s.name == name)
    skill = await instance.driver.skills.create(instance.scope, bundle, key="supported-skill")
    assert skill.latest_version is not None and skill.latest_version.version != "latest"
    assert await instance.driver.skills.retrieve(instance.scope, skill.id) == skill
    instance.transport.assert_consumed()


def test_authenticated_mcp_gap_is_driver_specific_not_native_mcp_unavailable() -> None:
    public = MCPConnection(name="daimon-mcp", url="https://daimon.invalid/mcp")
    spec = AgentSpec(name="daimon", model=ModelRef(provider="gemini", id="gemini-3.5-flash-lite"))
    request = compile_agent(spec.model_copy(update={"mcp_servers": (public,)}))
    assert request["tools"] == [
        {"type": "mcp_server", "name": "daimon-mcp", "url": "https://daimon.invalid/mcp"}
    ]
    private = public.model_copy(update={"credential_ref": "f1-account-mcp"})
    with pytest.raises(UnsupportedCapability):
        compile_agent(spec.model_copy(update={"mcp_servers": (private,)}))


def test_each_adapter_has_fresh_resources_and_transport() -> None:
    first, second = adapter(), adapter()
    assert first.driver is not second.driver and first.transport is not second.transport
    assert first.model == second.model
    assert first.model.id == "gemini-3.5-flash-lite"
    assert first.pending == second.pending == PENDING


def test_pending_transport_rejects_unconsumed_script_evidence() -> None:
    transport = PendingTransport()
    transport.responses.append({"id": "not-observed"})
    with pytest.raises(ConformanceFailure, match="unconsumed script evidence"):
        transport.assert_consumed()


@pytest.mark.asyncio
async def test_incomplete_f1_tape_cannot_be_certified_by_the_pending_adapter(
    manifest: DefaultManifest, tmp_path: Path
) -> None:
    tape = Tape(
        fixture_id="F1",
        provider="gemini",
        model="gemini-3.5-flash-lite",
        complete=False,
        batches=(),
    )
    path = tmp_path / "incomplete.json"
    path.write_text(tape.model_dump_json())
    result = await replay_default_capability(path, manifest, adapter)
    assert result.status == "fail"
    assert result.evidence == ("replay refused: RecordingError",)


def test_all_logical_builtins_route_to_documented_code_execution() -> None:
    instance = adapter()
    assert tuple(instance.builtin_mapping) == DEFAULT_TOOLS
    assert instance.builtin_mapping == BUILTIN_MAPPING
    assert all(
        tool == ToolSpec(name="code_execution", kind="builtin")
        for tool in instance.builtin_mapping.values()
    )
    spec = AgentSpec(
        name="builtin-probe", model=instance.model, tools=(instance.builtin_mapping["bash"],)
    )
    assert compile_agent(spec)["tools"] == [{"type": "code_execution"}]
    assert instance.pending == PENDING


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [None, *DEFAULT_TOOLS])
async def test_native_builtin_turn_replays_all_six_routes_and_rejects_missing_calls(
    manifest: DefaultManifest, tmp_path: Path, missing: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stable host-local IDs avoid recording a random UUID as an opaque blob.
    # These are logical resource IDs, not invented native tool call identities.
    logical_ids = iter(("agent-f1", "sess-f1", "root-f1"))
    monkeypatch.setattr("mux.drivers.gemini.core.uuid4", lambda: next(logical_ids))
    # Component-only evidence: no full skill deployment or authenticated MCP
    # provisioning is invented. The full adapter remains ADAPTER_DEPENDENCY.
    instance = adapter()
    transport = instance.transport
    assert isinstance(transport, PendingTransport)
    public = manifest.mcp.model_copy(update={"credential_ref": None})
    agent = await instance.driver.agents.create(
        instance.scope,
        AgentSpec(
            name="builtin-probe",
            model=instance.model,
            tools=(instance.builtin_mapping["bash"],),
            mcp_servers=(public,),
        ),
        key="builtin-probe",
    )
    thread = ThreadRef(
        channel=ChannelRef(tenant_id=instance.scope.tenant_id, platform="discord", channel_id="f1"),
        thread_id="builtin-component",
    )
    session = await instance.driver.sessions.create(
        instance.scope,
        SessionSpec(
            agent=agent.ref,
            agent_revision=agent.revision,
            config_revision=0,
            extensions={
                "gemini.session": ExtensionConfig(
                    namespace="gemini.session",
                    version=1,
                    value={
                        "thread": thread.model_dump(mode="json"),
                        "binding_id": "builtin-component",
                    },
                )
            },
        ),
        key="builtin-session",
    )
    steps: list[JsonValue] = []
    instructions = skill_text(manifest)
    operations = (
        ("skill-read", "cat .agents/skills/file-handling/SKILL.md", instructions),
        ("write", "printf f1-initial > f1.txt", ""),
        ("edit", "sed -i s/f1-initial/f1-edited/ f1.txt", ""),
        ("read", "cat f1.txt", "f1-edited"),
        ("grep", "grep -n f1-edited f1.txt", "1:f1-edited"),
        ("glob", "printf '%s' f1*.txt", "f1.txt"),
        ("bash", "printf f1-bash-ok", BASH_RESULT),
    )
    for name, command, output in operations:
        if name == missing:
            continue
        steps.extend(
            (
                {
                    "type": "code_execution_call",
                    "id": name,
                    "arguments": {"language": "bash", "code": command},
                },
                {"type": "code_execution_result", "call_id": name, "result": output},
            )
        )
    for name in ("describe_agent", "list_my_sessions"):
        steps.extend(
            (
                {
                    "type": "mcp_server_tool_call",
                    "id": name,
                    "name": name,
                    "server_name": "daimon-mcp",
                    "arguments": {},
                },
                {"type": "mcp_server_tool_result", "call_id": name, "result": "read-only-ok"},
            )
        )
    steps.append({"type": "model_output", "content": [{"type": "text", "text": FINAL_MESSAGE}]})
    stamp = datetime(2026, 10, 10, tzinfo=UTC).isoformat()
    started: Object = {
        "id": "builtin-turn",
        "status": "in_progress",
        "created": stamp,
        "updated": stamp,
        "environment_id": "builtin-env",
        "steps": [],
    }
    transport.responses.append(started)
    transport.reads["builtin-turn"] = [{**started, "status": "completed", "steps": steps}]
    transport.streams["builtin-turn"] = [{"event_type": "interaction.completed"}]
    inputs = (UserMessage(content=(TextPart(text=PROMPT),)),)
    await instance.driver.events.send(instance.scope, session.ref, inputs, key="builtin-turn")
    events = tuple(
        [event async for event in instance.driver.events.stream(instance.scope, session.ref)]
    )
    assert transport.requests[0]["tools"] == [
        {"type": "code_execution"},
        {"type": "mcp_server", "name": "daimon-mcp", "url": "https://daimon.invalid/mcp"},
    ]
    assert len(transport.requests) == 1 and transport.read_requests == ["builtin-turn"]
    recorder = Recorder()
    recorder.record(request_metadata(session.ref, stream=False), ())
    recorder.record(request_metadata(session.ref, stream=True), events)
    path = tmp_path / "builtin-component.json"
    recorder.save(
        path,
        fixture_id="F1",
        provider=instance.model.provider,
        model=instance.model.id,
        complete=True,
    )
    for _ in range(2):
        replay = Replay.load(path)
        port = DefaultCapabilityReplayEvents(replay)
        await port.send(instance.scope, session.ref, inputs, key="replay")
        observed = tuple([event async for event in port.stream(instance.scope, session.ref)])
        uses = tuple(event.typed_payload() for event in observed if event.type == "agent.tool_use")
        code_uses = tuple(
            call for call in uses if isinstance(call, ToolUsePayload) and call.executor == "agent"
        )
        assert {call.call_id for call in code_uses} == {
            name for name, _, _ in operations if name != missing
        }
        assert all(
            call.tool_name == "code_execution" and call.input == {"input_omitted": True}
            for call in code_uses
        )
        if missing is None:
            check_turn(observed, session.ref, instructions, instance.builtin_mapping)
        else:
            with pytest.raises(ConformanceFailure):
                check_turn(observed, session.ref, instructions, instance.builtin_mapping)
        replay.finish()
    result = await replay_default_capability(path, manifest, adapter)
    assert result.status == "pending" and result.pending_reason == PENDING
