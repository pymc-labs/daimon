"""Unattended capability limits, exercised through a fake MA transport."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
from types import UnionType
from typing import Annotated, Any, Literal, Union, cast, get_args, get_origin
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from anthropic._models import construct_type  # pyright: ignore[reportPrivateImportUsage]
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from daimon.core.mux_backend import platform_scope
from daimon.core.tool_safety import ToolCall, ToolSafetyPolicy
from daimon.core.turn import unattended_capabilities as caps
from daimon.core.turn.approvals import chat_tool_confirmation
from daimon.core.turn.driver import run_turn, unattended_sends
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.posture import BillingExempt, PolicyApproval, ToolConfirmationResult
from daimon.core.turn.unattended import gate_session_snapshot, session_gate, unattended_posture
from daimon.core.turn.unattended_capabilities import (
    ENTRY_POINT_POLICY,
    ServerTool,
    UnattendedEntryPoint,
    capability_posture,
    constrain_capabilities,
    require_allowed_capabilities,
    restore_capabilities,
    unattended_entry_point,
)
from daimon.testing import ma_agent
from daimon.testing.ma import MARouter
from daimon.testing.ma_sessions import session_turn_sse
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.ids import Scope
from mux.testing.drivers.anthropic.ma import (
    build_fake_anthropic,
    json_body,
    ma_session,
    send_events_response,
)
from pydantic import BaseModel

from .conftest import make_status_idle

ENTRY_POINTS: tuple[UnattendedEntryPoint, ...] = tuple(
    entry for entry in get_args(UnattendedEntryPoint) if entry != "long_job"
)
ALLOW = {"enabled": True, "permission_policy": {"type": "always_allow"}}
BUILTINS = ("bash", "write", "edit")


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "agent_toolset_20260401",
            "default_config": ALLOW,
            "configs": [{"name": name, **ALLOW} for name in BUILTINS],
        },
        {
            "type": "mcp_toolset",
            "mcp_server_name": "linear",
            "default_config": ALLOW,
            "configs": [{"name": "create_issue", **ALLOW}],
        },
    ]


SERVERS = [{"name": "linear", "type": "url", "url": "https://linear.example/mcp"}]


def _root_session(**agent: Any) -> BetaManagedAgentsSession:
    agent.setdefault("tools", _tools())
    agent.setdefault("mcp_servers", deepcopy(SERVERS))
    return ma_session(id="ses_coordinator", agent=ma_agent(id="agent_coordinator", **agent))


def _coordinator_session() -> BetaManagedAgentsSession:
    session = _root_session().model_dump(mode="json")
    child = ma_session(
        agent=ma_agent(id="agent_worker", tools=_tools(), mcp_servers=deepcopy(SERVERS))
    ).agent.model_dump(mode="json", exclude={"multiagent"})
    session["agent"]["multiagent"] = {"type": "coordinator", "agents": [child]}
    # Validate the full provider shape: the roster contains definitions, not IDs.
    return BetaManagedAgentsSession.model_validate(session)


def _with_agent_field(**fields: Any) -> BetaManagedAgentsSession:
    """A snapshot as the SDK parses a provider response: unvalidated, extras kept."""
    session = _root_session().model_dump(mode="json")
    session["agent"].update(fields)
    return cast(
        BetaManagedAgentsSession, construct_type(type_=BetaManagedAgentsSession, value=session)
    )


_FILE: dict[str, Any] = {
    "id": "sesrsc_1",
    "type": "file",
    "file_id": "file_1",
    "mount_path": "/mnt/session/uploads/a.txt",
    "created_at": "2026-10-01T00:00:00Z",
    "updated_at": "2026-10-01T00:00:00Z",
}


def _mutated(change: Callable[[dict[str, Any]], object]) -> BetaManagedAgentsSession:
    """A root snapshot changed in its raw provider form, then parsed as the SDK does."""
    session = _root_session().model_dump(mode="json")
    change(session)
    return cast(
        BetaManagedAgentsSession, construct_type(type_=BetaManagedAgentsSession, value=session)
    )


def _set(path: str, value: object) -> Callable[[dict[str, Any]], object]:
    """Set a dotted path (list indices as numbers) in a raw snapshot."""

    def change(session: dict[str, Any]) -> None:
        *parents, leaf = path.split(".")
        node: Any = session
        for key in parents:
            node = node[int(key)] if key.isdigit() else node[key]
        node[int(leaf) if leaf.isdigit() else leaf] = value

    return change


_HIDDEN = {"future_capability": True}
_ASKING = {"enabled": True, "permission_policy": {"type": "always_ask"}}

# A reviewed scalar that became an object or a list, at each kind of node.
_HIDDEN_SHAPES: dict[str, Callable[[dict[str, Any]], object]] = {
    "effort_list": _set("agent.model.effort", [_HIDDEN]),
    "effort_type_object": _set("agent.model.effort", {"type": _HIDDEN}),
    "effort_unknown_level": _set("agent.model.effort", "unbounded"),
    "inference_geo_object": _set("agent.model.inference_geo", _HIDDEN),
    "speed_list": _set("agent.model.speed", ["fast", _HIDDEN]),
    "model_id_object": _set("agent.model.id", _HIDDEN),
    "agent_version_object": _set("agent.version", _HIDDEN),
    "system_list": _set("agent.system", [_HIDDEN]),
    "server_url_object": _set("agent.mcp_servers.0.url", _HIDDEN),
    "builtin_enabled_object": _set("agent.tools.0.default_config.enabled", _HIDDEN),
    "builtin_config_enabled_list": _set("agent.tools.0.configs.0.enabled", [_HIDDEN]),
    "mcp_config_name_object": _set("agent.tools.1.configs.0.name", _HIDDEN),
    "policy_type_object": _set("agent.tools.1.default_config.permission_policy.type", _HIDDEN),
    "policy_list": _set("agent.tools.1.default_config.permission_policy", [_HIDDEN]),
    "tool_null": _set("agent.tools.1", None),
    "tool_list": _set("agent.tools.1", [_HIDDEN]),
    "vault_id_object": _set("vault_ids", [_HIDDEN]),
    "resource_mount_object": lambda s: s["resources"].append({**_FILE, "mount_path": _HIDDEN}),
    "checkout_name_list": lambda s: s["resources"].append(
        {
            "id": "sesrsc_2",
            "type": "github_repository",
            "url": "https://github.com/o/r",
            "mount_path": "/workspace/r",
            "checkout": {"type": "branch", "name": [_HIDDEN]},
            "created_at": "2026-10-01T00:00:00Z",
            "updated_at": "2026-10-01T00:00:00Z",
        }
    ),
    # Nothing to rewrite (already asking, no built-ins): no update would
    # run the native schema, so admission alone must catch it.
    "no_change_enabled_object": lambda s: s["agent"].update(
        tools=[
            {
                "type": "mcp_toolset",
                "mcp_server_name": "linear",
                "default_config": {**_ASKING, "enabled": _HIDDEN},
                "configs": [{"name": "create_issue", **_ASKING}],
            }
        ]
    ),
}


class _FutureModelConfig(BetaManagedAgentsModelConfig):
    """An SDK upgrade that starts parsing a field this release never reviewed."""

    budget: dict[str, Any] | None = None


def _sdk_recognized_field() -> BetaManagedAgentsSession:
    session = _root_session()
    model = _FutureModelConfig.model_validate({"id": session.agent.model.id})
    return session.model_copy(update={"agent": session.agent.model_copy(update={"model": model})})


def _can_run_without_decider(toolset: dict[str, Any], name: str) -> bool:
    config = next((c for c in toolset["configs"] if c["name"] == name), None)
    effective: dict[str, Any] = {**toolset["default_config"], **(config or {})}
    return effective["enabled"] and effective["permission_policy"]["type"] != "always_ask"


def _builtins_runnable(tools: list[dict[str, Any]]) -> bool:
    return any(
        _can_run_without_decider(t, name)
        for t in tools
        if t["type"] == "agent_toolset_20260401"
        for name in BUILTINS
    )


def _scope() -> Scope:
    return Scope(
        tenant_id=str(uuid.uuid4()),
        account_id=str(uuid.uuid4()),
        principal_id="owner",
        authorization_id="grant",
    )


class _Provider:
    """A fake MA session that applies only supplied update fields, as MA does."""

    def __init__(self, snapshot: BetaManagedAgentsSession) -> None:
        self.state = snapshot.model_dump(mode="json")
        self.updates: list[dict[str, Any]] = []
        self.at_send: list[dict[str, Any]] = []
        router = MARouter()
        router.add(
            "GET",
            rf"/v1/sessions/{snapshot.id}$",
            lambda _r, _m: httpx.Response(200, json=self.state),
        )
        router.add("POST", rf"/v1/sessions/{snapshot.id}$", self._update)
        router.add("POST", rf"/v1/sessions/{snapshot.id}/events$", self._send)
        router.add(
            "GET",
            rf"/v1/sessions/{snapshot.id}/events/stream$",
            lambda _r, _m: session_turn_sse(make_status_idle(event_id="sevt_done")),
        )
        self.client = build_fake_anthropic(router.dispatch)

    def _update(self, request: httpx.Request, _match: Any) -> httpx.Response:
        agent = json_body(request)["agent"]
        self.updates.append(agent)
        self.state["agent"].update(agent)
        return httpx.Response(200, json=self.state)

    def _send(self, request: httpx.Request, _match: Any) -> httpx.Response:
        assert json_body(request)["events"][0]["type"] == "user.message"
        self.at_send.append(deepcopy(self.state["agent"]))
        return send_events_response([])


async def _drive(
    snapshot: BetaManagedAgentsSession,
    entry_point: UnattendedEntryPoint | None,
    *,
    policy: ToolSafetyPolicy | None = None,
    public_url: str | None = None,
    routine_sandbox_tools: bool = False,
) -> tuple[_Provider, Any]:
    provider = _Provider(snapshot)
    policy = policy or ToolSafetyPolicy(enabled=True)
    try:
        with unattended_sends(
            session_gate(
                provider.client,
                snapshot.id,
                policy,
                scope=platform_scope("unattended capability regression"),
                public_url=public_url,
                session_factory=None,
                tenant_id=None,
                entry_point=entry_point,
                routine_sandbox_tools=routine_sandbox_tools,
            )
        ):
            result = await run_turn(
                anthropic=provider.client,
                session_id=snapshot.id,
                user_message="nightly work",
                lifecycle=RecordingLifecycle(),
                cancel=asyncio.Event(),
                billing=BillingExempt(reason="cli-operator-run"),
                path="legacy",
                tool_confirmation=unattended_posture(
                    policy,
                    session=snapshot,
                    public_url=public_url,
                    session_factory=None,
                    tenant_id=None,
                    entry_point=entry_point,
                    routine_sandbox_tools=routine_sandbox_tools,
                ),
            )
    except AdmissionDenied as err:
        result = err
    finally:
        await provider.client.close()
    return provider, result


@pytest.mark.parametrize("entry_point", [None, *ENTRY_POINTS])
async def test_coordinator_session_is_refused_before_any_send(
    entry_point: UnattendedEntryPoint | None,
) -> None:
    """MA cannot drop `multiagent` from a live session, so the turn never starts.

    The roster's children carry always_allow built-ins and MCP writes; neither
    the root update nor the message is sent.
    """
    provider, result = await _drive(_coordinator_session(), entry_point)

    error = result if isinstance(result, AdmissionDenied) else result.error
    assert isinstance(error, AdmissionDenied)
    assert error.reason == "backend_unsupported"
    assert provider.updates == []
    assert provider.at_send == []


@pytest.mark.parametrize("entry_point", [None, *ENTRY_POINTS])
async def test_unattended_send_always_disables_builtins(
    entry_point: UnattendedEntryPoint | None,
) -> None:
    """A real driver send observes the provider's post-gate effective snapshot."""
    provider, result = await _drive(_root_session(), entry_point)

    assert result.error is None
    [agent] = provider.at_send
    assert provider.updates, "the unattended gate must run before the message"
    root_mcp = next(t for t in agent["tools"] if t["type"] == "mcp_toolset")
    assert not _can_run_without_decider(root_mcp, "create_issue")
    assert not _builtins_runnable(agent["tools"])
    [builtin] = [t for t in agent["tools"] if t["type"] == "agent_toolset_20260401"]
    assert builtin["default_config"]["enabled"] is False
    assert all(config["enabled"] is False for config in builtin["configs"])


