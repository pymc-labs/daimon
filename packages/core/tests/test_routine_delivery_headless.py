"""FEAT-085 review regression: "did the agent already post?" read off the REAL
headless driver's final state (SSE events → reducer → `on_state`), including
a post made through the MCP search interface's `call_tool` proxy."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import httpx
import pytest
from daimon.core.headless_runner import run_turn
from daimon.core.routine_delivery import agent_posted_to
from daimon.core.stores.domain import RoutineRow
from daimon.core.turn.state import TurnState
from daimon.testing.ma import MARouter, build_fake_anthropic, sse_response
from daimon.testing.ma_models import ma_agent, ma_environment, ma_session

_NOW = dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.UTC)


def _events(tool_name: str, tool_input: dict[str, object], *, is_error: bool) -> list[Any]:
    at = _NOW.isoformat()
    return [
        {
            "id": "evt_tu",
            "type": "agent.mcp_tool_use",
            "name": tool_name,
            "mcp_server_name": "daimon-mcp",
            "input": tool_input,
            "processed_at": at,
        },
        {
            "id": "evt_tr",
            "type": "agent.mcp_tool_result",
            "mcp_tool_use_id": "evt_tu",
            "content": [{"type": "text", "text": "posted"}],
            "is_error": is_error,
            "processed_at": at,
        },
        {
            "id": "evt_msg",
            "type": "agent.message",
            "content": [{"type": "text", "text": "Weekly summary."}],
            "processed_at": at,
        },
        {
            "id": "evt_end",
            "type": "session.status_idle",
            "stop_reason": {"type": "end_turn"},
            "processed_at": at,
        },
    ]


async def _final_state(events: list[Any]) -> TurnState:
    router = MARouter()
    router.add(
        "POST",
        r"/v1/sessions",
        lambda r, m: httpx.Response(
            200,
            json=ma_session(
                id="ses_1", agent_id="agent_x", model="claude-sonnet-4-5", environment_id="env_x"
            ).model_dump(mode="json"),
        ),
    )
    router.add("GET", r"/v1/sessions/[^/]+/events/stream", lambda r, m: sse_response(events))
    router.add(
        "POST", r"/v1/sessions/[^/]+/events", lambda r, m: httpx.Response(200, json={"data": None})
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda r, m: httpx.Response(200, json=ma_agent(id="agent_x").model_dump(mode="json")),
    )
    router.add(
        "GET",
        r"/v1/environments/([^/]+)",
        lambda r, m: httpx.Response(
            200,
            json=ma_environment(id="env_x", name="env", created_at=_NOW.isoformat()).model_dump(
                mode="json"
            ),
        ),
    )
    states: list[TurnState] = []
    await run_turn(
        anthropic=build_fake_anthropic(router.dispatch),
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="go",
        on_state=states.append,
    )
    (state,) = states
    return state


def _row(destination_kind: str, destination_id: str) -> RoutineRow:
    return RoutineRow.model_validate(
        {
            "id": uuid.uuid4(),
            "tenant_id": uuid.uuid4(),
            "created_by_user_id": "U1",
            "agent_id": "ag",
            "agent_name": "daimon",
            "cron_expr": "0 9 * * 1",
            "timezone": "UTC",
            "trigger_message": "go",
            "enabled": True,
            "next_fire_at": None,
            "last_fired_at": None,
            "last_error": None,
            "last_result_tail": None,
            "destination_kind": destination_kind,
            "destination_id": destination_id,
            "created_at": _NOW,
            "updated_at": _NOW,
        }
    )


@pytest.mark.parametrize(
    ("tool_name", "tool_input", "is_error", "row", "posted"),
    [
        # The MCP search interface: call_tool(name=send_message, arguments=...).
        (
            "call_tool",
            {"name": "send_message", "arguments": {"channel_id": "C1", "content": "hi"}},
            False,
            ("channel", "C1"),
            True,
        ),
        (
            "call_tool",
            {"name": "send_message", "arguments": {"channel_id": "C1", "content": "hi"}},
            True,
            ("channel", "C1"),
            False,
        ),
        (
            "call_tool",
            {"name": "read_channel", "arguments": {"channel_id": "C1"}},
            False,
            ("channel", "C1"),
            False,
        ),
        # A Slack thread destination: only a post into that exact thread counts.
        (
            "send_message",
            {"channel_id": "C1:1717.5", "content": "hi"},
            False,
            ("thread", "C1:1717.5"),
            True,
        ),
        (
            "send_message",
            {"channel_id": "C1", "content": "hi"},
            False,
            ("thread", "C1:1717.5"),
            False,
        ),
    ],
)
async def test_post_detection_on_the_real_driver_state(
    tool_name: str,
    tool_input: dict[str, object],
    is_error: bool,
    row: tuple[str, str],
    posted: bool,
) -> None:
    state = await _final_state(_events(tool_name, tool_input, is_error=is_error))
    assert agent_posted_to(state, _row(*row)) is posted
    assert json.dumps(tool_input)  # the input survived the SSE round trip as JSON
