"""A routine (headless) fire under an enabled tool-safety policy.

End to end through `headless_runner.run_turn` over the HTTP transport: the
session is created with the third-party toolset overridden to `always_ask`,
and when the agent tries a write, the run refuses it — nobody is there to
approve it.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx
from daimon.core.headless_runner import run_turn
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.testing.ma import MARouter, build_fake_anthropic, sse_response
from daimon.testing.ma_models import ma_agent, ma_environment, ma_session

_NOW = dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.UTC)

_LINEAR_TOOLSET: dict[str, object] = {
    "type": "mcp_toolset",
    "mcp_server_name": "linear",
    "configs": [],
    "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
}


def _events() -> list[dict[str, Any]]:
    return [
        {
            "id": "evt_tu",
            "type": "agent.mcp_tool_use",
            "name": "create_issue",
            "mcp_server_name": "linear",
            "input": {"title": "Nightly bug"},
            "evaluated_permission": "ask",
            "processed_at": _NOW.isoformat(),
        },
        {
            "id": "evt_pause",
            "type": "session.status_idle",
            "stop_reason": {"type": "requires_action", "event_ids": ["evt_tu"]},
            "processed_at": _NOW.isoformat(),
        },
        {
            "id": "evt_msg",
            "type": "agent.message",
            "content": [{"type": "text", "text": "I could not file it."}],
            "processed_at": _NOW.isoformat(),
        },
        {
            "id": "evt_end",
            "type": "session.status_idle",
            "stop_reason": {"type": "end_turn"},
            "processed_at": _NOW.isoformat(),
        },
    ]


def _router(*, creates: list[dict[str, Any]], sends: list[dict[str, Any]]) -> MARouter:
    router = MARouter()

    def handle_create(request: httpx.Request, match: Any) -> httpx.Response:
        creates.append(json.loads(request.content))
        session = ma_session(
            id="ses_1", agent_id="agent_x", model="claude-sonnet-4-5", environment_id="env_x"
        )
        return httpx.Response(200, json=session.model_dump(mode="json"))

    def handle_send(request: httpx.Request, match: Any) -> httpx.Response:
        sends.append(json.loads(request.content))
        return httpx.Response(200, json={"data": None})

    def handle_agent(request: httpx.Request, match: Any) -> httpx.Response:
        agent = ma_agent(
            id="agent_x",
            tools=[_LINEAR_TOOLSET],
            mcp_servers=[{"name": "linear", "type": "url", "url": "https://mcp.linear.app/mcp"}],
        )
        return httpx.Response(200, json=agent.model_dump(mode="json"))

    def handle_env(request: httpx.Request, match: Any) -> httpx.Response:
        env = ma_environment(id="env_x", name="env", created_at=_NOW.isoformat())
        return httpx.Response(200, json=env.model_dump(mode="json"))

    router.add("POST", r"/v1/sessions", handle_create)
    router.add("GET", r"/v1/sessions/[^/]+/events/stream", lambda r, m: sse_response(_events()))
    router.add("POST", r"/v1/sessions/[^/]+/events", handle_send)
    router.add("GET", r"/v1/agents/([^/]+)", handle_agent)
    router.add("GET", r"/v1/environments/([^/]+)", handle_env)
    return router


def _confirmations(sends: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for body in sends for e in body["events"] if e["type"] == "user.tool_confirmation"]


async def test_routine_write_is_denied_when_the_policy_is_on() -> None:
    creates: list[dict[str, Any]] = []
    sends: list[dict[str, Any]] = []
    client = build_fake_anthropic(_router(creates=creates, sends=sends).dispatch)

    tail = await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="file tonight's bugs",
        tool_safety=ToolSafetyPolicy(enabled=True),
    )

    assert tail == "I could not file it."
    (created,) = creates
    agent = created["agent"]
    assert agent["type"] == "agent_with_overrides"
    (toolset,) = [t for t in agent["tools"] if t.get("mcp_server_name") == "linear"]
    assert toolset["default_config"] == {
        "enabled": True,
        "permission_policy": {"type": "always_ask"},
    }
    (sent,) = _confirmations(sends)
    assert sent["result"] == "deny"
    assert sent["tool_use_id"] == "evt_tu"
    assert "linear/create_issue" in sent["deny_message"]


async def test_routine_session_is_unchanged_when_the_policy_is_off() -> None:
    creates: list[dict[str, Any]] = []
    sends: list[dict[str, Any]] = []
    client = build_fake_anthropic(_router(creates=creates, sends=sends).dispatch)

    await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="file tonight's bugs",
    )

    assert creates[0]["agent"] == "agent_x", "no override when enforcement is off"
    (sent,) = _confirmations(sends)
    assert sent["result"] == "allow", "AutoApprove, as before"
