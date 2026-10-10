"""F2 offline: Daimon's default capabilities on the OpenAI backend, MCP served for real.

The F1 default-capability scenario (`mux.conformance.default_capability`) runs
against the OpenAI driver with the default agent's 11 skills and builtin tools,
exactly as the F1 OpenAI test scripts them. What F2 adds: the `daimon-mcp`
attachment is authenticated (`credential_ref` resolved per session by the
driver's `mcp_secrets`), and its two read tools are served by the disposable
loopback host (`daimon.testing.qa_mcp_host`) through its gate. When the driver
creates the session, the scripted provider does what OpenAI's server-side MCP
client would: it takes the serialized authorization, calls the real gate, and
the turn's `mcp_call` results are the real tool results. F1's own checks then
judge the whole turn.

Offline: no OpenAI request leaves the process (an httpx MockTransport stands in
for the API), no tunnel, no key. Only the scripted provider maps the fictional
HTTPS MCP URL to the loopback socket; the driver's destination checks are
unchanged.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from mux.conformance.default_capability import (
    BASH_RESULT,
    FINAL_MESSAGE,
    MCP_TOOLS,
    DefaultCapabilityAdapter,
    DefaultCapabilityReplayEvents,
    DefaultManifest,
    NamedSkill,
    skill_text,
)
from mux.conformance.recording import Audit, Recorder
from mux.conformance.runner import run_default_capability
from mux.contracts.ids import Scope
from mux.contracts.resources import AgentSpec, MCPConnection, SkillUpload, SkillUploadFile
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai._common import Context
from mux.drivers.openai.agents import agent_spec
from mux.drivers.openai.default_capability import (
    BUILTIN_MAPPING,
    MODEL,
    SCOPE,
    OpenAIDefaultCapabilityFactory,
    OpenAIDefaultCapabilityTransport,
)
from mux.drivers.openai.transport import Object, SDKTransport, object_json
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from openai import AsyncOpenAI
from pydantic import JsonValue

ROOT = Path(__file__).resolve().parents[2]
MCP_URL = "https://qa-mcp.invalid/mcp"
CREDENTIAL = "host:qa-mcp-run"


def _manifest() -> DefaultManifest:
    """The default agent, as F1's OpenAI test builds it, with an authenticated MCP."""
    raw = yaml.safe_load((ROOT / "defaults/agents/daimon.yaml").read_text())
    skills: list[NamedSkill] = []
    for name in (item["skill_id"] for item in raw["skills"]):
        directory = ROOT / "defaults/skills" / name
        paths = sorted(path for path in directory.rglob("*") if path.is_file())
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
        mcp=MCPConnection(
            name="daimon-mcp",
            url=MCP_URL,
            credential_ref=CREDENTIAL,
            tool_policy={"allowed_tools": list(MCP_TOOLS), "required": True},
        ),
    )


