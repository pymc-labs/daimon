"""F1 scripts the real SDK/driver, then rechecks a normalized-only replay."""

from __future__ import annotations

import ast
import asyncio
import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from email.parser import BytesParser
from email.policy import default
from functools import partial
from pathlib import Path

import httpx
import pytest
import yaml
from daimon.testing.ma_models import ma_agent, ma_session
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.conformance.default_capability import (
    BASH_RESULT,
    DEFAULT_TOOLS,
    FINAL_MESSAGE,
    MCP_TOOLS,
    PROMPT,
    DefaultCapabilityAdapter,
    DefaultCapabilityReplayEvents,
    DefaultManifest,
    NamedSkill,
    check_turn,
    skill_text,
)
from mux.conformance.recording import Recorder, Replay
from mux.conformance.runner import (
    PendingKind,
    PendingReason,
    replay_default_capability,
    run_default_capability,
)
from mux.contracts.ids import ModelRef, ResourceRef, Scope
from mux.contracts.resources import (
    Agent,
    AgentSpec,
    MCPConnection,
    SkillUpload,
    SkillUploadFile,
    ToolSpec,
)
from mux.drivers.anthropic import AnthropicManagedAgents
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization

ROOT = Path(__file__).resolve().parents[3]
TAPE = ROOT / "packages/mux/mux/conformance/tapes/anthropic-default-capability.json"
NOW = datetime(2026, 10, 10, tzinfo=UTC).isoformat()
MODEL = ModelRef(provider="anthropic", id="claude-haiku-4-5-20251001")
SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="qa", authorization_id="f1-offline"
)
ENVIRONMENT = ResourceRef(
    id="env_f1",
    kind="environment",
    provider="anthropic",
    account_scope_id="offline",
    tenant_id="tenant",
    account_id="account",
)


