"""Regression guard: CLI turn must write zero usage_events rows (REQ-7a).

The CLI path in command.py calls run_turn with billing=BillingExempt(...).
This test exercises the real run_conversation path (not a monkeypatched
run_turn) to catch any future regression where a Billed posture is
accidentally wired into the CLI turn driver.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog.testing
from anthropic.types.beta.sessions.beta_managed_agents_session_end_turn import (
    BetaManagedAgentsSessionEndTurn,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import (
    BetaManagedAgentsSessionStatusIdleEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.adapters.cli.run.command import TurnPath, run_conversation
from daimon.core.stores import usage_events
from daimon.core.turn.io import TurnIO
from mux.testing.drivers.anthropic.ma import (
    MARouter,
    build_fake_anthropic,
    send_events_response,
    sse_response,
)
from sqlalchemy.ext.asyncio import AsyncSession

from packages.adapters.cli.tests.run.conftest import (
    RuntimeFactory,
    assert_io_ran_on,
    cli_principal_count,
)


async def test_cli_turn_writes_no_usage_events(
    db_session: AsyncSession,
    make_rt: RuntimeFactory,
    leg_path: TurnPath,
    turn_ios: list[TurnIO],
) -> None:
    """CLI turn must produce zero usage_events rows.

    Exercises the real run_conversation path with a transport-level fake MA
    server. The fake SSE stream contains a well-formed span.model_request_end
    event (all four required token count fields present) followed by a terminal
    session.status_idle event. Since command.py always passes
    billing=BillingExempt(...) to run_turn, no usage_events row is written —
    this test will fail the moment that invariant is broken.
    """
    span_event = BetaManagedAgentsSpanModelRequestEndEvent(
        id="evt_span_1",
        type="span.model_request_end",
        processed_at=datetime.now(UTC),
        model_request_start_id="evt_span_start_1",
        model_usage=BetaManagedAgentsSpanModelUsage(
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            input_tokens=10,
            output_tokens=5,
        ),
    )
    span_event_dict = span_event.model_dump(mode="json")

    idle_event = BetaManagedAgentsSessionStatusIdleEvent(
        id="evt_idle_1",
        type="session.status_idle",
        processed_at=datetime.now(UTC),
        stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
    )
    idle_event_dict = idle_event.model_dump(mode="json")

    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions/[^/]+/events/stream",
        lambda request, _match: sse_response([span_event_dict, idle_event_dict]),
    )
    router.add(
        "POST",
        r"/v1/sessions/[^/]+/events",
        lambda request, _match: send_events_response(),
    )

    rt = make_rt(build_fake_anthropic(router.dispatch))

    with structlog.testing.capture_logs() as logs:
        exit_code = await run_conversation(
            rt=rt, session_id="test-session-abc", user_message="hello", path=leg_path
        )

    assert exit_code == 0
    # Which I/O ran: the leg's own turn I/O consumed the fake stream, and only the
    # mux bridge touched the identity tables.
    assert_io_ran_on(leg_path, turn_ios)
    assert await cli_principal_count(db_session) == (1 if leg_path == "mux" else 0)

    count = await usage_events.count_all(db_session)
    assert count == 0, "CLI turn must write zero usage_events rows"

    exempt_logs = [e for e in logs if e["event"] == "turn.billing_exempt"]
    assert len(exempt_logs) == 1, "CLI turn must emit exactly one turn.billing_exempt log entry"
    assert exempt_logs[0]["reason"] == "cli-operator-run", (
        "CLI turn's billing_exempt log must carry the cli-operator-run reason"
    )
