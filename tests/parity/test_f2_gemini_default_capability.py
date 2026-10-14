"""F2 offline: full default bytes, mapped shell operations and authenticated MCP.

Only MockTransport maps the reviewed fictional HTTPS MCP endpoint to N2's
real disposable loopback host. GCS deployment and model execution are replayed;
this proves SDK request bytes and host tool results, not a live provider cert.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import yaml
from daimon.testing.conformance.default_capability import (
    BASH_RESULT,
    FINAL_MESSAGE,
    MCP_TOOLS,
    PROMPT,
    DefaultCapabilityAdapter,
    DefaultManifest,
    NamedSkill,
    scenario,
    skill_text,
)
from daimon.testing.conformance.recording import Audit, Recorder, Replay, RequestMetadata
from daimon.testing.conformance.runner import run_default_capability
from daimon.testing.drivers.gemini.default_capability import BUILTIN_MAPPING
from daimon.testing.drivers.gemini.fake import MemoryStorage
from daimon.testing.drivers.gemini.live_cert import EvidenceAliases
from daimon.testing.isolated_sdk import build_gemini_client
from mux.contracts.actions import UserMessage
from mux.contracts.events import Event
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ModelRef, Scope, ThreadRef
from mux.contracts.resources import (
    Agent,
    AgentSpec,
    Session,
    SessionSpec,
    SkillUpload,
    SkillUploadFile,
)
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.core import GeminiSessions
from mux.drivers.gemini.skill_deployment import GCSBundleDeployment, file_manifest
from mux.drivers.gemini.transport import Object, SDKTransport, object_value
from mux.state.memory import MemoryStateStore
from pydantic import JsonValue, TypeAdapter

ROOT = Path(__file__).resolve().parents[2]
SCOPE = Scope(
    tenant_id="f2-gemini", account_id="qa", principal_id="replay", authorization_id="owned"
)
MODEL = ModelRef(provider="gemini", id="gemini-3.8-flash")
ACCOUNT = "f2-offline"
MCP_URL = "https://qa-mcp.invalid/mcp"
CREDENTIAL = "host:qa-mcp-run"
OBJECT = TypeAdapter[Object](Object)


def manifest() -> DefaultManifest:
    raw = yaml.safe_load((ROOT / "defaults/agents/daimon.yaml").read_text())
    skills: list[NamedSkill] = []
    for item in raw["skills"]:
        name = item["skill_id"]
        directory = ROOT / "defaults/skills" / name
        skills.append(
            NamedSkill(
                name,
                SkillUpload(
                    files=tuple(
                        SkillUploadFile(
                            path=p.relative_to(directory).as_posix(), content=p.read_bytes()
                        )
                        for p in sorted(directory.rglob("*"))
                        if p.is_file()
                    )
                ),
            )
        )
    from mux.contracts.resources import MCPConnection

    return DefaultManifest(
        name=raw["name"],
        system=raw["system"],
        skills=tuple(skills),
        builtin_tools=tuple(t["name"] for t in raw["tools"][0]["configs"]),
        mcp=MCPConnection(
            name="daimon-mcp",
            url=MCP_URL,
            credential_ref=CREDENTIAL,
            tool_policy=OBJECT.validate_python({"allowed_tools": sorted(MCP_TOOLS)}),
        ),
    )


class Publisher:
    """Injected offline GCS store; every supplied byte is retained without filtering."""

    def __init__(self) -> None:
        self.uploads: list[SkillUpload] = []
        self.files: dict[str, dict[str, bytes]] = {}

    async def publish(
        self, scope: Scope, *, account_scope_id: str, version: str, bundle: SkillUpload
    ) -> GCSBundleDeployment:
        assert scope == SCOPE and account_scope_id == ACCOUNT
        uri = f"gs://offline-f2/{scope.tenant_id}/{scope.account_id}/{version}/"
        self.uploads.append(bundle)
        self.files[uri] = {f.path: f.content for f in bundle.files}
        return GCSBundleDeployment(
            scope.tenant_id, scope.account_id, account_scope_id, version, uri, file_manifest(bundle)
        )


class ScenarioSessions(GeminiSessions):
    """Supply the explicit local binding/thread seam F1 intentionally does not own.

    Resolve the current local agent revision, not an atomic native-agent CAS.
    The adapter's atomic_revision_pin=False keeps that capability gap explicit.
    """

    async def create(
        self, scope: Scope, spec: SessionSpec, *, key: str, initial: UserMessage | None = None
    ) -> Session:
        async with self._storage.transaction() as records:
            agent = records.agents[spec.agent.id]
            assert agent.ref == spec.agent
            revision = agent.revision
        thread = ThreadRef(
            channel=ChannelRef(tenant_id=scope.tenant_id, platform="test", channel_id="f2-gemini"),
            thread_id=key,
        )
        spec = spec.model_copy(
            update={
                "agent_revision": revision,
                "extensions": {
                    "gemini.session": ExtensionConfig(
                        namespace="gemini.session",
                        version=1,
                        value={"binding_id": key, "thread": thread.model_dump(mode="json")},
                    )
                },
            }
        )
        return await super().create(scope, spec, key=key, initial=initial)


def steps(authored: DefaultManifest, outputs: dict[str, str], *, failed: bool) -> list[JsonValue]:
    if failed:
        # The real host refused initialization: replay a provider failed root,
        # not invented successful MCP calls/results.
        return []
    result: list[JsonValue] = []
    for id_, command, output in (
        (
            "skill-read",
            'cat "$(grep -l "^name: file-handling$" .agents/skills/*/SKILL.md)"',
            skill_text(authored),
        ),
        ("write", "printf f1-initial > f1.txt", "written"),
        ("edit", "sed -i s/f1-initial/f1-edited/ f1.txt", "edited"),
        ("read", "cat f1.txt", "f1-edited"),
        ("grep", "grep -n f1-edited f1.txt", "1:f1-edited"),
        ("glob", "printf %s f1.*", "f1.txt"),
        ("bash", "printf f1-bash-ok", BASH_RESULT),
    ):
        result.extend(
            [
                {
                    "type": "code_execution_call",
                    "id": id_,
                    "arguments": {
                        "language": "python",
                        "code": f'import subprocess; print(subprocess.check_output({command!r}, shell=True, text=True), end="")',
                    },
                },
                {
                    "type": "code_execution_result",
                    "id": id_ + ":result",
                    "call_id": id_,
                    "result": output,
                },
            ]
        )
    for name in sorted(MCP_TOOLS):
        result.extend(
            [
                {
                    "type": "mcp_server_tool_call",
                    "id": name,
                    "name": name,
                    "server_name": "daimon-mcp",
                    "arguments": {},
                },
                {
                    "type": "mcp_server_tool_result",
                    "id": name + ":result",
                    "call_id": name,
                    "result": outputs[name],
                },
            ]
        )
    result.append(
        {
            "type": "model_output",
            "id": "final",
            "content": [{"type": "text", "text": FINAL_MESSAGE}],
        }
    )
    return result


class AliasRecorder(Recorder):
    """Project identity fields using merged, credential-audited aliases only."""

    def __init__(self, *, secrets: tuple[str, ...]) -> None:
        super().__init__(secrets=secrets)
        self.aliases = EvidenceAliases(secrets)

    def record(self, request: RequestMetadata, events: Iterable[Event]) -> None:
        super().record(
            request.model_copy(update={"path": self.aliases.path(request.path)}),
            tuple(self.aliases.event(event) for event in events),
        )


class Evidence:
    """Compiled inline intent plus later SDK byte checks, not a native agent API."""

    def __init__(self, storage: MemoryStorage, publisher: Publisher) -> None:
        self.storage, self.publisher = storage, publisher
        self.posts: list[bytes] = []

    @property
    def skill_uploads(self) -> tuple[SkillUpload, ...]:
        return tuple(self.publisher.uploads)

    @property
    def deployed_agent(self) -> AgentSpec:
        (agent,) = self.storage._records.agents.values()  # pyright: ignore[reportPrivateUsage]
        return agent.spec

    def agent_spec(self, agent: Agent) -> AgentSpec:
        return agent.spec

    def assert_consumed(self) -> None:
        assert len(self.posts) == 1


@pytest.mark.parametrize("mode", ["valid", "wrong-bearer", "revoked-before-send"])
async def test_f2_gemini_default_capabilities_through_real_loopback(
    mode: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from daimon.testing.asgi import INIT_BODY, INIT_HEADERS, parse_jsonrpc_response
    from daimon.testing.qa_mcp_host import QA_SESSION_ID, build_host, cleanup, serve

    url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    assert url, "the parity lane provides an isolated test database"
    authored = manifest()
    qa = await build_host(database_url=url, port=0, root=tmp_path)
    path = qa.manifest.path(tmp_path)
    publisher, storage = Publisher(), MemoryStorage()
    evidence = Evidence(storage, publisher)
    resolutions: list[tuple[Scope, str, str]] = []
    gate_statuses: list[int] = []
    outputs: dict[str, str] = {}
    saved: Object | None = None
    recorder = AliasRecorder(secrets=(qa.bearer.token,))
    caplog.set_level(logging.DEBUG, logger="google.genai")
    caplog.set_level(logging.DEBUG, logger="httpx")
    try:
        async with serve(qa) as loopback, httpx.AsyncClient(timeout=30, trust_env=False) as host:

            async def resolve(scope: Scope, reference: str, destination: str) -> str:
                resolutions.append((scope, reference, destination))
                assert (scope, reference, destination) == (SCOPE, CREDENTIAL, MCP_URL)
                if not qa.bearer.accepts(qa.bearer.token, datetime.now(UTC)):
                    raise RuntimeError("revoked run bearer")
                return qa.bearer.token if mode != "wrong-bearer" else "wrong-owned-run-bearer"

            async def bridge(request: httpx.Request) -> httpx.Response:
                assert request.url.host == "gemini.invalid"
                assert request.headers["x-goog-api-key"] == "offline-f2-fixture"
                nonlocal saved
                assert request.url.host == "gemini.invalid"
                if request.method == "GET":
                    assert saved is not None
                    if request.url.params.get("stream") == "true":
                        return httpx.Response(
                            200,
                            headers={"content-type": "text/event-stream"},
                            content='data: {"event_type":"interaction.completed"}\n\n',
                        )
                    return httpx.Response(200, json=saved)
                assert request.method == "POST"
                evidence.posts.append(request.content)
                body = OBJECT.validate_json(request.content)
                assert object_value(body["agent_config"])["model"] == MODEL.id
                assert body["system_instruction"] == authored.system
                assert body["input"] == [{"type": "text", "text": PROMPT}]
                tools = body["tools"]
                assert isinstance(tools, list)
                assert [
                    t for t in tools if isinstance(t, dict) and t.get("type") == "code_execution"
                ] == [{"type": "code_execution"}]
                (server,) = [
                    object_value(t)
                    for t in tools
                    if isinstance(t, dict) and t.get("type") == "mcp_server"
                ]
                assert server["name"] == "daimon-mcp" and server["url"] == MCP_URL
                assert server["allowed_tools"] == [{"mode": "auto", "tools": sorted(MCP_TOOLS)}]
                authorization = object_value(server["headers"])["Authorization"]
                assert isinstance(authorization, str)
                assert request.content.count(qa.bearer.token.encode()) == (
                    1 if mode == "valid" else 0
                )
                sources = object_value(body["environment"])["sources"]
                assert isinstance(sources, list) and len(sources) == 11
                pins = evidence.deployed_agent.skills
                assert pins is not None and len(pins) == 11
                for mounted, named, pin in zip(sources, authored.skills, pins, strict=True):
                    source = object_value(mounted)
                    uri = source["source"]
                    assert isinstance(uri, str) and uri.endswith(f"/{pin.version}/")
                    assert source == {
                        "type": "gcs",
                        "source": uri,
                        "target": f".agents/skills/{pin.id}",
                    }
                    assert publisher.files[uri] == {f.path: f.content for f in named.upload.files}

                headers = {**INIT_HEADERS, "Authorization": authorization}
                init = await host.post(loopback, json=dict(INIT_BODY), headers=headers)
                gate_statuses.append(init.status_code)
                failed = init.status_code != 200
                if not failed:
                    for index, name in enumerate(sorted(MCP_TOOLS), start=2):
                        response = await host.post(
                            loopback,
                            json={
                                "jsonrpc": "2.0",
                                "id": index,
                                "method": "tools/call",
                                "params": {"name": name, "arguments": {}},
                            },
                            headers=headers,
                        )
                        gate_statuses.append(response.status_code)
                        assert response.status_code == 200
                        rpc = OBJECT.validate_python(parse_jsonrpc_response(response))
                        assert "error" not in rpc
                        outputs[name] = json.dumps(rpc["result"], sort_keys=True)
                stamp = datetime.now(UTC).isoformat()
                saved = {
                    "id": "f2-native-root",
                    "status": "failed" if failed else "completed",
                    "created": stamp,
                    "updated": stamp,
                    "environment_id": "f2-native-workspace",
                    "steps": steps(
                        authored,
                        outputs,
                        failed=failed,
                    ),
                    "usage": {
                        "total_input_tokens": 64,
                        "total_cached_tokens": 0,
                        "total_output_tokens": 8,
                        "total_thought_tokens": 0,
                    },
                }
                return httpx.Response(200, json={**saved, "status": "in_progress", "steps": []})

            with build_gemini_client(
                transport=httpx.MockTransport(bridge),
                api_key="offline-f2-fixture",
                base_url="https://gemini.invalid",
            ) as client:
                try:
                    transport = SDKTransport(client)
                    driver = GeminiManagedAgents(
                        transport,
                        storage=storage,
                        state_store=MemoryStateStore(),
                        account_scope_id=ACCOUNT,
                        mcp_secrets=resolve,
                        skill_bundle_publisher=publisher,
                    )
                    driver.sessions = ScenarioSessions(
                        transport, storage, ACCOUNT, mcp_secrets=resolve
                    )
                    if mode == "revoked-before-send":
                        qa.bearer.revoke()
                    adapter = DefaultCapabilityAdapter(
                        driver=driver,
                        scope=SCOPE,
                        model=MODEL,
                        environment=None,
                        transport=evidence,
                        builtin_mapping=BUILTIN_MAPPING,
                        atomic_revision_pin=False,
                    )
                    result = (
                        await scenario(authored, adapter, recorder=recorder)
                        if mode == "valid"
                        else await run_default_capability(authored, adapter, recorder=recorder)
                    )
                    assert result.status == ("pass" if mode == "valid" else "fail"), result.evidence
                    assert evidence.skill_uploads == tuple(s.upload for s in authored.skills)
                    assert len(evidence.deployed_agent.skills or ()) == 11
                    if mode == "valid":
                        assert gate_statuses == [200, 200, 200]
                        assert len(resolutions) == 2  # port admission + exact SDK dispatch
                        assert "qa-agent" in outputs["describe_agent"]
                        assert QA_SESSION_ID in outputs["list_my_sessions"]
                        tape = tmp_path / "F2-GEMINI.json"
                        recorder.save(
                            tape, fixture_id="F1", provider="gemini", model=MODEL.id, complete=True
                        )
                        Audit((qa.bearer.token,)).audit(json.loads(tape.read_text()))
                        replay = Replay.load(tape)
                        posts_before = len(evidence.posts)
                        for batch in replay.tape.batches:
                            assert await replay.events(batch.request) == batch.events
                        replay.finish()
                        assert len(evidence.posts) == posts_before
                        assert qa.bearer.token not in tape.read_text()
                    elif mode == "wrong-bearer":
                        assert gate_statuses == [401] and len(evidence.posts) == 1
                    else:
                        assert not gate_statuses and not evidence.posts
                    assert qa.bearer.token not in repr(storage._records)  # pyright: ignore[reportPrivateUsage]
                    assert qa.bearer.token not in path.read_text()
                    assert qa.bearer.token not in caplog.text
                    assert not qa.manifest.refused_egress
                finally:
                    await client.aio.aclose()
    finally:
        await qa.engine.dispose()
        cleaned = await cleanup(path, database_url=url)
    assert cleaned.status == "cleaned"
