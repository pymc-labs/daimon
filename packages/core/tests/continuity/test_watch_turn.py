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
    if level == "session":
        target = snapshot
    else:
        target = snapshot.agent.model if level == "model" else getattr(snapshot, level)
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


@pytest.mark.parametrize(
    "query",
    [
        "?extra=1",
        "?daimon_session=one&daimon_session=two",
        "?daimon_session=invalid",
        "?daimon_session=00000000-0000-0000-0000-000000000001&extra=1",
    ],
)
async def test_review_management_alias_cannot_survive(query, monkeypatch):
    snapshot = ma_session(
        agent=ma_agent(
            tools=[
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": "renamed",
                    "configs": [
                        {
                            "name": "get_agent",
                            "enabled": True,
                            "permission_policy": {"type": "always_allow"},
                        }
                    ],
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    },
                }
            ],
            mcp_servers=[
                {"name": "renamed", "type": "url", "url": "https://daimon.invalid/mcp" + query}
            ],
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
            principal_id="owner",
            authorization_id="grant",
        ),
        public_url="https://daimon.invalid/mcp",
        sessions=MagicMock(),
        tenant_id=uuid.uuid4(),
    )
    verdict = await watch_turn.watch_posture(ToolSafetyPolicy()).decide(
        ToolCall(tool_use_id="bypass", server_name="renamed", tool_name="get_agent")
    )
    assert verdict.allow, "probe must show the retained alias can pass its read decision"
    assert update.await_args.kwargs["agent"]["tools"] == []


@pytest.mark.parametrize(
    "url",
    [
        "https://daimon.invalid./mcp",
        "https://daimon.invalid/mcp;params",
        "https://daimon.invalid/MCP",
    ],
)
def test_review_management_endpoint_aliases_are_excluded_without_becoming_trusted(url):
    from daimon.core.mcp_server_url import same_mcp_endpoint, same_mcp_url

    assert same_mcp_endpoint(url, "https://daimon.invalid/mcp")
    assert not same_mcp_url(url, "https://daimon.invalid/mcp")


def _report_row(reason: str = "watch_report", **changes):
    from datetime import UTC, datetime

    from daimon.core.stores.domain import TaskContinuationRow

    return TaskContinuationRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        platform="discord",
        thread_id="4242",
        parent_channel_id="4241",
        requester_account_id=uuid.uuid4(),
        requester_external_user_id="555",
        target_ma_agent_id="ag_target",
        target_name="target-agent",
        requested_work="report",
        reason=reason,
        status="claimed",
        skip_reason=None,
        idempotency_key=uuid.uuid4(),
        created_at=datetime.now(UTC),
        claimed_at=None,
        delivered_at=None,
    ).model_copy(update=changes)


async def test_watch_report_turn_also_requires_explicit_unattended():
    from daimon.core.turn.run import run_prepared_turn_impl

    row = _report_row()
    with pytest.raises(ValueError, match="explicitly unattended"):
        await run_prepared_turn_impl(
            MagicMock(),
            MagicMock(),
            tenant_id=row.tenant_id,
            platform="discord",
            thread_id="dm:x",
            external_user_id="555",
            user_message="report",
            lifecycle=MagicMock(),
            cancel=MagicMock(),
            watch_row=row,
            attended=None,
            reseed_user_message=AsyncMock(),
            recovery_lifecycle=MagicMock(),
        )


async def test_dm_watch_turn_runs_under_watch_posture_not_routine_posture(monkeypatch):
    """The DM report turn hands its wake row to the shared turn, unattended: that row
    selects watch capabilities and `watch_posture` there (no daimon-mcp, no sandbox, no
    built-ins, reads only), never the DM routine's session authority."""
    from contextlib import asynccontextmanager

    from daimon.core import direct_messages
    from daimon.core.direct_messages import (
        _run_admitted_dm_turn,  # pyright: ignore[reportPrivateUsage]
    )
    from daimon.core.stores.domain import Role
    from daimon.core.turn.state import TerminationReason

    row = _report_row()
    run = AsyncMock(
        return_value=MagicMock(
            state=MagicMock(error=None, termination=TerminationReason.COMPLETED, content=[]),
            ma_session_id="sess_dm",
        )
    )

    @asynccontextmanager
    async def origin(*_args, **kwargs):
        assert kwargs["attended"] is False
        yield MagicMock()

    monkeypatch.setattr(direct_messages, "run_prepared_turn", run)
    monkeypatch.setattr(direct_messages, "turn_origin", origin)
    monkeypatch.setattr(direct_messages, "render_turn_origin", lambda *_a, **_k: "controls")
    monkeypatch.setattr(direct_messages, "extract_final_response", lambda _content: "report")
    prepared = MagicMock()
    prepared.admission.hold = None
    text, session_id = await _run_admitted_dm_turn(
        MagicMock(),
        prepared,
        tenant_id=row.tenant_id,
        platform="discord",
        parent_channel_id="dm:scope",
        scope_id="dm:scope",
        external_user_id="555",
        role=Role.USER,
        prompt="seed",
        deadline=MagicMock(),
        on_sealed=AsyncMock(),
        origin="routine",
        watch_row=row,
    )
    assert (text, session_id) == ("report", "sess_dm")
    kwargs = run.await_args.kwargs
    assert kwargs["watch_row"] is row and kwargs["attended"] is False


