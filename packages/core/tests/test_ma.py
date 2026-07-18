from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from anthropic import APIStatusError, AsyncAnthropic
from anthropic.types.beta import (
    BetaManagedAgentsAgent,
    BetaManagedAgentsModelConfig,
    BetaManagedAgentsSession,
)
from anthropic.types.beta.beta_managed_agents_session_agent import BetaManagedAgentsSessionAgent
from anthropic.types.beta.beta_managed_agents_session_stats import BetaManagedAgentsSessionStats
from anthropic.types.beta.beta_managed_agents_session_usage import BetaManagedAgentsSessionUsage
from anthropic.types.beta.sessions import (
    BetaManagedAgentsAgentMessageEvent,
    BetaManagedAgentsSessionEndTurn,
    BetaManagedAgentsSessionEvent,
    BetaManagedAgentsSessionRequiresAction,
    BetaManagedAgentsSessionRetriesExhausted,
    BetaManagedAgentsSessionStatusIdleEvent,
    BetaManagedAgentsTextBlock,
    BetaManagedAgentsUserMessageEvent,
)
from daimon.core.errors import TurnError
from daimon.core.ma import (
    _INTERRUPT_TERMINAL,
    _TERMINAL_STOP_REASONS,
    delete_sessions_for_account,
    replay_events,
    send_interrupt_and_wait,
    stream_events_with_dedup,
    terminal_stop_reason,
    update_agent_with_version_retry,
)
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response, sse_response


def _user_message(event_id: str, text: str) -> BetaManagedAgentsUserMessageEvent:
    return BetaManagedAgentsUserMessageEvent(
        id=event_id,
        type="user.message",
        processed_at=datetime(2026, 4, 21, tzinfo=UTC),
        content=[BetaManagedAgentsTextBlock(type="text", text=text)],
    )


def _agent_message(event_id: str, text: str) -> BetaManagedAgentsAgentMessageEvent:
    return BetaManagedAgentsAgentMessageEvent(
        id=event_id,
        type="agent.message",
        processed_at=datetime(2026, 4, 21, tzinfo=UTC),
        content=[BetaManagedAgentsTextBlock(type="text", text=text)],
    )


def _idle(event_id: str, stop: str = "end_turn") -> BetaManagedAgentsSessionStatusIdleEvent:
    stop_reason: (
        BetaManagedAgentsSessionEndTurn
        | BetaManagedAgentsSessionRequiresAction
        | BetaManagedAgentsSessionRetriesExhausted
    )
    if stop == "end_turn":
        stop_reason = BetaManagedAgentsSessionEndTurn(type="end_turn")
    elif stop == "retries_exhausted":
        stop_reason = BetaManagedAgentsSessionRetriesExhausted(type="retries_exhausted")
    elif stop == "requires_action":
        stop_reason = BetaManagedAgentsSessionRequiresAction(type="requires_action", event_ids=[])
    else:
        raise ValueError(f"unknown stop reason: {stop!r}")
    return BetaManagedAgentsSessionStatusIdleEvent(
        id=event_id,
        type="session.status_idle",
        processed_at=datetime(2026, 4, 21, tzinfo=UTC),
        stop_reason=stop_reason,
    )


def _make_list_client(events: Sequence[BetaManagedAgentsSessionEvent]) -> Any:
    """Build a transport-level client for list (paginator) tests."""
    router = MARouter()
    event_dicts = [e.model_dump(mode="json") for e in events]

    def handle_list(request: httpx.Request, match: Any) -> httpx.Response:
        return list_response(event_dicts)

    router.add("GET", r"/v1/sessions/[^/]+/events", handle_list)
    return build_fake_anthropic(router.dispatch)


def _make_stream_client(events: Sequence[BetaManagedAgentsSessionEvent]) -> Any:
    """Build a transport-level client for stream tests."""
    router = MARouter()
    event_dicts = [e.model_dump(mode="json") for e in events]

    def handle_stream(request: httpx.Request, match: Any) -> httpx.Response:
        return sse_response(event_dicts)

    router.add("GET", r"/v1/sessions/[^/]+/events/stream", handle_stream)
    return build_fake_anthropic(router.dispatch)


