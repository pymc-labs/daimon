"""Real pinned codex-rs, synthetic Responses SSE, no live model or credentials."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import httpx
import pytest
from daimon_harness.codex_adapter import CodexModelAdapter
from daimon_harness.journal import Journal
from daimon_harness.runtime import TurnContext
from mux.drivers.selfhosted.wire import TurnRequest

from .conftest import turn


def sse_response(output, *, number=1, usage=True, status="completed", input_tokens=10):
    response = {
        "id": f"resp_{number}",
        "object": "response",
        "status": status,
        "model": "gpt-6-luna",
        "output": output,
    }
    if usage:
        response["usage"] = {
            "input_tokens": input_tokens,
            "input_tokens_details": {"cached_tokens": 2},
            "output_tokens": 4,
            "output_tokens_details": {"reasoning_tokens": 1},
            "total_tokens": input_tokens + 4,
        }
    events = [
        {
            "type": "response.created",
            "response": {**response, "status": "in_progress", "output": []},
        }
    ]
    for i, item in enumerate(output):
        events.append({"type": "response.output_item.added", "output_index": i, "item": item})
        if item["type"] == "message":
            for text in ["do", "ne"]:
                events.append(
                    {
                        "type": "response.output_text.delta",
                        "item_id": item["id"],
                        "output_index": i,
                        "content_index": 0,
                        "delta": text,
                    }
                )
        events.append({"type": "response.output_item.done", "output_index": i, "item": item})
    events.append({"type": "response." + status, "response": response})
    return httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        content="".join("data: " + json.dumps(event) + "\n\n" for event in events),
    )


def assistant():
    return {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "done", "annotations": []}],
    }


def write_call():
    return {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "harness_write",
        "arguments": '{"path":"codex.txt","content":"durable"}',
    }


class OfflineCodex:
    def __init__(self, respond):
        # Skip Azure credential capture entirely. This is a synthetic model adapter.
        self.respond = respond
        self.binary = os.environ.get("HARNESS_CODEX_BINARY", "codex")
        self.calls = 0
        self.engine = object.__new__(CodexModelAdapter)
        self.engine.binary = self.binary

    async def run(self, context, history):
        def respond(request):
            self.calls += 1
            return self.respond(request)

        async def headers():
            return {}

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await self.engine.run_codex(
                context,
                history,
                client,
                "https://unit-test.openai.azure.com/openai/responses",
                headers,
            )


@pytest.fixture
def codex_binary():
    binary = os.environ.get("HARNESS_CODEX_BINARY", "codex")
    if shutil.which(binary) is None:
        pytest.skip("install codex-cli 0.162.1 to run real binary tests")
    return binary


async def test_real_codex_tool_loop_and_streaming(tmp_path, codex_binary):
    journal = Journal(tmp_path)
    request = TurnRequest.model_validate(turn())
    journal.claim(request)
    requests = []

    def respond(http):
        body = json.loads(http.content)
        requests.append(body)
        assert journal.events("session-1")[-1].type == "model_request_started"
        assert body["model"] == "gpt-6-luna" and body["store"] is False
        assert {tool["name"] for tool in body["tools"]} == {
            "harness_bash",
            "harness_read",
            "harness_write",
            "harness_edit",
        }
        if len(requests) == 1:
            return sse_response([write_call()])
        assert any(item.get("type") == "function_call_output" for item in body["input"])
        return sse_response([assistant()], number=2)

    async def emit(kind, data):
        journal.append(request, kind, data)

    context = TurnContext(request, tmp_path, emit)
    adapter = OfflineCodex(respond)
    async with asyncio.timeout(20):
        history = await adapter.run(context, [])
    assert (tmp_path / "work/codex.txt").read_text() == "durable"
    events = journal.events("session-1")
    assert len([event for event in events if event.type == "usage"]) == 2
    assert not journal.pending_requests(request)
    deltas = [event.data for event in events if event.type == "agent_message_delta"]
    final = next(event.data for event in events if event.type == "agent_message")
    assert "".join(delta["text"] for delta in deltas) == "done"
    assert {delta["message_id"] for delta in deltas} == {final["message_id"]}
    assert any(item.get("type") == "function_call_output" for item in history)
    journal.close()


@pytest.mark.parametrize("decision", ["allow", "deny", "result"])
async def test_codex_c3_approval_and_history(tmp_path, codex_binary, decision):
    from daimon_harness.service import Supervisor
    from mux.drivers.selfhosted.wire import ActionRequest

    requests = []

    def respond(http):
        body = json.loads(http.content)
        requests.append(body)
        return sse_response(
            [write_call()] if len(requests) == 1 else [assistant()], number=len(requests)
        )

    adapter = OfflineCodex(respond)
    supervisor = Supervisor(tmp_path, adapter)
    request = TurnRequest.model_validate({**turn(), "tool_policy": {"write": "ask"}})
    try:
        await supervisor.submit(request)
        async with asyncio.timeout(10):
            while True:
                context = supervisor.contexts.get(request.turn_id)
                if context is not None and context.pending:
                    break
                await asyncio.sleep(0.01)
            assert not (context.work / "codex.txt").exists()
            action_id = next(iter(context.pending))
            context.pending[action_id].set_result(
                ActionRequest(
                    action_id=action_id,
                    decision=decision,
                    **({"result": "external result"} if decision == "result" else {}),
                )
            )
            await supervisor.tasks[request.turn_id]
        assert (context.work / "codex.txt").exists() == (decision == "allow")
        events = supervisor.journal.events(request.session_id)
        assert events[-1].data["outcome"] == "completed"
        result = next(e.data for e in events if e.type == "tool_result")
        assert result["is_error"] == (decision == "deny")
        if decision == "result":
            assert result["output"] == "external result"
        assert len([e for e in events if e.type == "usage"]) == 2
        # A fresh controller receives durable history through the Responses relay.
        next_request = TurnRequest.model_validate(turn(turn_id="turn-2", key="key-2"))
        await supervisor.submit(next_request)
        await asyncio.wait_for(supervisor.tasks[next_request.turn_id], 10)
        assert any(i.get("type") == "function_call_output" for i in requests[-1]["input"])
        assert supervisor.journal.events(request.session_id)[-1].data["outcome"] == "completed"
    finally:
        await supervisor.close()


async def test_codex_denied_tool_hidden_and_refused(tmp_path, codex_binary):
    from daimon_harness.service import Supervisor

    requests = []

    def respond(http):
        body = json.loads(http.content)
        requests.append(body)
        assert "harness_write" not in {t["name"] for t in body["tools"]}
        return sse_response([write_call()] if len(requests) == 1 else [assistant()])

    supervisor = Supervisor(tmp_path, OfflineCodex(respond))
    request = TurnRequest.model_validate({**turn(), "tool_policy": {"write": "deny"}})
    try:
        await supervisor.submit(request)
        await asyncio.wait_for(supervisor.tasks[request.turn_id], 10)
        events = supervisor.journal.events(request.session_id)
        assert events[-1].data["outcome"] == "completed"
        assert next(e.data for e in events if e.type == "tool_result")["is_error"]
        assert not (supervisor.journal.sandbox(request.session_id)[0] / "work/codex.txt").exists()
    finally:
        await supervisor.close()


async def test_codex_cancellation_waiting_for_approval_reaps_controller(
    tmp_path, codex_binary, monkeypatch
):
    from daimon_harness import codex_adapter
    from daimon_harness.service import Supervisor

    processes = []
    original = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(codex_adapter.asyncio, "create_subprocess_exec", capture)
    adapter = OfflineCodex(lambda http: sse_response([write_call()]))
    supervisor = Supervisor(tmp_path, adapter)
    request = TurnRequest.model_validate({**turn(), "tool_policy": {"write": "ask"}})
    try:
        await supervisor.submit(request)
        async with asyncio.timeout(10):
            while not (
                supervisor.contexts.get(request.turn_id)
                and supervisor.contexts[request.turn_id].pending
            ):
                await asyncio.sleep(0.01)
        supervisor.tasks[request.turn_id].cancel()
        await supervisor.tasks[request.turn_id]
        assert all(p.returncode is not None for p in processes)
        events = supervisor.journal.events(request.session_id)
        assert events[-1].data["outcome"] == "interrupted"
        assert len([e for e in events if e.type == "usage"]) == 1
        assert not supervisor.journal.pending_requests(request)
        assert not list(tmp_path.rglob("config.toml"))
    finally:
        await supervisor.close()


async def test_codex_real_driver_mcp_approval_and_denial(tmp_path, monkeypatch, codex_binary):
    from .mcp_probe import mcp_turn_probe

    await mcp_turn_probe(tmp_path, monkeypatch, interop=True, codex=True)


async def test_codex_mcp_echo_redacts_operator_credentials(tmp_path, monkeypatch, codex_binary):
    from .mcp_probe import mcp_turn_probe

    await mcp_turn_probe(tmp_path, monkeypatch, codex=True)


async def test_codex_driver_recovery_resend_and_cancel(tmp_path, monkeypatch, codex_binary):
    from mux.contracts.actions import UserToolConfirmation

    from .conftest import serve_harness
    from .interop_support import SCOPE, collect, driver_session, message, workspace

    requests = []

    def respond(http):
        body = json.loads(http.content)
        requests.append(body)
        # Only request a tool when there is no result for this controller turn yet.
        latest_user_index = max(
            i for i, item in enumerate(body["input"]) if item.get("role") == "user"
        )
        has_result = any(
            i.get("type") == "function_call_output" for i in body["input"][latest_user_index:]
        )
        return sse_response([assistant()] if has_result else [write_call()], number=len(requests))

    adapter = OfflineCodex(respond)
    async with serve_harness(tmp_path, adapter) as (client, _, home):
        tools = [{"kind": "builtin", "name": "write", "permission": "ask"}]
        driver, session = await driver_session(client, tools=tools)
        try:
            sent = await driver.events.send(SCOPE, session.ref, [message("approval")], key="first")
            source = await driver.events.open_stream(SCOPE, session.ref, after="0")
            first = await anext(source)
            await source.aclose()
            async with asyncio.timeout(10):
                while True:
                    snapshot = await driver.events.reconcile(SCOPE, session.ref)
                    if snapshot.state == "requires_action":
                        break
                    await asyncio.sleep(0.01)
            assert not (workspace(home, session.ref.id) / "codex.txt").exists()
            action = snapshot.required_actions[0]
            await driver.events.send(
                SCOPE,
                session.ref,
                [UserToolConfirmation(action_id=action.id, decision="allow")],
                key="approve",
                expected_turn=sent.turn_id,
            )
            remaining = await collect(driver, session, after=str(first.sequence))
            assert remaining[-1].payload["outcome"] == "completed"
            assert (workspace(home, session.ref.id) / "codex.txt").read_text() == "durable"
            usage = await driver.usage.reconcile(SCOPE, session.ref)
            assert len(usage) == 2 and all(u.completeness == "measured" for u in usage)
            assert all(u.input_tokens == 10 and u.output_tokens == 4 for u in usage)
            calls = len(requests)
            rebuilt, restored = await driver_session(client, tools=tools)
            try:
                await rebuilt.events.send(SCOPE, restored.ref, [message("approval")], key="first")
                await collect(rebuilt, restored, after="0")
                assert len(requests) == calls
            finally:
                await rebuilt.aclose()
            second = await driver.events.send(
                SCOPE, session.ref, [message("approval")], key="second"
            )
            async with asyncio.timeout(10):
                while (
                    await driver.events.reconcile(SCOPE, session.ref)
                ).state != "requires_action":
                    await asyncio.sleep(0.01)
            await driver.events.cancel(SCOPE, session.ref, turn_id=second.turn_id, key="cancel")
            terminal = await collect(driver, session, after="0")
            assert terminal[-1].payload["outcome"] == "interrupted"
            assert len(requests) == calls + 1
        finally:
            await driver.aclose()


@pytest.mark.parametrize("managed_identity", [False, True])
async def test_codex_azure_auth_is_captured_and_not_in_child_environment(
    tmp_path, monkeypatch, codex_binary, managed_identity
):
    from azure.core.credentials import AccessToken
    from daimon_harness import codex_adapter

    secret = "synthetic-model-credential"

    def respond(request):
        assert (
            request.url
            == "https://fake.openai.azure.com/openai/responses?api-version=2025-04-01-preview"
        )
        if managed_identity:
            assert request.headers["Authorization"] == "Bearer " + secret
            assert "api-key" not in request.headers
        else:
            assert request.headers["api-key"] == secret
            assert "Authorization" not in request.headers
        assert json.loads(request.content)["model"] == "gpt-6-luna"
        return sse_response([assistant()])

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://fake.openai.azure.com")
    if managed_identity:
        monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("IDENTITY_ENDPOINT", "http://127.0.0.1:1/identity")
        monkeypatch.setenv("IDENTITY_HEADER", "synthetic-mi-header")

        class Credential:
            async def get_token(self, *args, **kwargs):
                return AccessToken(secret, 4_000_000_000)

            async def close(self):
                pass

        monkeypatch.setattr(
            "daimon_harness.model_adapter.ManagedIdentityCredential", lambda **_: Credential()
        )
    else:
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", secret)
    engine = CodexModelAdapter()
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("IDENTITY_HEADER", raising=False)
    monkeypatch.delenv("IDENTITY_ENDPOINT", raising=False)
    monkeypatch.setattr(codex_adapter.httpx, "AsyncClient", lambda **_: client)
    original_spawn = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        assert secret not in str(args) + str(kwargs)
        assert not any(
            "AZURE" in name or "IDENTITY" in name or "TOKEN" in name for name in kwargs["env"]
        )
        config = Path(kwargs["cwd"]) / "config.toml"
        assert secret not in config.read_text()
        return await original_spawn(*args, **kwargs)

    monkeypatch.setattr(codex_adapter.asyncio, "create_subprocess_exec", capture)
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    context = TurnContext(TurnRequest.model_validate(turn()), tmp_path, emit)
    await engine.run(context, [])
    assert any(kind == "usage" for kind, _ in events)
    assert secret not in str(events)
    assert not list(tmp_path.rglob("config.toml"))


async def test_cancel_during_controller_creation_is_reaped(tmp_path, monkeypatch, codex_binary):
    from daimon_harness import codex_adapter

    launched, release = asyncio.Event(), asyncio.Event()
    processes = []
    original = asyncio.create_subprocess_exec

    async def delayed(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        launched.set()
        await release.wait()
        return process

    monkeypatch.setattr(codex_adapter.asyncio, "create_subprocess_exec", delayed)

    async def emit(kind, data):
        raise AssertionError("cancelled creation must not reach model I/O")

    adapter = OfflineCodex(lambda _: sse_response([assistant()]))
    context = TurnContext(TurnRequest.model_validate(turn()), tmp_path, emit)
    task = asyncio.create_task(adapter.run(context, []))
    await asyncio.wait_for(launched.wait(), 5)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert processes and all(p.returncode is not None for p in processes)
    assert not list(tmp_path.rglob("config.toml"))


@pytest.mark.parametrize("budget", [0, 2])
async def test_real_codex_repeated_denied_calls_obey_budget(tmp_path, codex_binary, budget):
    from daimon_harness.service import Supervisor

    adapter = OfflineCodex(lambda _: sse_response([write_call()]))
    request = TurnRequest.model_validate(
        {
            **turn(),
            "tool_policy": {"write": "deny"},
            "limits": {"max_tool_calls": budget, "deadline_s": 20},
        }
    )
    supervisor = Supervisor(tmp_path, adapter)
    try:
        await supervisor.submit(request)
        await asyncio.wait_for(supervisor.tasks[request.turn_id], 15)
        events = supervisor.journal.events(request.session_id)
        assert events[-1].type == "turn_ended"
        assert events[-1].data["outcome"] == "errored"
        assert events[-1].data["error"]["kind"] == "tool_limit"
        assert adapter.calls == budget + 1
        assert sum(e.type == "usage" for e in events) == adapter.calls
        assert not (supervisor.journal.sandbox(request.session_id)[0] / "work/codex.txt").exists()
    finally:
        await supervisor.close()


async def test_real_codex_has_no_bundled_skills_or_private_prompt(
    tmp_path, codex_binary, monkeypatch
):
    from daimon_harness import codex_relay

    captured = []
    original = codex_relay.ResponsesRelay.forward

    async def inspect(self, request):
        source = await request.json()
        captured.append(source)
        # Check the controller's raw request before the relay sanitizes it.
        payload = json.dumps(source)
        for forbidden in [
            "skills_instructions",
            "<skill>",
            "skill-installer/SKILL.md",
            "imagegen/SKILL.md",
        ]:
            assert forbidden not in payload
        assert not list(tmp_path.rglob("skills/.system/*/SKILL.md"))
        return await original(self, request)

    monkeypatch.setattr(codex_relay.ResponsesRelay, "forward", inspect)

    async def emit(*args):
        pass

    request = TurnRequest.model_validate(
        {
            **turn(),
            "input": [{"type": "text", "text": "Use $imagegen and $skill-installer."}],
        }
    )
    context = TurnContext(request, tmp_path / "sandbox", emit, state_dir=tmp_path / "private")

    def respond(http):
        payload = http.content.decode()
        assert str(tmp_path / "private") not in payload
        assert "<environment_context>" not in payload
        assert "skills_instructions" not in payload and "<skill>" not in payload
        return sse_response([assistant()])

    await OfflineCodex(respond).run(context, [])
    assert captured


@pytest.mark.parametrize("prefix", ["<skills_instructions>", "<skill>", "<environment_context>"])
async def test_real_codex_preserves_authoritative_user_input(tmp_path, codex_binary, prefix):
    from daimon_harness.service import Supervisor

    text = prefix + " Explain invoice 917.42 and $imagegen $skill-installer verbatim."
    prior = {"role": "user", "content": prefix + " Earlier preference: metric units."}
    bodies = []

    def respond(http):
        body = json.loads(http.content)
        bodies.append(body)
        users = [item for item in body["input"] if item.get("role") == "user"]
        assert users == [prior, {"role": "user", "content": [{"type": "input_text", "text": text}]}]
        # No controller-origin environment or skill catalog reaches the provider.
        assert str(tmp_path) not in json.dumps(body)
        assert "skills/.system" not in json.dumps(body)
        return sse_response([write_call()] if len(bodies) == 1 else [assistant()])

    supervisor = Supervisor(tmp_path, OfflineCodex(respond))
    request = TurnRequest.model_validate({**turn(text)})
    supervisor.journal.save_history(request.session_id, [prior])
    try:
        await supervisor.submit(request)
        await asyncio.wait_for(supervisor.tasks[request.turn_id], 15)
        events = supervisor.journal.events(request.session_id)
        assert events[-1].data == {"outcome": "completed"}
        history = supervisor.journal.history(request.session_id)
        users = [item for item in history if item.get("role") == "user"]
        assert users == [prior, {"role": "user", "content": [{"type": "input_text", "text": text}]}]
        assert len(bodies) == 2
    finally:
        await supervisor.close()


@pytest.mark.parametrize("interrupt", ["cancel", "deadline"])
async def test_interrupt_during_controller_shutdown(tmp_path, monkeypatch, codex_binary, interrupt):
    from daimon_harness.codex_adapter import CodexConnection
    from daimon_harness.service import create_app

    stopping = asyncio.Event()
    processes = []
    original = CodexConnection.stop

    async def slow_shutdown(self):
        processes.append(self.process)
        stopping.set()
        # Model a controller that ignores graceful EOF until forcibly reaped.
        await self.process.wait()
        await original(self)

    monkeypatch.setattr(CodexConnection, "stop", slow_shutdown)
    request = TurnRequest.model_validate(
        {
            **turn(),
            "limits": {"max_tool_calls": 3, "deadline_s": 2 if interrupt == "deadline" else 30},
        }
    )
    app = create_app(
        home=tmp_path,
        token="test-token",
        adapter=OfflineCodex(lambda _: sse_response([assistant()])),
    )
    supervisor = app.state.supervisor
    try:
        await supervisor.submit(request)
        await asyncio.wait_for(stopping.wait(), 5)
        started = asyncio.get_running_loop().time()
        if interrupt == "cancel":
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/v0/turns/{request.turn_id}/cancel",
                    headers={"Authorization": "Bearer test-token"},
                )
                assert response.json() == {"cancelled": True}
        await asyncio.wait_for(supervisor.tasks[request.turn_id], 3)
        assert asyncio.get_running_loop().time() - started < (1 if interrupt == "cancel" else 3)
        events = supervisor.journal.events(request.session_id)
        assert events[-1].type == "turn_ended"
        assert events[-1].data["outcome"] == "interrupted"
        assert sum(e.type == "usage" for e in events) == 1
        assert sum(e.type == "turn_ended" for e in events) == 1
        assert all(p.returncode is not None for p in processes)
    finally:
        await supervisor.close()


async def test_real_codex_compaction_preserves_progress_and_budget(
    tmp_path, codex_binary, monkeypatch
):
    from daimon_harness import codex_relay
    from daimon_harness.service import Supervisor

    raw = []
    bodies = []
    relays = []
    original = codex_relay.ResponsesRelay.forward

    async def capture(self, request):
        raw.append(await request.json())
        relays.append(self)
        return await original(self, request)

    monkeypatch.setattr(codex_relay.ResponsesRelay, "forward", capture)
    # Marker-like authoritative content must survive both context replacements.
    prompt = "<environment_context> Run two writes exactly once, then finish."

    def respond(http):
        body = json.loads(http.content)
        bodies.append(body)
        n = len(bodies)
        users = [item for item in body["input"] if item.get("role") == "user"]
        assert users[0] == {"role": "user", "content": [{"type": "input_text", "text": prompt}]}
        assert str(tmp_path) not in json.dumps(body)
        assert "<permissions instructions>" not in json.dumps(body)
        assert "<current_date>" not in json.dumps(body)
        if n in (2, 4):
            # Pinned local-compaction artifact: ID-less final user message,
            # positioned after the stable original user item and tool output.
            assert raw[-1]["input"][-1]["role"] == "user"
            assert "id" not in raw[-1]["input"][-1]
            assert users[-1]["content"] == raw[-1]["input"][-1]["content"]
            assert "CONTEXT CHECKPOINT COMPACTION" in json.dumps(users[-1])
            summary = assistant()
            summary["content"][0]["text"] = (
                f"Progress checkpoint {n}: writes {n // 2} completed; do not repeat. "
                + str(relays[-1].context.state_dir)
                + " "
                + relays[-1].private_paths[-1]
            )
            return sse_response([summary], number=n)
        if n in (3, 5):
            assert f"Progress checkpoint {n - 1}" in json.dumps(users[1])
            assert "[private controller]" in json.dumps(users[1])
            assert users[1]["id"] != relays[-1].controller_user_id
        if n in (1, 3):
            call = write_call()
            call.update(id=f"fc_{n}", call_id=f"call_{n}")
            call["arguments"] = json.dumps({"path": f"write-{n}.txt", "content": "once"})
            return sse_response([call], number=n, input_tokens=116000)
        assert n == 5
        return sse_response([assistant()], number=n)

    supervisor = Supervisor(tmp_path, OfflineCodex(respond))
    request = TurnRequest.model_validate(
        {**turn(prompt), "limits": {"max_tool_calls": 2, "deadline_s": 20}}
    )
    try:
        await supervisor.submit(request)
        await asyncio.wait_for(supervisor.tasks[request.turn_id], 15)
        events = supervisor.journal.events(request.session_id)
        assert events[-1].data == {"outcome": "completed"}
        calls = [e.data["input"]["path"] for e in events if e.type == "tool_use"]
        assert calls == ["write-1.txt", "write-3.txt"]
        assert len(bodies) == 5 > request.limits.max_tool_calls + 2
        assert relays[-1].model_requests == 3
        assert relays[-1].compaction_requests == 2
        assert sum(e.type == "model_request_started" for e in events) == 5
        assert sum(e.type == "usage" for e in events) == 5
        assert not supervisor.journal.pending_requests(request)
        history = supervisor.journal.history(request.session_id)
        assert str(tmp_path) not in json.dumps(history)
        assert "Progress checkpoint 4" in json.dumps(history)
        assert sum(item.get("role") == "user" for item in history) == 2
    finally:
        await supervisor.close()
