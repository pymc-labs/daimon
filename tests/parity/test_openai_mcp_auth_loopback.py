"""Scripted OpenAI SDK authentication composed with N2's actual loopback MCP host.

This proves host authentication/discovery/read effects, not model execution or
service reachability. Only the provider boundary maps one fictional HTTPS URL
to the isolated loopback socket; production destination validation is unchanged.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx
import pytest
from mux.conformance.default_capability import MCP_TOOLS
from mux.contracts.ids import ModelRef, Revision, Scope
from mux.contracts.resources import AgentSpec, MCPConnection, SessionSpec, ToolSpec
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai._common import objects, text
from mux.drivers.openai.transport import Object, SDKTransport, object_json
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ProviderError
from openai import AsyncOpenAI

URL = "https://qa.example.com/mcp"


def spec() -> AgentSpec:
    return AgentSpec(
        name="fixture",
        model=ModelRef(provider="openai", id="gpt-6-luna"),
        tools=(ToolSpec(name="web_search", kind="builtin"),),
        mcp_servers=(
            MCPConnection(
                name="daimon-mcp",
                url=URL,
                credential_ref="host:owned-token",
                tool_policy={"allowed_tools": list(MCP_TOOLS), "required": True},
            ),
        ),
    )


@pytest.fixture(autouse=True)
def offline_openai_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("OPENAI_"):
            monkeypatch.delenv(name)


async def test_sdk_mcp_auth_uses_the_real_loopback_host_and_revocation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from daimon.testing.asgi import INIT_BODY, INIT_HEADERS, parse_jsonrpc_response
    from daimon.testing.db import build_test_engine
    from daimon.testing.qa_mcp_host import (
        ALLOWED_TOOLS,
        QA_SESSION_ID,
        build_host,
        cleanup,
        schema_exists,
        serve,
    )

    database_url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    assert database_url, "The parity CI/lane must provide its isolated test database"
    qa = await build_host(database_url=database_url, port=0, root=tmp_path)
    manifest_path = qa.manifest.path(tmp_path)
    scope = Scope(
        tenant_id=qa.manifest.tenant_id,
        account_id="qa-account",
        principal_id="qa-user",
        authorization_id="qa-mcp-grant",
    )
    caplog.set_level(logging.DEBUG, logger="openai")
    saved: Object = {}
    posted: list[Object] = []
    resolutions: list[tuple[Scope, str, str]] = []
    outcomes: list[tuple[str, Object]] = []
    token = qa.bearer.token
    try:
        async with serve(qa) as loopback_url, httpx.AsyncClient(timeout=30) as mcp:
            assert loopback_url.startswith("http://127.0.0.1:")

            async def resolve(authorized: Scope, ref: str, destination: str) -> str:
                assert (authorized, ref, destination) == (scope, "host:owned-token", URL)
                resolutions.append((authorized, ref, destination))
                return token

            async def handle(request: httpx.Request) -> httpx.Response:
                nonlocal saved
                assert request.url.host == "openai.invalid"
                if (request.method, request.url.path) == ("POST", "/v1/agents"):
                    saved = object_json(json.loads(request.content))
                    return httpx.Response(200, json={"id": "a", "created_at": 0, **saved})
                if (request.method, request.url.path) == ("GET", "/v1/agents/a"):
                    return httpx.Response(200, json={"id": "a", "created_at": 0, **saved})
                assert (request.method, request.url.path) == ("POST", "/v1/agents/sessions")
                body = object_json(json.loads(request.content))
                posted.append(body)
                server = objects(object_json(body["agent"])["tools"])[-1]
                transport = object_json(server["transport"])
                assert server["allowed_tools"] == list(MCP_TOOLS)
                assert set(MCP_TOOLS) == set(ALLOWED_TOOLS)
                assert server["required"] is True and server["connection_origin"] == "service"
                assert transport["server_url"] == URL
                # Only this fictional HTTPS destination is mapped by the scripted
                # provider to the actual loopback gate. The driver has no bypass.
                headers = {**INIT_HEADERS, "Authorization": str(transport["authorization"])}
                response = await mcp.post(loopback_url, json=dict(INIT_BODY), headers=headers)
                if response.status_code == 401:
                    # Even an upstream error echoing the synthetic credential
                    # must be removed by the real SDK/driver error boundary.
                    return httpx.Response(403, json={"error": {"message": token}})
                assert response.status_code == 200
                assert "error" not in parse_jsonrpc_response(response)
                for index, name in enumerate(("tools/list", *ALLOWED_TOOLS), start=2):
                    rpc: Object = {"jsonrpc": "2.0", "id": index}
                    if name == "tools/list":
                        rpc["method"] = name
                    else:
                        rpc.update(
                            {"method": "tools/call", "params": {"name": name, "arguments": {}}}
                        )
                    response = await mcp.post(loopback_url, json=rpc, headers=headers)
                    assert response.status_code == 200
                    result = object_json(parse_jsonrpc_response(response))
                    assert "error" not in result
                    outcomes.append((name, result))
                native: Object = {
                    "id": "s",
                    "agent": {"id": "a", "model": "gpt-6-luna"},
                    "created_at": 0,
                    "environment": {"type": "openai_hosted", "id": "env"},
                    "status": "idle",
                    "required_actions": [],
                    "metadata": body["metadata"],
                }
                return httpx.Response(200, json=native)

            async with AsyncOpenAI(
                api_key="offline-placeholder",
                base_url="https://openai.invalid/v1",
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
            ) as sdk:
                driver = OpenAIDriver(
                    SDKTransport(sdk),
                    account_scope_id="project",
                    authorization=lambda authorized, _kind, _id: authorized == scope,
                    journal=MemoryRecoveryJournal(),
                    usage_revisions=MemoryUsageRevisions(),
                    mcp_secrets=resolve,
                )
                agent = await driver.agents.create(scope, spec(), key="loopback-agent")
                session_spec = SessionSpec(
                    agent=agent.ref, agent_revision=Revision(local=0), config_revision=1
                )
                session = await driver.sessions.create(scope, session_spec, key="loopback-session")
                assert session.ref.id == "s"
                assert token not in repr(agent) and token not in repr(session)
                assert token not in json.dumps(saved) and token not in manifest_path.read_text()
                listed = object_json(outcomes[0][1]["result"])
                assert sorted(text(t["name"]) for t in objects(listed["tools"])) == list(
                    ALLOWED_TOOLS
                )
                assert "qa-agent" in json.dumps(dict(outcomes)["describe_agent"])
                assert QA_SESSION_ID in json.dumps(dict(outcomes)["list_my_sessions"])
                assert resolutions == [(scope, "host:owned-token", URL)]
                assert len(posted) == 1
                # Removing/changing the serialized session bearer fails at the
                # same real gate, without replacing it with a test accept stub.
                for presented in (None, "fictional-wrong-bearer"):
                    headers = dict(INIT_HEADERS)
                    if presented is not None:
                        headers["Authorization"] = "Bearer " + presented
                    assert (
                        await mcp.post(loopback_url, json=dict(INIT_BODY), headers=headers)
                    ).status_code == 401
                qa.bearer.revoke()
                with pytest.raises(ProviderError) as refused:
                    await driver.sessions.create(scope, session_spec, key="revoked-session")
                assert refused.value.category == "permission"
                assert token not in str(refused.value) and token not in repr(refused.value)
                assert refused.value.__cause__ is None
                assert len(posted) == len(resolutions) == 2
            assert token not in caplog.text
            assert qa.manifest.refused_egress == []
    finally:
        await qa.engine.dispose()
        cleaned = await cleanup(manifest_path, database_url=database_url)
    assert cleaned.status == "cleaned"
    probe = build_test_engine(database_url, "public")
    try:
        assert not await schema_exists(probe, qa.manifest.schema)
    finally:
        await probe.dispose()