def test_entry_point_policy_table() -> None:
    """Every unattended entry point disables the sandbox; watches also deny skills."""
    assert set(ENTRY_POINT_POLICY) == {*ENTRY_POINTS, "long_job"}
    off = {name for name, policy in ENTRY_POINT_POLICY.items() if not policy.allow_builtins}
    assert off == set(ENTRY_POINTS)
    assert not ENTRY_POINT_POLICY["watch"].allow_skills
    assert caps.capability_policy(None) == caps.CapabilityPolicy(
        allow_builtins=False, allow_skills=False
    )


@pytest.mark.parametrize("entry_point", [None, *ENTRY_POINTS])
def test_only_a_routine_takes_the_sandbox_opt_in(entry_point: UnattendedEntryPoint | None) -> None:
    """Watches, wakes, chat, workers, `daimon run` and checkpoints ignore the flag."""
    opted = caps.capability_policy(entry_point, routine_sandbox_tools=True)
    assert opted.allow_builtins is (entry_point == "routine")
    assert not caps.capability_policy(entry_point).allow_builtins
    if entry_point is not None:
        assert opted.allow_skills == ENTRY_POINT_POLICY[entry_point].allow_skills


def _with_custom_tool() -> BetaManagedAgentsSession:
    return _root_session(
        tools=[
            *_tools(),
            {
                "type": "custom",
                "name": "local_fn",
                "description": "d",
                "input_schema": {"type": "object"},
            },
        ]
    )


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
async def test_routine_opt_in_keeps_builtins_on_the_wire(entry_point: UnattendedEntryPoint) -> None:
    """An opted-in routine sends its built-ins as authored; every other entry point
    still disables them, and custom tools are removed either way."""
    provider, result = await _drive(_with_custom_tool(), entry_point, routine_sandbox_tools=True)

    assert result.error is None
    [agent] = provider.at_send
    assert _builtins_runnable(agent["tools"]) is (entry_point == "routine")
    assert all(t["type"] != "custom" for t in agent["tools"])
    root_mcp = next(t for t in agent["tools"] if t["type"] == "mcp_toolset")
    assert not _can_run_without_decider(root_mcp, "create_issue")