def manifest() -> DefaultManifest:
    authored = yaml.safe_load((ROOT / "defaults/agents/daimon.yaml").read_text())
    uploads = []
    for ref in authored["skills"]:
        assert ref["type"] == "custom"
        name = ref["skill_id"]
        directory = ROOT / "defaults/skills" / name
        files = tuple(
            SkillUploadFile(
                path=f"{name}/{path.relative_to(directory).as_posix()}",
                content=path.read_bytes(),
                media_type="application/octet-stream",
            )
            for path in sorted(directory.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts
        )
        uploads.append(NamedSkill(name, SkillUpload(files=files)))
    assert len(authored["tools"]) == 1
    assert authored["tools"][0]["type"] == "agent_toolset_20260401"
    return DefaultManifest(
        name=authored["name"],
        system=authored["system"],
        skills=tuple(uploads),
        builtin_tools=tuple(config["name"] for config in authored["tools"][0]["configs"]),
        mcp=MCPConnection(name="daimon-mcp", url="https://daimon.invalid/mcp"),
    )


def spec(raw) -> AgentSpec:
    tools = []
    for tool in raw["tools"]:
        default_config = tool.get("default_config") or {}

        def policy(config, default_config=default_config):
            enabled = config.get("enabled", default_config.get("enabled", True))
            permission = (
                config.get("permission_policy")
                or default_config.get("permission_policy")
                or {"type": "always_allow"}
            )
            return enabled, "ask" if permission["type"] == "always_ask" else "auto"

        if tool["type"] == "agent_toolset_20260401":
            for config in tool["configs"]:
                enabled, permission = policy(config)
                if enabled:
                    tools.append(
                        {"kind": "builtin", "name": config["name"], "permission": permission}
                    )
        elif tool["type"] == "mcp_toolset":
            policies = [
                policy(
                    next((c for c in tool.get("configs", ()) if c["name"] == name), default_config)
                )
                for name in ("describe_agent", "list_my_sessions")
            ]
            if all(enabled for enabled, _ in policies):
                tools.append(
                    {
                        "kind": "mcp_toolset",
                        "name": tool["mcp_server_name"],
                        "permission": "ask"
                        if any(permission == "ask" for _, permission in policies)
                        else "auto",
                    }
                )
        else:
            raise ValueError("unexpected native tool")
    model = raw["model"]
    return AgentSpec.model_validate(
        {
            "name": raw["name"],
            "system": raw["system"],
            "model": {
                "provider": "anthropic",
                "id": model if isinstance(model, str) else model["id"],
            },
            "tools": tools,
            "mcp_servers": [
                {"name": server["name"], "url": server["url"]} for server in raw["mcp_servers"]
            ],
            "skills": [
                {"id": pin["skill_id"], "version": pin["version"], "source": pin["type"]}
                for pin in raw["skills"]
            ],
        }
    )


class Script(ScriptedTransport):
    def __init__(self, expected: DefaultManifest, *, replay: bool = False, fault: str = "") -> None:
        super().__init__()
        self.fault = fault
        pins = []
        for index, named in enumerate(expected.skills):
            id_ = f"skill_f1_{index}"
            response = {
                "id": id_,
                "type": "skill",
                "source": "custom",
                "display_title": named.name,
                "latest_version": "1",
                "created_at": NOW,
                "updated_at": NOW,
            }
            if fault == "mutable_pin" and index == 0:
                response["latest_version"] = "latest"
            self.queue(ScriptedReply("POST", "/v1/skills", httpx.Response(200, json=response)))
            self.queue(
                ScriptedReply("GET", f"/v1/skills/{id_}", httpx.Response(200, json=response))
            )
            pins.append({"type": "custom", "skill_id": id_, "version": "1"})
        tools = [
            {
                "type": "agent_toolset_20260401",
                "configs": [
                    {"name": name, "enabled": True, "permission_policy": {"type": "always_allow"}}
                ],
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            }
            for name in expected.builtin_tools
        ]
        tools.append(
            {
                "type": "mcp_toolset",
                "mcp_server_name": "daimon-mcp",
                "configs": [],
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            }
        )
        servers = [{"type": "url", "name": "daimon-mcp", "url": expected.mcp.url}]
        if fault == "missing_builtin":
            tools = tools[1:]
        elif fault == "wrong_server":
            servers[0]["name"] = "foreign-mcp"
        elif fault == "changed_pin":
            pins[0]["version"] = "2"
        elif fault == "missing_skill":
            pins.pop()
        elif fault == "disabled_builtin":
            tools[0]["configs"][0]["enabled"] = False
        elif fault == "unconfirmed_builtin":
            tools[0]["configs"][0]["permission_policy"] = {"type": "always_ask"}
        elif fault == "disabled_mcp":
            tools[-1]["default_config"]["enabled"] = False
        if fault == "reordered_bindings":
            tools.reverse()
            pins.reverse()
        agent = ma_agent(
            id="ag_f1",
            name=expected.name,
            system=expected.system,
            model=MODEL.id,
            tools=tools,
            mcp_servers=servers,
            skills=pins,
        )
        self.queue(
            ScriptedReply(
                "POST", "/v1/agents", httpx.Response(200, json=agent.model_dump(mode="json"))
            )
        )
        self.queue(
            ScriptedReply(
                "GET", "/v1/agents/ag_f1", httpx.Response(200, json=agent.model_dump(mode="json"))
            )
        )
        session = ma_session(id="sess_f1", agent=agent, environment_id=ENVIRONMENT.id)
        session_payload = session.model_dump(mode="json")
        if fault == "wrong_revision":
            session_payload["agent"]["version"] = 2
        self.queue(ScriptedReply("POST", "/v1/sessions", httpx.Response(200, json=session_payload)))
        if not replay:
            self.queue(
                ScriptedReply(
                    "POST",
                    "/v1/sessions/sess_f1/events",
                    httpx.Response(200, json={"data": None}),
                    request_json={
                        "events": [
                            {"type": "user.message", "content": [{"type": "text", "text": PROMPT}]}
                        ]
                    },
                    check_json=True,
                )
            )
            self.queue(
                ScriptedReply.stream("/v1/sessions/sess_f1/events/stream", native_turn(expected))
            )

    @property
    def skill_uploads(self) -> tuple[SkillUpload, ...]:
        uploads = []
        for request in self.requests:
            if request.method != "POST" or request.path != "/v1/skills":
                continue
            content_type = dict(request.protocol_headers)["content-type"]
            message = BytesParser(policy=default).parsebytes(
                f"Content-Type: {content_type}\r\n\r\n".encode() + request.body
            )
            files = tuple(
                SkillUploadFile(
                    path=part.get_filename(),
                    content=part.get_payload(decode=True),
                    media_type=part.get_content_type(),
                )
                for part in message.iter_parts()
                if part.get_filename() is not None
            )
            uploads.append(SkillUpload(files=files))
        if self.fault == "changed_upload":
            uploads[0] = SkillUpload(files=())
        return tuple(uploads)

    @property
    def deployed_agent(self) -> AgentSpec:
        return spec(
            next(
                request.json()
                for request in self.requests
                if request.method == "POST" and request.path == "/v1/agents"
            )
        )

    def agent_spec(self, agent: Agent) -> AgentSpec:
        return spec(agent.native)

    def assert_consumed(self) -> None:
        if self.violations or self.replies:
            raise RuntimeError("F1 script not fully consumed")


def native_turn(expected: DefaultManifest):
    def event(type_, id_, **fields):
        return {"type": type_, "id": id_, "processed_at": NOW, **fields}

    events = [
        event("user.message", "user_f1", content=[{"type": "text", "text": PROMPT}]),
        event("session.status_running", "root_f1"),
    ]
    operations = (
        ("read", {"file_path": "/skills/file-handling/SKILL.md"}, skill_text(expected), None),
        ("describe_agent", {}, "offline-agent-description", "daimon-mcp"),
        ("list_my_sessions", {}, "offline-my-sessions", "daimon-mcp"),
        ("write", {"file_path": "f1.txt", "content": "f1-initial"}, "written", None),
        (
            "edit",
            {"file_path": "f1.txt", "old_string": "f1-initial", "new_string": "f1-edited"},
            "edited",
            None,
        ),
        ("read", {"file_path": "f1.txt"}, "f1-edited", None),
        ("grep", {"pattern": "f1-edited", "path": "f1.txt"}, "1:f1-edited", None),
        ("glob", {"pattern": "f1.txt"}, "f1.txt", None),
        ("bash", {"command": "printf f1-bash-ok"}, BASH_RESULT, None),
    )
    for index, (name, input_, result, server) in enumerate(operations):
        call = f"call_f1_{index}"
        use = event(
            "agent.mcp_tool_use" if server else "agent.tool_use", call, name=name, input=input_
        )
        if server:
            use["mcp_server_name"] = server
        events.append(use)
        events.append(
            event(
                "agent.mcp_tool_result" if server else "agent.tool_result",
                f"result_f1_{index}",
                **{("mcp_tool_use_id" if server else "tool_use_id"): call},
                content=[{"type": "text", "text": result}],
                is_error=False,
            )
        )
    events.extend(
        (
            event("agent.message", "message_f1", content=[{"type": "text", "text": FINAL_MESSAGE}]),
            event("session.status_idle", "end_f1", stop_reason={"type": "end_turn"}),
        )
    )
    return events


def adapter(client, script: Script, replay: Replay | None = None) -> DefaultCapabilityAdapter:
    driver = AnthropicManagedAgents(
        client,
        account_scope_id="offline",
        authorization=ResourceAuthorization(
            SCOPE,
            frozenset(
                {
                    ("agent", "ag_f1"),
                    ("session", "sess_f1"),
                    ("environment", "env_f1"),
                    *(("skill", f"skill_f1_{i}") for i in range(11)),
                }
            ),
        ),
        events=DefaultCapabilityReplayEvents(replay) if replay is not None else None,
    )
    return DefaultCapabilityAdapter(driver, SCOPE, MODEL, ENVIRONMENT, script)


async def record(path: Path) -> None:
    expected = manifest()
    script = Script(expected)
    recorder = Recorder()
    async with script.client() as client:
        result = await run_default_capability(expected, adapter(client, script), recorder=recorder)
    if result.status != "pass":
        raise RuntimeError(result.evidence)
    recorder.save(path, fixture_id="F1", provider=MODEL.provider, model=MODEL.id, complete=True)


def test_required_mcp_tools_are_registered_no_argument_agent_reads() -> None:
    catalog = (ROOT / "docs/mcp-tools.md").read_text()
    module = ast.parse(
        (ROOT / "packages/adapters/mcp/daimon/adapters/mcp/tools/agent_chat.py").read_text()
    )
    functions = {
        node.name: node for node in ast.walk(module) if isinstance(node, ast.AsyncFunctionDef)
    }
    assert {"describe_agent", "list_my_sessions"} == MCP_TOOLS
    for name in MCP_TOOLS:
        assert f"| `{name}` | agent tokens only |" in catalog
        function = functions[name]
        assert [arg.arg for arg in function.args.args] == ["ctx"]
        assert not function.args.kwonlyargs and function.args.vararg is function.args.kwarg is None
        assert any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "tool"
            for decorator in function.decorator_list
        )


