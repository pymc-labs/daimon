"""Regression guard: CLI turn must write zero usage_events rows (REQ-7a).

The CLI path in command.py calls run_turn without wiring usage_record=.
This test exercises the real run_conversation path (not a monkeypatched
run_turn) to catch any future regression where usage_record= is accidentally
wired into the CLI turn driver.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
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
from daimon.adapters.cli.run.command import run_conversation
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core._models import UsageEvent
from daimon.core.config import Settings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.testing.ma import MARouter, build_fake_anthropic, send_events_response, sse_response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio


async def test_cli_turn_writes_no_usage_events(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CLI turn must produce zero usage_events rows.

    Exercises the real run_conversation path with a transport-level fake MA
    server. The fake SSE stream contains a well-formed span.model_request_end
    event (all four required token count fields present) followed by a terminal
    session.status_idle event. Since command.py never passes usage_record= to
    run_turn, no usage_events row is written — this test will fail the moment
    that invariant is broken.
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

    rt = CliRuntime(
        settings=cast(Settings, object()),
        anthropic=build_fake_anthropic(router.dispatch),
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )

    await run_conversation(rt=rt, session_id="test-session-abc", user_message="hello")

    count = await db_session.scalar(select(func.count()).select_from(UsageEvent))
    assert count == 0, "CLI turn must write zero usage_events rows"
