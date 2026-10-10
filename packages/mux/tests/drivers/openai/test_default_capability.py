"""Actual default bytes -> native SDK script -> normalized F1 -> fresh replay."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from mux.conformance.default_capability import (
    BASH_RESULT,
    FINAL_MESSAGE,
    DefaultManifest,
    NamedSkill,
    skill_text,
)
from mux.conformance.recording import Audit, Recorder
from mux.conformance.runner import replay_default_capability, run_default_capability
from mux.contracts.resources import MCPConnection, SkillUpload, SkillUploadFile
from mux.drivers.openai.default_capability import (
    BUILTIN_MAPPING,
    MODEL,
    OpenAIDefaultCapabilityFactory,
)
from mux.drivers.openai.normalize import EventNormalizer
from mux.drivers.openai.transport import Object, object_json

ROOT = Path(__file__).resolve().parents[5]
TAPE = ROOT / "tests/conformance/recordings/openai/F1-scripted.json"


@pytest.fixture
def manifest() -> DefaultManifest:
    raw = yaml.safe_load((ROOT / "defaults/agents/daimon.yaml").read_text())
    names = tuple(item["skill_id"] for item in raw["skills"])
    skills: list[NamedSkill] = []
    for name in names:
        directory = ROOT / "defaults/skills" / name
        paths = tuple(sorted(path for path in directory.rglob("*") if path.is_file()))
        skills.append(
            NamedSkill(
                name,
                SkillUpload(
                    files=tuple(
                        SkillUploadFile(
                            path=path.relative_to(directory).as_posix(), content=path.read_bytes()
                        )
                        for path in paths
                    )
                ),
            )
        )
    return DefaultManifest(
        name=raw["name"],
        system=raw["system"],
        skills=tuple(skills),
        builtin_tools=tuple(item["name"] for item in raw["tools"][0]["configs"]),
        mcp=MCPConnection(name="daimon-mcp", url="https://daimon-mcp.invalid/mcp"),
    )


def native_events(manifest: DefaultManifest) -> tuple[Object, ...]:
    # Scripted file paths are fixtures. They are not a provider installation-path
    # claim. Skill read output comes from the actual authored bundle bytes.
    def event(kind: str, identity: str, **body: object) -> Object:
        return object_json(
            {
                "type": "agent.session." + kind,
                "event_id": identity,
                "session_id": "session",
                "turn_id": "root",
                **body,
            }
        )

    turn: Object = {
        "id": "root",
        "session_id": "session",
        "subagent_id": None,
        "agent_id": "agent",
        "created_at": 0,
        "status": "in_progress",
        "usage": None,
    }
    events = [event("turn.in_progress", "running", turn=turn)]
    operations = (
        ("skill-read", "cat /fixture/file-handling/SKILL.md", skill_text(manifest)),
        (
            "write",
            "apply_patch '*** Begin Patch\n*** Add File: f1.txt\n+f1-initial\n*** End Patch'",
            "Success",
        ),
        (
            "edit",
            "apply_patch '*** Begin Patch\n*** Update File: f1.txt\n@@\n-f1-initial\n+f1-edited\n*** End Patch'",
            "Success",
        ),
        ("file-read", "cat f1.txt", "f1-edited"),
        ("grep", "grep -n f1-edited f1.txt", "1:f1-edited"),
        ("glob", "printf %s f1.*", "f1.txt"),
        ("bash", "printf f1-bash-ok", BASH_RESULT),
    )
    for identity, command, output in operations:
        item: Object = {
            "id": identity,
            "type": "command_execution",
            "turn_id": "root",
            "command": command,
            "cwd": "/fixture",
            "duration_ms": 1,
            "exit_code": 0,
            "output": output,
            "status": "completed",
        }
        events.append(event("turn.item.done", identity, item=item))
    for name in ("describe_agent", "list_my_sessions"):
        item = {
            "id": name,
            "type": "mcp_call",
            "turn_id": "root",
            "name": name,
            "server_label": "daimon-mcp",
            "arguments": {},
            "output": "f1-mcp-ok",
            "error": None,
            "status": "completed",
        }
        events.append(event("turn.item.done", name, item=item))
    message: Object = {
        "id": "final",
        "type": "message",
        "turn_id": "root",
        "role": "assistant",
        "status": "completed",
        "phase": "final_answer",
        "content": [{"type": "output_text", "text": FINAL_MESSAGE}],
    }
    events.append(event("turn.item.done", "final", item=message))
    events.append(event("turn.completed", "ended", turn={**turn, "status": "completed"}))
    return tuple(events)


@pytest.mark.asyncio
async def test_actual_default_sdk_provisioning_record_and_fresh_replay(
    manifest: DefaultManifest,
    tmp_path: Path,
) -> None:
    recorder = Recorder()
    path = tmp_path / "F1.json"
    async with OpenAIDefaultCapabilityFactory(manifest, native_events(manifest)) as factory:
        adapter = factory()
        result = await run_default_capability(manifest, adapter, recorder=recorder)
        assert result.status == "pass", result.evidence
        assert any("no CAS claim" in line for line in result.evidence)
        recorder.save(path, fixture_id="F1", provider="openai", model=MODEL.id, complete=True)
        replay = await replay_default_capability(path, manifest, factory)
        assert replay.status == "pass", replay.evidence
        assert factory.scripts[0] is not factory.scripts[1]
        assert len(factory.scripts[0].skill_uploads) == len(factory.scripts[1].skill_uploads) == 11
        assert all(tool.name == "bash" for tool in BUILTIN_MAPPING.values())
        for script in factory.scripts:
            script.assert_consumed()
            agent = script.deployed_agent
            assert agent.skills is not None and len(agent.skills) == 11
            assert all(pin.version == "1" for pin in agent.skills)
            posted = next(
                request for request in script.requests if request.url.path == "/v1/agents"
            )
            body = json.loads(posted.content)
            assert body["tools"] == [
                {
                    "type": "mcp",
                    "server_label": "daimon-mcp",
                    "transport": {"type": "http", "server_url": manifest.mcp.url},
                }
            ]
            assert "bash" not in [tool["type"] for tool in body["tools"]]
            assert body["metadata"]["mux_skill_pins"].startswith("chunks:")
            assert all(len(value) <= 512 for value in body["metadata"].values())
    tape = json.loads(path.read_text())
    Audit(()).audit(tape)
    assert len(tape["batches"]) == 2
    calls = [
        event
        for batch in tape["batches"]
        for event in batch["events"]
        if event["type"] == "agent.tool_use"
    ]
    assert len(calls) == 9
    assert all(call["payload"]["input"] == {"input_omitted": True} for call in calls)


@pytest.mark.asyncio
async def test_checked_in_scripted_tape_reprovisions_fresh_upstream(
    manifest: DefaultManifest,
) -> None:
    async with OpenAIDefaultCapabilityFactory(manifest) as factory:
        result = await replay_default_capability(TAPE, manifest, factory)
        assert result.status == "pass", result.evidence
        assert len(factory.scripts[0].skill_uploads) == 11
        assert not any(
            request.url.path.endswith("/events") for request in factory.scripts[0].requests
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "missing_mcp",
        "nonexistent_mcp",
        "mcp_server",
        "command_failed",
        "skill_bytes",
        "foreign_root",
        "extra_native",
    ],
)
async def test_native_sdk_mutants_fail_f1(manifest: DefaultManifest, fault: str) -> None:
    events = list(native_events(manifest))
    if fault == "missing_mcp":
        events = [event for event in events if event["event_id"] != "list_my_sessions"]
    elif fault == "extra_native":
        events.insert(
            -1, {"type": "agent.session.idle", "event_id": "extra", "session_id": "session"}
        )
    else:
        identity = "describe_agent" if fault in ("mcp_server", "nonexistent_mcp") else "skill-read"
        index = next(i for i, event in enumerate(events) if event["event_id"] == identity)
        raw = events[index]
        item = object_json(raw["item"])
        if fault == "mcp_server":
            item["server_label"] = "wrong-server"
        elif fault == "nonexistent_mcp":
            item["name"] = "client_context"
        elif fault == "command_failed":
            item["exit_code"] = 1
        elif fault == "skill_bytes":
            item["output"] = "wrong skill text"
        else:
            item["turn_id"] = "other-root"
        events[index] = {**raw, "item": item}
    async with OpenAIDefaultCapabilityFactory(manifest, tuple(events)) as factory:
        result = await run_default_capability(manifest, factory())
        assert result.status == "fail"


@pytest.mark.asyncio
async def test_missing_logical_mapping_is_typed_pending_before_any_sdk_io(
    manifest: DefaultManifest,
) -> None:
    async with OpenAIDefaultCapabilityFactory(manifest) as factory:
        adapter = replace(factory(), builtin_mapping={})
        result = await run_default_capability(manifest, adapter)
        assert result.status == "pending" and result.pending_reason is not None
        assert factory.scripts[0].requests == []


def test_native_command_completion_failure_and_preview() -> None:
    normalizer = EventNormalizer("session")
    base: Object = {
        "id": "command",
        "type": "command_execution",
        "turn_id": "root",
        "command": "false",
        "cwd": "/fixture",
        "duration_ms": 1,
        "exit_code": None,
        "output": None,
        "status": "in_progress",
    }
    preview = normalizer.saved_item_batch(base)
    assert len(preview) == 1 and preview[0].authority == "preview"
    # saved items share one synthetic event ID, so live added/done coverage is in
    # test_mcp_codec; a new normalizer represents a refreshed snapshot here.
    final = EventNormalizer("session").saved_item_batch(
        {**base, "status": "failed", "exit_code": 1, "output": "failed"}
    )
    assert len(final) == 2 and final[-1].payload["is_error"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    ["mcp_result_missing", "wrong_executor", "wrong_server", "model", "unconsumed", "arguments"],
)
async def test_normalized_replay_mutants_refuse_without_event_network(
    manifest: DefaultManifest,
    tmp_path: Path,
    fault: str,
) -> None:
    tape = json.loads(TAPE.read_text())
    events = tape["batches"][1]["events"]
    if fault == "mcp_result_missing":
        tape["batches"][1]["events"] = [
            event
            for event in events
            if not (
                event["type"] == "agent.tool_result"
                and event["payload"]["call_id"] == "list_my_sessions"
            )
        ]
    elif fault in ("wrong_executor", "wrong_server", "arguments"):
        call = next(
            event
            for event in events
            if event["type"] == "agent.tool_use"
            and event["payload"]["tool_name"] == "describe_agent"
        )
        if fault == "wrong_executor":
            call["payload"]["executor"] = "host"
        elif fault == "wrong_server":
            call["payload"]["mcp_server"] = "other-server"
        else:
            call["payload"]["input"] = {"unrecorded_argument": "value"}
    elif fault == "model":
        tape["model"] = "other-offline-model"
    else:
        tape["batches"].append(tape["batches"][0])
    path = tmp_path / "mutated.json"
    path.write_text(json.dumps(tape))
    async with OpenAIDefaultCapabilityFactory(manifest) as factory:
        result = await replay_default_capability(path, manifest, factory)
        assert result.status == "fail"
        assert all(
            not request.url.path.endswith("/events")
            for script in factory.scripts
            for request in script.requests
        )


@pytest.mark.asyncio
async def test_upstream_upload_byte_loss_is_detected_independently(
    manifest: DefaultManifest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mux.drivers.openai import skills

    original = skills.bundle_zip

    def broken(bundle: SkillUpload) -> bytes:
        files = tuple(
            file.model_copy(update={"content": file.content + b"changed"})
            if file.path == "SKILL.md"
            else file
            for file in bundle.files
        )
        return original(bundle.model_copy(update={"files": files}))

    monkeypatch.setattr(skills, "bundle_zip", broken)
    async with OpenAIDefaultCapabilityFactory(manifest, native_events(manifest)) as factory:
        result = await run_default_capability(manifest, factory())
        assert result.status == "fail" and "upstream skill bytes changed" in result.evidence[0]
        assert not any(request.url.path == "/v1/agents" for request in factory.scripts[0].requests)


@pytest.mark.asyncio
async def test_upstream_remote_mcp_url_loss_fails_actual_deployment_evidence(
    manifest: DefaultManifest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mux.contracts.resources import AgentSpec
    from mux.drivers.openai import agents
    from mux.drivers.openai._common import Context, objects

    original = agents.agent_body

    def broken(spec: AgentSpec, context: Context) -> Object:
        body = original(spec, context)
        tools = list(objects(body["tools"]))
        tools[0] = {
            **tools[0],
            "transport": {"type": "http", "server_url": "https://wrong.invalid/mcp"},
        }
        return object_json({**body, "tools": tools})

    monkeypatch.setattr(agents, "agent_body", broken)
    async with OpenAIDefaultCapabilityFactory(manifest, native_events(manifest)) as factory:
        result = await run_default_capability(manifest, factory())
        assert result.status == "fail" and "MCP attachment changed" in result.evidence[0]
        assert not any(
            request.url.path == "/v1/agents/sessions" for request in factory.scripts[0].requests
        )
