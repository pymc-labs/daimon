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
from daimon.core.config import McpSettings
from daimon.core.headless_runner import run_turn
from daimon.core.sessions import create_session
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.testing.ma import MARouter, build_fake_anthropic, sse_response
from daimon.testing.ma_models import ma_agent, ma_environment, ma_session
from pydantic import HttpUrl

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


_PUBLIC_URL = "https://mcp.example.com/mcp"


def _toolset(server: str) -> dict[str, object]:
    return {
        "type": "mcp_toolset",
        "mcp_server_name": server,
        "configs": [],
        "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
    }


async def _create_with_reserved_name(public_url: str | None) -> dict[str, Any]:
    """Real `create_session` for an agent whose `daimon-mcp` points elsewhere."""
    creates: list[dict[str, Any]] = []
    client = build_fake_anthropic(_router(creates=creates, sends=[]).dispatch)
    agent = ma_agent(
        id="agent_x",
        tools=[_toolset("daimon-mcp"), _toolset("linear")],
        mcp_servers=[
            {"name": "daimon-mcp", "type": "url", "url": "https://third-party.example/mcp"},
            {"name": "linear", "type": "url", "url": "https://mcp.linear.app/mcp"},
        ],
    )
    await create_session(
        client,
        agent=agent,
        environment=ma_environment(id="env_x", name="env", created_at=_NOW.isoformat()),
        mcp_settings=McpSettings(public_url=HttpUrl(public_url) if public_url else None),
        tool_safety=ToolSafetyPolicy(enabled=True),
    )
    (created,) = creates
    return created["agent"]


def _policies(agent: dict[str, Any]) -> dict[str, str]:
    return {
        t["mcp_server_name"]: t["default_config"]["permission_policy"]["type"]
        for t in agent["tools"]
        if t.get("type") == "mcp_toolset"
    }


async def test_a_foreign_url_under_the_reserved_name_is_healed_before_it_is_trusted() -> None:
    agent = await _create_with_reserved_name(_PUBLIC_URL)

    assert agent["type"] == "agent_with_overrides"
    servers = {s["name"]: s["url"] for s in agent["mcp_servers"]}
    assert servers["daimon-mcp"] == _PUBLIC_URL, (
        "the reserved name must reach daimon, not a third party"
    )
    assert _policies(agent) == {"daimon-mcp": "always_allow", "linear": "always_ask"}


async def test_without_a_daimon_endpoint_the_reserved_name_is_gated_like_any_server() -> None:
    agent = await _create_with_reserved_name(None)

    servers = {s["name"]: s["url"] for s in agent["mcp_servers"]}
    assert servers["daimon-mcp"] == "https://third-party.example/mcp"
    assert _policies(agent) == {"daimon-mcp": "always_ask", "linear": "always_ask"}
