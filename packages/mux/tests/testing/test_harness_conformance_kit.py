"""Kit internals, with no harness: SSE parsing, the tiny MCP server, reporting and verdicts."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from mux.testing.fake_model_server import FakeModelServer
from mux.testing.harness_conformance import (
    CASES,
    Case,
    CaseResult,
    HarnessTarget,
    SkipCase,
    format_report,
    parse_sse,
    run_conformance,
)
from mux.testing.harness_conformance.mcp_server import TinyMCPServer

EVENT = {
    "seq": 4,
    "session_id": "s",
    "turn_id": "t",
    "at": "2026-01-01T00:00:00Z",
    "type": "status_running",
    "data": {},
}


def frame(event: dict[str, Any], *, event_id: str | None = None, name: str | None = None) -> str:
    return f"id: {event_id or event['seq']}\nevent: {name or event['type']}\ndata: {json.dumps(event)}\n\n"


def test_parse_sse_reads_validated_events_and_skips_comments() -> None:
    later = {**EVENT, "seq": 5}
    events = parse_sse(": keepalive\n\n" + frame(EVENT) + frame(later))
    assert [event.seq for event in events] == [4, 5]


@pytest.mark.parametrize(
    "body", [frame(EVENT, event_id="9"), frame(EVENT, name="usage")], ids=["id", "name"]
)
def test_parse_sse_rejects_inconsistent_framing(body: str) -> None:
    with pytest.raises(AssertionError):
        parse_sse(body)


async def rpc(client: httpx.AsyncClient, method: str, params: dict[str, Any] | None = None) -> Any:
    response = await client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    )
    assert response.headers["content-type"] == "application/json"
    return response.json()["result"]


async def test_tiny_mcp_server_speaks_enough_streamable_http() -> None:
    server = TinyMCPServer()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server), base_url="http://mcp"
    ) as client:
        init = await rpc(client, "initialize", {"protocolVersion": "2025-06-18"})
        assert init["protocolVersion"] == "2025-06-18"
        notified = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        assert notified.status_code == 202
        assert [tool["name"] for tool in (await rpc(client, "tools/list"))["tools"]] == ["echo"]
        called = await rpc(client, "tools/call", {"name": "echo", "arguments": {"text": "hi"}})
        assert called == {"content": [{"type": "text", "text": "hi"}], "isError": False}
        assert (await client.get("/mcp")).status_code == 405
    assert [call["arguments"] for call in server.calls] == [{"text": "hi"}]


Handler = Callable[[httpx.Request], httpx.Response]


def transport_for(handler: Handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def target_with(handler: Handler, **overrides: Any) -> HarnessTarget:
    return HarnessTarget(
        base_url="http://harness",
        token="secret",
        model=FakeModelServer(),
        http_transport=transport_for(handler),
        **overrides,
    )


def health_handler(*, enforce_auth: bool) -> Handler:
    def handle(request: httpx.Request) -> httpx.Response:
        authorized = request.headers.get("authorization") == "Bearer secret"
        if enforce_auth and not authorized:
            return httpx.Response(401)
        return httpx.Response(
            200, json={"ok": True, "protocol": "daimon-harness/0", "version": "t"}
        )

    return handle


async def test_health_case_passes_a_compliant_target_and_fails_a_lax_one() -> None:
    [good] = await run_conformance(
        target_with(health_handler(enforce_auth=True)), only=["health_and_auth"]
    )
    [lax] = await run_conformance(
        target_with(health_handler(enforce_auth=False)), only=["health_and_auth"]
    )
    assert good.status == "pass"
    assert lax.status == "fail" and "unauthenticated" in lax.detail


async def test_restart_case_is_skipped_with_a_reason_when_no_hook_is_given() -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a skipped case must not call the harness")

    [result] = await run_conformance(target_with(unreachable), only=["restart_without_rerun"])
    assert result.status == "skip" and "restart hook" in result.detail


async def test_runner_reports_pass_skip_and_fail_and_keeps_going() -> None:
    async def passes(ctx: object) -> None:
        return None

    async def skips(ctx: object) -> None:
        raise SkipCase("not offered")

    async def fails(ctx: object) -> None:
        raise AssertionError("broken")

    async def crashes(ctx: object) -> None:
        raise RuntimeError("unexpected")

    cases = [
        Case(name, run)  # type: ignore[arg-type]
        for name, run in [("a", passes), ("b", skips), ("c", fails), ("d", crashes), ("e", passes)]
    ]
    results = await run_conformance(target_with(health_handler(enforce_auth=True)), cases=cases)
    assert [(r.name, r.status) for r in results] == [
        ("a", "pass"),
        ("b", "skip"),
        ("c", "fail"),
        ("d", "fail"),
        ("e", "pass"),
    ]
    assert results[1].detail == "not offered" and results[3].detail == "RuntimeError: unexpected"


async def test_unknown_case_name_is_an_error() -> None:
    with pytest.raises(ValueError, match="nope"):
        await run_conformance(target_with(health_handler(enforce_auth=True)), only=["nope"])


def test_report_lists_every_case_with_its_verdict() -> None:
    report = format_report(
        [
            CaseResult("a", "pass"),
            CaseResult("b", "skip", "no hook"),
            CaseResult("c", "fail", "why"),
        ]
    )
    assert report.splitlines() == [
        "PASS  a",
        "SKIP  b  (no hook)",
        "FAIL  c  (why)",
        "1 passed, 1 skipped, 1 failed",
    ]


def test_case_names_are_unique() -> None:
    names = [case.name for case in CASES]
    assert len(names) == len(set(names))


async def test_target_maps_builtin_and_mcp_tool_names() -> None:
    from mux.testing.harness_conformance.target import CaseContext

    target = target_with(
        health_handler(enforce_auth=True), tool_name=lambda name: f"harness_{name}"
    )
    context = CaseContext(target, None, "x")  # type: ignore[arg-type]
    assert context.tool("write") == "harness_write"
    assert target.mcp_tool_name("srv", "echo") == "srv__echo"
