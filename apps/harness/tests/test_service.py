from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import httpx
import pytest
from daimon_harness.journal import Journal
from daimon_harness.service import create_app
from mux.drivers.selfhosted.wire import Event, TurnRequest

from .conftest import FakeModelAdapter, turn


def stream_events(response: httpx.Response) -> list[Event]:
    response.raise_for_status()
    assert response.headers["content-type"].startswith("text/event-stream")
    return [
        Event.model_validate_json(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


async def test_health_and_auth(harness: tuple[httpx.AsyncClient, FakeModelAdapter, Path]) -> None:
    client, _, _ = harness
    assert (await client.get("/v0/healthz")).json()["protocol"] == "daimon-harness/0"
    assert (
        await client.get("/v0/healthz", headers={"Authorization": "Bearer wrong"})
    ).status_code == 401
    assert (await client.post("/v0/turns", json={"bad": True})).status_code == 422


async def test_tool_stream_replay_history_and_conflicts(
    harness: tuple[httpx.AsyncClient, FakeModelAdapter, Path],
) -> None:
    client, model, home = harness
    first = stream_events(await client.post("/v0/turns", json=turn("tool")))
    assert [e.type for e in first] == [
        "status_running",
        "model_request_started",
        "usage",
        "tool_use",
        "tool_result",
        "agent_message_delta",
        "agent_message",
        "turn_ended",
    ]
    assert (
        home / "sessions" / hashlib.sha256(b"session-1").hexdigest()[:32] / "work/result.txt"
    ).read_text() == "durable"
    assert len([e for e in first if e.type == "usage"]) == 1
    replay = stream_events(await client.post("/v0/turns", json=turn("tool")))
    assert replay == first
    assert model.calls == 1
    assert (await client.post("/v0/turns", json=turn("different"))).status_code == 409
    assert (await client.post("/v0/turns", json=turn("tool", key="different"))).status_code == 409
    second = stream_events(await client.post("/v0/turns", json=turn(turn_id="turn-2", key="key-2")))
    assert model.histories[1] == [
        {"role": "user", "content": "tool"},
        {"role": "assistant", "content": "done"},
    ]
    assert second[0].seq > first[-1].seq
    reconciled = (
        await client.get(f"/v0/sessions/session-1/events?after_seq={first[2].seq}")
    ).json()
    assert reconciled["complete"] is True
    assert [e["seq"] for e in reconciled["events"]] == list(
        range(first[2].seq + 1, second[-1].seq + 1)
    )


async def test_cancel_and_active_session_conflict(
    harness: tuple[httpx.AsyncClient, FakeModelAdapter, Path],
) -> None:
    client, model, _ = harness
    stream = asyncio.create_task(client.post("/v0/turns", json=turn("wait")))
    await asyncio.wait_for(model.started.wait(), 5)
    assert (
        await client.post("/v0/turns", json=turn(turn_id="turn-2", key="key-2"))
    ).status_code == 409
    assert (await client.get("/v0/sessions/session-1/events")).json()["complete"] is False
    assert (await client.post("/v0/turns/turn-1/cancel")).json() == {"cancelled": True}
    events = stream_events(await stream)
    assert events[-1].data["outcome"] == "interrupted"
    assert sum(e.type == "turn_ended" for e in events) == 1
    assert (await client.post("/v0/turns/turn-1/cancel")).json() == {"cancelled": False}


@pytest.mark.parametrize(
    "decision,result",
    [("allow", None), ("deny", None), ("result", {"external": "result"}), ("result", None)],
)
async def test_approval(
    harness: tuple[httpx.AsyncClient, FakeModelAdapter, Path],
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
    result: object,
) -> None:
    client, _, home = harness
    body = turn("approval")
    body["tool_policy"] = {"write": "ask"}
    stream = asyncio.create_task(client.post("/v0/turns", json=body))
    async with asyncio.timeout(5):
        while True:
            response = (await client.get("/v0/sessions/session-1/events")).json()
            pending = [e for e in response["events"] if e["type"] == "requires_action"]
            if pending:
                break
            await asyncio.sleep(0.01)
    assert not (
        home / "sessions" / hashlib.sha256(b"session-1").hexdigest()[:32] / "work/result.txt"
    ).exists()
    body = {"action_id": pending[0]["data"]["action_id"], "decision": decision}
    if decision == "result":
        body["result"] = result
    assert (await client.post("/v0/turns/turn-1/actions", json=body)).status_code == 200
    events = stream_events(await stream)
    assert events[-1].data["outcome"] == "completed"
    assert (
        home / "sessions" / hashlib.sha256(b"session-1").hexdigest()[:32] / "work/result.txt"
    ).exists() == (decision == "allow" and result is None)
    tool_result = next(e for e in events if e.type == "tool_result")
    assert tool_result.data["is_error"] == (decision == "deny")
    if decision == "result":
        assert tool_result.data["output"] == result


@pytest.mark.parametrize("mode", ["error", "unmetered"])
async def test_failure_never_claims_completion(
    harness: tuple[httpx.AsyncClient, FakeModelAdapter, Path], mode: str
) -> None:
    client, _, _ = harness
    response = await client.post("/v0/turns", json=turn(mode))
    events = stream_events(response)
    assert events[-1].data["outcome"] == "errored"
    assert "secret provider message" not in response.text
    if mode == "unmetered":
        assert events[-1].data["error"]["kind"] == "usage_missing"
        unavailable = [
            event
            for event in events
            if event.type == "error" and event.data["kind"] == "usage_unavailable"
        ]
        assert len(unavailable) == 1
        assert unavailable[0].data["request_id"] == "req-1"


async def test_restart_durability_and_no_rerun(tmp_path: Path) -> None:
    journal = Journal(tmp_path)
    request = TurnRequest.model_validate(turn())
    assert journal.claim(request)
    journal.append(request, "status_running", {})
    journal.append(
        request, "model_request_started", {"request_id": "pending-request", "model": "gpt-5"}
    )
    journal.save_history(request.session_id, [{"role": "user", "content": "persisted"}])
    journal.close()
    model = FakeModelAdapter()
    app = create_app(home=tmp_path, token="test", adapter=model)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer test"},
        ) as client,
    ):
        events = stream_events(await client.post("/v0/turns", json=turn()))
        assert events[-1].data["outcome"] == "interrupted"
        assert model.calls == 0
        journal2 = Journal(tmp_path)
        assert journal2.history("session-1") == [{"role": "user", "content": "persisted"}]
        journal2.close()