async def test_replay_events_returns_all_events_in_order_when_paginator_yields_multiple() -> None:
    events = [
        _user_message("sevt_1", "hi"),
        _agent_message("sevt_2", "hello"),
        _idle("sevt_3"),
    ]
    client = _make_list_client(events)

    result = await replay_events(client, session_id="sesn_test")

    assert [e.id for e in result] == ["sevt_1", "sevt_2", "sevt_3"], (
        "replay must preserve SDK paginator order end-to-end"
    )


async def test_replay_events_returns_empty_list_when_session_has_no_events() -> None:
    client = _make_list_client([])

    result = await replay_events(client, session_id="sesn_empty")

    assert result == [], "empty session should yield an empty history"


async def test_replay_events_raises_turn_error_when_paginator_stalls_past_timeout() -> None:
    """A stalled upstream paginator must not wedge a reconnecting turn forever.
    `timeout_s` bounds the walk; on timeout the helper raises
    TurnError(kind="upstream") with the TimeoutError preserved as __cause__."""

    async def handle_list_stall(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return list_response([])

    client = build_fake_anthropic(handle_list_stall)

    with pytest.raises(TurnError) as exc_info:
        await replay_events(client, session_id="sesn_test", timeout_s=0.05)

    assert exc_info.value.kind == "upstream"
    assert "0.05" in exc_info.value.message
    assert isinstance(exc_info.value.__cause__, TimeoutError), (
        "the original asyncio TimeoutError must be preserved as __cause__"
    )


async def test_stream_events_with_dedup_skips_ids_already_in_seen_when_iterated() -> None:
    events = [
        _user_message("sevt_1", "a"),
        _agent_message("sevt_2", "b"),
        _idle("sevt_3"),
    ]
    client = _make_stream_client(events)
    seen: set[str] = {"sevt_2"}

    yielded = [
        event async for event in stream_events_with_dedup(client, session_id="sesn_test", seen=seen)
    ]

    assert [e.id for e in yielded] == ["sevt_1", "sevt_3"], (
        "dedup must drop events whose id was pre-populated in seen"
    )


async def test_stream_events_with_dedup_adds_ids_to_seen_when_yielded() -> None:
    events = [_user_message("sevt_1", "a"), _agent_message("sevt_2", "b")]
    client = _make_stream_client(events)
    seen: set[str] = set()

    async for _ in stream_events_with_dedup(client, session_id="sesn_test", seen=seen):
        pass

    assert seen == {"sevt_1", "sevt_2"}, (
        "seen set is the caller's running ledger; helper must mutate it"
    )


async def test_stream_events_with_dedup_returns_empty_when_all_events_already_seen() -> None:
    events = [_user_message("sevt_1", "a"), _agent_message("sevt_2", "b")]
    client = _make_stream_client(events)
    seen: set[str] = {"sevt_1", "sevt_2"}

    yielded = [
        event async for event in stream_events_with_dedup(client, session_id="sesn_test", seen=seen)
    ]

    assert yielded == [], "fully-deduped stream should yield nothing"


async def test_send_interrupt_and_wait_sends_user_interrupt_when_invoked() -> None:
    sent_events: list[dict[str, Any]] = []

    router = MARouter()

    def handle_send(request: httpx.Request, match: Any) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content)
        sent_events.extend(body.get("events", []))
        return httpx.Response(200, json={"data": None})

    def handle_stream(request: httpx.Request, match: Any) -> httpx.Response:
        idle = _idle("sevt_1", stop="end_turn")
        return sse_response([idle.model_dump(mode="json")])

    router.add("POST", r"/v1/sessions/[^/]+/events", handle_send)
    router.add("GET", r"/v1/sessions/[^/]+/events/stream", handle_stream)

    client = build_fake_anthropic(router.dispatch)

    await send_interrupt_and_wait(client, session_id="sesn_test", timeout_s=1.0)

    assert len(sent_events) == 1, "must have sent exactly one event"
    assert sent_events[0] == {"type": "user.interrupt"}, (
        "SDK body carries a single `user.interrupt` event param"
    )


