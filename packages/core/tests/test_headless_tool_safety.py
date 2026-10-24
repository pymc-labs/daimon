"""A routine (headless) fire under an enabled tool-safety policy.

End to end through `headless_runner.run_turn` over the HTTP transport: the
session is created with the third-party toolset overridden to `always_ask`,
and when the agent tries a write, the run refuses it — nobody is there to
approve it.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsSession
from daimon.core.config import McpSettings
from daimon.core.headless_runner import run_turn
from daimon.core.sessions import create_session
from daimon.core.tool_safety import OPEN_TOOL_SAFETY, ToolSafetyPolicy
from daimon.core.turn.errors import AdmissionDenied
from daimon.testing.ma_models import (
    ma_agent,
    ma_environment,
    with_coordinator,
    with_hidden_capability,
)
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, sse_response
from mux.testing.drivers.anthropic.ma_models import ma_session
from pydantic import HttpUrl

from .turn_path_support import HeadlessIdentity, TurnIOSpy, TurnPath

_NOW = dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.UTC)

_LINEAR_TOOLSET: dict[str, object] = {
    "type": "mcp_toolset",
    "mcp_server_name": "linear",
    "configs": [],
    "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
}


def _events(
    server: str = "linear", name: str = "create_issue", tool_input: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    return [
        {
            "id": "evt_tu",
            "type": "agent.mcp_tool_use",
            "name": name,
            "mcp_server_name": server,
            "input": tool_input if tool_input is not None else {"title": "Nightly bug"},
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


def _router(
    *,
    creates: list[dict[str, Any]],
    sends: list[dict[str, Any]],
    events: list[dict[str, Any]] | None = None,
    agent_tools: list[dict[str, Any]] | None = None,
    agent_servers: list[dict[str, Any]] | None = None,
    unconstrainable: Callable[[BetaManagedAgentsSession], BetaManagedAgentsSession] | None = None,
    updates: list[dict[str, Any]] | None = None,
) -> MARouter:
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
            tools=agent_tools or [_LINEAR_TOOLSET],
            mcp_servers=agent_servers
            or [{"name": "linear", "type": "url", "url": "https://mcp.linear.app/mcp"}],
        )
        return httpx.Response(200, json=agent.model_dump(mode="json"))

    def handle_env(request: httpx.Request, match: Any) -> httpx.Response:
        env = ma_environment(id="env_x", name="env", created_at=_NOW.isoformat())
        return httpx.Response(200, json=env.model_dump(mode="json"))

    router.add("POST", r"/v1/sessions", handle_create)
    # The unattended gate reads the session before each send.
    frozen = ma_session(
        id="ses_1",
        agent=ma_agent(
            id="agent_x",
            tools=agent_tools or [_LINEAR_TOOLSET],
            mcp_servers=agent_servers
            or [{"name": "linear", "type": "url", "url": "https://mcp.linear.app/mcp"}],
        ),
    )
    if unconstrainable is not None:
        frozen = unconstrainable(frozen)
    router.add(
        "GET",
        r"/v1/sessions/ses_1$",
        lambda _r, _m: httpx.Response(200, json=frozen.model_dump(mode="json")),
    )

    def handle_update(request: httpx.Request, match: Any) -> httpx.Response:
        if updates is not None:
            updates.append(json.loads(request.content))
        return router.dispatch(httpx.Request("GET", request.url))

    router.add("POST", r"/v1/sessions/ses_1$", handle_update)
    router.add(
        "GET", r"/v1/sessions/[^/]+/events/stream", lambda r, m: sse_response(events or _events())
    )
    router.add("POST", r"/v1/sessions/[^/]+/events", handle_send)
    router.add("GET", r"/v1/agents/([^/]+)", handle_agent)
    router.add("GET", r"/v1/environments/([^/]+)", handle_env)
    return router


def _confirmations(sends: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for body in sends for e in body["events"] if e["type"] == "user.tool_confirmation"]


async def test_routine_write_is_denied_when_the_policy_is_on(
    turn_path: TurnPath, headless_identity: HeadlessIdentity, turn_io_used: TurnIOSpy
) -> None:
    creates: list[dict[str, Any]] = []
    sends: list[dict[str, Any]] = []
    client = build_fake_anthropic(_router(creates=creates, sends=sends).dispatch)

    tail = await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="file tonight's bugs",
        tool_safety=ToolSafetyPolicy(enabled=True),
        tenant_id=headless_identity.tenant_id,
        account_id=headless_identity.account_id,
        turn_path=turn_path,
    )

    turn_io_used.assert_ran(turn_path)
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


async def test_routine_session_is_unchanged_when_the_policy_is_off(
    turn_path: TurnPath, headless_identity: HeadlessIdentity, turn_io_used: TurnIOSpy
) -> None:
    creates: list[dict[str, Any]] = []
    sends: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    client = build_fake_anthropic(_router(creates=creates, sends=sends, updates=updates).dispatch)

    await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="file tonight's bugs",
        tool_safety=OPEN_TOOL_SAFETY,
        tenant_id=headless_identity.tenant_id,
        account_id=headless_identity.account_id,
        turn_path=turn_path,
    )

    turn_io_used.assert_ran(turn_path)
    assert creates[0]["agent"] == "agent_x", "no override when enforcement is off"
    # The fake keeps serving the frozen snapshot, so every gate rewrites it.
    assert updates
    (toolset,) = updates[0]["agent"]["tools"]
    assert toolset["default_config"]["permission_policy"] == {"type": "always_ask"}
    (sent,) = _confirmations(sends)
    assert sent["result"] == "allow", "off allows an ordinary third-party call"


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


@pytest.mark.parametrize("trusted", [("linear",), ()], ids=["trusted", "untrusted"])
async def test_routine_read_marked_read_only_by_a_trusted_server_runs(
    monkeypatch: pytest.MonkeyPatch, trusted: tuple[str, ...], headless_identity: HeadlessIdentity
) -> None:
    """The production wiring: a routine's decider reads a trusted server's
    catalogue, so a noun-first read the server marks `readOnlyHint` runs where
    its name alone would have been refused. With no database the read is
    anonymous. A server outside `trusted_annotations` is never listed and its
    name decides."""
    from daimon.core import mcp_tool_annotations
    from daimon.core.mcp_oauth import discovery

    tokens: list[str | None] = []

    async def _catalogue(url: str, token: str | None) -> list[dict[str, Any]]:
        assert url == "https://mcp.linear.app/mcp"
        tokens.append(token)

        def handle(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == url
            assert request.headers.get("authorization") is None
            body = json.loads(request.content)
            if body["method"] == "notifications/initialized":
                return httpx.Response(204)
            result = (
                {
                    "tools": [
                        {"name": "memory_flag_list", "annotations": {"readOnlyHint": True}},
                        {"name": "create_issue", "annotations": {"readOnlyHint": False}},
                    ]
                }
                if body["method"] == "tools/list"
                else {}
            )
            return httpx.Response(200, json={"id": body["id"], "result": result})

        original = httpx.AsyncClient

        def client(**kwargs: Any) -> httpx.AsyncClient:
            return original(transport=httpx.MockTransport(handle), **kwargs)

        with monkeypatch.context() as scoped:
            scoped.setattr(httpx, "AsyncClient", client)
            scoped.setattr(discovery, "assert_public_https_url", lambda *a, **k: None)
            return list(await discovery.read_mcp_catalogue(url, token))

    monkeypatch.setattr(mcp_tool_annotations, "read_mcp_catalogue", _catalogue)
    creates: list[dict[str, Any]] = []
    sends: list[dict[str, Any]] = []
    router = _router(creates=creates, sends=sends)
    events = _events()
    events[0] = {**events[0], "name": "memory_flag_list", "input": {}}
    # The session freezes the agent with its servers, which is where the
    # decider finds the URL to read.
    frozen = ma_session(
        id="ses_1",
        agent=ma_agent(
            id="agent_x",
            tools=[_LINEAR_TOOLSET],
            mcp_servers=[{"name": "linear", "type": "url", "url": "https://mcp.linear.app/mcp"}],
        ),
        environment_id="env_x",
    ).model_dump(mode="json")
    router.routes[:0] = [
        ("POST", re.compile(r"/v1/sessions$"), lambda r, m: httpx.Response(200, json=frozen)),
        (
            "GET",
            re.compile(r"/v1/sessions/[^/]+/events/stream"),
            lambda r, m: sse_response(events),
        ),
    ]
    client = build_fake_anthropic(router.dispatch)

    await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="list the flags",
        tool_safety=ToolSafetyPolicy(enabled=True, trusted_annotations=trusted),
        tenant_id=headless_identity.tenant_id,
        account_id=headless_identity.account_id,
    )

    (sent,) = _confirmations(sends)
    if trusted:
        assert sent["result"] == "allow", "readOnlyHint made memory_flag_list a read"
        assert tokens == [None]
    else:
        assert sent["result"] == "deny", "the name decides: a noun-first read is a write"
        assert tokens == [], "no tools/list for an untrusted server"


async def _create_with_rules(
    rule_tools: frozenset[str], *, policy: ToolSafetyPolicy
) -> dict[str, Any]:
    creates: list[dict[str, Any]] = []
    client = build_fake_anthropic(_router(creates=creates, sends=[]).dispatch)
    agent = ma_agent(
        id="agent_x",
        tools=[_toolset("daimon-mcp"), _toolset("linear")],
        mcp_servers=[
            {"name": "daimon-mcp", "type": "url", "url": _PUBLIC_URL},
            {"name": "linear", "type": "url", "url": "https://mcp.linear.app/mcp"},
        ],
    )
    await create_session(
        client,
        agent=agent,
        environment=ma_environment(id="env_x", name="env", created_at=_NOW.isoformat()),
        mcp_settings=McpSettings(public_url=HttpUrl(_PUBLIC_URL)),
        tool_safety=policy,
        daimon_rule_tools=rule_tools,
    )
    (created,) = creates
    return created["agent"]


async def test_a_session_for_an_agent_with_daimon_rules_asks_on_call_tool_and_the_named_tool() -> (
    None
):
    agent = await _create_with_rules(
        frozenset({"hubspot_update_objects"}),
        policy=ToolSafetyPolicy(enabled=True, agent_rules=True),
    )

    (daimon,) = [t for t in agent["tools"] if t.get("mcp_server_name") == "daimon-mcp"]
    assert daimon["default_config"]["permission_policy"] == {"type": "always_allow"}
    asked = sorted(
        c["name"] for c in daimon["configs"] if c["permission_policy"]["type"] == "always_ask"
    )
    # Enrollment requires a person's card independently of agent-specific rules.
    assert asked == [
        "add_skill",
        "call_tool",
        "create_watch",
        "hubspot_update_objects",
        "resume_watch",
        "rotate_watch_key",
    ]


async def test_a_session_for_an_agent_without_daimon_rules_is_exactly_as_before() -> None:
    policy = ToolSafetyPolicy(enabled=True, agent_rules=True)
    without = await _create_with_rules(frozenset(), policy=policy)
    inert = await _create_with_rules(
        frozenset({"hubspot_update_objects"}), policy=ToolSafetyPolicy(enabled=True)
    )

    assert without == inert, "rules that are not applied leave the session's tools alone"
    (daimon,) = [t for t in without["tools"] if t.get("mcp_server_name") == "daimon-mcp"]
    assert [c["name"] for c in daimon["configs"]] == [
        "add_skill",
        "create_watch",
        "resume_watch",
        "rotate_watch_key",
    ]


def test_no_production_signature_defaults_the_policy() -> None:
    """Design test 6: safety off is an explicit policy; omitting one is an error."""
    import dataclasses
    import inspect

    from daimon.core import headless_runner, session_snapshot, sessions, smoke
    from daimon.core.turn.deps import TurnDeps

    for fn in (
        headless_runner.run_turn,
        headless_runner.run_turn_impl,
        sessions.create_session,
        sessions.create_isolated_session,
        sessions.project_session_resources,
        session_snapshot.desired_snapshot,
        smoke.run_smoke_check,
    ):
        param = inspect.signature(fn).parameters["tool_safety"]
        assert param.default is inspect.Parameter.empty, fn.__qualname__
    field = next(f for f in dataclasses.fields(TurnDeps) if f.name == "tool_safety")
    assert field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING


async def test_run_turn_without_a_policy_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="tool_safety"):
        await run_turn(  # type: ignore[call-arg]  # pyright: ignore[reportCallIssue]
            anthropic=build_fake_anthropic(MARouter().dispatch),
            agent_id="ag",
            environment_id="env",
            trigger_message="hi",
        )


@pytest.mark.parametrize("enabled", [False, True], ids=["off", "on"])
async def test_a_routine_never_sends_microsoft_mail_policy_on_or_off(
    enabled: bool, headless_identity: HeadlessIdentity
) -> None:
    """Design §3 routines row: the one unattended decider, never AutoApprove; the
    session asks on Daimon's card-only actions and the routine refuses them."""
    daimon = {"name": "daimon-mcp", "type": "url", "url": _PUBLIC_URL}
    creates: list[dict[str, Any]] = []
    sends: list[dict[str, Any]] = []
    client = build_fake_anthropic(
        _router(
            creates=creates,
            sends=sends,
            events=_events("daimon-mcp", "microsoft_send", {"action": "send_mail"}),
            agent_tools=[_toolset("daimon-mcp")],
            agent_servers=[daimon],
        ).dispatch
    )
    await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="mail the summary",
        mcp_settings=McpSettings(public_url=HttpUrl(_PUBLIC_URL)),
        tool_safety=ToolSafetyPolicy(enabled=enabled),
        tenant_id=headless_identity.tenant_id,
        account_id=headless_identity.account_id,
    )
    (created,) = creates
    (toolset,) = created["agent"]["tools"]
    asked = {c["name"] for c in toolset["configs"]}
    assert "microsoft_send" in asked
    (sent,) = _confirmations(sends)
    assert sent["result"] == "deny"