async def test_scripted_default_agent_turn_and_repeatable_normalized_replay(tmp_path: Path) -> None:
    path = tmp_path / "f1.json"
    await record(path)
    data = json.loads(path.read_text())
    assert data["fixture_id"] == "F1" and len(data["batches"]) == 2
    uses = [event for event in data["batches"][1]["events"] if event["type"] == "agent.tool_use"]
    assert len(uses) == 9 and all(
        event["payload"]["input"] == {"input_omitted": True} for event in uses
    )
    assert all(event["native"].get("record") is None for event in data["batches"][1]["events"])
    for _ in range(2):
        expected = manifest()
        script = Script(expected, replay=True)
        async with script.client() as client:
            result = await replay_default_capability(
                path, expected, partial(adapter, client, script)
            )
        assert result.status == "pass", result.evidence
        assert len(script.requests) == 25  # 11 uploads/readbacks + agent create/get + session.
        script.assert_consumed()


async def test_committed_anthropic_f1_tape_replays_offline() -> None:
    expected = manifest()
    script = Script(expected, replay=True)
    async with script.client() as client:
        result = await replay_default_capability(TAPE, expected, partial(adapter, client, script))
    assert result.status == "pass", result.evidence


@pytest.mark.parametrize(
    "fault",
    [
        "mutable_pin",
        "changed_pin",
        "missing_skill",
        "missing_builtin",
        "wrong_server",
        "changed_upload",
        "disabled_builtin",
        "unconfirmed_builtin",
        "disabled_mcp",
        "wrong_revision",
    ],
)
async def test_broken_provisioning_cannot_pass(fault: str) -> None:
    expected = manifest()
    script = Script(expected, fault=fault)
    async with script.client() as client:
        result = await run_default_capability(expected, adapter(client, script))
    assert result.status == "fail", result.evidence