async def test_send_interrupt_and_wait_returns_when_terminal_idle_arrives() -> None:
    router = MARouter()

    def handle_send(request: httpx.Request, match: Any) -> httpx.Response:
        return httpx.Response(200, json={"data": None})

    def handle_stream(request: httpx.Request, match: Any) -> httpx.Response:
        events = [
            _user_message("sevt_a", "hi"),  # not a terminal
            _idle("sevt_b", stop="end_turn"),  # terminal
        ]
        return sse_response([e.model_dump(mode="json") for e in events])

    router.add("POST", r"/v1/sessions/[^/]+/events", handle_send)
    router.add("GET", r"/v1/sessions/[^/]+/events/stream", handle_stream)
    client = build_fake_anthropic(router.dispatch)

    # Should not raise — idle arrives before timeout.
    await send_interrupt_and_wait(client, session_id="sesn_test", timeout_s=1.0)


async def test_send_interrupt_and_wait_treats_requires_action_as_terminal() -> None:
    """`requires_action` means the session is idle (paused on tool approval) —
    for interrupt purposes, this IS terminal. The cancel is complete: the turn
    was running, user hit cancel, and MA confirmed idle (paused on a tool call
    it will never execute now). Helper must return normally, not time out."""
    router = MARouter()

    def handle_send(request: httpx.Request, match: Any) -> httpx.Response:
        return httpx.Response(200, json={"data": None})

    def handle_stream(request: httpx.Request, match: Any) -> httpx.Response:
        idle = _idle("sevt_a", stop="requires_action")
        return sse_response([idle.model_dump(mode="json")])

    router.add("POST", r"/v1/sessions/[^/]+/events", handle_send)
    router.add("GET", r"/v1/sessions/[^/]+/events/stream", handle_stream)
    client = build_fake_anthropic(router.dispatch)

    # Should not raise — requires_action is now terminal for interrupt acks.
    await send_interrupt_and_wait(client, session_id="sesn_test", timeout_s=1.0)


def test_send_interrupt_and_wait_interrupt_terminal_is_superset_of_terminal_stop_reasons() -> None:
    """_INTERRUPT_TERMINAL must be a strict superset of _TERMINAL_STOP_REASONS.

    _TERMINAL_STOP_REASONS lists only the variants
    `send_interrupt_and_wait` treats as terminal. `terminal_stop_reason()` (the
    driver's broader helper) treats ANY status_idle, including requires_action,
    as stream-terminal -- the driver has no approval/resume loop; it finalizes
    requires_action as an actionable failure. _INTERRUPT_TERMINAL extends
    _TERMINAL_STOP_REASONS with requires_action for the cancel-button path
    only. This structural test ensures no one collapses the two constants
    together.
    """
    assert _TERMINAL_STOP_REASONS < _INTERRUPT_TERMINAL, (
        "_TERMINAL_STOP_REASONS must be a strict subset of _INTERRUPT_TERMINAL"
    )
    assert "requires_action" in _INTERRUPT_TERMINAL, (
        "_INTERRUPT_TERMINAL must include requires_action for interrupt acks"
    )
    assert "requires_action" not in _TERMINAL_STOP_REASONS, (
        "_TERMINAL_STOP_REASONS must NOT include requires_action (driver approval loop)"
    )