def _native_events(
    manifest: DefaultManifest, mcp_outputs: dict[str, str], mcp_error: str | None = None
) -> list[Object]:
    """F1's scripted OpenAI turn; the two MCP results are filled in from the real host.

    With `mcp_error`, the MCP calls fail the way OpenAI reports a server it could
    not reach or authenticate to: failed `mcp_call` items carrying the error.
    """

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
    for identity, command, output in (
        ("skill-read", "cat /fixture/file-handling/SKILL.md", skill_text(manifest)),
        (
            "write",
            "apply_patch '*** Begin Patch\n*** Add File: f1.txt\n+f1-initial\n*** End Patch'",
            "Success",
        ),
        (
            "edit",
            "apply_patch '*** Begin Patch\n*** Update File: f1.txt\n@@\n-f1-initial\n"
            "+f1-edited\n*** End Patch'",
            "Success",
        ),
        ("file-read", "cat f1.txt", "f1-edited"),
        ("grep", "grep -n f1-edited f1.txt", "1:f1-edited"),
        ("glob", "printf %s f1.*", "f1.txt"),
        ("bash", "printf f1-bash-ok", BASH_RESULT),
    ):
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
    for name in sorted(MCP_TOOLS):
        item = {
            "id": name,
            "type": "mcp_call",
            "turn_id": "root",
            "name": name,
            "server_label": "daimon-mcp",
            "arguments": {},
            "output": None if mcp_error else mcp_outputs.get(name, "unserved"),
            "error": mcp_error,
            "status": "failed" if mcp_error else "completed",
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
    return events


class _ScopedTranscript(OpenAIDefaultCapabilityTransport):
    """F1's scripted transport, reading the deployed agent back with scoped decoding.

    An agent with an authenticated MCP keeps its credential intent as references
    that only a scoped driver context may decode (`agents.agent_spec`), so F1's
    agent check reads it back through the same context the driver uses.
    """

    context: Context | None = None

    @property
    def deployed_agent(self) -> AgentSpec:
        if self._agent is None:  # pyright: ignore[reportPrivateUsage]
            raise ValueError("no native deployed agent")
        assert self.context is not None
        return self._mapped(agent_spec(self._agent, self.context))  # pyright: ignore[reportPrivateUsage]


class _HostServedMcp:
    """Wraps F1's scripted OpenAI transport: at session creation, serve MCP for real."""

    def __init__(
        self,
        script: OpenAIDefaultCapabilityTransport,
        manifest: DefaultManifest,
        gate_url: str,
        mcp: httpx.AsyncClient,
        secondary: Object | None = None,
    ) -> None:
        self.script, self.manifest, self.gate_url, self.mcp = script, manifest, gate_url, mcp
        # A native MCP server the provider adds to the stored agent, never bound by Daimon.
        self.secondary = secondary
        self.agent_reads = 0
        self.authorizations: list[str] = []
        # (server_label, authorization) for every MCP tool in every session POST.
        self.posted_mcp: list[tuple[str, str | None]] = []
        # Every session POST body, verbatim, whatever its tools' types.
        self.posted_bodies: list[str] = []
        self.results: dict[str, Object] = {}
        self.gate_statuses: list[int] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if (request.method, request.url.path) == ("POST", "/v1/agents/sessions"):
            self.posted_bodies.append(request.content.decode())
            await self._serve(object_json(json.loads(request.content)))
        response = await self.script.handle(request)
        if (request.method, request.url.path) != ("GET", "/v1/agents/agent"):
            return response
        self.agent_reads += 1
        # Only the read inside session creation: F1's own retrieve check comes first.
        if self.secondary is None or self.agent_reads < 2:
            return response
        raw = object_json(json.loads(response.content))
        raw["tools"] = [*list(raw["tools"]), self.secondary]  # type: ignore[arg-type]
        return httpx.Response(response.status_code, json=raw)

    async def _serve(self, body: Object) -> None:
        from daimon.testing.asgi import INIT_BODY, INIT_HEADERS, parse_jsonrpc_response

        tools: list[Any] = list(object_json(body["agent"])["tools"])  # type: ignore[arg-type]
        for tool in tools:
            if tool.get("type") == "mcp":
                auth = object_json(tool.get("transport") or {}).get("authorization")
                label = str(tool.get("server_label"))
                self.posted_mcp.append((label, None if auth is None else str(auth)))
        (server,) = [tool for tool in tools if tool.get("type") == "mcp"]
        transport = object_json(server["transport"])
        assert transport["server_url"] == MCP_URL
        authorization = str(transport["authorization"])
        self.authorizations.append(authorization)
        headers = {**INIT_HEADERS, "Authorization": authorization}
        init = await self.mcp.post(self.gate_url, json=dict(INIT_BODY), headers=headers)
        self.gate_statuses.append(init.status_code)
        if init.status_code != 200:
            self.script.native_events = tuple(
                _native_events(
                    self.manifest, {}, mcp_error=f"MCP server returned {init.status_code}"
                )
            )
            return
        outputs: dict[str, str] = {}
        for index, name in enumerate(sorted(MCP_TOOLS), start=2):
            response = await self.mcp.post(
                self.gate_url,
                json={
                    "jsonrpc": "2.0",
                    "id": index,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": {}},
                },
                headers=headers,
            )
            self.gate_statuses.append(response.status_code)
            result = object_json(parse_jsonrpc_response(response))
            assert "error" not in result, result
            self.results[name] = result
            outputs[name] = json.dumps(result["result"], sort_keys=True)
        self.script.native_events = tuple(_native_events(self.manifest, outputs))


def _authorization(scope: Scope, _kind: str, _identity: str | None) -> bool:
    return scope == SCOPE


class _F2Factory(OpenAIDefaultCapabilityFactory):
    """F1's OpenAI factory with the driver's MCP credential resolver and the real host."""

    def __init__(
        self,
        manifest: DefaultManifest,
        gate_url: str,
        bearer: str,
        mcp: httpx.AsyncClient,
        secondary: Object | None = None,
    ) -> None:
        super().__init__(manifest, tuple(_native_events(manifest, {})))
        self.gate_url, self.bearer, self.mcp = gate_url, bearer, mcp
        self.secondary = secondary
        self.served: list[_HostServedMcp] = []
        self.resolutions: list[tuple[Scope, str, str]] = []

    async def _resolve(self, scope: Scope, ref: str, destination: str) -> str:
        self.resolutions.append((scope, ref, destination))
        assert (scope, ref, destination) == (SCOPE, CREDENTIAL, MCP_URL)
        return self.bearer

    def __call__(self, replay: Any = None) -> DefaultCapabilityAdapter:
        script = _ScopedTranscript(self.manifest, self.native_events, replay=replay is not None)
        served = _HostServedMcp(script, self.manifest, self.gate_url, self.mcp, self.secondary)
        sdk = AsyncOpenAI(
            api_key="offline-fixture",
            base_url="https://openai.invalid/v1",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(served.handle)),
        )
        script.context = Context(
            SDKTransport(sdk), "offline", "openai.persistent_workspace", _authorization
        )
        driver = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="offline",
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            authorization=_authorization,
            events=DefaultCapabilityReplayEvents(replay) if replay is not None else None,
            mcp_secrets=self._resolve,
        )
        self.scripts.append(script)
        self.served.append(served)
        self._clients.append(sdk)
        return DefaultCapabilityAdapter(
            driver=driver,
            scope=SCOPE,
            model=MODEL,
            environment=None,
            transport=script,
            builtin_mapping=BUILTIN_MAPPING,
            atomic_revision_pin=False,
        )


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    for name in tuple(os.environ):
        if name.startswith("OPENAI_"):
            monkeypatch.delenv(name)