@pytest.mark.parametrize("entry_point", [None, *ENTRY_POINTS])
async def test_decider_lets_builtins_through_only_for_an_opted_in_routine(
    entry_point: UnattendedEntryPoint | None,
) -> None:
    inner = AsyncMock(return_value=caps.ToolConfirmationResult(allow=True))
    opted_in = entry_point == "routine"
    posture = caps.capability_posture(
        PolicyApproval(decide=inner),
        allow_builtins=caps.capability_policy(
            entry_point, routine_sandbox_tools=True
        ).allow_builtins,
    )
    decision = await posture.decide(
        ToolCall(tool_use_id="tu_builtin", server_name=None, tool_name="bash")
    )
    assert decision.allow is opted_in
    assert inner.await_count == (1 if opted_in else 0)


@pytest.mark.parametrize(
    ("origin", "entry_point"),
    [("worker", "fleet_worker"), ("routine", "routine"), ("chat", "wake"), ("relay", "wake")],
)
def test_prepared_origin_maps_to_entry_point(origin: Any, entry_point: str) -> None:
    assert unattended_entry_point(origin) == entry_point


@pytest.mark.parametrize("entry_point", [None, *ENTRY_POINTS])
@pytest.mark.parametrize("name", BUILTINS)
async def test_unattended_decider_always_refuses_builtins(
    entry_point: UnattendedEntryPoint | None, name: str
) -> None:
    """Asking is not enough: with built-ins off the decider refuses them too."""
    posture = unattended_posture(
        ToolSafetyPolicy(enabled=True),
        session=_root_session(),
        public_url=None,
        session_factory=None,
        tenant_id=None,
        entry_point=entry_point,
    )
    decision = await posture.decide(
        ToolCall(tool_use_id="tu_builtin", server_name=None, tool_name=name)
    )
    assert decision.allow is False


async def test_attended_chat_posture_is_unchanged() -> None:
    """Only unattended turns go through the capability limits."""
    posture = chat_tool_confirmation(
        ToolSafetyPolicy(enabled=True),
        requester_platform_user_id="U1",
        confirm=None,
    )
    assert isinstance(posture, PolicyApproval)
    decision = await posture.decide(ToolCall(tool_use_id="tu", server_name=None, tool_name="bash"))
    assert decision.allow


@pytest.mark.parametrize(
    "snapshot",
    [
        pytest.param(lambda: _with_agent_field(sandbox_policy={"network": "open"}), id="extra"),
        pytest.param(
            lambda: _with_agent_field(
                tools=[{"type": "computer_use_20270101", "default_config": ALLOW}]
            ),
            id="tool_type",
        ),
        pytest.param(
            lambda: _with_agent_field(
                tools=[{**_tools()[1], "grants": ["everything"]}],
            ),
            id="tool_field",
        ),
        pytest.param(
            lambda: _with_agent_field(
                mcp_servers=[{**SERVERS[0], "headers": {"x": "y"}}],
            ),
            id="server_field",
        ),
        pytest.param(_coordinator_session, id="multiagent"),
        pytest.param(lambda: _with_agent_field(sandbox_policy={}), id="empty_agent_field"),
        pytest.param(lambda: _with_agent_field(sandbox_policy=None), id="null_agent_field"),
        pytest.param(
            lambda: _mutated(lambda s: s["agent"]["tools"][0]["default_config"].update(net=1)),
            id="builtin_default_config_field",
        ),
        pytest.param(
            lambda: _mutated(lambda s: s["agent"]["tools"][0]["configs"][0].update(net={})),
            id="builtin_config_field",
        ),
        pytest.param(
            lambda: _mutated(lambda s: s["agent"]["tools"][0]["configs"][0].update(type="write")),
            id="builtin_config_foreign_type",
        ),
        pytest.param(
            lambda: _mutated(lambda s: s["agent"]["tools"][1]["configs"][0].update(scope="*")),
            id="mcp_config_field",
        ),
        pytest.param(
            lambda: _mutated(
                lambda s: s["agent"]["tools"][1]["default_config"]["permission_policy"].update(
                    budget=5
                )
            ),
            id="permission_policy_field",
        ),
        pytest.param(
            lambda: _mutated(
                lambda s: s["agent"]["tools"][1]["configs"][0].update(
                    permission_policy={"type": "allow_within_budget"}
                )
            ),
            id="permission_policy_type",
        ),
        pytest.param(
            lambda: _mutated(
                lambda s: s["agent"]["tools"][1]["configs"].append(
                    {"name": "create_issue", **ALLOW}
                )
            ),
            id="duplicate_config",
        ),
        pytest.param(
            lambda: _mutated(lambda s: s["agent"]["mcp_servers"].append(dict(SERVERS[0]))),
            id="duplicate_server",
        ),
        pytest.param(lambda: _mutated(lambda s: s["agent"]["model"].update(tier={})), id="model"),
        pytest.param(
            lambda: _mutated(
                lambda s: s["agent"]["model"].update(effort={"type": "max", "tools": True})
            ),
            id="model_effort_field",
        ),
        pytest.param(lambda: _mutated(lambda s: s.update(sandbox={})), id="session_field"),
        pytest.param(
            lambda: _mutated(lambda s: s["resources"].append({**_FILE, "exec": True})),
            id="resource_field",
        ),
        pytest.param(
            lambda: _mutated(lambda s: s["resources"].append({**_FILE, "type": "shell"})),
            id="resource_type",
        ),
        pytest.param(_sdk_recognized_field, id="sdk_recognized_field"),
        *(
            pytest.param(lambda change=change: _mutated(change), id=f"shape_{name}")
            for name, change in _HIDDEN_SHAPES.items()
        ),
    ],
)
@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
async def test_unknown_capability_is_refused_before_mutation(
    snapshot: Any, entry_point: UnattendedEntryPoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    update = AsyncMock()
    monkeypatch.setattr(caps, "update_session_record", update)
    session = snapshot()
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        require_allowed_capabilities(session, allow_skills=True)
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await gate_session_snapshot(
            MagicMock(),
            session,
            ToolSafetyPolicy(enabled=True),
            scope=_scope(),
            public_url=None,
            session_factory=None,
            tenant_id=None,
            entry_point=entry_point,
        )
    update.assert_not_awaited()


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
async def test_skills_follow_the_entry_point_policy(
    entry_point: UnattendedEntryPoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    update = AsyncMock()
    monkeypatch.setattr(caps, "update_session_record", update)
    session = _root_session(skills=[{"type": "custom", "skill_id": "skill_x", "version": "1"}])
    gate = gate_session_snapshot(
        MagicMock(),
        session,
        ToolSafetyPolicy(enabled=True),
        scope=_scope(),
        public_url=None,
        session_factory=None,
        tenant_id=None,
        entry_point=entry_point,
    )
    if ENTRY_POINT_POLICY[entry_point].allow_skills:
        await gate
        update.assert_awaited_once()
    else:
        with pytest.raises(AdmissionDenied, match="backend_unsupported"):
            await gate
        update.assert_not_awaited()


async def test_allowlist_keeps_only_named_tools_and_asks() -> None:
    session = _root_session(
        tools=[
            *_tools(),
            {
                "type": "custom",
                "name": "local",
                "description": "d",
                "input_schema": {"type": "object"},
            },
            {
                "type": "mcp_toolset",
                "mcp_server_name": "github",
                "default_config": ALLOW,
                "configs": [],
            },
        ],
        mcp_servers=[*SERVERS, {"name": "github", "type": "url", "url": "https://gh.example"}],
    )
    constrained = await constrain_capabilities(
        session,
        ToolSafetyPolicy(enabled=True),
        public_url=None,
        session_factory=None,
        tenant_id=None,
        tool_allowlist=frozenset({ServerTool("linear", "get_issue")}),
    )
    tools = constrained.send.tools
    assert [t["type"] for t in tools] == ["agent_toolset_20260401", "mcp_toolset"]
    assert not _builtins_runnable(tools)
    [linear] = [t for t in tools if t["type"] == "mcp_toolset"]
    assert linear["default_config"]["enabled"] is False
    assert [(c["name"], c["enabled"], c["permission_policy"]) for c in linear["configs"]] == [
        ("get_issue", True, {"type": "always_ask"})
    ]
    assert [s["name"] for s in constrained.send.mcp_servers] == ["linear"]
    assert constrained.original.tools == [t.model_dump(mode="json") for t in session.agent.tools]

    posture = capability_posture(
        PolicyApproval(decide=AsyncMock()),
        tool_allowlist=frozenset({ServerTool("linear", "get_issue")}),
    )
    refused = await posture.decide(
        ToolCall(tool_use_id="x", server_name="linear", tool_name="create_issue")
    )
    assert not refused.allow


@pytest.mark.parametrize(
    "query",
    [
        "?extra=1",
        "?daimon_session=one&daimon_session=two",
        "?daimon_session=invalid",
        "?daimon_session=00000000-0000-0000-0000-000000000001&extra=1",
        "/",
        "/../mcp",
        "/%2e/",
    ],
)
async def test_allowlist_drops_management_endpoint_aliases(query: str) -> None:
    """Ported from the watches review: a renamed Daimon endpoint never survives."""
    session = ma_session(
        agent=ma_agent(
            tools=[
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": name,
                    "configs": [],
                    "default_config": ALLOW,
                }
                for name in ("renamed", "daimon-mcp")
            ],
            mcp_servers=[
                {"name": "renamed", "type": "url", "url": "https://daimon.invalid/mcp" + query},
                {"name": "daimon-mcp", "type": "url", "url": "https://daimon.invalid/mcp"},
            ],
        )
    )
    constrained = await constrain_capabilities(
        session,
        ToolSafetyPolicy(),
        public_url="https://daimon.invalid/mcp",
        session_factory=None,
        tenant_id=None,
        tool_allowlist=frozenset(
            {ServerTool("renamed", "get_agent"), ServerTool("daimon-mcp", "get_agent")}
        ),
    )
    assert constrained.send.tools == []
    assert constrained.send.mcp_servers == []


def _bridged_session() -> BetaManagedAgentsSession:
    """Ported from the watches review: `linear/bridge` beside `linear`, both with get_issue.

    `linear` lists get_issue disabled; the second server enables it. Split at
    the first `/`, the allowlist key `linear/bridge/get_issue` enabled an
    absent `bridge/get_issue` config on `linear`.
    """
    off = {"enabled": False, "permission_policy": {"type": "always_ask"}}
    return _mutated(
        lambda s: s["agent"].update(
            tools=[
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": "linear",
                    "default_config": _ASKING,
                    "configs": [{"name": "get_issue", **off}],
                },
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": "linear/bridge",
                    "default_config": _ASKING,
                    "configs": [{"name": "get_issue", **_ASKING}],
                },
            ],
            mcp_servers=[
                *deepcopy(SERVERS),
                {"name": "linear/bridge", "type": "url", "url": "https://bridge.invalid/mcp"},
            ],
        )
    )