async def test_send_interrupt_and_wait_raises_turn_error_when_timeout_fires() -> None:
    # Stream yields only non-terminal events, then ends; helper must time out.
    # Transport-level: response body ends after yielding non-terminal events.
    # The SDK's stream parser closes the iterator when the body ends.
    # Since run_turn breaks on status_idle before reaching end, but here we
    # only yield non-terminals, the stream closes without a terminal idle —
    # the asyncio.wait_for fires.
    router = MARouter()

    def handle_send(request: httpx.Request, match: Any) -> httpx.Response:
        return httpx.Response(200, json={"data": None})

    def handle_stream(request: httpx.Request, match: Any) -> httpx.Response:
        non_terminal = _user_message("sevt_a", "still going")
        return sse_response([non_terminal.model_dump(mode="json")])

    router.add("POST", r"/v1/sessions/[^/]+/events", handle_send)
    router.add("GET", r"/v1/sessions/[^/]+/events/stream", handle_stream)
    client = build_fake_anthropic(router.dispatch)

    with pytest.raises(TurnError) as exc_info:
        await send_interrupt_and_wait(client, session_id="sesn_test", timeout_s=0.05)

    assert exc_info.value.kind == "interrupt_timeout", (
        "timeout path must surface as TurnError(kind='interrupt_timeout')"
    )
    assert "0.05" in exc_info.value.message or "timeout" in exc_info.value.message.lower(), (
        "message should indicate timeout duration for operator log context"
    )


def test_terminal_stop_reason_returns_end_turn_on_idle_end_turn() -> None:
    event = _idle("sevt_1", stop="end_turn")
    assert terminal_stop_reason(event) == "end_turn"


def test_terminal_stop_reason_returns_requires_action_on_idle_paused() -> None:
    event = _idle("sevt_2", stop="requires_action")
    # requires_action is reported; callers decide whether to treat it as terminal
    assert terminal_stop_reason(event) == "requires_action"


def test_terminal_stop_reason_returns_none_on_non_idle_event() -> None:
    event = _user_message("sevt_3", "hi")
    assert terminal_stop_reason(event) is None


# ---------------------------------------------------------------------------
# delete_sessions_for_account tests
# ---------------------------------------------------------------------------


def _make_session(
    session_id: str,
    agent_id: str,
    account_id: uuid.UUID | None,
    extra_meta: dict[str, str] | None = None,
) -> BetaManagedAgentsSession:
    """Build a minimal BetaManagedAgentsSession for transport-level fakes."""
    now = datetime.now(UTC).isoformat()
    meta: dict[str, str] = {}
    if account_id is not None:
        meta["daimon_account"] = str(account_id)
    if extra_meta:
        meta.update(extra_meta)
    return BetaManagedAgentsSession(
        id=session_id,
        agent=BetaManagedAgentsSessionAgent(
            id=agent_id,
            description=None,
            mcp_servers=[],
            model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6"),
            name="test-agent",
            skills=[],
            system=None,
            tools=[],
            type="agent",
            version=1,
        ),
        archived_at=None,
        created_at=now,
        environment_id="env_test123",
        metadata=meta,
        resources=[],
        stats=BetaManagedAgentsSessionStats(),
        status="idle",
        title=None,
        type="session",
        updated_at=now,
        usage=BetaManagedAgentsSessionUsage(),
        vault_ids=[],
    )


def _make_agent(agent_id: str, tenant_id: uuid.UUID) -> BetaManagedAgentsAgent:
    """Build a minimal BetaManagedAgentsAgent for transport-level fakes."""
    now = datetime.now(UTC).isoformat()
    return BetaManagedAgentsAgent(
        id=agent_id,
        archived_at=None,
        created_at=now,
        description=None,
        mcp_servers=[],
        metadata={"daimon_tenant": str(tenant_id)},
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6"),
        name="test-agent",
        skills=[],
        system=None,
        tools=[],
        type="agent",
        updated_at=now,
        version=1,
    )