async def test_equivalent_resource_order_does_not_weaken_the_capability_check() -> None:
    expected = manifest()
    script = Script(expected, fault="reordered_bindings")
    async with script.client() as client:
        result = await run_default_capability(expected, adapter(client, script))
    assert result.status == "pass", result.evidence


async def test_declared_pending_does_not_provision_or_pass() -> None:
    expected = manifest()
    script = Script(expected)
    reason = PendingReason(PendingKind.CAPABILITY_UNAVAILABLE, "remote MCP attachment unavailable")
    async with script.client() as client:
        result = await run_default_capability(
            expected, replace(adapter(client, script), pending=reason)
        )
    assert result.status == "pending" and result.pending_reason == reason
    assert not script.requests


@pytest.mark.parametrize("capability", DEFAULT_TOOLS)
async def test_missing_logical_capability_is_typed_pending_before_io(capability: str) -> None:
    expected = manifest()
    script = Script(expected)
    async with script.client() as client:
        a = adapter(client, script)
        mapping = dict(a.builtin_mapping)
        del mapping[capability]
        result = await run_default_capability(expected, replace(a, builtin_mapping=mapping))
    assert result.status == "pending"
    assert result.pending_reason.kind == PendingKind.CAPABILITY_UNAVAILABLE
    assert not script.requests


@pytest.mark.parametrize("atomic", [True, False])
async def test_atomic_revision_pin_is_optional_and_its_gap_is_explicit(atomic: bool) -> None:
    expected = manifest()
    script = Script(expected)
    async with script.client() as client:
        result = await run_default_capability(
            expected, replace(adapter(client, script), atomic_revision_pin=atomic)
        )
    assert result.status == "pass", result.evidence
    created = next(r.json() for r in script.requests if r.path == "/v1/sessions")
    assert created["agent"] == (
        {"type": "agent", "id": "ag_f1", "version": 1} if atomic else "ag_f1"
    )
    gaps = [line for line in result.evidence if line.startswith("capability gap:")]
    assert bool(gaps) is not atomic
    if not atomic:
        assert "unpinned" in gaps[0] and "no CAS claim" in gaps[0]


def test_shared_turn_oracle_accepts_mapped_bash_and_patch_routes() -> None:
    """Routing proof only; this synthetic variant is not OpenAI certification."""
    replay = Replay.load(TAPE)
    bash = ToolSpec(name="bash", kind="builtin")
    patch = ToolSpec(name="apply_patch", kind="builtin")
    mapping = {name: patch if name in ("edit", "write") else bash for name in DEFAULT_TOOLS}
    events = []
    for event in replay.tape.batches[1].events:
        if event.type == "agent.tool_use" and event.payload["executor"] == "agent":
            event = event.model_copy(
                update={
                    "payload": {
                        **event.payload,
                        "tool_name": mapping[event.payload["tool_name"]].name,
                    }
                }
            )
        events.append(event)
    session = ENVIRONMENT.model_copy(update={"kind": "session", "id": "sess_f1"})
    check_turn(tuple(events), session, skill_text(manifest()), mapping)