@pytest.mark.parametrize(
    "allowlist",
    [
        None,
        frozenset({ServerTool("linear/bridge", "get_issue")}),
        frozenset({ServerTool("linear", "bridge/get_issue")}),
    ],
    ids=["no_allowlist", "bridge_pair", "split_pair"],
)
async def test_a_server_name_with_a_slash_is_refused_before_mutation(
    allowlist: frozenset[ServerTool] | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    update = AsyncMock()
    monkeypatch.setattr(caps, "update_session_record", update)
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await constrain_capabilities(
            _bridged_session(),
            ToolSafetyPolicy(),
            public_url=None,
            session_factory=None,
            tenant_id=None,
            tool_allowlist=allowlist,
        )
    update.assert_not_awaited()


@pytest.mark.parametrize("where", ["server", "toolset"])
def test_a_slash_in_either_server_name_is_refused(where: str) -> None:
    session = _mutated(
        lambda s: (
            s["agent"]["mcp_servers"][0] if where == "server" else s["agent"]["tools"][1]
        ).update(**{"name" if where == "server" else "mcp_server_name": "linear/bridge"})
    )
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        require_allowed_capabilities(session)


async def test_an_allowlist_pair_never_reaches_another_server() -> None:
    """A tool name may hold `/`; it stays that tool on that server."""
    session = _root_session()
    constrained = await constrain_capabilities(
        session,
        ToolSafetyPolicy(),
        public_url=None,
        session_factory=None,
        tenant_id=None,
        tool_allowlist=frozenset({ServerTool("linear", "bridge/get_issue")}),
    )
    [linear] = [t for t in constrained.send.tools if t["type"] == "mcp_toolset"]
    assert linear["mcp_server_name"] == "linear"
    assert [c["name"] for c in linear["configs"]] == ["bridge/get_issue"]
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await constrain_capabilities(
            session,
            ToolSafetyPolicy(),
            public_url=None,
            session_factory=None,
            tenant_id=None,
            tool_allowlist=frozenset({ServerTool("linear/bridge", "get_issue")}),
        )
    posture = capability_posture(
        PolicyApproval(decide=AsyncMock(return_value=ToolConfirmationResult(allow=True))),
        tool_allowlist=frozenset({ServerTool("linear", "bridge/get_issue")}),
    )
    aliased = await posture.decide(
        ToolCall(tool_use_id="a", server_name="linear/bridge", tool_name="get_issue")
    )
    listed = await posture.decide(
        ToolCall(tool_use_id="l", server_name="linear", tool_name="bridge/get_issue")
    )
    assert not aliased.allow
    assert listed.allow


async def test_apply_invalidates_cached_tools_before_mutating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    async def invalidate(_db: Any, *, ma_session_id: str) -> None:
        order.append(f"invalidate:{ma_session_id}")

    async def update(*_args: Any, **_kwargs: Any) -> None:
        order.append("update")

    class _Begin:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_exc: object) -> None:
            order.append("commit")

    factory = MagicMock()
    factory.begin = _Begin
    monkeypatch.setattr(caps, "invalidate_session_tools", invalidate)
    monkeypatch.setattr(caps, "update_session_record", update)
    await gate_session_snapshot(
        MagicMock(),
        _root_session(),
        ToolSafetyPolicy(enabled=True),
        scope=_scope(),
        public_url=None,
        session_factory=None,
        tenant_id=None,
        entry_point="watch",
    )
    assert order == ["update"]
    order.clear()
    session = _root_session()
    constrained = await constrain_capabilities(
        session,
        ToolSafetyPolicy(enabled=True),
        public_url=None,
        session_factory=None,
        tenant_id=None,
    )
    await caps.apply_capabilities(
        MagicMock(), session, constrained, scope=_scope(), session_factory=factory
    )
    assert order == [f"invalidate:{session.id}", "commit", "update"]


