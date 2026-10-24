"""Watch posture closes shell/built-in and deployment-write allowance escapes."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.core.continuity import watch_turn
from daimon.core.continuity.watch_turn import read_report_policy, watch_posture
from daimon.core.tool_safety import ToolCall, ToolSafetyPolicy
from daimon.core.turn import unattended_capabilities as capabilities
from daimon.testing import ma_agent
from mux.contracts.ids import Scope
from mux.testing.drivers.anthropic.ma import ma_session


async def _apply_watch_capabilities(
    client, snapshot, policy, *, scope, public_url, sessions, tenant_id
):
    # Exercise the real shared provider-write helper after watch-specific preparation.
    constrained = await watch_turn.watch_capabilities(
        snapshot, policy, public_url=public_url, sessions=None, tenant_id=tenant_id
    )
    await capabilities.apply_capabilities(client, snapshot, constrained, scope=scope)


@pytest.mark.parametrize(
    "server,tool,allowed",
    [
        ("linear", "get_issue", True),
        ("linear", "create_issue", False),
        (None, "bash", False),
        (None, "execute_code", False),
        ("sandbox", "bash", False),
        ("daimon-mcp", "create_agent", False),
        ("daimon-mcp", "call_tool", False),
        ("linear", "get_or_create", False),
    ],
)
async def test_watch_read_report_refuses_writes_shell_and_builtins_even_with_write_grants(
    server, tool, allowed
):
    policy = ToolSafetyPolicy(
        enabled=False,
        unattended_writes=("*",),
        agent_rules=True,
        effects={"linear/create_issue": "read"},
    )
    result = await watch_posture(policy).decide(
        ToolCall(tool_use_id="blocked", server_name=server, tool_name=tool)
    )
    assert result.allow is allowed
    strict = read_report_policy(policy)
    assert strict.enabled and not strict.unattended_writes
    assert strict.agent_rules == policy.agent_rules


@pytest.mark.parametrize("session_url", [False, True])
async def test_watch_gate_removes_shell_skills_and_builtin_server_before_send(
    monkeypatch, session_url
):

    snapshot = ma_session(
        agent=ma_agent(
            tools=[
                {
                    "type": "agent_toolset_20260401",
                    "configs": [],
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    },
                },
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": "daimon-mcp",
                    "configs": [],
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    },
                },
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": "linear",
                    "configs": [
                        {
                            "name": "get_issue",
                            "enabled": True,
                            "permission_policy": {"type": "always_allow"},
                        }
                    ],
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    },
                },
            ],
            mcp_servers=[
                {"name": "daimon-mcp", "type": "url", "url": "https://daimon.invalid/mcp"},
                {"name": "linear", "type": "url", "url": "https://linear.invalid/mcp"},
            ],
        )
    )
    from daimon.core.mcp_server_url import session_mcp_url

    public_url = "https://DAIMON.invalid:443/mcp/"
    own_url = "https://daimon.invalid/mcp"
    if session_url:
        own_url = session_mcp_url(own_url, str(uuid.uuid4()))
    snapshot.agent.tools.append(
        snapshot.agent.tools[1].model_copy(update={"mcp_server_name": "renamed-management"})
    )
    snapshot.agent.mcp_servers.append(
        snapshot.agent.mcp_servers[0].model_copy(
            update={"name": "renamed-management", "url": own_url}
        )
    )
    update = AsyncMock()
    monkeypatch.setattr(capabilities, "update_session_record", update)
    await _apply_watch_capabilities(
        MagicMock(),
        snapshot,
        ToolSafetyPolicy(),
        scope=Scope(
            tenant_id=str(uuid.uuid4()),
            account_id=str(uuid.uuid4()),
            principal_id="watch-owner",
            authorization_id="test-grant",
        ),
        public_url=public_url,
        sessions=MagicMock(),
        tenant_id=uuid.uuid4(),
    )
    agent = update.await_args.kwargs["agent"]
    assert "skills" not in agent
    assert [t["mcp_server_name"] for t in agent["tools"]] == ["linear"]
    assert [s["name"] for s in agent["mcp_servers"]] == ["linear"]
    update.assert_awaited_once()
    assert agent["tools"][0]["default_config"]["enabled"] is False
    assert agent["tools"][0]["configs"][0]["permission_policy"] == {"type": "always_ask"}


async def test_watch_turn_requires_explicit_unattended_before_any_work():
    from datetime import UTC, datetime

    from daimon.core.stores.domain import TaskContinuationRow
    from daimon.core.turn.run import run_prepared_turn_impl

    row = TaskContinuationRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        platform="slack",
        thread_id="t",
        parent_channel_id="c",
        requester_account_id=uuid.uuid4(),
        requester_external_user_id="u",
        target_ma_agent_id="a",
        target_name="a",
        requested_work="report",
        reason="watch_fired",
        status="pending",
        skip_reason=None,
        idempotency_key=uuid.uuid4(),
        created_at=datetime.now(UTC),
        claimed_at=None,
        delivered_at=None,
    )
    with pytest.raises(ValueError, match="explicitly unattended"):
        await run_prepared_turn_impl(
            MagicMock(),
            MagicMock(),
            tenant_id=row.tenant_id,
            platform="slack",
            thread_id="t",
            external_user_id="u",
            user_message="report",
            lifecycle=MagicMock(),
            cancel=MagicMock(),
            watch_row=row,
            attended=True,
            reseed_user_message=AsyncMock(),
            recovery_lifecycle=MagicMock(),
        )


async def test_watch_reads_recheck_current_agent_denials_without_allowing_writes():
    from daimon.core.tool_safety import ResolvedAgentToolRule

    current = "allow"

    async def lookup(call):
        return ResolvedAgentToolRule(rule=current, scope="chat_and_routines")

    posture = watch_posture(ToolSafetyPolicy(agent_rules=True), rules_for=lookup)
    read = ToolCall(tool_use_id="r", server_name="linear", tool_name="get_issue")
    assert (await posture.decide(read)).allow
    current = "deny"
    assert not (await posture.decide(read)).allow
    current = "allow"
    assert not (
        await posture.decide(
            ToolCall(tool_use_id="w", server_name="linear", tool_name="create_issue")
        )
    ).allow


@pytest.mark.parametrize("cancellations", [1, 2])
async def test_restore_finishes_before_cancellation_releases_watch_session(
    monkeypatch, cancellations
):
    import asyncio

    started = asyncio.Event()
    finish = asyncio.Event()
    restored = asyncio.Event()
    snapshot = ma_session()

    async def update(*args, **kwargs):
        started.set()
        await finish.wait()
        assert kwargs["agent"]["tools"] == [
            tool.model_dump(mode="json") for tool in snapshot.agent.tools
        ]
        restored.set()

    monkeypatch.setattr(capabilities, "update_session_record", update)
    task = asyncio.create_task(
        capabilities.restore_capabilities(
            MagicMock(),
            snapshot,
            capabilities.SessionToolConfig.of(snapshot),
            scope=Scope(
                tenant_id=str(uuid.uuid4()),
                account_id=str(uuid.uuid4()),
                principal_id="watch-owner",
                authorization_id="test-grant",
            ),
        )
    )
    await started.wait()
    for _ in range(cancellations):
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert restored.is_set()


async def test_watch_refuses_skill_session_before_any_provider_mutation(monkeypatch):
    from daimon.core.turn.errors import AdmissionDenied

    update = AsyncMock()
    monkeypatch.setattr(capabilities, "update_session_record", update)
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await _apply_watch_capabilities(
            MagicMock(),
            ma_session(
                agent=ma_agent(
                    skills=[{"type": "custom", "skill_id": "skill_test", "version": "latest"}]
                )
            ),
            ToolSafetyPolicy(),
            scope=Scope(
                tenant_id=str(uuid.uuid4()),
                account_id=str(uuid.uuid4()),
                principal_id="watch-owner",
                authorization_id="test-grant",
            ),
            public_url=None,
            sessions=MagicMock(),
            tenant_id=uuid.uuid4(),
        )
    update.assert_not_awaited()


async def test_watch_refuses_multiagent_session_whose_subagent_has_shell(monkeypatch):
    from anthropic.types.beta.beta_managed_agents_session_multiagent_coordinator import (
        BetaManagedAgentsSessionMultiagentCoordinator,
    )

    SHELL = {
        "type": "agent_toolset_20260401",
        "configs": [],
        "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
    }
    from daimon.core.turn.errors import AdmissionDenied

    update = AsyncMock()
    monkeypatch.setattr(capabilities, "update_session_record", update)
    base = ma_session(agent=ma_agent())
    sub = ma_session(agent=ma_agent(id="agent_sub", name="sub", tools=[SHELL])).agent
    coordinator = BetaManagedAgentsSessionMultiagentCoordinator.model_validate(
        {"type": "coordinator", "agents": [sub.model_dump(mode="json")]}
    )
    snapshot = base.model_copy(
        update={"agent": base.agent.model_copy(update={"multiagent": coordinator})}
    )
    with pytest.raises(AdmissionDenied):
        await _apply_watch_capabilities(
            MagicMock(),
            snapshot,
            ToolSafetyPolicy(),
            scope=Scope(
                tenant_id=str(uuid.uuid4()),
                account_id=str(uuid.uuid4()),
                principal_id="watch-owner",
                authorization_id="test-grant",
            ),
            public_url=None,
            sessions=MagicMock(),
            tenant_id=uuid.uuid4(),
        )
    # If we got here without raising, the update below proves the coordinator stayed.
    update.assert_not_awaited()


def _explicit_read_snapshot():
    return ma_session(
        agent=ma_agent(
            tools=[
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": "linear",
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    },
                    "configs": [
                        {
                            "name": name,
                            "enabled": enabled,
                            "permission_policy": {"type": "always_allow"},
                        }
                        for name, enabled in [
                            ("get_issue", True),
                            ("create_issue", True),
                            ("get_secret", False),
                            ("future_action", True),
                        ]
                    ],
                }
            ],
            mcp_servers=[{"name": "linear", "type": "url", "url": "https://linear.invalid/mcp"}],
        )
    ).model_copy(deep=True)


@pytest.mark.parametrize(
    "level",
    [
        "agent",
        "server",
        "tool",
        "default",
        "config",
        "policy",
    ],
)
@pytest.mark.parametrize("value", [None, False, [], {"enabled": True}])
async def test_watch_refuses_unknown_capability_fields_before_mutation(monkeypatch, level, value):
    from daimon.core.turn.errors import AdmissionDenied

    snapshot = _explicit_read_snapshot()
    targets = {
        "stats": snapshot.stats,
        "usage": snapshot.usage,
        "session": snapshot,
        "agent": snapshot.agent,
        "model": snapshot.agent.model,
        "server": snapshot.agent.mcp_servers[0],
        "tool": snapshot.agent.tools[0],
        "default": snapshot.agent.tools[0].default_config,
        "config": snapshot.agent.tools[0].configs[0],
        "policy": snapshot.agent.tools[0].configs[0].permission_policy,
    }
    # Unknown fields in capability containers refuse even when empty.
    targets[level].__pydantic_extra__["future_execution_capability"] = value
    update = AsyncMock()
    monkeypatch.setattr(capabilities, "update_session_record", update)
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await _apply_watch_capabilities(
            MagicMock(),
            snapshot,
            ToolSafetyPolicy(),
            scope=Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="g"),
            public_url=None,
            sessions=MagicMock(),
            tenant_id=uuid.uuid4(),
        )
    update.assert_not_awaited()


def test_watch_refuses_capability_newly_recognized_by_sdk():
    from daimon.core.continuity.watch_turn import validate_watch_snapshot
    from daimon.core.turn.errors import AdmissionDenied
    from pydantic import create_model

    snapshot = ma_session()
    agent_type = create_model(
        "FutureAgent", __base__=type(snapshot.agent), future_execution_capability=(bool, True)
    )
    snapshot = snapshot.model_copy(
        update={"agent": agent_type.model_validate(snapshot.agent.model_dump())}
    )
    with pytest.raises(AdmissionDenied):
        validate_watch_snapshot(snapshot)


async def test_watch_installs_only_explicit_read_configs_with_disabled_default(monkeypatch):

    update = AsyncMock()
    monkeypatch.setattr(capabilities, "update_session_record", update)
    await _apply_watch_capabilities(
        MagicMock(),
        _explicit_read_snapshot(),
        ToolSafetyPolicy(enabled=False, unattended_writes=("*",)),
        scope=Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="g"),
        public_url=None,
        sessions=MagicMock(),
        tenant_id=uuid.uuid4(),
    )
    update.assert_awaited_once()
    agent = update.await_args.kwargs["agent"]
    assert agent["tools"] == [
        {
            "type": "mcp_toolset",
            "mcp_server_name": "linear",
            "default_config": {"enabled": False, "permission_policy": {"type": "always_ask"}},
            "configs": [
                {"name": "get_issue", "enabled": True, "permission_policy": {"type": "always_ask"}}
            ],
        }
    ]


async def test_watch_does_not_expand_default_only_toolset(monkeypatch):

    snapshot = _explicit_read_snapshot()
    snapshot.agent.tools[0].configs.clear()
    update = AsyncMock()
    monkeypatch.setattr(capabilities, "update_session_record", update)
    await _apply_watch_capabilities(
        MagicMock(),
        snapshot,
        ToolSafetyPolicy(),
        scope=Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="g"),
        public_url=None,
        sessions=MagicMock(),
        tenant_id=uuid.uuid4(),
    )
    assert update.await_args.kwargs["agent"] == {"tools": [], "mcp_servers": []}


@pytest.mark.parametrize("name", ["get_*", "get_[issue]", "get_?", "get_issue;delete", "*"])
async def test_watch_tool_allowlist_never_contains_patterns(monkeypatch, name):

    snapshot = _explicit_read_snapshot()
    snapshot.agent.tools[0].configs = [
        snapshot.agent.tools[0].configs[0].model_copy(update={"name": name})
    ]
    update = AsyncMock()
    monkeypatch.setattr(capabilities, "update_session_record", update)
    await _apply_watch_capabilities(
        MagicMock(),
        snapshot,
        ToolSafetyPolicy(),
        scope=Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="g"),
        public_url=None,
        sessions=MagicMock(),
        tenant_id=uuid.uuid4(),
    )
    assert update.await_args.kwargs["agent"] == {"tools": [], "mcp_servers": []}


def test_watch_accepts_sdk_request_id_as_diagnostic_metadata():
    from anthropic._models import add_request_id
    from daimon.core.continuity.watch_turn import validate_watch_snapshot

    snapshot = _explicit_read_snapshot()
    add_request_id(snapshot, "req_watch_validation")
    validate_watch_snapshot(snapshot)


async def test_watch_uses_shared_watch_policy_and_named_read_projection(monkeypatch):
    projection = AsyncMock(wraps=capabilities.constrain_capabilities)
    entry_point = MagicMock(wraps=capabilities.capability_policy)
    monkeypatch.setattr(capabilities, "constrain_capabilities", projection)
    monkeypatch.setattr(capabilities, "capability_policy", entry_point)
    constrained = await watch_turn.watch_capabilities(
        _explicit_read_snapshot(),
        ToolSafetyPolicy(enabled=False, unattended_writes=("*",)),
        public_url=None,
        sessions=None,
        tenant_id=uuid.uuid4(),
    )
    assert entry_point.call_args_list
    assert all(call.args == ("watch",) for call in entry_point.call_args_list)
    projection.assert_awaited_once()
    assert projection.await_args.kwargs["allow_skills"] is False
    assert projection.await_args.kwargs["tool_allowlist"] == frozenset(
        {capabilities.ServerTool("linear", "get_issue")}
    )
    assert constrained.send.tools[0]["default_config"] == {
        "enabled": False,
        "permission_policy": {"type": "always_ask"},
    }


@pytest.mark.parametrize("level", ["session", "model", "stats", "usage"])
def test_watch_accepts_additive_non_capability_fields(level: str) -> None:

    snapshot = _explicit_read_snapshot()
    target = {"session": snapshot, "model": snapshot.agent.model}.get(level)
    if target is None:
        target = getattr(snapshot, level)
    extras = target.__pydantic_extra__
    assert extras is not None
    extras["new_metadata"] = False
    capabilities.require_allowed_capabilities(snapshot, allow_skills=False)
    watch_turn.validate_watch_snapshot(snapshot)


@pytest.mark.parametrize("level", ["session", "agent", "model", "tool", "config"])
def test_watch_ignores_sdk_private_instance_bookkeeping(level):

    snapshot = _explicit_read_snapshot()
    target = {
        "session": snapshot,
        "agent": snapshot.agent,
        "model": snapshot.agent.model,
        "tool": snapshot.agent.tools[0],
        "config": snapshot.agent.tools[0].configs[0],
    }[level]
    object.__setattr__(target, "_sdk_bookkeeping", False)
    capabilities.require_allowed_capabilities(snapshot, allow_skills=False)
    watch_turn.validate_watch_snapshot(snapshot)


async def test_watch_server_names_cannot_alias_shared_allowlist_keys():
    from daimon.core.turn.errors import AdmissionDenied

    snapshot = _explicit_read_snapshot()
    regular = snapshot.agent.tools[0]
    read = regular.configs[0]
    regular.configs = [read.model_copy(update={"enabled": False})]
    snapshot.agent.tools.append(
        regular.model_copy(
            update={
                "mcp_server_name": "linear/bridge",
                "configs": [read],
            }
        )
    )
    snapshot.agent.mcp_servers.append(
        snapshot.agent.mcp_servers[0].model_copy(
            update={
                "name": "linear/bridge",
                "url": "https://bridge.invalid/mcp",
            }
        )
    )
    # The shared helper refuses ambiguous server identities before projection.
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await watch_turn.watch_capabilities(
            snapshot,
            ToolSafetyPolicy(),
            public_url=None,
            sessions=None,
            tenant_id=uuid.uuid4(),
        )


@pytest.mark.parametrize("effort", ["max", {"type": "high"}])
def test_watch_accepts_additions_to_inference_settings(effort):
    snapshot = _explicit_read_snapshot()
    snapshot.agent.model = snapshot.agent.model.model_copy(
        update={
            "inference_geo": "global",
            "effort": effort,
        }
    )
    watch_turn.validate_watch_snapshot(snapshot)
    for name in ("future_execution_capability", "tools", "system", "multiagent"):
        for value in (None, False, [], {"enabled": True}):
            unknown = snapshot.model_copy(deep=True)
            unknown.agent.model = unknown.agent.model.model_copy(update={name: value})
            watch_turn.validate_watch_snapshot(unknown)


async def test_watch_refuses_duplicate_server_toolsets_before_any_mutation(monkeypatch):
    from daimon.core.turn.errors import AdmissionDenied

    snapshot = _explicit_read_snapshot()
    original = snapshot.agent.tools[0]
    get_issue = original.configs[0]
    original.configs = [get_issue]
    snapshot.agent.tools.append(
        original.model_copy(
            update={
                "configs": [
                    get_issue.model_copy(update={"enabled": False}),
                    get_issue.model_copy(update={"name": "list_issues", "enabled": True}),
                ]
            }
        )
    )
    update = AsyncMock()
    projection = AsyncMock(wraps=capabilities.constrain_capabilities)
    monkeypatch.setattr(capabilities, "update_session_record", update)
    monkeypatch.setattr(capabilities, "constrain_capabilities", projection)
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        watch_turn.validate_watch_snapshot(snapshot)
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await _apply_watch_capabilities(
            MagicMock(),
            snapshot,
            ToolSafetyPolicy(),
            scope=Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="g"),
            public_url=None,
            sessions=MagicMock(),
            tenant_id=uuid.uuid4(),
        )
    projection.assert_not_awaited()
    update.assert_not_awaited()


async def test_watch_cannot_be_opted_in_to_sandbox_tools():
    """The per-routine sandbox opt-in never reaches a watch.

    The shared policy ignores the flag for the watch entry point, the watch
    path takes no such flag at all, and its posture refuses a native call.
    """
    import inspect

    opted = capabilities.capability_policy("watch", routine_sandbox_tools=True)
    assert opted.allow_builtins is False
    assert opted.allow_skills is False
    for entry in (
        watch_turn.watch_capabilities,
        watch_turn.validate_watch_snapshot,
        watch_posture,
    ):
        assert not {"routine_sandbox_tools", "sandbox_tools", "allow_builtins"} & set(
            inspect.signature(entry).parameters
        )
    decision = await watch_posture(ToolSafetyPolicy(enabled=True)).decide(
        ToolCall(tool_use_id="tu_bash", server_name=None, tool_name="bash")
    )
    assert decision.allow is False