CORRUPTIONS = (
    "skill_text",
    "missing_skill_read",
    "missing_mcp",
    "nonexistent_mcp",
    "duplicate_mcp_name",
    "foreign_server",
    "bash_result",
    "missing_write",
    "failed_edit",
    "missing_grep",
    "file_read",
    "glob",
    "early_read",
    "duplicate_call",
    "duplicate_result",
    "orphan_result",
    "null_running_start",
    "mismatched_running_start",
    "foreign_user_start",
    "foreign_root",
    "foreign_session",
    "preview",
    "incomplete_message",
    "errored_root",
    "second_root",
    "session_error",
    "trailing_event",
    "unconsumed_batch",
    "provider",
    "model",
    "stored_verdict",
    "incomplete_tape",
    "other_fixture",
    "arbitrary_arguments",
)


def corrupt(data, fault: str) -> None:
    events = data["batches"][1]["events"]
    uses = {e["payload"]["call_id"]: e for e in events if e["type"] == "agent.tool_use"}
    results = {e["payload"]["call_id"]: e for e in events if e["type"] == "agent.tool_result"}
    if fault in ("skill_text", "bash_result", "file_read", "glob"):
        index = {"skill_text": 0, "bash_result": 8, "file_read": 5, "glob": 7}[fault]
        results[f"call_f1_{index}"]["payload"]["content"] = [{"type": "text", "text": "altered"}]
    elif fault.startswith("missing_"):
        index = {"missing_skill_read": 0, "missing_mcp": 1, "missing_write": 3, "missing_grep": 6}[
            fault
        ]
        events.remove(uses[f"call_f1_{index}"])
        events.remove(results[f"call_f1_{index}"])
    elif fault == "duplicate_mcp_name":
        uses["call_f1_2"]["payload"]["tool_name"] = "describe_agent"
    elif fault == "nonexistent_mcp":
        uses["call_f1_1"]["payload"]["tool_name"] = "client_context"
    elif fault == "foreign_server":
        uses["call_f1_1"]["payload"]["mcp_server"] = "other"
    elif fault == "failed_edit":
        results["call_f1_4"]["payload"]["is_error"] = True
    elif fault == "early_read":
        pair = (uses["call_f1_5"], results["call_f1_5"])
        for event in pair:
            events.remove(event)
        position = events.index(uses["call_f1_3"])
        events[position:position] = pair
    elif fault in ("duplicate_call", "duplicate_result"):
        source = uses["call_f1_3"] if fault == "duplicate_call" else results["call_f1_3"]
        duplicate = json.loads(json.dumps(source))
        duplicate["id"] += "_copy"
        events.insert(events.index(source) + 1, duplicate)
    elif fault == "orphan_result":
        results["call_f1_3"]["payload"]["call_id"] = "orphan"
    elif fault in ("null_running_start", "mismatched_running_start"):
        running = next(e for e in events if e["type"] == "session.status_running")
        running["turn_id"] = None if fault == "null_running_start" else "foreign-root"
    elif fault == "foreign_user_start":
        next(e for e in events if e["type"] == "user.message")["turn_id"] = "foreign-root"
    elif fault in ("foreign_root", "foreign_session", "preview"):
        key, value = {
            "foreign_root": ("turn_id", "another-root"),
            "foreign_session": ("session_id", "another-session"),
            "preview": ("authority", "preview"),
        }[fault]
        uses["call_f1_8"][key] = value
    elif fault == "incomplete_message":
        next(e for e in events if e["type"] == "agent.message")["payload"]["complete"] = False
    elif fault == "errored_root":
        events[-1]["payload"]["outcome"] = "errored"
    elif fault == "second_root":
        duplicate = json.loads(json.dumps(events[1]))
        duplicate["id"] += "_copy"
        events.insert(2, duplicate)
    elif fault in ("session_error", "trailing_event"):
        added = json.loads(json.dumps(events[-1]))
        added.update(
            id="fault",
            type="session.error",
            payload={"category": "upstream", "retry_status": "terminal"},
        )
        events.insert(len(events) if fault == "trailing_event" else len(events) - 2, added)
    elif fault == "unconsumed_batch":
        data["batches"].append(json.loads(json.dumps(data["batches"][1])))
    elif fault in ("provider", "model", "other_fixture"):
        key = "fixture_id" if fault == "other_fixture" else fault
        data[key] = {"provider": "openai", "model": "another-model", "other_fixture": "C16"}[fault]
    elif fault == "stored_verdict":
        data["result"] = "pass"
    elif fault == "incomplete_tape":
        data["complete"] = False
    elif fault == "arbitrary_arguments":
        uses["call_f1_0"]["payload"]["input"] = {"free_key": "free value"}
    else:
        raise ValueError("unknown mutation")
    for sequence, event in enumerate(events):
        event["sequence"] = sequence