@pytest.mark.parametrize(
    "row",
    [_report_row("watch_fired"), _report_row(requester_external_user_id="777")],
)
async def test_dm_watch_refuses_a_thread_wake_or_another_recipient(row):
    from daimon.core.direct_messages import DirectMessageAccessRevoked, run_dm_watch
    from daimon.core.turn.admission import DmSource

    with pytest.raises(DirectMessageAccessRevoked):
        await run_dm_watch(
            MagicMock(),
            row,
            owner_user_id="555",
            prompt="seed",
            source=DmSource(channel_id="4241", thread_id="4242"),
            budget_channel_id="4241",
        )


def _send_world(monkeypatch, *, delivery, row, origin_place, session_id="sess_new", **firing):
    """Patch the stores `require_watch_send` reads, around one watch and its firing."""
    from contextlib import asynccontextmanager
    from datetime import UTC, datetime

    from daimon.core.stores.domain import WatchFiringRow
    from daimon.core.turn_origin import current_execution_origin_id

    watch = MagicMock(delivery=delivery, session_id="sess_bound" if delivery == "thread" else None)
    record = WatchFiringRow(
        wake_key=row.idempotency_key,
        watch_id=uuid.uuid4(),
        revision=1,
        generation=0,
        events=[],
        omitted=0,
        created_at=datetime.now(UTC),
        **firing,
    )
    parent, thread = origin_place
    origin = MagicMock(
        attended=False,
        chat_session_id="chat",
        responder_ma_agent_id="ag_target",
        parent_channel_id=parent,
        thread_id=thread,
    )
    live = MagicMock(ma_session_id=session_id)
    live.effective_config.chat_session_id = "chat"
    monkeypatch.setattr(watch_turn, "watch_seed", AsyncMock(return_value="seed"))
    monkeypatch.setattr(watch_turn, "get_firing", AsyncMock(return_value=record))
    monkeypatch.setattr(watch_turn, "get_watch", AsyncMock(return_value=watch))
    monkeypatch.setattr(watch_turn, "get_active_origin", AsyncMock(return_value=origin))
    monkeypatch.setattr(watch_turn, "get_live_thread_session", AsyncMock(return_value=live))
    current_execution_origin_id.set(uuid.uuid4())

    @asynccontextmanager
    async def sessions():
        yield MagicMock()

    admission = MagicMock(account_id=row.requester_account_id)
    admission.agent.id = "ag_target"
    return sessions, admission


@pytest.mark.parametrize(
    "recorded,place,allowed",
    [
        ("sess_new", "dm:scope", True),
        (None, "dm:scope", False),  # not the session this firing made
        ("sess_other", "dm:scope", False),
        ("sess_new", "4242", False),  # never the thread the watch was made in
    ],
)
async def test_dm_report_sends_only_on_the_session_its_firing_recorded(
    monkeypatch, recorded, place, allowed
):
    from daimon.core.turn.errors import AdmissionDenied

    row = _report_row()
    sessions, admission = _send_world(
        monkeypatch,
        delivery="dm",
        row=row,
        origin_place=(place, place),
        delivery_session_id=recorded,
    )
    send = watch_turn.require_watch_send(
        sessions,
        row,
        admission,
        session_id="sess_new",
        tenant_id=row.tenant_id,
        platform="discord",
        thread_id=place,
    )
    if allowed:
        await send
    else:
        with pytest.raises(AdmissionDenied):
            await send


@pytest.mark.parametrize(
    "recorded,turn_thread,allowed",
    [("9001", "9001", True), (None, "9001", False), ("9001", "4242", False)],
)
async def test_new_thread_report_sends_only_in_the_thread_its_firing_made(
    monkeypatch, recorded, turn_thread, allowed
):
    from daimon.core.turn.errors import AdmissionDenied

    row = _report_row(thread_id=turn_thread)
    sessions, admission = _send_world(
        monkeypatch,
        delivery="new_thread",
        row=row,
        origin_place=("4241", turn_thread),
        delivery_thread_id=recorded,
    )
    send = watch_turn.require_watch_send(
        sessions,
        row,
        admission,
        session_id="sess_new",
        tenant_id=row.tenant_id,
        platform="discord",
        thread_id=turn_thread,
    )
    if allowed:
        await send
    else:
        with pytest.raises(AdmissionDenied):
            await send