_BUILTINS: dict[str, object] = {
    "type": "agent_toolset_20260401",
    "configs": [],
    "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
}


@pytest.mark.parametrize(
    "unconstrainable",
    [with_coordinator, with_hidden_capability],
    ids=["coordinator", "hidden_field"],
)
@pytest.mark.parametrize("enabled", [True, False])
async def test_a_routine_on_a_coordinator_session_is_refused_before_its_message(
    enabled: bool,
    unconstrainable: Callable[[BetaManagedAgentsSession], BetaManagedAgentsSession],
    headless_identity: HeadlessIdentity,
) -> None:
    """The roster cannot be removed from a live session, so nothing is sent."""
    creates: list[dict[str, Any]] = []
    sends: list[dict[str, Any]] = []
    client = build_fake_anthropic(
        _router(creates=creates, sends=sends, unconstrainable=unconstrainable).dispatch
    )

    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await run_turn(
            anthropic=client,
            agent_id="agent_x",
            environment_id="env_x",
            trigger_message="file tonight's bugs",
            tool_safety=ToolSafetyPolicy(enabled=enabled),
            tenant_id=headless_identity.tenant_id,
            account_id=headless_identity.account_id,
        )

    assert sends == []


async def test_a_routine_disables_its_sandbox_before_sending(
    headless_identity: HeadlessIdentity,
) -> None:
    """Routines cannot generate files using sandbox tools."""
    creates: list[dict[str, Any]] = []
    sends: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    tools = [_BUILTINS, _LINEAR_TOOLSET]
    client = build_fake_anthropic(
        _router(creates=creates, sends=sends, agent_tools=tools, updates=updates).dispatch
    )

    await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="write the report",
        tool_safety=ToolSafetyPolicy(enabled=True),
        tenant_id=headless_identity.tenant_id,
        account_id=headless_identity.account_id,
    )

    assert updates, "the unattended gate rewrites the MCP toolset before the message"
    for body in updates:
        (builtins,) = [t for t in body["agent"]["tools"] if t["type"] == "agent_toolset_20260401"]
        assert builtins["default_config"]["enabled"] is False
        assert all(config["enabled"] is False for config in builtins.get("configs", []))