async def test_a_restore_registered_between_invalidation_and_send_survives_a_failed_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watches flow: invalidate, register the restore, then send."""
    order: list[str] = []

    async def invalidate(_db: Any, *, ma_session_id: str) -> None:
        order.append("invalidate")

    class _Begin:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_exc: object) -> None:
            order.append("commit")

    async def update(*_args: Any, **kwargs: Any) -> None:
        if kwargs["agent"]["tools"] == original.tools:
            order.append("restore")
            return
        order.append("send")
        raise RuntimeError("provider down")

    factory = MagicMock()
    factory.begin = _Begin
    monkeypatch.setattr(caps, "invalidate_session_tools", invalidate)
    monkeypatch.setattr(caps, "update_session_record", update)
    session = _root_session()
    original = caps.SessionToolConfig.of(session)
    constrained = await constrain_capabilities(
        session,
        ToolSafetyPolicy(enabled=True),
        public_url=None,
        session_factory=None,
        tenant_id=None,
    )
    await caps.invalidate_cached_tools(session, constrained, session_factory=factory)
    order.append("register_restore")
    try:
        with pytest.raises(RuntimeError, match="provider down"):
            await caps.send_capabilities(MagicMock(), session, constrained, scope=_scope())
    finally:
        await restore_capabilities(MagicMock(), session, constrained.original, scope=_scope())
    assert order == ["invalidate", "commit", "register_restore", "send", "restore"]


async def test_split_apply_does_nothing_when_nothing_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalidate = AsyncMock()
    update = AsyncMock()
    monkeypatch.setattr(caps, "invalidate_session_tools", invalidate)
    monkeypatch.setattr(caps, "update_session_record", update)
    session = _root_session()
    unchanged = caps.ConstrainedSession(
        send=caps.SessionToolConfig.of(session),
        original=caps.SessionToolConfig.of(session),
        changed=False,
    )
    await caps.invalidate_cached_tools(session, unchanged, session_factory=MagicMock())
    await caps.send_capabilities(MagicMock(), session, unchanged, scope=_scope())
    invalidate.assert_not_awaited()
    update.assert_not_awaited()


async def test_restore_finishes_through_repeated_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ported from the watches turn: the original tools are back before release."""
    started = asyncio.Event()
    finish = asyncio.Event()
    restored = asyncio.Event()
    snapshot = _root_session()
    original = caps.SessionToolConfig.of(snapshot)

    async def update(*_args: Any, **kwargs: Any) -> None:
        started.set()
        await finish.wait()
        assert kwargs["agent"]["tools"] == original.tools
        restored.set()

    monkeypatch.setattr(caps, "update_session_record", update)
    task = asyncio.create_task(
        restore_capabilities(MagicMock(), snapshot, original, scope=_scope())
    )
    await started.wait()
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert restored.is_set()


def test_a_reviewed_session_with_every_known_capability_passes() -> None:
    """No false refusal: skills, resources of each kind and a custom tool are known."""
    session = _mutated(
        lambda s: (
            s["agent"]["skills"].append({"type": "custom", "skill_id": "skill_1", "version": "1"}),
            s["agent"]["tools"].append(
                {
                    "type": "custom",
                    "name": "local",
                    "description": "d",
                    "input_schema": {"type": "object", "anything": {"goes": True}},
                }
            ),
            s["resources"].extend(
                [
                    _FILE,
                    {
                        "id": "sesrsc_2",
                        "type": "github_repository",
                        "url": "https://github.com/o/r",
                        "mount_path": "/workspace/r",
                        "checkout": {"type": "branch", "name": "main"},
                        "created_at": "2026-10-01T00:00:00Z",
                        "updated_at": "2026-10-01T00:00:00Z",
                    },
                    {
                        "type": "memory_store",
                        "memory_store_id": "memstore_1",
                        "access": "read_write",
                        "description": None,
                        "instructions": None,
                        "mount_path": "/mnt/memory",
                        "name": "notes",
                    },
                ]
            ),
        )
    )
    require_allowed_capabilities(session, allow_skills=True)
    with pytest.raises(AdmissionDenied):
        require_allowed_capabilities(session, allow_skills=False)


def _daimon_and_foreign(public_url: str) -> BetaManagedAgentsSession:
    return _root_session(
        tools=[
            *_tools(),
            {
                "type": "mcp_toolset",
                "mcp_server_name": "daimon-mcp",
                "default_config": ALLOW,
                "configs": [{"name": "list_agents", **ALLOW}],
            },
        ],
        mcp_servers=[*deepcopy(SERVERS), {"name": "daimon-mcp", "type": "url", "url": public_url}],
    )


@pytest.mark.parametrize("enabled", [True, False], ids=["safety_on", "safety_off"])
@pytest.mark.parametrize("entry_point", [None, *ENTRY_POINTS])
async def test_every_retained_mcp_call_pauses_for_the_decider(
    entry_point: UnattendedEntryPoint | None, enabled: bool
) -> None:
    """Trusted Daimon tools and, with safety off, foreign writes ask too.

    Nothing an MCP server offers runs on its own in an unattended session; the
    decider then answers as before (Daimon's ordinary tools and, with safety
    off, foreign calls are allowed).
    """
    public_url = "https://daimon.example/mcp"
    policy = ToolSafetyPolicy(enabled=enabled)
    snapshot = _daimon_and_foreign(public_url)
    provider, result = await _drive(snapshot, entry_point, policy=policy, public_url=public_url)

    assert result.error is None
    [agent] = provider.at_send
    toolsets = [t for t in agent["tools"] if t["type"] == "mcp_toolset"]
    assert {t["mcp_server_name"] for t in toolsets} == {"linear", "daimon-mcp"}
    for toolset in toolsets:
        assert toolset["default_config"]["permission_policy"] == {"type": "always_ask"}
        assert all(c["permission_policy"] == {"type": "always_ask"} for c in toolset["configs"]), (
            toolset
        )
    posture = unattended_posture(
        policy,
        session=snapshot,
        public_url=public_url,
        session_factory=None,
        tenant_id=None,
        entry_point=entry_point,
    )
    daimon = await posture.decide(
        ToolCall(tool_use_id="d", server_name="daimon-mcp", tool_name="list_agents")
    )
    foreign = await posture.decide(
        ToolCall(tool_use_id="f", server_name="linear", tool_name="create_issue")
    )
    assert daimon.allow
    assert foreign.allow is not enabled


@pytest.mark.parametrize("effort", ["medium", {"type": "medium"}])
def test_model_inference_settings_daimon_writes_pass(effort: object) -> None:
    """`inference_geo` and `effort`, which Daimon's driver sets and the SDK does not parse."""
    session = _mutated(
        lambda s: s["agent"]["model"].update(inference_geo="us", effort=effort, speed="fast")
    )
    require_allowed_capabilities(session)


def test_a_builtin_config_may_echo_its_own_name_as_type() -> None:
    session = _mutated(lambda s: s["agent"]["tools"][0]["configs"][0].update(type="bash"))
    require_allowed_capabilities(session)


