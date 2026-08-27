"""Shared fixtures for the adapter parity suite.

Re-exports the standard schema-per-test DB fixtures from `daimon.testing.db`
(the established pattern — no local Base import) and provides
`build_turn_router`, the single MARouter builder both `DiscordDriver` and
`SlackDriver` use to fake the MA transport for a turn (agent/environment
resolution + SSE event stream). Keeping the router builder here (rather than
duplicated per driver) is the one shared piece of turn-scenario setup; the
platform entry points themselves stay in their own driver modules.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from anthropic.types.beta.sessions.beta_managed_agents_agent_message_event import (
    BetaManagedAgentsAgentMessageEvent,
)
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
from anthropic.types.beta.sessions.beta_managed_agents_text_block import (
    BetaManagedAgentsTextBlock,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.testing.db import db_engine as db_engine  # noqa: F401
from daimon.testing.db import db_session as db_session  # noqa: F401
from daimon.testing.db import db_session_factory as db_session_factory  # noqa: F401
from daimon.testing.ma import (
    EMPTY_CLOUD_CONFIG,
    MARouter,
    list_response,
    send_events_response,
    sse_response,
)

from .drivers.discord_driver import DiscordDriver
from .drivers.protocol import PlatformDriver
from .drivers.slack_driver import SlackDriver

__all__ = ["build_turn_router", "AGENT_TEXT", "AGENT_ID", "ENV_ID", "MODEL_ID"]

AGENT_TEXT = "Hello from the agent!"
AGENT_ID = "ag_parity_test"
ENV_ID = "env_parity_test"
MODEL_ID = "claude-sonnet-4-6"


def build_turn_router(
    tenant_id_str: str,
    *,
    agent_id: str = AGENT_ID,
    env_id: str = ENV_ID,
    model_id: str = MODEL_ID,
    agent_text: str = AGENT_TEXT,
    usage_event_id: str = "evt_parity_usage",
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> MARouter:
    """Build a MARouter handling agent/environment resolution + a turn SSE stream.

    Routes registered:
      GET  /v1/agents               -- list, for resolver tag lookup
      GET  /v1/agents/{id}          -- retrieve, for re-fetch after resolve
      GET  /v1/environments         -- list, for resolver tag lookup
      GET  /v1/environments/{id}    -- retrieve, for re-fetch after resolve
      POST /v1/sessions/{id}/events -- send-initial event (run_turn)
      GET  /v1/sessions/{id}/events/stream -- SSE turn stream (run_turn)

    The SSE stream emits an `agent.message` event, a `span.model_request_end`
    event (the event `usage_record` binds on), then `session.status_idle` --
    driving both the final agent text and the usage_events/tenant_ledger
    write through the REAL run_turn driver (D-01).
    """
    agent_item = BetaManagedAgentsAgent(
        id=agent_id,
        type="agent",
        name="test-agent",
        model=BetaManagedAgentsModelConfig(id=model_id),
        metadata={
            MA_METADATA_KEY_TENANT: tenant_id_str,
            MA_METADATA_KEY_NAME: "test-agent",
        },
        description=None,
        created_at="2026-06-14T00:00:00Z",  # pyright: ignore[reportArgumentType]
        updated_at="2026-06-14T00:00:00Z",  # pyright: ignore[reportArgumentType]
        version=1,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    ).model_dump(mode="json")

    env_item = BetaEnvironment(
        id=env_id,
        type="environment",
        name="test-env",
        config=EMPTY_CLOUD_CONFIG,
        metadata={
            MA_METADATA_KEY_TENANT: tenant_id_str,
            MA_METADATA_KEY_NAME: "test-env",
        },
        description="",
        created_at="2026-06-14T00:00:00Z",
        updated_at="2026-06-14T00:00:00Z",
    ).model_dump(mode="json")

    now = datetime.now(UTC)
    agent_message_event = BetaManagedAgentsAgentMessageEvent(
        id="evt_parity_msg",
        type="agent.message",
        processed_at=now,
        content=[BetaManagedAgentsTextBlock(type="text", text=agent_text)],
    ).model_dump(mode="json")

    model_request_end_event = BetaManagedAgentsSpanModelRequestEndEvent(
        id=usage_event_id,
        is_error=False,
        model_request_start_id="start_parity",
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        processed_at=now,
        type="span.model_request_end",
    ).model_dump(mode="json")

    idle_event = BetaManagedAgentsSessionStatusIdleEvent(
        id="evt_parity_idle",
        type="session.status_idle",
        processed_at=now,
        stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
    ).model_dump(mode="json")

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([agent_item]))
    router.add(
        "GET",
        r"/v1/agents/[^/]+",
        lambda req, _m: httpx.Response(200, json=agent_item),
    )
    router.add("GET", r"/v1/environments", lambda req, _m: list_response([env_item]))
    router.add(
        "GET",
        r"/v1/environments/[^/]+",
        lambda req, _m: httpx.Response(200, json=env_item),
    )
    router.add(
        "POST",
        r"/v1/sessions/[^/]+/events",
        lambda req, _m: send_events_response(),
    )
    # The SSE script above ends in `session.status_idle` -- a real terminal
    # event. This matters: `run_turn` now asks MA for the session's status
    # (`GET /v1/sessions/{id}`) whenever a stream ends or stalls WITHOUT one
    # (a "clean close" or read-timeout eventless cycle -- see driver.py's
    # `_EventlessCycle`). A parity scenario whose SSE script does NOT end in
    # a terminal event needs a `GET /v1/sessions/[^/]+$` route registered too
    # (`daimon.testing.ma.session_response` builds the response), or the
    # driver's status check 404s against this router and the turn finalizes
    # as an unexpected upstream failure instead of exercising the scenario.
    router.add(
        "GET",
        r"/v1/sessions/[^/]+/events/stream",
        lambda req, _m: sse_response([agent_message_event, model_request_end_event, idle_event]),
    )
    return router


@pytest.fixture(params=["discord", "slack"], ids=["discord", "slack"])
def driver(request: pytest.FixtureRequest) -> PlatformDriver:
    """Yield the PlatformDriver under test, parametrized over both platforms."""
    if request.param == "discord":
        return DiscordDriver()
    return SlackDriver()
