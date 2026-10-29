"""HTTP-level MCP fixture server contract: catalogue, bearer, execution evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from daimon.testing import preview_parity_mcp
from daimon.testing.asgi import INIT_BODY, INIT_HEADERS, asgi_lifespan
from daimon.testing.mcp_fixture_server import build_fixture_server, create_app

TOKEN = "fixture-bearer-" + "x" * 32


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic " + TOKEN])
async def test_auth_protects_mcp_and_readback(tmp_path: Path, authorization: str | None) -> None:
    path = tmp_path / "calls.jsonl"
    app = build_fixture_server(token=TOKEN, journal_path=path)
    headers = {"Authorization": authorization} if authorization else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qa") as c:
        for method, url in [("POST", "/mcp"), ("GET", "/calls")]:
            response = await c.request(method, url, headers=headers, json=INIT_BODY)
            assert response.status_code == 401
    assert path.read_text() == ""


async def test_catalogue_execution_and_persistent_readback(tmp_path: Path) -> None:
    path = tmp_path / "calls.jsonl"
    app = build_fixture_server(token=TOKEN, journal_path=path)
    headers = {**INIT_HEADERS, "Authorization": f"Bearer {TOKEN}"}
    async with (
        asgi_lifespan(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://qa", headers=headers
        ) as c,
    ):

        async def rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            response = await c.post(
                "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params or {}}
            )
            assert response.status_code == 200, response.text
            return response.json()

        assert (await c.post("/mcp", json=INIT_BODY)).status_code == 200
        listed = {t["name"]: t for t in (await rpc("tools/list"))["result"]["tools"]}
        assert set(listed) == {
            "lead_fixture_read",
            "lead_fixture_submit",
            "fixture_counter_read",
            "fixture_mislabeled_write",
        }
        tools = {name: listed[name] for name in ("lead_fixture_read", "lead_fixture_submit")}
        assert tools["lead_fixture_read"]["annotations"]["readOnlyHint"] is True
        assert tools["lead_fixture_submit"]["annotations"]["readOnlyHint"] is False
        assert tools["lead_fixture_submit"]["annotations"]["destructiveHint"] is False
        assert (await c.get("/calls")).json() == {"calls": [], "write_count": 0}
        for name in tools:
            result = await rpc(
                "tools/call", {"name": name, "arguments": {"marker": "fixture-marker"}}
            )
            assert not result["result"].get("isError")
        # Invalid arguments and unknown tools must never count as executed writes.
        result = await rpc("tools/call", {"name": "lead_fixture_submit", "arguments": {}})
        assert result["result"]["isError"]
        response = await c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "real_write", "arguments": {"marker": "refused"}},
            },
        )
        assert response.status_code == 403
        evidence = (await c.get("/calls")).json()
        assert evidence["write_count"] == 1
        assert len(evidence["calls"]) == 6
        executed = [e for e in evidence["calls"] if e["phase"] == "executed"]
        assert [e["tool"] for e in executed] == list(tools)
        assert all(e["args"] == {"marker": "fixture-marker"} for e in executed)
        assert all(e["caller"] == "mcp-fixture" for e in evidence["calls"])
        assert TOKEN not in path.read_text()
    restarted = build_fixture_server(token=TOKEN, journal_path=path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=restarted), base_url="http://qa", headers=headers
    ) as c:
        assert (await c.get("/calls")).json() == evidence


def test_factory_refuses_unless_enabled_and_missing_or_weak_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAIMON_MCP_FIXTURE_CALL_LOG", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("DAIMON_MCP_FIXTURE_TOKEN", TOKEN)
    for enabled in (None, "false", "1"):
        if enabled is None:
            monkeypatch.delenv("DAIMON_MCP_FIXTURE_ENABLED", raising=False)
        else:
            monkeypatch.setenv("DAIMON_MCP_FIXTURE_ENABLED", enabled)
        with pytest.raises(ValueError, match="DAIMON_MCP_FIXTURE_ENABLED"):
            create_app()
    monkeypatch.setenv("DAIMON_MCP_FIXTURE_ENABLED", "true")
    monkeypatch.delenv("DAIMON_MCP_FIXTURE_TOKEN")
    with pytest.raises(KeyError):
        create_app()
    for token in ("", "short", TOKEN + "\n"):
        monkeypatch.setenv("DAIMON_MCP_FIXTURE_TOKEN", token)
        with pytest.raises(ValueError, match="bearer"):
            create_app()
    monkeypatch.setenv("DAIMON_MCP_FIXTURE_TOKEN", TOKEN)
    assert create_app()


def test_old_entry_point_keeps_its_variables_and_refuses_other_lanes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = preview_parity_mcp.create_app
    monkeypatch.setenv("DAIMON_PARITY_CALL_LOG", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("DAIMON_PARITY_LANE", "staging")
    with pytest.raises(ValueError, match="only run in the preview lane"):
        old()
    monkeypatch.setenv("DAIMON_PARITY_LANE", "preview")
    monkeypatch.delenv("DAIMON_PREVIEW_PARITY_MCP_TOKEN", raising=False)
    with pytest.raises(KeyError):
        old()
    for token in ("", "short", TOKEN + "\n"):
        monkeypatch.setenv("DAIMON_PREVIEW_PARITY_MCP_TOKEN", token)
        with pytest.raises(ValueError, match="bearer"):
            old()
    monkeypatch.setenv("DAIMON_PREVIEW_PARITY_MCP_TOKEN", TOKEN)
    assert old()


async def test_counter_reads_submissions_and_mislabeled_write_counts(tmp_path: Path) -> None:
    path = tmp_path / "calls.jsonl"
    app = build_fixture_server(token=TOKEN, journal_path=path)
    headers = {**INIT_HEADERS, "Authorization": f"Bearer {TOKEN}"}
    async with (
        asgi_lifespan(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://qa", headers=headers
        ) as c,
    ):

        async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            response = await c.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            assert response.status_code == 200, response.text
            return response.json()["result"]

        assert (await c.post("/mcp", json=INIT_BODY)).status_code == 200
        listed = (
            await c.post("/mcp", json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
        ).json()["result"]["tools"]
        hints = {tool["name"]: tool["annotations"] for tool in listed}
        # The mislabeled write claims to only read, exactly like the honest counter.
        assert hints["fixture_mislabeled_write"]["readOnlyHint"] is True
        assert hints["fixture_counter_read"]["readOnlyHint"] is True
        assert (await call("fixture_counter_read", {}))["structuredContent"] == {"markers": []}
        await call("lead_fixture_submit", {"marker": "m-1"})
        # A read never changes what the next read returns: no self-trigger.
        for _ in range(2):
            result = await call("fixture_counter_read", {})
            assert result["structuredContent"] == {"markers": ["m-1"]}
        await call("fixture_mislabeled_write", {"marker": "m-2"})
        evidence = (await c.get("/calls")).json()
        assert evidence["write_count"] == 2
        assert (await call("fixture_counter_read", {}))["structuredContent"] == {"markers": ["m-1"]}