def test_a_hidden_field_in_a_session_needing_no_update_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-change fast path never reaches the native update schema."""
    clean = _mutated(
        lambda s: s["agent"].update(
            tools=[
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": "linear",
                    "default_config": _ASKING,
                    "configs": [{"name": "create_issue", **_ASKING}],
                }
            ]
        )
    )
    require_allowed_capabilities(clean)
    with pytest.raises(AdmissionDenied):
        require_allowed_capabilities(_mutated(_HIDDEN_SHAPES["no_change_enabled_object"]))


_OUTCOME: dict[str, Any] = {
    "type": "outcome_evaluation",
    "outcome_id": "outc_1",
    "description": "d",
    "result": "satisfied",
    "iteration": 1,
    "explanation": None,
    "completed_at": "2026-10-01T00:00:00Z",
}


def _with_telemetry(session: dict[str, Any]) -> None:
    session["stats"] = {"active_seconds": 1.5, "duration_seconds": 3}
    session["usage"] = {
        "cache_creation": {"ephemeral_1h_input_tokens": 1, "ephemeral_5m_input_tokens": 2},
        "cache_read_input_tokens": 3,
        "input_tokens": 4,
        "output_tokens": 5,
    }
    session["outcome_evaluations"] = [dict(_OUTCOME)]


def test_reviewed_telemetry_passes() -> None:
    require_allowed_capabilities(_mutated(_with_telemetry))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("stats.future_execution_capability", False),
        ("usage.future_execution_capability", False),
        ("usage.cache_creation.future_execution_capability", False),
        ("outcome_evaluations.0.future_execution_capability", False),
        ("stats.active_seconds", _HIDDEN),
        ("usage.input_tokens", [_HIDDEN]),
        ("usage.cache_creation", [_HIDDEN]),
        ("usage.cache_creation.ephemeral_1h_input_tokens", _HIDDEN),
        ("outcome_evaluations.0.iteration", _HIDDEN),
        ("outcome_evaluations.0", [_HIDDEN]),
    ],
)
def test_telemetry_is_frozen_like_every_other_node(path: str, value: object) -> None:
    """Ported from the watches review: telemetry no longer passes opaquely."""
    change = _set(path, value)
    session = _mutated(lambda s: (_with_telemetry(s), change(s)))
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        require_allowed_capabilities(session)


def _private_targets(session: BetaManagedAgentsSession) -> dict[str, object]:
    toolset = session.agent.tools[1]
    assert toolset.type == "mcp_toolset"
    return {
        "session": session,
        "agent": session.agent,
        "model": session.agent.model,
        "skill": session.agent.skills[0],
        "server": session.agent.mcp_servers[0],
        "tool": toolset,
        "default_config": toolset.default_config,
        "config": toolset.configs[0],
        "policy": toolset.configs[0].permission_policy,
        "resource": session.resources[0],
        "stats": session.stats,
        "usage": session.usage,
        "cache_creation": session.usage.cache_creation,
        "outcome": session.outcome_evaluations[0],
    }


def _private_session() -> BetaManagedAgentsSession:
    return _mutated(
        lambda s: (
            _with_telemetry(s),
            s["agent"]["skills"].append({"type": "custom", "skill_id": "skill_1", "version": "1"}),
            s["resources"].append(dict(_FILE)),
        )
    )


@pytest.mark.parametrize("name", ["_future_execution_capability", "__future", "_request_ids"])
@pytest.mark.parametrize("level", list(_private_targets(_private_session())))
def test_private_instance_attributes_are_refused_at_every_level(level: str, name: str) -> None:
    """Ported from the watches review: only the SDK's own `_request_id` is allowed."""
    session = _private_session()
    require_allowed_capabilities(session, allow_skills=True)
    object.__setattr__(_private_targets(session)[level], name, False)
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        require_allowed_capabilities(session, allow_skills=True)


def test_the_sdk_request_id_is_the_one_private_attribute_allowed() -> None:
    from anthropic._models import add_request_id  # pyright: ignore[reportPrivateImportUsage]

    session = _private_session()
    add_request_id(session, "req_1")
    require_allowed_capabilities(session, allow_skills=True)
    object.__setattr__(session, "_request_id", {"tools": True})
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        require_allowed_capabilities(session, allow_skills=True)
    pydantic_private = _private_session()
    object.__setattr__(pydantic_private.agent, "__pydantic_private__", {"_tools": True})
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        require_allowed_capabilities(pydantic_private, allow_skills=True)


# --- Real provider shapes -------------------------------------------------
#
# Built from the installed SDK's own types, not hand-written dicts: every
# field the SDK declares is populated, recursively, so a new SDK field shows
# up here without anyone listing it. On top come the fields Managed Agents
# sends that the pinned SDK does not parse yet, as Preview returned them.


def _models_in(annotation: Any) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return [model for arg in get_args(annotation) for model in _models_in(arg)]


def _tag(model: type[BaseModel]) -> str:
    [kind] = get_args(model.model_fields["type"].annotation)
    return kind


# Realistic leaves where a placeholder string would not read as a real one.
_LEAVES: dict[str, object] = {
    "url": "https://linear.example/mcp",
    "name": "linear",
    "mcp_server_name": "linear",
    "mount_path": "/workspace/r",
    "amount": "2500",
    "currency": "USD",
    "version": "1",
}
_INPUT_SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}


def _sdk_value(annotation: Any, field: str) -> object:
    """A realistic value for one SDK annotation; a list holds every union member."""
    origin = get_origin(annotation)
    if origin is Annotated:
        return _sdk_value(get_args(annotation)[0], field)
    if origin is Literal:
        return get_args(annotation)[0]
    if origin is list:
        [item] = get_args(annotation)
        if len(members := _models_in(item)) > 1:
            return [_sdk_payload(member) for member in members]
        return [_sdk_value(item, field)]
    if origin is dict:
        return {"daimon_tenant": "449f4dc5-5990-52c0-96e0-45a4ab3bf0e4"}
    if origin in (Union, UnionType):
        options = [arg for arg in get_args(annotation) if arg is not type(None)]
        if models := _models_in(annotation):
            return _sdk_payload(models[-1])  # policies: the last is always_ask
        return _sdk_value(options[0], field)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _sdk_payload(annotation)
    if annotation is datetime:
        return "2026-10-20T09:07:08Z"
    if annotation is bool:
        return True
    if annotation is int:
        return 3
    if annotation is float:
        return 1.5
    if annotation is str:
        return _LEAVES.get(field, f"{field}_01")
    raise AssertionError(f"no realistic value for {field}: {annotation!r}")