@pytest.mark.parametrize("fault", CORRUPTIONS)
async def test_corrupted_f1_tape_never_certifies(tmp_path: Path, fault: str) -> None:
    data = json.loads(TAPE.read_text())
    corrupt(data, fault)
    path = tmp_path / "f1.json"
    path.write_text(json.dumps(data))
    expected = manifest()
    script = Script(expected, replay=True)
    async with script.client() as client:
        result = await replay_default_capability(path, expected, partial(adapter, client, script))
    assert result.status == "fail", (fault, result.evidence)
    if fault in ("null_running_start", "mismatched_running_start"):
        assert result.evidence == ("F1: running record root identity missing or mismatched",)
    elif fault == "foreign_user_start":
        assert result.evidence == ("F1: event belongs to another root",)
    assert all(not r.path.endswith(("/events", "/stream")) for r in script.requests)


@pytest.mark.parametrize(
    "event_index", range(len(json.loads(TAPE.read_text())["batches"][1]["events"]))
)
async def test_each_f1_record_refuses_a_foreign_turn_id(tmp_path: Path, event_index: int) -> None:
    data = json.loads(TAPE.read_text())
    event = data["batches"][1]["events"][event_index]
    event["turn_id"] = "foreign-root"
    path = tmp_path / "foreign-record.json"
    path.write_text(json.dumps(data))
    expected = manifest()
    script = Script(expected, replay=True)
    async with script.client() as client:
        result = await replay_default_capability(path, expected, partial(adapter, client, script))
    assert result.status == "fail", (event_index, event["type"], result.evidence)
    diagnostic = (
        "F1: running record root identity missing or mismatched"
        if event["type"] == "session.status_running"
        else "F1: event belongs to another root"
    )
    assert result.evidence == (diagnostic,)


async def test_null_pre_running_user_turn_id_remains_legal(tmp_path: Path) -> None:
    data = json.loads(TAPE.read_text())
    next(e for e in data["batches"][1]["events"] if e["type"] == "user.message")["turn_id"] = None
    path = tmp_path / "null-user.json"
    path.write_text(json.dumps(data))
    expected = manifest()
    script = Script(expected, replay=True)
    async with script.client() as client:
        result = await replay_default_capability(path, expected, partial(adapter, client, script))
    assert result.status == "pass", result.evidence
    script.assert_consumed()


@pytest.mark.parametrize(
    "fault",
    (
        "failed_edit",
        "nonexistent_mcp",
        "null_running_start",
        "mismatched_running_start",
        "foreign_user_start",
    ),
)
def test_corrupt_f1_checks_survive_python_optimization(tmp_path: Path, fault: str) -> None:
    path = tmp_path / "optimized.json"
    data = json.loads(TAPE.read_text())
    corrupt(data, fault)
    path.write_text(json.dumps(data))
    code = """
import asyncio, runpy, sys
from pathlib import Path
qa = runpy.run_path(sys.argv[1], run_name='offline_f1_fixture')
async def check():
    expected = qa['manifest']()
    script = qa['Script'](expected, replay=True)
    async with script.client() as client:
        result = await qa['replay_default_capability'](
            Path(sys.argv[2]), expected, lambda tape: qa['adapter'](client, script, tape))
    if result.status != 'fail':
        raise SystemExit('optimized F1 checks vanished')
    if sys.argv[3] in ('null_running_start', 'mismatched_running_start'):
        if result.evidence != ('F1: running record root identity missing or mismatched',):
            raise SystemExit('optimized F1 start rejected without the identity diagnostic')
    elif sys.argv[3] == 'foreign_user_start':
        if result.evidence != ('F1: event belongs to another root',):
            raise SystemExit('optimized F1 user start rejected without the root diagnostic')
asyncio.run(check())
"""
    subprocess.run(
        [sys.executable, "-O", "-c", code, __file__, str(path), fault],
        check=True,
        capture_output=True,
        timeout=30,
    )


if __name__ == "__main__":
    asyncio.run(record(Path(sys.argv[1])))
