"""The real mux driver and harness communicate over a loopback HTTP socket."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from mux.contracts.actions import UserToolConfirmation

from .interop_support import SCOPE, collect, driver_session, message, workspace


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


async def test_dropped_reader_reconciles_approval_and_approved_write_executes(harness):
    client, model, home = harness
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
        await source.aclose()
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