def _sdk_payload(model: type[BaseModel]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field, info in model.model_fields.items():
        if field == "multiagent":
            payload[field] = None  # A coordinator is refused; see its own tests.
        elif field == "input_schema":
            payload[field] = deepcopy(_INPUT_SCHEMA)
        else:
            payload[field] = _sdk_value(info.annotation, field)
    return payload


# Seen on Preview (2026-10-20) on every session and agent, and absent from the
# pinned SDK: the outage this section guards against.
_PREVIEW_SESSION_EXTRAS: dict[str, Any] = {"budget": None}
_PREVIEW_AGENT_EXTRAS: dict[str, Any] = {"execution_identity": {"type": "service_account"}}
# What a later SDK declares on the same nodes (the session budget as a limit,
# the agent model's inference settings, usage counters).
_LATER_SDK_SESSION_EXTRAS: dict[str, Any] = {
    "budget": {"type": "limit", "max_list_cost": {"amount": "2500", "currency": "USD"}},
}
_LATER_SDK_MODEL_EXTRAS: dict[str, Any] = {"inference_geo": "us", "effort": {"type": "high"}}
_LATER_SDK_USAGE_EXTRAS: dict[str, Any] = {
    "active_seconds": 2.5,
    "list_cost": {"amount": "31", "currency": "USD"},
    "server_tool_use": {"web_fetch_requests": 0, "web_search_requests": 1},
}


def _provider_payload(*, later_sdk: bool = False, skills: bool = True) -> dict[str, Any]:
    payload = _sdk_payload(BetaManagedAgentsSession)
    if not skills:
        payload["agent"]["skills"] = []
    payload.update(deepcopy(_PREVIEW_SESSION_EXTRAS))
    payload["agent"].update(deepcopy(_PREVIEW_AGENT_EXTRAS))
    if later_sdk:
        payload.update(deepcopy(_LATER_SDK_SESSION_EXTRAS))
        payload["agent"]["model"].update(deepcopy(_LATER_SDK_MODEL_EXTRAS))
        payload["usage"].update(deepcopy(_LATER_SDK_USAGE_EXTRAS))
    return payload


def _parsed(payload: dict[str, Any]) -> BetaManagedAgentsSession:
    """Parsed as the SDK parses a response: validated declared fields, extras kept."""
    BetaManagedAgentsSession.model_validate(payload)  # Every declared field is well formed.
    return cast(
        BetaManagedAgentsSession, construct_type(type_=BetaManagedAgentsSession, value=payload)
    )


def test_the_provider_payload_populates_every_declared_field() -> None:
    session = BetaManagedAgentsSession.model_validate(_provider_payload())
    assert session.model_fields_set >= set(BetaManagedAgentsSession.model_fields)
    assert {tool.type for tool in session.agent.tools} == {
        "agent_toolset_20260401",
        "mcp_toolset",
        "custom",
    }
    assert {resource.type for resource in session.resources} == set(caps._RESOURCES)  # pyright: ignore[reportPrivateUsage]
    assert session.usage.cache_creation is not None
    assert session.model_extra == _PREVIEW_SESSION_EXTRAS
    assert session.agent.model_extra == _PREVIEW_AGENT_EXTRAS


@pytest.mark.parametrize("later_sdk", [False, True], ids=["preview", "later_sdk"])
def test_a_real_shaped_session_is_admitted(later_sdk: bool) -> None:
    """The 2026-10-20 Preview outage: `budget` and `execution_identity` were refused."""
    session = _parsed(_provider_payload(later_sdk=later_sdk))
    require_allowed_capabilities(session, allow_skills=True)
    require_allowed_capabilities(_parsed(_provider_payload(later_sdk=later_sdk, skills=False)))


@pytest.mark.parametrize("later_sdk", [False, True], ids=["preview", "later_sdk"])
@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
async def test_every_unattended_entry_point_runs_a_real_shaped_session(
    entry_point: UnattendedEntryPoint, later_sdk: bool
) -> None:
    """Read back through the SDK, gated, and sent: the model call happens."""
    skills = ENTRY_POINT_POLICY[entry_point].allow_skills
    snapshot = _parsed(_provider_payload(later_sdk=later_sdk, skills=skills))
    provider, result = await _drive(snapshot, entry_point)

    assert not isinstance(result, AdmissionDenied), result.detail
    assert result.error is None
    [agent] = provider.at_send
    assert agent["execution_identity"] == {"type": "service_account"}
    assert not _builtins_runnable(agent["tools"])


def _sdk_nodes(
    model: type[BaseModel], path: str, unwalked: set[str]
) -> list[tuple[str, type[BaseModel]]]:
    """Every model the SDK can parse under `path`, keyed as `REVIEWED_FIELDS` keys it."""
    nodes = [(path, model)]
    for field, info in model.model_fields.items():
        members = _models_in(info.annotation)
        for member in members:
            child = f"{path}.{field}" + (f"[{_tag(member)}]" if len(members) > 1 else "")
            if child in caps.UNWALKED_NODES:
                unwalked.add(child)
            else:
                nodes += _sdk_nodes(member, child, unwalked)
    return nodes


def test_every_field_the_sdk_declares_has_been_reviewed() -> None:
    """An SDK upgrade that parses a new field fails here, not on a deployment.

    Each model reachable from the session type must be a reviewed node, and
    each field it declares a reviewed field of that node, or the node must be
    one the gate refuses or treats as opaque (`UNWALKED_NODES`).
    """
    unwalked: set[str] = set()
    nodes = _sdk_nodes(BetaManagedAgentsSession, "session", unwalked)
    assert len(nodes) > 20
    assert unwalked == caps.UNWALKED_NODES
    unreviewed = {
        f"{path}.{field}"
        for path, model in nodes
        for field in model.model_fields
        if field not in caps.REVIEWED_FIELDS.get(path, frozenset())
    }
    assert not unreviewed, f"review these SDK fields in unattended_capabilities: {unreviewed}"


@pytest.mark.parametrize(
    ("change", "detail"),
    [
        pytest.param(
            _set("agent.execution_identity", {"type": "user", "user_id": "u_1"}),
            "unreviewed provider field session.agent.execution_identity.user_id",
            id="identity_with_a_principal",
        ),
        pytest.param(
            _set("agent.execution_identity", {"type": "user"}),
            "unreviewed value at session.agent.execution_identity.type",
            id="identity_of_another_kind",
        ),
        pytest.param(
            _set("agent.execution_identity", "service_account"),
            "unreviewed value at session.agent.execution_identity",
            id="identity_as_a_string",
        ),
        pytest.param(
            _set("agent.execution_identity", {}),
            "missing provider field session.agent.execution_identity.type",
            id="identity_empty",
        ),
        pytest.param(
            _set("budget", {}),
            "missing provider field session.budget.type",
            id="budget_empty",
        ),
        pytest.param(
            _set("budget", {"max_list_cost": {"amount": "2500", "currency": "USD"}}),
            "missing provider field session.budget.type",
            id="budget_without_type",
        ),
        pytest.param(
            _set("budget", {"type": "unlimited"}),
            "unreviewed value at session.budget.type",
            id="budget_of_another_kind",
        ),
        pytest.param(
            _set(
                "budget", {"type": "limit", "max_list_cost": {"amount": "1.5", "currency": "USD"}}
            ),
            "unreviewed value at session.budget.max_list_cost.amount",
            id="budget_not_in_minor_units",
        ),
        pytest.param(
            _set("budget", {"type": "limit", "max_list_cost": None, "tools": True}),
            "unreviewed provider field session.budget.tools",
            id="budget_field",
        ),
        pytest.param(
            _set("usage.server_tool_use", {"code_execution_requests": 1}),
            "unreviewed provider field session.usage.server_tool_use.code_execution_requests",
            id="usage_counter",
        ),
        pytest.param(
            _set("sandbox", {}), "unreviewed provider field session.sandbox", id="session_field"
        ),
        pytest.param(
            _set("agent.tools.1.configs.0.scope", "all"),
            "unreviewed provider field session.agent.tools.1.configs.0.scope",
            id="config_field",
        ),
    ],
)
def test_the_refusal_names_the_unreviewed_path(
    change: Callable[[dict[str, Any]], object], detail: str
) -> None:
    session = _mutated(lambda s: (s.update(deepcopy(_PREVIEW_SESSION_EXTRAS)), change(s)))
    with pytest.raises(AdmissionDenied, match="backend_unsupported") as refused:
        require_allowed_capabilities(session)
    assert refused.value.reason == "backend_unsupported"
    assert refused.value.detail == detail
    assert str(refused.value) == f"backend_unsupported: {detail}"


def test_only_a_coordinator_is_refused_as_delegation() -> None:
    with pytest.raises(AdmissionDenied) as refused:
        require_allowed_capabilities(_coordinator_session(), allow_skills=True)
    assert refused.value.detail == caps.COORDINATOR_DETAIL
    with pytest.raises(AdmissionDenied) as refused:
        require_allowed_capabilities(_mutated(_set("budget", {"type": "unlimited"})))
    assert "delegat" not in str(refused.value.detail)


class _Money(BaseModel):
    amount: str
    currency: str


class _BudgetLimit(BaseModel):
    """`BetaManagedAgentsBudgetLimit`, as anthropic 1.11 declares it."""

    max_list_cost: _Money
    type: Literal["limit"]


class _Identity(BaseModel):
    type: str


def _later_sdk_session(identity: str = "service_account") -> BetaManagedAgentsSession:
    """`budget` and `execution_identity` parsed as models, not kept as extras."""
    session = _root_session()
    budget = _BudgetLimit.model_validate(_LATER_SDK_SESSION_EXTRAS["budget"])
    agent = session.agent.model_copy(update={"execution_identity": _Identity(type=identity)})
    return session.model_copy(update={"budget": budget, "agent": agent})


def test_a_later_sdk_parsing_the_reviewed_fields_is_admitted() -> None:
    require_allowed_capabilities(_later_sdk_session())
    with pytest.raises(AdmissionDenied) as refused:
        require_allowed_capabilities(_later_sdk_session(identity="user"))
    assert refused.value.detail == "unreviewed value at session.agent.execution_identity.type"


def test_builtin_tool_names_match_the_sdk_toolset() -> None:
    """Tripwire: a built-in the SDK adds stays refused until it is reviewed here."""
    from anthropic.types.beta.beta_managed_agents_agent_tool_config import (
        BetaManagedAgentsAgentToolConfig,
    )

    sdk = set(get_args(BetaManagedAgentsAgentToolConfig.model_fields["name"].annotation))
    assert sdk == caps.BUILTIN_TOOL_NAMES


@pytest.mark.parametrize("name", ["local_fn", "custom_writer", "bash2"])
async def test_an_opted_in_routine_still_refuses_a_call_that_is_not_a_builtin(name: str) -> None:
    """The second lock: with built-ins on, a custom tool's call is still refused."""
    inner = AsyncMock(return_value=caps.ToolConfirmationResult(allow=True))
    posture = caps.capability_posture(PolicyApproval(decide=inner), allow_builtins=True)

    decision = await posture.decide(
        ToolCall(tool_use_id="tu_custom", server_name=None, tool_name=name)
    )

    assert decision.allow is False
    inner.assert_not_awaited()


@pytest.mark.parametrize(
    "kind,permission,refusal",
    [
        ("builtin", "auto", "Automatic sandbox or custom tools"),
        ("custom", "auto", "Automatic sandbox or custom tools"),
        ("mcp_toolset", "auto", "cannot inspect these tools"),
        ("builtin", "ask", None),
        ("custom", "ask", None),
        (None, None, None),
    ],
)
def test_native_unattended_tools_cannot_bypass_host_inspection(kind, permission, refusal):
    from daimon.core.turn.unattended import require_native_unattended_capabilities
    from mux.contracts.ids import ModelRef
    from mux.contracts.resources import AgentSpec, ToolSpec

    spec = AgentSpec(
        name="native-worker",
        model=ModelRef(provider="openai", id="gpt-6-astra"),
        tools=() if kind is None else (ToolSpec(name="bash", kind=kind, permission=permission),),
    )
    if refusal is not None:
        with pytest.raises(AdmissionDenied, match=refusal):
            require_native_unattended_capabilities(spec, profile_id="openai.conversation_only")
    else:
        require_native_unattended_capabilities(spec, profile_id="openai.conversation_only")
    with pytest.raises(AdmissionDenied, match="tool configuration cannot be checked"):
        require_native_unattended_capabilities(None, profile_id="openai.conversation_only")


@pytest.mark.parametrize("capability", ["mcp", "provider_options"])
def test_native_unattended_refuses_ungated_connections_and_provider_options(capability):
    from daimon.core.turn.unattended import require_native_unattended_capabilities
    from mux.contracts.ids import ModelRef
    from mux.contracts.resources import AgentSpec, MCPConnection

    spec = AgentSpec(
        name="native-worker",
        model=ModelRef(
            provider="openai",
            id="gpt-6-astra",
            options={"future_execution": True} if capability == "provider_options" else {},
        ),
        mcp_servers=(MCPConnection(name="remote", url="https://example.invalid/mcp"),)
        if capability == "mcp"
        else (),
    )
    with pytest.raises(AdmissionDenied, match="cannot inspect these tools"):
        require_native_unattended_capabilities(spec, profile_id="openai.conversation_only")


def test_native_hosted_bash_refuses_even_with_an_empty_agent_tool_readback():
    from daimon.core.turn.unattended import require_native_unattended_capabilities
    from mux.contracts.ids import ModelRef
    from mux.contracts.resources import AgentSpec

    spec = AgentSpec(name="hosted", model=ModelRef(provider="openai", id="gpt-6-astra"), tools=())
    with pytest.raises(AdmissionDenied, match="Automatic sandbox or custom tools"):
        require_native_unattended_capabilities(spec, profile_id="openai.persistent_workspace")


async def test_long_job_policy_keeps_builtins_and_removes_all_mcp():
    session = _root_session()
    policy = caps.capability_policy("long_job")
    assert policy.allow_builtins and not policy.allow_skills
    provider, result = await _drive(session, "long_job")
    assert result.error is None
    [sent] = provider.at_send
    assert sent["mcp_servers"] == []
    assert all(t.get("type") != "mcp_toolset" for t in sent["tools"])
    assert _builtins_runnable(sent["tools"])
    posture = unattended_posture(
        ToolSafetyPolicy(),
        session=session,
        public_url=None,
        session_factory=None,
        tenant_id=None,
        entry_point="long_job",
    )
    assert (
        await posture.decide(ToolCall(tool_use_id="shell", server_name=None, tool_name="bash"))
    ).allow
    assert not (
        await posture.decide(
            ToolCall(tool_use_id="read", server_name="linear", tool_name="get_issue")
        )
    ).allow


async def test_job_completion_posture_allows_only_get_long_job():
    allowlist = frozenset({ServerTool("daimon-mcp", "get_long_job")})
    posture = capability_posture(
        PolicyApproval(decide=AsyncMock(return_value=ToolConfirmationResult(allow=True))),
        tool_allowlist=allowlist,
    )
    for server, tool, expected in [
        ("daimon-mcp", "get_long_job", True),
        ("daimon-mcp", "start_long_job", False),
        ("linear", "get_issue", False),
        (None, "bash", False),
    ]:
        result = await posture.decide(
            ToolCall(tool_use_id="call", server_name=server, tool_name=tool)
        )
        assert result.allow is expected


async def test_completion_allowlist_keeps_verified_daimon_reader_only():
    session = _root_session(
        tools=[
            *_tools(),
            {
                "type": "mcp_toolset",
                "mcp_server_name": "daimon-mcp",
                "default_config": ALLOW,
                "configs": [{"name": "start_long_job", **ALLOW}],
            },
        ],
        mcp_servers=[
            *SERVERS,
            {"name": "daimon-mcp", "type": "url", "url": "https://daimon.example/mcp"},
        ],
    )
    constrained = await caps.constrain_capabilities(
        session,
        ToolSafetyPolicy(),
        public_url="https://daimon.example/mcp",
        session_factory=None,
        tenant_id=None,
        allow_builtins=False,
        allow_skills=True,
        tool_allowlist=frozenset({ServerTool("daimon-mcp", "get_long_job")}),
    )
    assert [s["name"] for s in constrained.send.mcp_servers] == ["daimon-mcp"]
    [mcp] = [t for t in constrained.send.tools if t["type"] == "mcp_toolset"]
    assert not mcp["default_config"]["enabled"]
    assert [c["name"] for c in mcp["configs"]] == ["get_long_job"]
    assert not _builtins_runnable(list(constrained.send.tools))