def _build_delete_sessions_client(
    tenant_id: uuid.UUID,
    agents: list[BetaManagedAgentsAgent],
    sessions_by_agent: dict[str, list[BetaManagedAgentsSession]],
    delete_status_by_session: dict[str, int] | None = None,
) -> Any:
    """Build a transport-level client for delete_sessions_for_account tests.

    delete_status_by_session maps session_id -> HTTP status for DELETE responses.
    Absent entries default to 200 (success).
    """
    delete_statuses = delete_status_by_session or {}

    router = MARouter()

    def handle_agents_list(request: httpx.Request, match: Any) -> httpx.Response:
        return list_response([a.model_dump(mode="json") for a in agents])

    def handle_sessions_list(request: httpx.Request, match: Any) -> httpx.Response:
        agent_id = request.url.params.get("agent_id", "")
        sessions = sessions_by_agent.get(agent_id, [])
        return list_response([s.model_dump(mode="json") for s in sessions])

    def handle_session_delete(request: httpx.Request, match: Any) -> httpx.Response:
        session_id = match.group(1)
        status = delete_statuses.get(session_id, 200)
        if status == 200:
            return httpx.Response(200, json={"id": session_id, "type": "session_deleted"})
        return httpx.Response(
            status,
            json={"type": "error", "error": {"type": "api_error", "message": "server error"}},
        )

    router.add("GET", r"/v1/agents", handle_agents_list)
    router.add("GET", r"/v1/sessions", handle_sessions_list)
    router.add("DELETE", r"/v1/sessions/([^/]+)", handle_session_delete)
    return build_fake_anthropic(router.dispatch)


async def test_delete_sessions_for_account_deletes_exactly_target_sessions_across_two_agents() -> (
    None
):
    """3 sessions tagged for target account across 2 agents; 2 for a different account are skipped."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    other_account_id = uuid.uuid4()

    agent1 = _make_agent("agent_1", tenant_id)
    agent2 = _make_agent("agent_2", tenant_id)

    sessions_by_agent: dict[str, list[BetaManagedAgentsSession]] = {
        "agent_1": [
            _make_session("sesn_t1", "agent_1", account_id),
            _make_session("sesn_t2", "agent_1", account_id),
            _make_session("sesn_other1", "agent_1", other_account_id),
        ],
        "agent_2": [
            _make_session("sesn_t3", "agent_2", account_id),
            _make_session("sesn_other2", "agent_2", other_account_id),
        ],
    }

    deleted_ids: list[str] = []

    def handle_agents_list(request: httpx.Request, match: Any) -> httpx.Response:
        return list_response([agent1.model_dump(mode="json"), agent2.model_dump(mode="json")])

    def handle_sessions_list(request: httpx.Request, match: Any) -> httpx.Response:
        agent_id = request.url.params.get("agent_id", "")
        sessions = sessions_by_agent.get(agent_id, [])
        return list_response([s.model_dump(mode="json") for s in sessions])

    def handle_session_delete(request: httpx.Request, match: Any) -> httpx.Response:
        session_id = match.group(1)
        deleted_ids.append(session_id)
        return httpx.Response(200, json={"id": session_id, "type": "session_deleted"})

    router = MARouter()
    router.add("GET", r"/v1/agents", handle_agents_list)
    router.add("GET", r"/v1/sessions", handle_sessions_list)
    router.add("DELETE", r"/v1/sessions/([^/]+)", handle_session_delete)
    client = build_fake_anthropic(router.dispatch)

    from daimon.core.ma import SessionDeletionReport

    result = await delete_sessions_for_account(client, tenant_id=tenant_id, account_id=account_id)

    assert isinstance(result, SessionDeletionReport), "must return SessionDeletionReport"
    assert result.deleted == 3, "should delete exactly 3 sessions tagged for target account"
    assert result.failed == 0, "no failures expected"
    assert sorted(deleted_ids) == ["sesn_t1", "sesn_t2", "sesn_t3"], (
        "only the 3 target sessions must be deleted; different-account sessions must survive"
    )


async def test_delete_sessions_for_account_counts_failed_session_without_aborting_others() -> None:
    """One session whose DELETE returns 500 is counted as failed; other sessions are still deleted."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    agent = _make_agent("agent_1", tenant_id)
    sessions: list[BetaManagedAgentsSession] = [
        _make_session("sesn_ok1", "agent_1", account_id),
        _make_session("sesn_fail", "agent_1", account_id),
        _make_session("sesn_ok2", "agent_1", account_id),
    ]
    client = _build_delete_sessions_client(
        tenant_id,
        [agent],
        {"agent_1": sessions},
        delete_status_by_session={"sesn_fail": 500},
    )

    result = await delete_sessions_for_account(client, tenant_id=tenant_id, account_id=account_id)

    assert result.deleted == 2, "2 successful deletes (ok1 + ok2)"
    assert result.failed == 1, "1 failed delete (sesn_fail with 500)"


