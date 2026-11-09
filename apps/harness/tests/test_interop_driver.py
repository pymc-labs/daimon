"""The real mux driver and harness communicate over a loopback HTTP socket."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from mux.contracts.actions import UserToolConfirmation
from mux.drivers.selfhosted.normalize import normalize
from mux.drivers.selfhosted.wire import Event

from .interop_support import SCOPE, collect, driver_session, message, workspace


async def test_fresh_driver_opens_stream_before_first_send(harness):
    client, model, _ = harness
    driver, session = await driver_session(client)
    source = await driver.events.open_stream(SCOPE, session.ref, previews=True)
    first = asyncio.create_task(anext(source))
    try:
        await asyncio.sleep(0)
        assert not first.done()
        assert model.calls == 0
        sent = await driver.events.send(SCOPE, session.ref, [message("hello")], key="send")
        async with asyncio.timeout(10):
            events = [await first, *[event async for event in source]]
        assert events[0].type == "session.status_running"
        assert events[-1].type == "session.turn_ended"
        assert events[-1].payload["outcome"] == "completed"
        assert all(event.turn_id == sent.turn_id for event in events)
        assert len({event.id for event in events}) == len(events)
        assert sum(event.type == "usage.observed" for event in events) == 1
        assert model.calls == 1
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await source.aclose()
        await driver.aclose()


async def test_stream_tool_usage_and_resend(harness):
    client, model, home = harness
    driver, session = await driver_session(client)
    try:
        sent = await driver.events.send(SCOPE, session.ref, [message("tool")], key="send")
        events = await collect(driver, session)
        assert [event.type for event in events] == [
            "session.status_running",
            "native.selfhosted.model_request_started",
            "usage.observed",
            "agent.tool_use",
            "agent.tool_result",
            "agent.message.delta",
            "agent.message",
            "session.turn_ended",
        ]
        assert events[-1].payload["outcome"] == "completed"
        assert (workspace(home, session.ref.id) / "result.txt").read_text() == "durable"
        delta = next(event for event in events if event.type == "agent.message.delta")
        final = next(event for event in events if event.type == "agent.message")
        assert delta.payload["item_id"] == final.payload["item_id"]
        assert await driver.events.send(SCOPE, session.ref, [message("tool")], key="send") == sent
        assert model.calls == 1
        # Also exercise harness idempotency over HTTP, bypassing the driver's receipt cache.
        raw = await client.get(f"/v0/sessions/{session.ref.id}/events")
        before = raw.json()["events"]
        # The driver request identity is stable across fresh driver reconstruction.
        rebuilt, restored = await driver_session(client)
        try:
            assert restored.ref == session.ref
            await rebuilt.events.send(SCOPE, restored.ref, [message("tool")], key="send")
            await collect(rebuilt, restored, after="0")
            assert model.calls == 1
        finally:
            await rebuilt.aclose()
        assert (await client.get(f"/v0/sessions/{session.ref.id}/events")).json()[
            "events"
        ] == before
        usage = await driver.usage.reconcile(SCOPE, session.ref)
        assert len(usage) == 1
        assert usage[0].completeness == "measured"
        assert usage[0].input_tokens == 10
        assert usage[0].input_cached_tokens == 2
        assert usage[0].output_tokens == 4
        assert await driver.usage.reconcile(SCOPE, session.ref) == usage
    finally:
        await driver.aclose()


async def test_dropped_reader_reconciles_approval_and_approved_write_executes(harness, monkeypatch):
    client, model, home = harness
    reader_closed = asyncio.Event()
    approval_ready = asyncio.Event()
    original_run = model.run

    async def gated_run(context, history):
        original_emit = context.emit

        async def emit(kind, data):
            if kind == "requires_action":
                approval_ready.set()
                await reader_closed.wait()
            await original_emit(kind, data)

        context.emit = emit
        return await original_run(context, history)

    monkeypatch.setattr(model, "run", gated_run)
    tools = [
        {"kind": "builtin", "name": name, "permission": "ask" if name == "write" else "auto"}
        for name in ("bash", "read", "write", "edit")
    ]
    driver, session = await driver_session(client, tools=tools)
    try:
        receipt = await driver.events.send(SCOPE, session.ref, [message("approval")], key="send")
        source = await driver.events.open_stream(SCOPE, session.ref, after="0")
        async with asyncio.timeout(10):
            first = await anext(source)
            await approval_ready.wait()
        before_close = await client.get(f"/v0/sessions/{session.ref.id}/events")
        assert not any(
            event["type"] == "requires_action" for event in before_close.json()["events"]
        )
        await source.aclose()
        reader_closed.set()
        async with asyncio.timeout(10):
            while True:
                snapshot = await driver.events.reconcile(SCOPE, session.ref)
                if snapshot.state == "requires_action":
                    break
                await asyncio.sleep(0.01)
        assert not (workspace(home, session.ref.id) / "result.txt").exists()
        action = snapshot.required_actions[0]
        approved = await driver.events.send(
            SCOPE,
            session.ref,
            [UserToolConfirmation(action_id=action.id, decision="allow")],
            key="approve",
            expected_turn=receipt.turn_id,
        )
        assert (
            await driver.events.send(
                SCOPE,
                session.ref,
                [UserToolConfirmation(action_id=action.id, decision="allow")],
                key="approve",
                expected_turn=receipt.turn_id,
            )
            == approved
        )
        remaining = await collect(driver, session, after=str(first.sequence))
        durable = await client.get(
            f"/v0/sessions/{session.ref.id}/events", params={"after_seq": first.sequence}
        )
        durable.raise_for_status()
        assert durable.json()["complete"]
        assert remaining == [
            normalize(Event.model_validate(event), session.ref)
            for event in durable.json()["events"]
        ]
        assert all(event.sequence > first.sequence for event in remaining)
        assert len({event.id for event in remaining}) == len(remaining)
        assert remaining[-1].payload["outcome"] == "completed"
        assert (workspace(home, session.ref.id) / "result.txt").read_text() == "durable"
        assert model.calls == 1
        assert (await driver.events.reconcile(SCOPE, session.ref)).state == "idle"
    finally:
        await driver.aclose()


async def test_cancel_reconciles_terminal_usage_and_preserves_session(harness):
    client, model, _ = harness
    driver, session = await driver_session(client)
    try:
        sent = await driver.events.send(SCOPE, session.ref, [message("wait")], key="send")
        await asyncio.wait_for(model.started.wait(), 10)
        cancelled = await driver.events.cancel(
            SCOPE, session.ref, turn_id=sent.turn_id, key="cancel"
        )
        stopped = await driver.events.wait_stopped(
            SCOPE,
            cancelled,
            deadline=datetime.now(UTC) + timedelta(seconds=10),
        )
        assert stopped.stopped and stopped.outcome == "interrupted"
        terminal = await collect(driver, session, after="0")
        assert sum(event.type == "session.turn_ended" for event in terminal) == 1
        assert (await driver.usage.reconcile(SCOPE, session.ref))[0].completeness == "measured"
        assert (
            await driver.events.cancel(SCOPE, session.ref, turn_id=sent.turn_id, key="again")
        ).status == "already_stopped"
        await driver.events.send(SCOPE, session.ref, [message("hello")], key="next")
        assert (await collect(driver, session))[-1].payload["outcome"] == "completed"
        assert model.calls == 2
    finally:
        await driver.aclose()


async def test_mcp_policy_exposure_approval_and_denial_over_http(tmp_path, monkeypatch):
    from .mcp_probe import mcp_turn_probe

    await mcp_turn_probe(tmp_path, monkeypatch, interop=True)


async def test_narrowed_agent_omits_and_refuses_write(harness, monkeypatch):
    from daimon_harness.model_adapter import builtin_tools

    client, model, home = harness
    exposed = []
    original = model.run

    async def inspect_tools(context, history):
        exposed.extend(tool.name for tool in builtin_tools(context))
        return await original(context, history)

    monkeypatch.setattr(model, "run", inspect_tools)
    driver, session = await driver_session(
        client,
        tools=[
            {
                "kind": "builtin",
                "name": "read",
                "permission": "auto",
            }
        ],
    )
    try:
        await driver.events.send(SCOPE, session.ref, [message("tool")], key="send")
        events = await collect(driver, session, after="0")
        assert exposed == ["read"]
        denied = next(event for event in events if event.type == "agent.tool_result")
        assert denied.payload["is_error"]
        assert "denied" in str(denied.payload["content"]).lower()
        assert not (workspace(home, session.ref.id) / "result.txt").exists()
        assert events[-1].payload["outcome"] == "completed"
    finally:
        await driver.aclose()


async def test_sealed_multi_cleanup_heals_and_real_driver_codec_finishes(tmp_path, monkeypatch):
    from contextlib import suppress

    import httpx
    from daimon.core.turn.selfhosted_codec import display_batch
    from daimon_harness.runtime import TurnContext
    from daimon_harness.service import create_app
    from mux.errors import ProviderError

    from .conftest import FakeModelAdapter

    monkeypatch.setenv("HARNESS_SESSION_MODE", "multi")
    monkeypatch.setattr("daimon_harness.service.CLEANUP_TIMEOUT", 0.03)
    monkeypatch.setattr("daimon_harness.service.CANCEL_JOIN_TIMEOUT", 0.02)
    monkeypatch.setattr("daimon_harness.service.CLEANUP_RETRY_DELAY", 0.01)
    monkeypatch.setattr("daimon_harness.service.CLEANUP_BACKOFF_CAP", 0.03)
    started, release = asyncio.Event(), asyncio.Event()
    original_stop = TurnContext.stop_workers
    attempts = []

    async def transient(context):
        attempts.append(context.request.turn_id)
        if not release.is_set():
            raise TimeoutError("transient verifier failure")
        await original_stop(context)

    class Model(FakeModelAdapter):
        async def run(self, context, history):
            history = await super().run(context, history)

            async def resistant():
                started.set()
                while not release.is_set():
                    with suppress(asyncio.CancelledError):
                        await release.wait()
                return "stopped"

            await context.tool("resistant", "write", {}, resistant)
            return history

    monkeypatch.setattr(TurnContext, "stop_workers", transient)
    app = create_app(home=tmp_path, token="test-token", adapter=Model())
    supervisor = app.state.supervisor
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            headers={"Authorization": "Bearer test-token", "Accept": "application/json"},
        ) as client,
    ):
        driver, session = await driver_session(client)
        try:
            sent = await driver.events.send(SCOPE, session.ref, [message("hello")], key="send")
            await started.wait()
            await client.post(
                f"/v0/turns/{sent.turn_id}/cancel", params={"session_id": session.ref.id}
            )
            async with asyncio.timeout(3):
                while session.ref.id not in supervisor.sealed_sessions:
                    await asyncio.sleep(0.01)
            count = len(attempts)
            async with asyncio.timeout(3):
                while len(attempts) <= count:
                    await asyncio.sleep(0.01)
            release.set()
            events = await collect(driver, session, after="0")
            terminal = [event for event in events if event.type == "session.turn_ended"]
            assert len(terminal) == 1
            assert terminal[0].payload["outcome"] == "errored"
            assert terminal[0].payload["native_reason"] == "cleanup_exhausted"
            snapshot = await driver.events.reconcile(SCOPE, session.ref)
            assert snapshot.state == "idle" and snapshot.active_root_turn is None, (
                "sealed turn kept polling as running"
            )
            displayed = [
                value
                for event in events
                for value in display_batch(event, session_id=session.ref.id)
            ]
            notices = [
                item
                for item in displayed
                if item.type == "agent.message" and "Start a new conversation" in str(item.content)
            ]
            assert len(notices) == 1, "cleanup exhaustion hidden by core codec"
            assert any(item.type == "session.status_idle" for item in displayed)
            # The real driver must honor the structured non-retryable 503 too.
            with pytest.raises(ProviderError) as error:
                await driver.events.send(SCOPE, session.ref, [message("again")], key="again")
            assert not error.value.retryable and error.value.native_code == "cleanup_exhausted"
            assert not supervisor.unhealthy
            assert sent.turn_id not in supervisor.contexts
        finally:
            release.set()
            await driver.aclose()
