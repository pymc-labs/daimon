"""Offline F2 composition: real Anthropic ports and real loopback MCP reads.

Inference, skill execution and file operations remain F1's scripted evidence;
only MCP executes on N2's disposable host with synthetic owned backing data.
No paid call, remote MCP tunnel, key read or live certificate is involved.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.testing.asgi import INIT_BODY, INIT_HEADERS, parse_jsonrpc_response
from daimon.testing.qa_mcp_host import (
    QA_SESSION_ID,
    Manifest,
    SharedQaHost,
    build_run,
    end_run,
    serve,
)
from mux.contracts.actions import UserMessage
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, ResourceRef, Scope
from mux.contracts.ports import Vaults
from mux.contracts.resources import CredentialBinding, Session, SessionSpec
from mux.drivers.anthropic import AnthropicManagedAgents
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.sessions_lifecycle import AnthropicSessions
from mux.errors import ScopeViolation
from mux.testing.conformance.default_capability import (
    MCP_TOOLS,
    DefaultCapabilityAdapter,
    DefaultCapabilityReplayEvents,
    DefaultManifest,
)
from mux.testing.conformance.recording import Audit, Recorder, Replay
from mux.testing.conformance.runner import replay_default_capability, run_default_capability
from sqlalchemy import text as sql_text

ROOT = Path(__file__).resolve().parents[2]
F1_SPEC = importlib.util.spec_from_file_location(
    "anthropic_f1", ROOT / "packages/mux/tests/test_default_capability_anthropic.py"
)
assert F1_SPEC is not None and F1_SPEC.loader is not None
F1 = importlib.util.module_from_spec(F1_SPEC)
F1_SPEC.loader.exec_module(F1)
ENVIRONMENT: ResourceRef = F1.ENVIRONMENT
MODEL: ModelRef = F1.MODEL
NOW: str = F1.NOW
SCOPE: Scope = F1.SCOPE

MCP_URL = "https://qa-mcp.invalid/mcp"
SECRET_REF = "host:qa-mcp-run"
VAULT_ID = "vault_f2"


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    for name in tuple(os.environ):
        if name.startswith("ANTHROPIC_"):
            monkeypatch.delenv(name)


class _VaultSession(AnthropicSessions):
    """F2 host composition using the existing native session extension."""

    async def create(
        self, scope: Scope, spec: SessionSpec, *, key: str, initial: UserMessage | None = None
    ) -> Session:
        extension = ExtensionConfig(
            namespace="anthropic.session_create", version=1, value={"vault_ids": [VAULT_ID]}
        )
        mounted = spec.model_copy(update={"extensions": {extension.namespace: extension}})
        return await super().create(scope, mounted, key=key, initial=initial)


class _Provider:
    """Stand-in for managed agents' vault-backed, server-side MCP client.

    Its bearer comes from the real SDK credential POST, and its destination
    comes from the real SDK agent POST. Only that explicit fictional HTTPS
    destination maps to the loopback socket; no provider network fallback.
    """

    def __init__(self, expected: DefaultManifest, gate_url: str, mcp: httpx.AsyncClient) -> None:
        self.script = F1.Script(expected)
        self.expected, self.gate_url, self.mcp = expected, gate_url, mcp
        self.auth: dict[str, str] | None = None
        self.mounted = False
        self.statuses: list[int] = []
        self.results: dict[str, object] = {}
        self.vault_requests: list[tuple[str, str]] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/v1/vaults"):
            self.vault_requests.append((request.method, path))
            if (request.method, path) == ("GET", "/v1/vaults"):
                return httpx.Response(200, json={"data": [], "next_page": None})
            common: dict[str, object] = {"created_at": NOW, "updated_at": NOW, "metadata": {}}
            if (request.method, path) == ("POST", "/v1/vaults"):
                assert json.loads(request.content) == {"display_name": "f2-run"}
                return httpx.Response(
                    200, json={**common, "id": VAULT_ID, "type": "vault", "display_name": "f2-run"}
                )
            if (request.method, path) == ("POST", f"/v1/vaults/{VAULT_ID}/credentials"):
                body = json.loads(request.content)
                self.auth = body["auth"]
                assert self.auth is not None
                assert set(self.auth) == {"type", "mcp_server_url", "token"}
                assert self.auth["type"] == "static_bearer"
                assert self.auth["mcp_server_url"] == MCP_URL
                return httpx.Response(
                    200,
                    json={
                        **common,
                        "id": "credential_f2",
                        "type": "vault_credential",
                        "vault_id": VAULT_ID,
                        "display_name": body["display_name"],
                        "auth": {"type": "static_bearer", "mcp_server_url": MCP_URL},
                    },
                )
            raise AssertionError("unexpected offline vault request")
        if (request.method, path) == ("POST", "/v1/sessions"):
            body = json.loads(request.content)
            self.mounted = body.get("vault_ids") == [VAULT_ID]
            assert self.mounted
        if (request.method, path) == ("GET", "/v1/sessions/sess_f1/events/stream"):
            await self._serve()
        return self.script.dispatch(request)

    async def _serve(self) -> None:
        from mux.testing.drivers.anthropic.ma_transport import ScriptedReply

        assert self.mounted and self.auth is not None
        deployed = self.script.deployed_agent
        assert deployed.mcp_servers is not None
        assert [(server.name, server.url) for server in deployed.mcp_servers] == [
            ("daimon-mcp", self.auth["mcp_server_url"])
        ]
        headers = {**INIT_HEADERS, "Authorization": "Bearer " + self.auth["token"]}
        response = await self.mcp.post(self.gate_url, json=dict(INIT_BODY), headers=headers)
        self.statuses.append(response.status_code)
        if response.status_code == 200:
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
                self.statuses.append(response.status_code)
                result = parse_jsonrpc_response(response)
                assert response.status_code == 200 and "error" not in result
                output = result["result"]
                assert isinstance(output, dict)
                output = cast(dict[str, object], output)
                assert not output.get("isError", False)
                self.results[name] = output
        events: list[dict[str, Any]] = F1.native_turn(self.expected)
        calls = {
            event["id"]: event["name"] for event in events if event["type"] == "agent.mcp_tool_use"
        }
        for event in events:
            if event["type"] == "agent.mcp_tool_result":
                name = calls[event["mcp_tool_use_id"]]
                event["content"] = [
                    {"type": "text", "text": json.dumps(self.results.get(name, "MCP refused"))}
                ]
                event["is_error"] = name not in self.results
        # Replace the queued F1 stream only; its upload/provisioning checks stay intact.
        self.script.replies[0] = ScriptedReply.stream("/v1/sessions/sess_f1/events/stream", events)


def _driver(
    client: AsyncAnthropic,
    *,
    secrets: Callable[[Scope, str], str] | None = None,
    replay: Replay | None = None,
) -> AnthropicManagedAgents:
    authorization = ResourceAuthorization(
        SCOPE,
        frozenset(
            {
                ("agent", "ag_f1"),
                ("session", "sess_f1"),
                ("environment", ENVIRONMENT.id),
                ("vault", VAULT_ID),
                *(("skill", f"skill_f1_{i}") for i in range(11)),
            }
        ),
    )
    return AnthropicManagedAgents(
        client,
        account_scope_id="offline",
        authorization=authorization,
        secrets=secrets,
        sessions=_VaultSession(client, "offline", authorization),
        events=DefaultCapabilityReplayEvents(replay) if replay is not None else None,
    )


@pytest.mark.parametrize("auth_state", ["valid", "revoked", "wrong"])
async def test_f2_anthropic_default_capability_through_loopback(
    auth_state: str,
    qa_shared_host: SharedQaHost,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    qa = await build_run(qa_shared_host, port=0, root=tmp_path)
    manifest_path = qa.manifest.path(tmp_path)
    authored = F1.manifest()
    expected = replace(authored, mcp=authored.mcp.model_copy(update={"url": MCP_URL}))
    recorder = Recorder()
    resolutions: list[tuple[Scope, str]] = []

    def resolve(scope: Scope, reference: str) -> str:
        resolutions.append((scope, reference))
        assert (scope, reference) == (SCOPE, SECRET_REF)
        return qa.bearer.token if auth_state != "wrong" else "offline-invalid-bearer"

    caplog.set_level(logging.DEBUG, logger="anthropic")
    caplog.set_level(logging.DEBUG, logger="httpx")
    try:
        async with serve(qa) as gate_url, httpx.AsyncClient(timeout=30, trust_env=False) as mcp:
            if auth_state == "revoked":
                qa.bearer.revoke()
            provider = _Provider(expected, gate_url, mcp)
            async with AsyncAnthropic(
                api_key="offline-fixture",
                base_url="https://anthropic.invalid",
                max_retries=0,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(provider.handle)),
            ) as client:
                driver = _driver(client, secrets=resolve)
                vaults = driver.extension(Vaults, namespace="anthropic.vaults", version=1)
                vault = await vaults.ensure(SCOPE, "f2-run", key="f2-vault")
                credential = await vaults.put(
                    SCOPE,
                    vault.ref,
                    CredentialBinding(
                        name="daimon-mcp",
                        kind="static_bearer",
                        credential_ref=SECRET_REF,
                        mcp_server_url=MCP_URL,
                    ),
                    expected=None,
                    key="f2-credential",
                )
                result = await run_default_capability(
                    expected,
                    DefaultCapabilityAdapter(driver, SCOPE, MODEL, ENVIRONMENT, provider.script),
                    recorder=recorder,
                )
            provider.script.assert_consumed()
            assert resolutions == [(SCOPE, SECRET_REF)]
            assert provider.vault_requests == [
                ("GET", "/v1/vaults"),
                ("POST", "/v1/vaults"),
                ("POST", f"/v1/vaults/{VAULT_ID}/credentials"),
            ]
            if auth_state == "valid":
                assert result.status == "pass", result.evidence
                assert provider.statuses == [200, 200, 200]
                assert "qa-agent" in json.dumps(provider.results["describe_agent"])
                assert QA_SESSION_ID in json.dumps(provider.results["list_my_sessions"])
                tape = tmp_path / "F2-anthropic.json"
                recorder.save(
                    tape, fixture_id="F1", provider="anthropic", model=MODEL.id, complete=True
                )
                data = json.loads(tape.read_text())
                Audit(()).audit(data)
                assert "qa-agent" in tape.read_text() and QA_SESSION_ID in tape.read_text()
                # The new replay provisions afresh but makes no provider event or MCP request.
                replay_script = F1.Script(expected, replay=True)
                async with replay_script.client() as replay_client:

                    def replay_adapter(replay: Replay) -> DefaultCapabilityAdapter:
                        return DefaultCapabilityAdapter(
                            _driver(replay_client, replay=replay),
                            SCOPE,
                            MODEL,
                            ENVIRONMENT,
                            replay_script,
                        )

                    replayed = await replay_default_capability(tape, expected, replay_adapter)
                assert replayed.status == "pass", replayed.evidence
                replay_script.assert_consumed()
                assert provider.statuses == [200, 200, 200]
                assert qa.bearer.token not in tape.read_text()
            else:
                assert result.status == "fail", result.evidence
                assert provider.statuses == [401] and provider.results == {}
            for text in (
                manifest_path.read_text(),
                repr(provider.script.deployed_agent),
                repr(credential),
            ):
                assert qa.bearer.token not in text
            assert qa.manifest.refused_egress == []
        assert qa.bearer.token not in caplog.text
        assert "offline-invalid-bearer" not in caplog.text
        assert any(record.name.startswith("anthropic") for record in caplog.records)
    finally:
        cleaned = await end_run(qa)
        assert cleaned.status == Manifest.load(manifest_path).status == "cleaned"
        assert qa.bearer.revoked_marker.exists()
        # The run's Daimon token is revoked; the shared schema stays for the next test.
        async with qa_shared_host.sessions() as session:
            revoked_at = await session.scalar(
                sql_text("SELECT revoked_at FROM mcp_tokens WHERE jti = :jti"),
                {"jti": uuid.UUID(str(cleaned.token_jti))},
            )
        assert revoked_at is not None


@pytest.mark.parametrize("field", ["tenant_id", "account_id", "authorization_id"])
async def test_f2_foreign_scope_cannot_resolve_or_write_the_mcp_credential(field: str) -> None:
    writes: list[str] = []
    resolutions: list[str] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        writes.append(request.url.path)
        raise AssertionError("foreign credential reached the native provider")

    def resolve(scope: Scope, reference: str) -> str:
        resolutions.append(reference)
        raise AssertionError("foreign scope reached the host secret store")

    async with AsyncAnthropic(
        api_key="offline-fixture",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
    ) as client:
        vaults = _driver(client, secrets=resolve).extension(
            Vaults, namespace="anthropic.vaults", version=1
        )
        vault = ResourceRef(
            id=VAULT_ID,
            kind="vault",
            provider="anthropic",
            account_scope_id="offline",
            tenant_id=SCOPE.tenant_id,
            account_id=SCOPE.account_id,
        )
        with pytest.raises(ScopeViolation):
            await vaults.put(
                SCOPE.model_copy(update={field: "foreign"}),
                vault,
                CredentialBinding(
                    name="daimon-mcp",
                    kind="static_bearer",
                    credential_ref=SECRET_REF,
                    mcp_server_url=MCP_URL,
                ),
                expected=None,
                key="foreign-write",
            )
    assert resolutions == [] and writes == []