async def test_delete_sessions_for_account_treats_404_delete_as_deleted_not_failed() -> None:
    """A session whose DELETE returns 404 (already gone) is counted as deleted (idempotent re-run)."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    agent = _make_agent("agent_1", tenant_id)
    sessions: list[BetaManagedAgentsSession] = [
        _make_session("sesn_gone", "agent_1", account_id),
        _make_session("sesn_present", "agent_1", account_id),
    ]
    client = _build_delete_sessions_client(
        tenant_id,
        [agent],
        {"agent_1": sessions},
        delete_status_by_session={"sesn_gone": 404},
    )

    result = await delete_sessions_for_account(client, tenant_id=tenant_id, account_id=account_id)

    assert result.deleted == 2, "404 is treated as already-gone, so both sessions count as deleted"
    assert result.failed == 0, "404 is not a failure"


async def test_delete_sessions_for_account_returns_zero_counts_when_no_matching_sessions() -> None:
    """With no sessions tagged for the target account, returns SessionDeletionReport(deleted=0, failed=0)."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    other_account_id = uuid.uuid4()

    agent = _make_agent("agent_1", tenant_id)
    sessions: list[BetaManagedAgentsSession] = [
        _make_session("sesn_other", "agent_1", other_account_id),
    ]
    client = _build_delete_sessions_client(tenant_id, [agent], {"agent_1": sessions})

    result = await delete_sessions_for_account(client, tenant_id=tenant_id, account_id=account_id)

    assert result.deleted == 0, "no matching sessions means deleted=0"
    assert result.failed == 0, "no matching sessions means failed=0"


# ---------------------------------------------------------------------------
# update_agent_with_version_retry tests
# ---------------------------------------------------------------------------


def _conflict_response() -> httpx.Response:
    """Return an httpx.Response shaped like MA's 409 stale-version conflict."""
    return httpx.Response(
        409,
        json={
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "Concurrent modification detected. Please fetch the latest version and retry.",
            },
        },
    )


def _build_no_retry_anthropic(router: MARouter) -> AsyncAnthropic:
    """Build an AsyncAnthropic with max_retries=0 backed by the given MARouter.

    The SDK auto-retries 409 by default (max_retries=2). Tests for
    update_agent_with_version_retry must disable SDK retries so the helper's
    own retry logic is exercised in isolation — otherwise the SDK consumes
    the first conflict internally before our code can inspect it.
    """
    return AsyncAnthropic(
        api_key="test",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(router.dispatch),
            base_url="https://api.anthropic.com",
        ),
        max_retries=0,
    )


