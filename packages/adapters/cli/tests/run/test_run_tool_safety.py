"""`daimon run` under the unattended tool gate, through the real driver.

Design tests 1 and 2: a session frozen while tool safety was off is re-gated
before the operator's message, so a write pauses and is refused; a foreign
`daimon-mcp` URL on an old snapshot is healed before the send, so the
built-in exemption never covers it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsSession
from daimon.adapters.cli.run import command as cmd_mod
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.tenants import get_tenant
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.core.turn.errors import AdmissionDenied
from daimon.testing.ma import MARouter
from daimon.testing.ma_models import ma_agent, with_coordinator, with_hidden_capability
from mux.testing.drivers.anthropic.ma import build_fake_anthropic, json_body, sse_response
from mux.testing.drivers.anthropic.ma_models import ma_session
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_SESSION = "ses_cli"
_PUBLIC = "https://daimon.example/mcp"
_LINEAR = {"name": "linear", "type": "url", "url": "https://mcp.linear.app/mcp"}


def _toolset(server: str, *, per_tool: str | None = None) -> dict[str, Any]:
    return {
        "type": "mcp_toolset",
        "mcp_server_name": server,
        "configs": (
            [{"name": per_tool, "enabled": True, "permission_policy": {"type": "always_allow"}}]
            if per_tool
            else []
        ),
        "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
    }


def _events(server: str, tool: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "evt_tu",
            "type": "agent.mcp_tool_use",
            "name": tool,
            "mcp_server_name": server,
            "input": {},
            "evaluated_permission": "ask",
        },
        {
            "id": "evt_pause",
            "type": "session.status_idle",
            "stop_reason": {"type": "requires_action", "event_ids": ["evt_tu"]},
        },
        {
            "id": "evt_msg",
            "type": "agent.message",
            "content": [{"type": "text", "text": "ok"}],
        },
        {"id": "evt_end", "type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
    ]


class _Session:
    """The operator's existing session; `wire` is every mutation, in order."""

    def __init__(self, tools: list[dict[str, Any]], servers: list[dict[str, Any]]) -> None:
        self.tools = tools
        self.servers = servers
        self.metadata: dict[str, str] = {}
        self.wire: list[tuple[str, Any]] = []
        self.unconstrainable: (
            Callable[[BetaManagedAgentsSession], BetaManagedAgentsSession] | None
        ) = None

    def model(self) -> dict[str, Any]:
        snapshot = ma_session(
            id=_SESSION,
            agent=ma_agent(id="ag_cli", tools=self.tools, mcp_servers=self.servers),
            status="idle",
            metadata=self.metadata,
        )
        changed = self.unconstrainable(snapshot) if self.unconstrainable else snapshot
        return changed.model_dump(mode="json")

    def router(self, events: list[dict[str, Any]]) -> MARouter:
        router = MARouter()

        def update(request: httpx.Request, _m: Any) -> httpx.Response:
            agent = json_body(request)["agent"]
            self.wire.append(("update", agent))
            self.tools, self.servers = agent["tools"], agent["mcp_servers"]
            return httpx.Response(200, json=self.model())

        def send(request: httpx.Request, _m: Any) -> httpx.Response:
            for event in json.loads(request.content)["events"]:
                self.wire.append((event["type"], event))
            return httpx.Response(200, json={"data": None})

        router.add(
            "GET",
            rf"/v1/sessions/{_SESSION}$",
            lambda _r, _m: httpx.Response(200, json=self.model()),
        )
        router.add("POST", rf"/v1/sessions/{_SESSION}$", update)
        router.add(
            "GET", rf"/v1/sessions/{_SESSION}/events/stream$", lambda _r, _m: sse_response(events)
        )
        router.add("POST", rf"/v1/sessions/{_SESSION}/events$", send)
        return router


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    session: _Session,
    events: list[dict[str, Any]],
    policy: ToolSafetyPolicy,
    *,
    public_url: str | None = None,
) -> int:
    settings = Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(public_url=public_url, jwt_secret=None),
        tool_safety=policy,
    )
    client = build_fake_anthropic(session.router(events).dispatch)

    @asynccontextmanager
    async def fake_runtime(_settings: Settings) -> AsyncIterator[CliRuntime]:
        yield CliRuntime(
            settings=settings,
            anthropic=client,
            sessionmaker=factory,
            deployment_default=DeploymentDefault(),
            resolver_cache=new_resolver_cache(),
        )

    monkeypatch.setattr(cmd_mod, "build_runtime", fake_runtime)
    try:
        return await cmd_mod._run_with_defaults(settings, _SESSION, "file it")  # pyright: ignore[reportPrivateUsage]
    finally:
        await client.close()


