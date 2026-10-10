"""The disposable QA MCP host: isolation refusals, the gate, and one real loopback run."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from daimon.testing.asgi import INIT_BODY, INIT_HEADERS, parse_jsonrpc_response
from daimon.testing.db import build_test_engine
from daimon.testing.qa_mcp_host import (
    ALLOWED_TOOLS,
    QA_SESSION_ID,
    Manifest,
    McpGate,
    QaHostError,
    RunBearer,
    build_host,
    check_database,
    check_loopback,
    cleanup,
    schema_exists,
    scrubbed_environment,
    serve,
)
from starlette.types import Message, Receive, Scope, Send

NOW = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.UTC)


# Isolation refusals


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://u:p@localhost:5432/daimon",
        "postgresql+asyncpg://u:p@db.example.com:5432/daimon_test",
        "postgresql+asyncpg://u:p@localhost:5432/production_latest",
        # Query parameters override the URL's own host, database and port.
        "postgresql+asyncpg://u:p@localhost:5432/daimon_test?host=db.example.com",
        "postgresql+asyncpg://u:p@localhost:5432/daimon_test?database=daimon",
        "postgresql+asyncpg://u:p@localhost:5432/daimon_test?port=6543",
        "postgresql+asyncpg://u:p@localhost:5432/daimon_test?host=/var/run/postgresql",
        "postgresql+asyncpg://u:p@/daimon_test",
        "not a url",
    ],
)
def test_only_a_local_disposable_database_is_accepted(url: str) -> None:
    with pytest.raises(QaHostError):
        check_database(url)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://u:p@127.0.0.1:5432/daimon_qa_run",
        "postgresql+asyncpg://u:p@localhost:5432/daimon_test",
        "postgresql+asyncpg://u:p@localhost/daimon_test_nc_n2",
    ],
)
def test_a_local_disposable_database_passes(url: str) -> None:
    check_database(url)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com"])
def test_the_origin_binds_loopback_only(host: str) -> None:
    with pytest.raises(QaHostError):
        check_loopback(host)
    check_loopback("127.0.0.1")


def test_provider_and_daimon_settings_are_hidden_while_building(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real")
    monkeypatch.setenv("DAIMON_MCP__PUBLIC_URL", "https://prod.example.com/mcp")
    with scrubbed_environment():
        assert "ANTHROPIC_API_KEY" not in os.environ
        assert "DAIMON_MCP__PUBLIC_URL" not in os.environ
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-real"


# The gate, against a recording app


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[bytes, bytes], bytes]] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        message: Message = await receive()
        self.calls.append((dict(scope["headers"]), message.get("body", b"")))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})


def _gate(tmp_path: Path, *, expires: dt.datetime = NOW + dt.timedelta(minutes=5)):
    recorder = _Recorder()
    bearer = RunBearer(token="run-bearer", expires_at=expires, revoked_marker=tmp_path / "revoked")
    gate = McpGate(recorder, bearer=bearer, daimon_token="daimon-token", clock=lambda: NOW)
    return gate, recorder, bearer


def _call(name: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name}}


async def _post(
    gate: McpGate,
    *,
    path: str = "/mcp",
    method: str = "POST",
    bearer: str | None = "run-bearer",
    body: object = None,
    raw: bytes | None = None,
) -> httpx.Response:
    headers = {"content-type": "application/json", "cookie": "sid=x"}
    if bearer is not None:
        headers["authorization"] = f"Bearer {bearer}"
    content = (
        raw
        if raw is not None
        else json.dumps(body or {"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
    )
    transport = httpx.ASGITransport(app=gate)  # pyright: ignore[reportArgumentType]
    async with httpx.AsyncClient(transport=transport, base_url="http://qa") as client:
        return await client.request(method, path, headers=headers, content=content)


async def test_the_gate_admits_only_post_mcp_with_the_live_run_bearer(tmp_path: Path) -> None:
    gate, recorder, bearer = _gate(tmp_path)
    for path in ("/healthz", "/web", "/uploads/x", "/billing/checkout", "/mcpx"):
        assert (await _post(gate, path=path)).status_code == 404
    assert (await _post(gate, method="GET")).status_code == 405
    for presented in (None, "", "wrong", "run-bearer-but-longer"):
        assert (await _post(gate, bearer=presented)).status_code == 401
    expired, _, _ = _gate(tmp_path / "e", expires=NOW)
    assert (await _post(expired)).status_code == 401
    assert recorder.calls == []

    allowed = await _post(gate, body=_call(ALLOWED_TOOLS[0]))
    assert allowed.status_code == 200
    ((headers, body),) = recorder.calls
    assert headers[b"authorization"] == b"Bearer daimon-token"
    assert b"cookie" not in headers
    assert json.loads(body)["params"]["name"] == ALLOWED_TOOLS[0]

    bearer.revoke()
    assert (await _post(gate)).status_code == 401
    assert len(recorder.calls) == 1


async def test_the_gate_refuses_other_tools_batches_and_large_bodies_before_dispatch(
    tmp_path: Path,
) -> None:
    gate, recorder, _ = _gate(tmp_path)
    for name in ("start_turn", "ask", "archive_my_session", "list_events", None):
        refused = await _post(gate, body=_call(name) if name else {"method": "tools/call"})
        assert refused.status_code == 403
    assert (
        await _post(gate, raw=b"[" + json.dumps(_call("describe_agent")).encode() + b"]")
    ).status_code == 403
    assert (await _post(gate, raw=b"not json")).status_code == 403
    assert (await _post(gate, raw=b" " * (256 * 1024 + 1))).status_code == 413
    assert recorder.calls == []


# One real run over a loopback socket


def _database_url() -> str:
    url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not url:
        pytest.skip("DAIMON_DATABASE__TEST_URL is not set")
    return url


async def _rpc(
    client: httpx.AsyncClient, url: str, bearer: str, body: dict[str, Any]
) -> dict[str, object]:
    response = await client.post(
        url, json=body, headers={**INIT_HEADERS, "Authorization": f"Bearer {bearer}"}
    )
    assert response.status_code == 200, response.text
    return parse_jsonrpc_response(response)


async def test_a_run_serves_two_read_tools_on_loopback_then_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = _database_url()
    # A production-looking ambient setting must not reach the host.
    monkeypatch.setenv("DAIMON_MCP__PUBLIC_URL", "https://prod.example.com/mcp")
    qa = await build_host(database_url=database_url, port=0, root=tmp_path)
    manifest_path = qa.manifest.path(tmp_path)
    try:
        assert qa.tool_names == tuple(sorted(ALLOWED_TOOLS))
        async with serve(qa) as url, httpx.AsyncClient(timeout=30) as client:
            assert url.startswith("http://127.0.0.1:")
            token = qa.bearer.token
            await _rpc(client, url, token, dict(INIT_BODY))
            listed = await _rpc(
                client, url, token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
            )
            names = sorted(t["name"] for t in listed["result"]["tools"])  # type: ignore[index]
            assert names == sorted(ALLOWED_TOOLS)

            described = await _rpc(
                client,
                url,
                token,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "describe_agent", "arguments": {}},
                },
            )
            assert "error" not in described, described
            assert "qa-agent" in json.dumps(described)

            sessions = await _rpc(
                client,
                url,
                token,
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "list_my_sessions", "arguments": {}},
                },
            )
            assert "error" not in sessions, sessions
            assert QA_SESSION_ID in json.dumps(sessions)

            refused = await client.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {"name": "start_turn", "arguments": {}},
                },
                headers={**INIT_HEADERS, "Authorization": f"Bearer {token}"},
            )
            assert refused.status_code == 403
            unauthenticated = await client.post(url, json=dict(INIT_BODY), headers=INIT_HEADERS)
            assert unauthenticated.status_code == 401
            # Another route of the Daimon app is not reachable through the gate.
            assert (await client.get(url.replace("/mcp", "/healthz"))).status_code == 404

            qa.bearer.revoke()
            revoked = await client.post(
                url,
                json=dict(INIT_BODY),
                headers={**INIT_HEADERS, "Authorization": f"Bearer {token}"},
            )
            assert revoked.status_code == 401
        assert qa.manifest.refused_egress == []
        assert qa.manifest.public_url_host == "127.0.0.1"
    finally:
        await qa.engine.dispose()
        cleaned = await cleanup(manifest_path, database_url=database_url)
    assert cleaned.status == "cleaned"
    probe = build_test_engine(database_url, "public")
    try:
        assert not await schema_exists(probe, qa.manifest.schema)
    finally:
        await probe.dispose()
    assert Manifest.load(manifest_path).status == "cleaned"
    again = await cleanup(manifest_path, database_url=database_url)
    assert again.status == "cleaned"


async def test_a_build_that_fails_after_creating_its_schema_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = _database_url()
    import daimon.adapters.mcp.server as server

    def broken(**_kwargs: object) -> object:
        raise RuntimeError("app build failed")

    monkeypatch.setattr(server, "create_mcp_app", broken)
    with pytest.raises(RuntimeError, match="app build failed"):
        await build_host(database_url=database_url, port=0, root=tmp_path, run_id="failedbuild")
    manifest = Manifest.load(tmp_path / "failedbuild" / "manifest.json")
    assert manifest.status == "cleaned"
    probe = build_test_engine(database_url, "public")
    try:
        assert not await schema_exists(probe, "qa_mcp_failedbuild")
    finally:
        await probe.dispose()


async def test_a_schema_create_that_fails_halfway_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = _database_url()
    from daimon.testing import qa_mcp_host

    real = getattr(qa_mcp_host, "_create_schema_with_tables")  # noqa: B009 - a private name

    async def create_then_fail(engine: Any, schema: str) -> None:
        await real(engine, schema)
        raise RuntimeError("create_all failed halfway")

    monkeypatch.setattr(qa_mcp_host, "_create_schema_with_tables", create_then_fail)
    with pytest.raises(RuntimeError, match="halfway"):
        await build_host(database_url=database_url, port=0, root=tmp_path, run_id="halfway")
    assert Manifest.load(tmp_path / "halfway" / "manifest.json").status == "cleaned"
    probe = build_test_engine(database_url, "public")
    try:
        assert not await schema_exists(probe, "qa_mcp_halfway")
    finally:
        await probe.dispose()


@pytest.mark.parametrize("schema", ["public", "qa_mcp_another_run", "test_w1_abc"])
async def test_cleanup_refuses_a_manifest_naming_any_other_schema(
    tmp_path: Path, schema: str
) -> None:
    database_url = _database_url()
    manifest = Manifest(
        run_id="edited",
        created_at=NOW.isoformat(),
        expires_at=NOW.isoformat(),
        host="127.0.0.1",
        port=1,
        database="postgresql+asyncpg://localhost/whatever",
        schema=schema,
        tenant_id=str(NOW.timestamp()),
    )
    path = manifest.save(tmp_path)
    with pytest.raises(QaHostError, match="not this run's own"):
        await cleanup(path, database_url=database_url)
    probe = build_test_engine(database_url, "public")
    try:
        assert await schema_exists(probe, "public")
    finally:
        await probe.dispose()


def test_this_host_has_no_tunnel_and_refuses_a_manifest_that_names_one(tmp_path: Path) -> None:
    from daimon.testing import qa_mcp_host

    for name in ("open_tunnel", "spawn_tunnel", "tunnel_command"):
        assert not hasattr(qa_mcp_host, name)
    path = tmp_path / "old" / "manifest.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "run_id": "old",
                "created_at": NOW.isoformat(),
                "expires_at": NOW.isoformat(),
                "host": "127.0.0.1",
                "port": 1,
                "database": "postgresql+asyncpg://localhost/daimon_test",
                "schema": "qa_mcp_old",
                "tenant_id": "t",
                "tunnel_pid": 4242,
            }
        )
    )
    with pytest.raises(QaHostError, match="does not own"):
        Manifest.load(path)