async def test_update_agent_with_version_retry_refetches_once_when_first_update_conflicts() -> None:
    """On first update returning 409, helper retrieves fresh agent and retries exactly once.

    Router: GET /v1/agents/{id} returns the agent (tracked for retrieve count);
    POST /v1/agents/{id} returns 409 on call 1, 200 on call 2.
    Assert: result is the updated agent, retrieve called twice, update called twice.
    """
    now = datetime.now(UTC)
    agent_id = "agent_test123"
    agent_payload = BetaManagedAgentsAgent(
        id=agent_id,
        archived_at=None,
        created_at=now,
        description=None,
        mcp_servers=[],
        metadata={},
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6"),
        name="test-agent",
        skills=[],
        system="original",
        tools=[],
        type="agent",
        updated_at=now,
        version=1,
    ).model_dump(mode="json")
    updated_payload = {**agent_payload, "system": "updated", "version": 2}

    retrieve_count = 0
    update_count = 0

    def handle_retrieve(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        nonlocal retrieve_count
        retrieve_count += 1
        return httpx.Response(200, json=agent_payload)

    def handle_update(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        nonlocal update_count
        update_count += 1
        if update_count == 1:
            return _conflict_response()
        return httpx.Response(200, json=updated_payload)

    router = MARouter()
    router.add("GET", r"/v1/agents/[^/]+", handle_retrieve)
    router.add("POST", r"/v1/agents/[^/]+", handle_update)
    client = _build_no_retry_anthropic(router)

    async def apply_update(agent: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
        return await client.beta.agents.update(agent.id, version=agent.version, system="updated")

    result = await update_agent_with_version_retry(client, agent_id, apply_update)

    assert result.system == "updated", "result must be the updated agent from the second attempt"
    assert retrieve_count == 2, "must retrieve the agent twice (initial + after conflict)"
    assert update_count == 2, "must attempt the update twice (first conflict + successful retry)"


async def test_update_agent_with_version_retry_reraises_when_error_is_not_conflict() -> None:
    """Non-conflict APIStatusError (e.g. 400) propagates immediately without retry.

    Update returns 400 once; assert the SDK error propagates and only one update attempted.
    """
    now = datetime.now(UTC)
    agent_id = "agent_test456"
    agent_payload = BetaManagedAgentsAgent(
        id=agent_id,
        archived_at=None,
        created_at=now,
        description=None,
        mcp_servers=[],
        metadata={},
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6"),
        name="test-agent",
        skills=[],
        system="original",
        tools=[],
        type="agent",
        updated_at=now,
        version=1,
    ).model_dump(mode="json")

    update_count = 0

    def handle_retrieve(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        return httpx.Response(200, json=agent_payload)

    def handle_update(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        nonlocal update_count
        update_count += 1
        return httpx.Response(
            400,
            json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "bad request"},
            },
        )

    router = MARouter()
    router.add("GET", r"/v1/agents/[^/]+", handle_retrieve)
    router.add("POST", r"/v1/agents/[^/]+", handle_update)
    client = _build_no_retry_anthropic(router)

    async def apply_update(agent: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
        return await client.beta.agents.update(agent.id, version=agent.version, system="bad")

    with pytest.raises(APIStatusError) as exc_info:
        await update_agent_with_version_retry(client, agent_id, apply_update)

    assert exc_info.value.status_code == 400, (
        "non-conflict error must propagate with original status"
    )
    assert update_count == 1, "must not retry on non-conflict errors"


async def test_update_agent_with_version_retry_propagates_second_conflict() -> None:
    """When both update attempts return 409, the second conflict propagates.

    Router returns conflict on both calls; assert ConflictError propagates
    after exactly two update attempts.
    """
    now = datetime.now(UTC)
    agent_id = "agent_test789"
    agent_payload = BetaManagedAgentsAgent(
        id=agent_id,
        archived_at=None,
        created_at=now,
        description=None,
        mcp_servers=[],
        metadata={},
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6"),
        name="test-agent",
        skills=[],
        system="original",
        tools=[],
        type="agent",
        updated_at=now,
        version=1,
    ).model_dump(mode="json")

    update_count = 0

    def handle_retrieve(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        return httpx.Response(200, json=agent_payload)

    def handle_update(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        nonlocal update_count
        update_count += 1
        return _conflict_response()

    router = MARouter()
    router.add("GET", r"/v1/agents/[^/]+", handle_retrieve)
    router.add("POST", r"/v1/agents/[^/]+", handle_update)
    client = _build_no_retry_anthropic(router)

    async def apply_update(agent: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
        return await client.beta.agents.update(agent.id, version=agent.version, system="retry")

    with pytest.raises(APIStatusError) as exc_info:
        await update_agent_with_version_retry(client, agent_id, apply_update)

    assert exc_info.value.status_code == 409, "second conflict must propagate as 409"
    assert update_count == 2, "must attempt exactly two updates before propagating"
