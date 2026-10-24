"""A gateway fixture routes unchanged C3 requests to independent single-session apps."""

import asyncio

import httpx
import pytest
from daimon_harness.service import create_app
from mux.drivers.selfhosted.transport import DirectTransport, sandbox_id
from mux.errors import ProviderError

from .conftest import FakeModelAdapter, turn


async def test_pool_routes_two_single_session_harnesses_and_replays_after_reconstruction(tmp_path):
    models = {name: FakeModelAdapter() for name in ("a", "b")}
    apps = {
        sandbox_id(name): create_app(home=tmp_path / name, token="pool-token", adapter=model)
        for name, model in models.items()
    }
    routed = []

    async def gateway(request):
        identifier = request.url.params["identifier"]
        routed.append((identifier, request.method, request.url.path))
        app = apps[identifier]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://harness"
        ) as downstream:
            return await downstream.request(
                request.method,
                request.url.path.removeprefix("/pool"),
                params=[
                    (key, value)
                    for key, value in request.url.params.multi_items()
                    if key != "identifier"
                ],
                headers=request.headers,
                content=request.content,
            )

    async def token():
        return "pool-token"

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        transport = DirectTransport(
            client, base_url="https://pool.example/pool", token_provider=token, session_pool=True
        )
        try:
            for name in models:
                body = turn("tool", turn_id="tool-" + name, key="tool-" + name) | {
                    "session_id": name
                }
                await transport.request(
                    "POST", "/v0/turns", after_seq=0, session_id=name, body=body
                )
                supervisor = apps[sandbox_id(name)].state.supervisor
                await supervisor.tasks[body["turn_id"]]
                workspace, _ = supervisor.journal.sandbox(name)
                assert (workspace / "work/result.txt").read_text() == "durable"
                with pytest.raises(ProviderError) as refused:
                    await transport.request(
                        "POST",
                        "/v0/turns",
                        after_seq=0,
                        session_id=name,
                        body=turn() | {"session_id": "foreign"},
                    )
                assert refused.value.category == "conflict"
                # A new transport resumes the same sandbox without re-execution.
                reconstructed = DirectTransport(
                    client,
                    base_url="https://pool.example/pool",
                    token_provider=token,
                    session_pool=True,
                )
                await reconstructed.request(
                    "POST", "/v0/turns", after_seq=0, session_id=name, body=body
                )
                assert models[name].calls == 1
                events = await reconstructed.request(
                    "GET", f"/v0/sessions/{name}/events?after_seq=0", after_seq=0, session_id=name
                )
                assert events["complete"] is True
                assert events["events"][-1]["turn_id"] == body["turn_id"]

            name = "a"
            supervisor = apps[sandbox_id(name)].state.supervisor
            body = turn("approval", turn_id="ask", key="ask") | {
                "session_id": name,
                "tool_policy": {"write": "ask"},
            }
            await transport.request("POST", "/v0/turns", after_seq=0, session_id=name, body=body)
            async with asyncio.timeout(5):
                while True:
                    result = await transport.request(
                        "GET", "/v0/sessions/a/events?after_seq=0", after_seq=0, session_id=name
                    )
                    approvals = [
                        event for event in result["events"] if event["type"] == "requires_action"
                    ]
                    if approvals:
                        break
                    await asyncio.sleep(0.01)
            await transport.request(
                "POST",
                "/v0/turns/ask/actions",
                after_seq=0,
                session_id=name,
                body={"action_id": approvals[-1]["data"]["action_id"], "decision": "allow"},
            )
            await supervisor.tasks["ask"]
            models[name].started.clear()
            body = turn("wait", turn_id="cancel", key="cancel") | {"session_id": name}
            await transport.request("POST", "/v0/turns", after_seq=0, session_id=name, body=body)
            await asyncio.wait_for(models[name].started.wait(), 5)
            assert await transport.request(
                "POST", "/v0/turns/cancel/cancel", after_seq=0, session_id=name, body={}
            ) == {"cancelled": True}
            assert supervisor.journal.events(name)[-1].data["outcome"] == "interrupted"
            assert models["b"].calls == 1
            assert {identifier for identifier, _, _ in routed} == set(apps)
        finally:
            for app in apps.values():
                await app.state.supervisor.close()