def _database_url() -> str:
    url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    assert url, "the parity lane provides an isolated test database"
    return url


async def test_f2_default_skills_builtins_and_authenticated_mcp_on_openai(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from daimon.testing.qa_mcp_host import QA_SESSION_ID, build_host, cleanup, serve

    manifest = _manifest()
    database_url = _database_url()
    qa = await build_host(database_url=database_url, port=0, root=tmp_path)
    manifest_path = qa.manifest.path(tmp_path)
    recorder = Recorder()
    caplog.set_level(logging.DEBUG, logger="openai")
    caplog.set_level(logging.DEBUG, logger="httpx")
    try:
        async with serve(qa) as gate_url, httpx.AsyncClient(timeout=30) as mcp:
            f2 = _F2Factory(manifest, gate_url, qa.bearer.token, mcp)
            async with f2:
                result = await run_default_capability(manifest, f2(), recorder=recorder)
                assert result.status == "pass", result.evidence
                (served,) = f2.served
                f2.scripts[0].assert_consumed()
                deployed = f2.scripts[0].deployed_agent
                assert deployed.skills is not None and len(deployed.skills) == len(manifest.skills)

            # MCP was really served, by the real host, under the run bearer.
            assert served.authorizations == [f"Bearer {qa.bearer.token}"]
            assert served.gate_statuses == [200, 200, 200]
            # Only the bound attachment carries an authorization into the session
            # POST, and it is exactly the resolved run bearer: no other, unbound
            # authorization is copied in.
            assert served.posted_mcp == [("daimon-mcp", f"Bearer {qa.bearer.token}")]
            assert f2.resolutions == [(SCOPE, CREDENTIAL, MCP_URL)]
            assert "qa-agent" in json.dumps(served.results["describe_agent"])
            assert QA_SESSION_ID in json.dumps(served.results["list_my_sessions"])
            # The turn the provider streamed carries those real results, not a placeholder.
            streamed = json.dumps(list(f2.scripts[0].native_events))
            assert "qa-agent" in streamed and QA_SESSION_ID in streamed
            assert "unserved" not in streamed
            # The bearer never reaches the agent, the tape or the manifest.
            tape = tmp_path / "F2.json"
            recorder.save(tape, fixture_id="F1", provider="openai", model=MODEL.id, complete=True)
            Audit(()).audit(json.loads(tape.read_text()))
            for text in (tape.read_text(), manifest_path.read_text(), repr(deployed)):
                assert qa.bearer.token not in text
        # Nor does any credential reach the SDK's or httpx's debug logs.
        assert qa.bearer.token not in caplog.text
        assert "Bearer " not in caplog.text
        assert qa.manifest.refused_egress == []
    finally:
        await qa.engine.dispose()
        await cleanup(manifest_path, database_url=database_url)


async def test_f2_fails_when_the_mcp_bearer_is_revoked(tmp_path: Path) -> None:
    from daimon.testing.qa_mcp_host import build_host, cleanup, serve

    manifest = _manifest()
    database_url = _database_url()
    qa = await build_host(database_url=database_url, port=0, root=tmp_path)
    manifest_path = qa.manifest.path(tmp_path)
    try:
        async with serve(qa) as gate_url, httpx.AsyncClient(timeout=30) as mcp:
            qa.bearer.revoke()
            f2 = _F2Factory(manifest, gate_url, qa.bearer.token, mcp)
            async with f2:
                result = await run_default_capability(manifest, f2())
            assert result.status != "pass"
            assert f2.served[0].gate_statuses == [401]
    finally:
        await qa.engine.dispose()
        await cleanup(manifest_path, database_url=database_url)


_SENTINEL = "fictional-unbound-secondary-private-value"
_SECONDARY_URL = "https://secondary.example.com/mcp"


def _mcp(**transport: JsonValue) -> Object:
    native: Object = {"type": "http", "server_url": _SECONDARY_URL, **transport}
    return {"type": "mcp", "server_label": "secondary", "transport": native}


# Unbound tools the provider may add to the stored agent, each carrying a credential.
_UNBOUND: dict[str, Object] = {
    # 9d06f2b's leak: a native authorization copied into the session POST.
    "authorization": _mcp(authorization="Bearer " + _SENTINEL),
    "headers": _mcp(headers={"Authorization": "Bearer " + _SENTINEL}),
    # cb15780's leaks: the credential carried in the destination itself.
    "url_userinfo": _mcp(server_url=f"https://user:{_SENTINEL}@secondary.example.com/mcp"),
    "url_query": _mcp(server_url=f"{_SECONDARY_URL}?token={_SENTINEL}"),
    # 48a1dc8's leaks: tools whose type is not exactly "mcp".
    "type_case": {**_mcp(authorization="Bearer " + _SENTINEL), "type": "MCP"},
    "type_padded": {**_mcp(authorization="Bearer " + _SENTINEL), "type": "mcp "},
    "remote_mcp": {
        "type": "remote_mcp",
        "server_label": "secondary",
        "server_url": _SECONDARY_URL,
        "authorization": _SENTINEL,
    },
    "function_headers": {
        "type": "function",
        "name": "lookup",
        "parameters": {"type": "object", "properties": {}},
        "headers": {"Authorization": "Bearer " + _SENTINEL},
    },
    "builtin_authorization": {"type": "web_search", "authorization": _SENTINEL},
}


@pytest.mark.parametrize("carrier", sorted(_UNBOUND))
async def test_f2_never_copies_an_unbound_credential(
    carrier: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The provider returns the stored agent with an unbound, credential-bearing tool."""
    from daimon.testing.qa_mcp_host import build_host, cleanup, serve

    sentinel = _SENTINEL
    secondary = _UNBOUND[carrier]
    manifest = _manifest()
    database_url = _database_url()
    qa = await build_host(database_url=database_url, port=0, root=tmp_path)
    manifest_path = qa.manifest.path(tmp_path)
    caplog.set_level(logging.DEBUG, logger="openai")
    caplog.set_level(logging.DEBUG, logger="httpx")
    try:
        async with serve(qa) as gate_url, httpx.AsyncClient(timeout=30) as mcp:
            f2 = _F2Factory(manifest, gate_url, qa.bearer.token, mcp, secondary)
            async with f2:
                result = await run_default_capability(manifest, f2())
            assert result.status != "pass"
            (served,) = f2.served
            # Refused before the session POST: nothing was posted, the run bearer was
            # never resolved, and the real host was never called.
            assert served.posted_bodies == [] and served.posted_mcp == []
            assert served.authorizations == []
            assert served.gate_statuses == [] and f2.resolutions == []
            assert served.agent_reads == 2  # the injection reached session creation
            assert sentinel not in json.dumps(result.evidence, default=str)
        assert sentinel not in caplog.text and qa.bearer.token not in caplog.text
        assert any(r.name.startswith("openai") for r in caplog.records)
        assert qa.manifest.refused_egress == []
    finally:
        await qa.engine.dispose()
        await cleanup(manifest_path, database_url=database_url)
