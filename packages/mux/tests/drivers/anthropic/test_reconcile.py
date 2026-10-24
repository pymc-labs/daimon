"""Anthropic `Events.reconcile` over the real SDK client and an in-file HTTP double."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from anthropic import AsyncAnthropic
from mux.contracts.ids import ResourceRef, Scope
from mux.drivers.anthropic.reconcile import (
    HISTORY_TRUNCATED,
    MAX_EVENTS,
    STATUS_DISAGREEMENT,
    native_state,
)
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.turn import AnthropicEvents
from mux.errors import ProviderError, ScopeViolation

SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="caller", authorization_id="grant"
)
SESSION = ResourceRef(
    id="sess_1",
    kind="session",
    provider="anthropic",
    account_scope_id="workspace",
    tenant_id="tenant",
    account_id="account",
)
AUTHORIZATION = ResourceAuthorization(SCOPE, frozenset({("session", "sess_1")}))
TIME = "2026-10-09T00:00:00Z"
END_TURN: dict[str, object] = {"type": "end_turn"}


def record(kind: str, id_: str, **fields: object) -> dict[str, object]:
    return {"type": kind, "id": id_, "processed_at": TIME, **fields}


def idle(id_: str, stop: dict[str, object] = END_TURN) -> dict[str, object]:
    return record("session.status_idle", id_, stop_reason=stop)


def tool_use(id_: str, kind: str = "agent.mcp_tool_use") -> dict[str, object]:
    extra: dict[str, object] = (
        {"mcp_server_name": "tools", "evaluated_permission": "ask"}
        if kind == "agent.mcp_tool_use"
        else {}
    )
    return record(kind, id_, name="run", input={"command": "pwd"}, **extra)


class Wire:
    """Serves one session record and a newest-first event log, page by page."""

    def __init__(self, status: str, newest_first: list[dict[str, object]], page: int = 100):
        self.status = status
        self.events = newest_first
        self.page = page
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/v1/sessions/sess_1":
            return httpx.Response(200, json={"id": "sess_1", "status": self.status})
        assert request.url.path == "/v1/sessions/sess_1/events"
        start = int(request.url.params.get("page", "0"))
        end = start + self.page
        more = end < len(self.events)
        return httpx.Response(
            200,
            json={"data": self.events[start:end], "next_page": str(end) if more else None},
        )

    @property
    def event_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/events")]


def driver(wire: Callable[[httpx.Request], httpx.Response]) -> AnthropicEvents:
    sdk = AsyncAnthropic(
        api_key="test",
        base_url="http://anthropic.test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(wire)),
    )
    return AnthropicEvents(sdk, "workspace", AUTHORIZATION)


def chronological(*events: dict[str, object]) -> list[dict[str, object]]:
    return list(reversed(events))


async def test_idle_session_with_a_finished_turn():
    wire = Wire(
        "idle",
        chronological(
            record("user.message", "m1", content=[{"type": "text", "text": "hi"}]),
            record("session.status_running", "r1"),
            record("agent.message", "a1", content=[{"type": "text", "text": "hello"}]),
            idle("i1"),
        ),
    )
    snapshot = await driver(wire).reconcile(SCOPE, SESSION)
    assert snapshot.state == "idle"
    assert snapshot.active_root_turn is None and snapshot.required_actions == ()
    assert snapshot.gaps == () and snapshot.cursor == "i1" and snapshot.session == SESSION
    assert [r.url.params["order"] for r in wire.event_requests] == ["desc"]


async def test_idle_session_with_no_events_reports_the_native_state():
    snapshot = await driver(Wire("idle", [])).reconcile(SCOPE, SESSION)
    assert snapshot.state == "idle" and snapshot.cursor == "0" and snapshot.gaps == ()


@pytest.mark.parametrize("status", ["running", "rescheduling"])
async def test_running_root_turn(status: str):
    wire = Wire(
        status,
        chronological(
            record("user.message", "m1", content=[{"type": "text", "text": "hi"}]),
            record("session.status_running", "r1"),
            record("agent.message", "a1", content=[{"type": "text", "text": "working"}]),
        ),
    )
    snapshot = await driver(wire).reconcile(SCOPE, SESSION)
    assert snapshot.state == "running"
    assert snapshot.active_root_turn == "m1"
    assert snapshot.required_actions == () and snapshot.gaps == ()


async def test_running_turn_is_not_confused_with_an_earlier_finished_one():
    wire = Wire(
        "running",
        chronological(
            record("user.message", "m1", content=[]),
            record("session.status_running", "r1"),
            idle("i1"),
            record("user.message", "m2", content=[]),
            record("session.status_running", "r2"),
        ),
    )
    snapshot = await driver(wire).reconcile(SCOPE, SESSION)
    assert snapshot.state == "running" and snapshot.active_root_turn == "m2"


async def test_requires_action_lists_pending_tool_confirmations():
    wire = Wire(
        "idle",
        chronological(
            record("user.message", "m1", content=[]),
            record("session.status_running", "r1"),
            tool_use("c1"),
            tool_use("c2", "agent.custom_tool_use"),
            idle("i1", {"type": "requires_action", "event_ids": ["c1", "c2"]}),
        ),
    )
    snapshot = await driver(wire).reconcile(SCOPE, SESSION)
    assert snapshot.state == "requires_action" and snapshot.active_root_turn == "m1"
    assert [(a.id, a.kind) for a in snapshot.required_actions] == [
        ("c1", "tool_confirmation"),
        ("c2", "function_result"),
    ]
    assert snapshot.required_actions[0].payload["mcp_server_name"] == "tools"
    assert snapshot.gaps == ()


async def test_resolved_action_is_dropped_and_resumed_turn_clears_the_rest():
    paused = [
        record("user.message", "m1", content=[]),
        record("session.status_running", "r1"),
        tool_use("c1"),
        tool_use("c2"),
        idle("i1", {"type": "requires_action", "event_ids": ["c1", "c2"]}),
    ]
    partial = paused + [record("user.tool_result", "u1", tool_use_id="c1", content=[])]
    snapshot = await driver(Wire("idle", chronological(*partial))).reconcile(SCOPE, SESSION)
    assert snapshot.state == "requires_action"
    assert [a.id for a in snapshot.required_actions] == ["c2"]
    resumed = partial + [record("session.status_running", "r2")]
    snapshot = await driver(Wire("running", chronological(*resumed))).reconcile(SCOPE, SESSION)
    assert snapshot.state == "running" and snapshot.required_actions == ()


async def test_terminated_session():
    wire = Wire(
        "terminated",
        chronological(
            record("user.message", "m1", content=[]),
            record("session.status_running", "r1"),
            record("session.status_terminated", "t1"),
        ),
    )
    snapshot = await driver(wire).reconcile(SCOPE, SESSION)
    assert snapshot.state == "terminated"
    assert snapshot.active_root_turn is None and snapshot.required_actions == ()


async def test_native_terminated_wins_over_an_unfinished_log():
    wire = Wire("terminated", chronological(record("session.status_running", "r1")))
    assert (await driver(wire).reconcile(SCOPE, SESSION)).state == "terminated"


async def test_foreign_scope_is_refused_before_any_request():
    wire = Wire("idle", [])
    foreign = SCOPE.model_copy(update={"tenant_id": "foreign"})
    with pytest.raises(ScopeViolation):
        await driver(wire).reconcile(foreign, SESSION)
    other = SESSION.model_copy(update={"id": "sess_2"})
    with pytest.raises(ScopeViolation):
        await driver(wire).reconcile(SCOPE, other)
    assert wire.requests == []


async def test_pagination_follows_the_sdk_cursor_with_a_stable_page_size():
    events = chronological(
        record("session.status_running", "r1"),
        tool_use("c1"),
        idle("i1", {"type": "requires_action", "event_ids": ["c1"]}),
    )
    wire = Wire("idle", events, page=1)
    snapshot = await driver(wire).reconcile(SCOPE, SESSION)
    assert snapshot.state == "requires_action" and snapshot.gaps == ()
    assert [r.url.params.get("page") for r in wire.event_requests] == [None, "1", "2"]
    assert {r.url.params["limit"] for r in wire.event_requests} == {"100"}
    assert {r.url.params["order"] for r in wire.event_requests} == {"desc"}


async def test_walk_stops_after_the_previous_turn_boundary():
    older = [idle(f"old{i}") for i in range(300)]
    wire = Wire(
        "running",
        chronological(*older, record("session.status_running", "r2")),
        page=100,
    )
    snapshot = await driver(wire).reconcile(SCOPE, SESSION)
    assert snapshot.state == "running" and snapshot.gaps == ()
    assert len(wire.event_requests) == 1


async def test_history_over_the_cap_is_reported_as_a_gap():
    filler = [record("agent.message", f"a{i}", content=[]) for i in range(MAX_EVENTS + 10)]
    wire = Wire("running", list(reversed([record("session.status_running", "r1"), *filler])))
    snapshot = await driver(wire).reconcile(SCOPE, SESSION)
    assert snapshot.state == "running"
    assert snapshot.gaps == (HISTORY_TRUNCATED,)
    assert len(wire.event_requests) == MAX_EVENTS // 100 + 1


async def test_truncated_requires_action_keeps_unseen_calls_native():
    newest = [idle("i1", {"type": "requires_action", "event_ids": ["unseen"]})]
    filler = [record("agent.message", f"a{i}", content=[]) for i in range(MAX_EVENTS)]
    snapshot = await driver(Wire("idle", newest + filler)).reconcile(SCOPE, SESSION)
    assert snapshot.state == "requires_action"
    assert snapshot.gaps == (HISTORY_TRUNCATED,)
    assert [(a.id, a.kind) for a in snapshot.required_actions] == [("unseen", "native")]


async def test_exactly_the_cap_is_complete():
    events = [record("agent.message", f"a{i}", content=[]) for i in range(MAX_EVENTS)]
    snapshot = await driver(Wire("idle", events)).reconcile(SCOPE, SESSION)
    assert snapshot.gaps == ()


@pytest.mark.parametrize("status", ["archived", "", None, 3])
async def test_unknown_native_status_is_an_error_not_idle(status: object):
    wire = Wire(status, [])  # type: ignore[arg-type]
    with pytest.raises(ProviderError) as caught:
        await driver(wire).reconcile(SCOPE, SESSION)
    assert caught.value.native_code == "unknown_session_status"
    assert not caught.value.retryable
    assert wire.event_requests == []


def test_native_status_map():
    assert {s: native_state(s) for s in ("idle", "running", "rescheduling", "terminated")} == {
        "idle": "idle",
        "running": "running",
        "rescheduling": "running",
        "terminated": "terminated",
    }


@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings")
async def test_unknown_idle_stop_reason_is_an_error_not_idle():
    wire = Wire(
        "idle", chronological(record("session.status_running", "r1"), idle("i1", {"type": "novel"}))
    )
    with pytest.raises(ProviderError) as caught:
        await driver(wire).reconcile(SCOPE, SESSION)
    assert caught.value.native_code == "unknown_idle_stop_reason"


@pytest.mark.parametrize(
    ("status", "tail"),
    [
        ("running", [idle("i1")]),
        ("idle", [record("session.status_running", "r1")]),
    ],
)
async def test_disagreeing_status_and_log_is_never_idle_and_is_flagged(
    status: str, tail: list[dict[str, object]]
):
    snapshot = await driver(Wire(status, chronological(*tail))).reconcile(SCOPE, SESSION)
    assert snapshot.state == "running"
    assert snapshot.gaps == (STATUS_DISAGREEMENT,)


async def test_provider_failure_is_a_normalized_error():
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"type": "error", "error": {"type": "api_error"}})

    with pytest.raises(ProviderError) as caught:
        await driver(failing).reconcile(SCOPE, SESSION)
    assert caught.value.retryable


async def test_malformed_event_is_a_non_retryable_upstream_error():
    wire = Wire("idle", [{"type": "agent.message", "content": []}])
    with pytest.raises(ProviderError) as caught:
        await driver(wire).reconcile(SCOPE, SESSION)
    assert caught.value.native_code == "malformed_event" and not caught.value.retryable