async def test_invocation_json_accept_poll_cancel_and_validation(
    harness: tuple[httpx.AsyncClient, FakeModelAdapter, Path],
) -> None:
    client, model, _ = harness
    accepted = await client.post(
        "/invocations",
        headers={"Accept": "application/json"},
        json={"c3": {"method": "POST", "path": "/v0/turns", "query": {}, "body": turn("wait")}},
    )
    assert accepted.status_code == 202
    assert accepted.json() == {"accepted": True, "turn_id": "turn-1"}
    await asyncio.wait_for(model.started.wait(), 5)
    replay = await client.post(
        "/invocations",
        json={
            "c3": {
                "method": "GET",
                "path": "/v0/sessions/session-1/events",
                "query": {"after_seq": 0},
                "body": None,
            }
        },
    )
    assert replay.json()["complete"] is False
    assert any(e["type"] == "usage" for e in replay.json()["events"])
    cancelled = await client.post(
        "/invocations",
        json={"c3": {"method": "POST", "path": "/v0/turns/turn-1/cancel", "body": None}},
    )
    assert cancelled.json() == {"cancelled": True}
    final = (await client.get("/v0/sessions/session-1/events")).json()
    assert final["complete"] is True
    assert final["events"][-1]["data"]["outcome"] == "interrupted"
    for bad in [
        {"method": "GET", "path": "/v0/sessions/session-1/events", "query": {"after_seq": -1}},
        {"method": "GET", "path": "/v0/healthz", "body": {}},
        {"method": "GET", "path": "/v0/healthz", "query": {"unexpected": 1}},
        {"method": "POST", "path": "/v0/turns", "body": {"bad": True}},
    ]:
        assert (await client.post("/invocations", json={"c3": bad})).status_code == 422
    assert (
        await client.post("/invocations", json={"c3": {"method": "POST", "path": "/invocations"}})
    ).status_code == 404
    assert (await client.get("/readiness", headers={"Authorization": ""})).json() == {"ready": True}


async def test_invocation_sse_and_direct_json_acceptance(
    harness: tuple[httpx.AsyncClient, FakeModelAdapter, Path],
) -> None:
    client, model, _ = harness
    events = stream_events(
        await client.post(
            "/invocations", json={"c3": {"method": "POST", "path": "/v0/turns", "body": turn()}}
        )
    )
    assert events[-1].data["outcome"] == "completed"
    response = await client.post("/v0/turns", headers={"Accept": "application/json"}, json=turn())
    assert response.status_code == 202
    assert model.calls == 1


async def test_deadline_interrupts_background_turn(
    harness: tuple[httpx.AsyncClient, FakeModelAdapter, Path],
) -> None:
    client, _, _ = harness
    body = turn("wait")
    body["limits"] = {"max_tool_calls": 1, "deadline_s": 0.05}
    response = await client.post("/v0/turns", json=body, headers={"Accept": "application/json"})
    assert response.status_code == 202
    async with asyncio.timeout(5):
        while True:
            replay = (await client.get("/v0/sessions/session-1/events")).json()
            if replay["complete"]:
                break
            await asyncio.sleep(0.01)
    assert replay["events"][-1]["data"]["outcome"] == "interrupted"


async def test_foundry_invocation_logs_authorized_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from daimon_harness import service

    monkeypatch.setenv("HARNESS_AUTH_MODE", "foundry_gateway")
    monkeypatch.setenv("HARNESS_ALLOWED_CALLERS", "host-identity")
    for name in ("FOUNDRY_PROJECT_ENDPOINT", "FOUNDRY_AGENT_NAME", "FOUNDRY_AGENT_SESSION_ID"):
        monkeypatch.setenv(name, "platform-value")
    logged: list[str] = []
    monkeypatch.setattr(service.audit_logger, "info", logged.append)
    app = create_app(home=tmp_path, adapter=FakeModelAdapter())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        body = {"c3": {"method": "POST", "path": "/v0/turns", "body": turn()}}
        assert (await client.post("/invocations", json=body)).status_code == 401
        response = await client.post(
            "/invocations",
            headers={
                "x-agent-user-id": "host-identity",
                "x-agent-foundry-call-id": "call-1",
                "Accept": "application/json",
            },
            json=body,
        )
        assert response.status_code == 202
        assert json.loads(logged[0]) == {
            "event": "harness.turn_authorized",
            "caller": "host-identity",
            "session_id": "session-1",
            "turn_id": "turn-1",
        }
        assert (
            await client.get(
                "/v0/healthz",
                headers={
                    "x-agent-user-id": "another-identity",
                    "x-agent-foundry-call-id": "call-2",
                },
            )
        ).status_code == 403