def _confirmations(session: _Session) -> list[str]:
    return [e["result"] for kind, e in session.wire if kind == "user.tool_confirmation"]


async def test_a_session_frozen_while_safety_was_off_is_gated_before_the_message(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Design test 1: default and per-tool always_allow are rewritten first."""
    session = _Session([_toolset("linear", per_tool="create_issue")], [_LINEAR])
    await _run(
        monkeypatch,
        db_session_factory,
        session,
        _events("linear", "create_issue"),
        ToolSafetyPolicy(enabled=True),
    )
    kinds = [kind for kind, _ in session.wire]
    assert kinds.index("update") < kinds.index("user.message")
    toolset = session.wire[0][1]["tools"][0]
    assert toolset["default_config"]["permission_policy"] == {"type": "always_ask"}
    assert [c["permission_policy"] for c in toolset["configs"]] == [{"type": "always_ask"}]
    assert _confirmations(session) == ["deny"]


async def test_unattended_writes_decide_an_operator_run(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The unattended decider, not the attended one: a listed write runs."""
    session = _Session([_toolset("linear")], [_LINEAR])
    await _run(
        monkeypatch,
        db_session_factory,
        session,
        _events("linear", "create_issue"),
        ToolSafetyPolicy(enabled=True, unattended_writes=("linear/create_issue",)),
    )
    assert _confirmations(session) == ["allow"]


async def test_a_foreign_reserved_server_is_healed_before_the_message(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Design test 2: the reserved name never exempts a third party's write."""
    foreign = {"name": "daimon-mcp", "type": "url", "url": "https://evil.example/mcp"}
    session = _Session([_toolset("daimon-mcp"), _toolset("linear")], [foreign, _LINEAR])
    await _run(
        monkeypatch,
        db_session_factory,
        session,
        _events("daimon-mcp", "microsoft_send"),
        ToolSafetyPolicy(enabled=True),
        public_url=_PUBLIC,
    )
    kinds = [kind for kind, _ in session.wire]
    assert kinds.index("update") < kinds.index("user.message")
    healed = {s["name"]: s["url"] for s in session.wire[0][1]["mcp_servers"]}
    assert healed["daimon-mcp"] == _PUBLIC


@pytest.mark.parametrize("enabled", [False, True], ids=["off", "on"])
async def test_an_operator_run_never_sends_microsoft_mail(
    enabled: bool,
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    daimon = {"name": "daimon-mcp", "type": "url", "url": _PUBLIC}
    session = _Session([_toolset("daimon-mcp")], [daimon])
    events = _events("daimon-mcp", "microsoft_send")
    events[0]["input"] = {"action": "send_mail"}
    await _run(
        monkeypatch,
        db_session_factory,
        session,
        events,
        ToolSafetyPolicy(enabled=enabled),
        public_url=_PUBLIC,
    )
    assert _confirmations(session) == ["deny"]


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        ({"daimon_channel": "C1"}, "deny"),
        ({"daimon_channel": "C1", "daimon_private_dm": "dm-1"}, "allow"),
        ({"daimon_channel": "C2"}, "allow"),
    ],
    ids=["channel-deny", "dm-excluded", "other-channel"],
)
async def test_a_stamped_session_runs_under_its_own_tenant_and_channel_rules(
    stamp: dict[str, str],
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The owned CLI session retains its stamped channel and DM rule context."""
    from daimon.core.authz import Subject
    from daimon.core.stores import agent_tool_rules

    async with db_session_factory() as db:
        tenant = await get_tenant(db, derive_tenant_uuid(platform="cli", workspace_id="local"))
        assert tenant is not None
        await agent_tool_rules.set_rule(
            db,
            tenant_id=tenant.id,
            agent_id="ag_cli",
            agent_name="cli",
            tool_key="linear/get_issue",
            channel_id="C1",
            server_url=_LINEAR["url"],
            rule="deny",
            set_by="admin",
            actor=Subject(is_admin=True, platform_user_id="UA"),
        )
        await db.commit()
    session = _Session([_toolset("linear")], [_LINEAR])
    session.metadata = {"daimon_tenant": str(tenant.id), **stamp}
    await _run(
        monkeypatch,
        db_session_factory,
        session,
        _events("linear", "get_issue"),
        ToolSafetyPolicy(enabled=True, agent_rules=True),
    )
    assert _confirmations(session) == [expected]


@pytest.mark.parametrize("platform", ["discord", "slack"])
async def test_continuing_a_created_routine_session_keeps_channel_deny(
    platform: str,
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.authz import Subject
    from daimon.core.rule_views import routine_origin
    from daimon.core.sessions import create_session
    from daimon.core.stores import agent_tool_rules, routines
    from daimon.testing.ma_models import ma_environment

    async with db_session_factory() as db:
        tenant = await get_tenant(db, derive_tenant_uuid(platform="cli", workspace_id="local"))
        assert tenant is not None
        routine = await routines.create_routine(
            db,
            tenant_id=tenant.id,
            created_by_user_id="U1",
            agent_id="ag_cli",
            agent_name="cli",
            cron_expr="0 * * * *",
            timezone_="UTC",
            trigger_message="check the issue",
            destination_kind="channel",
            destination_id="C1",
            channel_id="C1",
        )
        await agent_tool_rules.set_rule(
            db,
            tenant_id=tenant.id,
            agent_id="ag_cli",
            agent_name="cli",
            tool_key="linear/get_issue",
            channel_id="C1",
            server_url=_LINEAR["url"],
            rule="deny",
            set_by="admin",
            actor=Subject(is_admin=True, platform_user_id="UA"),
        )
        await db.commit()
    place = routine_origin(TenantAccessPolicy(), routine, platform=platform)
    assert place is not None
    session = _Session([_toolset("linear")], [_LINEAR])
    router = session.router(_events("linear", "get_issue"))

    def created(request: httpx.Request, _m: Any) -> httpx.Response:
        body = json_body(request)
        session.metadata = body["metadata"]
        return httpx.Response(200, json=session.model())

    router.add("POST", r"/v1/sessions$", created)
    client = build_fake_anthropic(router.dispatch)
    try:
        snapshot = await create_session(
            client,
            agent=ma_agent(id="ag_cli", tools=session.tools, mcp_servers=session.servers),
            environment=ma_environment(),
            tenant_id=tenant.id,
            tool_safety=ToolSafetyPolicy(enabled=True, agent_rules=True),
            requester_is_headless=True,
            memory_read_only=True,
            origin_channel_id=place.channel_id,
            origin_thread_id=place.thread_id,
            origin_seal_ids=place.seal_ids,
            private_dm_id=place.private_dm_id,
            budget_channel_id="BUDGET_ONLY",
        )
        assert snapshot.metadata["daimon_private_dm"] == f"routine:{routine.id}"
        assert snapshot.metadata["daimon_channel"] == "C1"
    finally:
        await client.close()
    await _run(
        monkeypatch,
        db_session_factory,
        session,
        _events("linear", "get_issue"),
        ToolSafetyPolicy(enabled=True, agent_rules=True),
    )
    assert _confirmations(session) == ["deny"]


@pytest.mark.parametrize(
    ("trusted", "destructive", "expected"),
    [(True, False, "allow"), (False, False, "deny"), (True, True, "deny")],
)
async def test_cli_uses_only_trusted_session_annotations(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
    trusted: bool,
    destructive: bool,
    expected: str,
) -> None:
    from daimon.core import mcp_tool_annotations

    catalogues: list[str] = []

    async def catalogue(url: str, token: str | None) -> list[dict[str, Any]]:
        catalogues.append(url)
        return [
            {
                "name": "memory_flag_list",
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": destructive,
                },
            }
        ]

    monkeypatch.setattr(mcp_tool_annotations, "read_mcp_catalogue", catalogue)
    session = _Session([_toolset("linear")], [_LINEAR])
    await _run(
        monkeypatch,
        db_session_factory,
        session,
        _events("linear", "memory_flag_list"),
        ToolSafetyPolicy(enabled=True, trusted_annotations=("linear",) if trusted else ()),
    )
    assert _confirmations(session) == [expected]
    assert catalogues == ([str(_LINEAR["url"])] if trusted else [])


@pytest.mark.parametrize(
    "unconstrainable",
    [with_coordinator, with_hidden_capability],
    ids=["coordinator", "hidden_field"],
)
async def test_an_operator_run_on_a_coordinator_session_is_refused_before_anything_is_sent(
    unconstrainable: Callable[[BetaManagedAgentsSession], BetaManagedAgentsSession],
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The roster's children could write unasked; MA cannot remove it mid-session."""
    session = _Session([_toolset("linear")], [_LINEAR])
    session.unconstrainable = unconstrainable
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await _run(
            monkeypatch,
            db_session_factory,
            session,
            _events("linear", "create_issue"),
            ToolSafetyPolicy(enabled=True),
        )
    assert session.wire == []


async def test_an_operator_run_disables_its_sandbox_and_refuses_bash(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """`daimon run` disables built-ins and denies even a paused bash call."""
    builtins = {
        "type": "agent_toolset_20260401",
        "configs": [],
        "default_config": {"enabled": True, "permission_policy": {"type": "always_ask"}},
    }
    session = _Session([builtins, _toolset("linear")], [_LINEAR])
    events = _events("linear", "create_issue")
    events[0] = {
        "id": "evt_tu",
        "type": "agent.tool_use",
        "name": "bash",
        "input": {"command": "ls"},
        "evaluated_permission": "ask",
    }
    await _run(monkeypatch, db_session_factory, session, events, ToolSafetyPolicy(enabled=True))
    assert session.tools[0]["default_config"]["enabled"] is False
    assert _confirmations(session) == ["deny"]


@pytest.mark.parametrize(
    ("server", "policy", "public_url"),
    [
        ("linear", ToolSafetyPolicy(enabled=False), None),
        ("daimon-mcp", ToolSafetyPolicy(enabled=True), _PUBLIC),
        ("daimon-mcp", ToolSafetyPolicy(enabled=False), _PUBLIC),
    ],
    ids=["foreign-safety-off", "trusted-safety-on", "trusted-safety-off"],
)
async def test_every_mcp_call_of_an_operator_run_pauses_for_the_decider(
    server: str,
    policy: ToolSafetyPolicy,
    public_url: str | None,
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Neither a trusted server nor safety off leaves a tool that runs unasked;
    the decider then allows the call as it did before."""
    url = public_url if server == "daimon-mcp" else _LINEAR["url"]
    session = _Session(
        [_toolset(server, per_tool="list_agents")], [{"name": server, "type": "url", "url": url}]
    )
    await _run(
        monkeypatch,
        db_session_factory,
        session,
        _events(server, "list_agents"),
        policy,
        public_url=public_url,
    )
    kinds = [kind for kind, _ in session.wire]
    assert kinds.index("update") < kinds.index("user.message")
    toolset = session.wire[0][1]["tools"][0]
    assert toolset["default_config"]["permission_policy"] == {"type": "always_ask"}
    assert toolset["configs"]
    assert all(c["permission_policy"] == {"type": "always_ask"} for c in toolset["configs"])
    assert _confirmations(session) == ["allow"]
