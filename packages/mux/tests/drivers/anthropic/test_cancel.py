"""Cancel receipts need independent stop evidence, decoded by the real SDK."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Literal

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.receipts import CancelReceipt
from mux.drivers.anthropic import turn as turn_module
from mux.drivers.anthropic.cancel import observed_stop
from mux.drivers.anthropic.normalize import EventNormalizer
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.turn import AnthropicEvents
from mux.errors import ScopeViolation
from pydantic import JsonValue

NOW = datetime(2026, 10, 9, tzinfo=UTC)
SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="host", authorization_id="grant"
)
SESSION = ResourceRef(
    id="session",
    kind="session",
    provider="anthropic",
    account_scope_id="workspace",
    tenant_id="tenant",
    account_id="account",
)
AUTH = ResourceAuthorization(SCOPE, frozenset({("session", "session")}))


def port(client: AsyncAnthropic) -> AnthropicEvents:
    return AnthropicEvents(client, "workspace", AUTH)


def receipt(
    status: Literal["requested", "already_stopped", "outcome_unknown"] = "requested",
    session: ResourceRef = SESSION,
) -> CancelReceipt:
    return CancelReceipt(
        operation_id="cancel", session=session, turn_id="root", status=status, requested_at=NOW
    )


def record(kind: str, **fields: JsonValue) -> dict[str, JsonValue]:
    return {"type": kind, "id": "event", "processed_at": NOW.isoformat(), **fields}


@pytest.mark.parametrize("stop", ["end_turn", "retries_exhausted", "requires_action", "terminated"])
async def test_cancel_wait_requires_a_real_root_idle_or_termination_record(stop: str) -> None:
    raw = (
        record("session.status_terminated")
        if stop == "terminated"
        else record(
            "session.status_idle",
            stop_reason={
                "type": stop,
                **({"event_ids": ["call"]} if stop == "requires_action" else {}),
            },
        )
    )
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply.stream("/v1/sessions/session/events/stream", [dict[str, object](raw)])
    )
    async with transport.client() as client:
        stopped = await port(client).wait_stopped(
            SCOPE, receipt(), deadline=datetime.now(UTC) + timedelta(seconds=30)
        )
    assert stopped.stopped
    assert stopped.receipt_operation_id == "cancel"
    assert stopped.outcome == ("terminated" if stop == "terminated" else "interrupted")
    transport.assert_consumed()
    assert len(transport.requests) == 1


@pytest.mark.parametrize("status", ["requested", "already_stopped", "outcome_unknown"])
@pytest.mark.parametrize("evidence", ["empty", "echo", "running", "subagent", "unknown_idle"])
async def test_receipt_echo_and_nonterminal_records_never_prove_stop(
    status: Literal["requested", "already_stopped", "outcome_unknown"], evidence: str
) -> None:
    raw = {
        "empty": [],
        "echo": [record("user.interrupt")],
        "running": [record("session.status_running")],
        "subagent": [
            record(
                "session.thread_status_idle",
                session_thread_id="child",
                stop_reason={"type": "end_turn"},
            )
        ],
        "unknown_idle": [record("session.status_idle", stop_reason={"type": "future_reason"})],
    }[evidence]
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply.stream(
            "/v1/sessions/session/events/stream", [dict[str, object](event) for event in raw]
        )
    )
    async with transport.client() as client:
        stopped = await port(client).wait_stopped(
            SCOPE, receipt(status), deadline=datetime.now(UTC) + timedelta(seconds=30)
        )
    assert not stopped.stopped and stopped.outcome is None
    assert stopped.receipt_operation_id == "cancel"
    transport.assert_consumed()


def test_preview_of_a_terminal_event_is_not_authoritative_stop_evidence():
    event = EventNormalizer(SESSION).normalize(
        record("session.status_idle", stop_reason={"type": "end_turn"}), observed_at=NOW
    )
    assert observed_stop(event.model_copy(update={"authority": "preview"}), receipt()) is None


def test_stop_evidence_from_another_session_is_rejected():
    event = EventNormalizer(SESSION.model_copy(update={"id": "other"})).normalize(
        record("session.status_idle", stop_reason={"type": "end_turn"}), observed_at=NOW
    )
    with pytest.raises(ScopeViolation):
        observed_stop(event, receipt())


@pytest.mark.parametrize("status", ["requested", "already_stopped", "outcome_unknown"])
async def test_expired_deadline_never_treats_a_receipt_as_stop_proof_or_opens_io(
    status: Literal["requested", "already_stopped", "outcome_unknown"],
) -> None:
    transport = ScriptedTransport()
    async with transport.client() as client:
        stopped = await port(client).wait_stopped(
            SCOPE, receipt(status), deadline=datetime.now(UTC) - timedelta(seconds=1)
        )
    assert not stopped.stopped and stopped.outcome is None
    assert transport.requests == []


class WaitingBody(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        await self.release.wait()
        yield b""

    async def aclose(self) -> None:
        self.closed = True


async def test_deadline_closes_the_idle_stream_without_claiming_a_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return NOW

    monkeypatch.setattr(turn_module, "datetime", FrozenClock)
    body = WaitingBody()
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "GET",
            "/v1/sessions/session/events/stream",
            httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body),
        )
    )
    async with transport.client() as client:
        stopped = await port(client).wait_stopped(
            SCOPE, receipt(), deadline=NOW + timedelta(milliseconds=50)
        )
    assert not stopped.stopped and stopped.outcome is None
    assert body.closed and body.started.is_set()
    transport.assert_consumed()


async def test_caller_cancellation_closes_the_stream_and_propagates():
    body = WaitingBody()
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "GET",
            "/v1/sessions/session/events/stream",
            httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body),
        )
    )
    async with transport.client() as client:
        task = asyncio.create_task(
            port(client).wait_stopped(
                SCOPE, receipt(), deadline=datetime.now(UTC) + timedelta(seconds=30)
            )
        )
        try:
            await asyncio.wait_for(body.started.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert body.closed
    transport.assert_consumed()


async def test_foreign_receipt_scope_is_rejected_before_io():
    transport = ScriptedTransport()
    foreign = SESSION.model_copy(update={"tenant_id": "other"})
    async with transport.client() as client:
        with pytest.raises(ScopeViolation):
            await port(client).wait_stopped(
                SCOPE, receipt(session=foreign), deadline=datetime.now(UTC) + timedelta(seconds=30)
            )
    assert transport.requests == []