@pytest.mark.parametrize("enabled", [True, False])
async def test_a_routines_own_daimon_tools_pause_for_the_decider(
    enabled: bool, headless_identity: HeadlessIdentity
) -> None:
    """Trusted is not unasked: Daimon's ordinary tools pause, and the decider allows them."""
    creates: list[dict[str, Any]] = []
    sends: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    client = build_fake_anthropic(
        _router(
            creates=creates,
            sends=sends,
            updates=updates,
            agent_tools=[_toolset("daimon-mcp")],
            agent_servers=[{"name": "daimon-mcp", "type": "url", "url": _PUBLIC_URL}],
            events=_events(server="daimon-mcp", name="list_agents", tool_input={}),
        ).dispatch
    )

    await run_turn(
        anthropic=client,
        agent_id="agent_x",
        environment_id="env_x",
        trigger_message="list them",
        tool_safety=ToolSafetyPolicy(enabled=enabled),
        mcp_settings=McpSettings(public_url=HttpUrl(_PUBLIC_URL)),
        tenant_id=headless_identity.tenant_id,
        account_id=headless_identity.account_id,
    )

    toolset = updates[-1]["agent"]["tools"][0]
    assert toolset["mcp_server_name"] == "daimon-mcp"
    assert toolset["default_config"]["permission_policy"] == {"type": "always_ask"}
    (sent,) = _confirmations(sends)
    assert sent["result"] == "allow"
