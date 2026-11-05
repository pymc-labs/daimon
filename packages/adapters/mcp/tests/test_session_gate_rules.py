"""A session bound with daimon agent rules still proves its card from the recorded hash."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import _session_gate
from daimon.core.config import RepoMemorySettings
from daimon.core.session_snapshot import hash_tools, session_tools
from daimon.core.stores.domain import Role
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.testing.ma_models import ma_agent

TENANT = uuid.UUID(int=1)
AUTH = AuthIdentity(tenant_id=TENANT, account_id=uuid.UUID(int=2), role=Role.USER)
POLICY = ToolSafetyPolicy(enabled=True, agent_rules=True)
AGENT = ma_agent(
    id="agent",
    tools=[
        {
            "type": "mcp_toolset",
            "mcp_server_name": "daimon-mcp",
            "configs": [],
            "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
        }
    ],
    mcp_servers=[{"name": "daimon-mcp", "type": "url", "url": "https://mcp.example/mcp"}],
)


def _runtime() -> McpRuntime:
    settings = SimpleNamespace(
        tool_safety=POLICY,
        repo_memory=RepoMemorySettings(),
        mcp=SimpleNamespace(public_url="https://mcp.example/mcp"),
        turn=SimpleNamespace(),
        microsoft_accounts=SimpleNamespace(configured=False),
    )
    return cast(
        McpRuntime, SimpleNamespace(client=object(), session_factory=object(), settings=settings)
    )


@pytest.fixture(autouse=True)
def patch_session_gate_collaborators(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_session_gate, "retrieve_agent", AsyncMock(return_value=AGENT))
    monkeypatch.setattr(
        _session_gate, "resolve_hidden_mcp_server_names", AsyncMock(return_value=frozenset[str]())
    )

    def backend_off(_turn: object) -> bool:
        return False

    monkeypatch.setattr(_session_gate, "backend_selection_enabled", backend_off)
    monkeypatch.setattr(
        _session_gate, "confirmation_tools", AsyncMock(return_value=frozenset[str]())
    )


def _recorded_sha(rule_tools: frozenset[str], confirmed: frozenset[str] = frozenset[str]()) -> str:
    return hash_tools(
        session_tools(
            AGENT,
            frozenset[str](),
            tool_safety=POLICY,
            public_url="https://mcp.example/mcp",
            asks_before_publishing=True,
            daimon_rule_tools=rule_tools,
            confirmed_tools=confirmed,
        )
    )


async def _gated(
    monkeypatch: pytest.MonkeyPatch,
    *,
    recorded: frozenset[str],
    now: frozenset[str],
    confirmed: frozenset[str] = frozenset[str](),
    tool_name: str = "publish_report",
) -> bool:
    monkeypatch.setattr(_session_gate, "daimon_rule_tools_for", AsyncMock(return_value=now))
    live = cast(Any, SimpleNamespace())
    live.effective_config = SimpleNamespace(tools_sha256=_recorded_sha(recorded, confirmed))
    return await _session_gate._recorded_as_gated(  # pyright: ignore[reportPrivateUsage]
        _runtime(),
        AUTH,
        live,
        tool_name=tool_name,
        ma_agent_id="agent",
        reported_tools_sha256=hash_tools(AGENT.tools),
    )


@pytest.mark.parametrize(
    ("recorded", "now"),
    [
        (frozenset[str](), frozenset[str]()),
        (frozenset({"hubspot_update_objects"}), frozenset({"hubspot_update_objects"})),
        (frozenset[str](), frozenset({"hubspot_update_objects"})),
    ],
)
async def test_the_recorded_hash_proves_the_card_with_or_without_daimon_rules(
    monkeypatch: pytest.MonkeyPatch, recorded: frozenset[str], now: frozenset[str]
) -> None:
    assert await _gated(monkeypatch, recorded=recorded, now=now)


async def test_a_hash_from_other_rules_than_either_candidate_is_not_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert not await _gated(
        monkeypatch,
        recorded=frozenset({"get_session"}),
        now=frozenset({"hubspot_update_objects"}),
    )


async def test_a_rule_cleared_since_the_bind_reads_as_not_gated_until_the_next_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal direction: the recorded hash cannot be matched, so no card is assumed."""
    assert not await _gated(
        monkeypatch, recorded=frozenset({"hubspot_update_objects"}), now=frozenset[str]()
    )


@pytest.mark.parametrize(
    "recorded,current,expected", [(True, True, True), (False, True, False), (True, False, False)]
)
async def test_lead_resolution_requires_recorded_and_current_confirmation_gate(
    monkeypatch: pytest.MonkeyPatch, recorded: bool, current: bool, expected: bool
) -> None:
    tools = frozenset({"lead_intake_resolve"})
    monkeypatch.setattr(
        _session_gate,
        "confirmation_tools",
        AsyncMock(return_value=tools if current else frozenset[str]()),
    )
    assert (
        await _gated(
            monkeypatch,
            recorded=frozenset[str](),
            now=frozenset[str](),
            confirmed=tools if recorded else frozenset[str](),
            tool_name="lead_intake_resolve",
        )
        is expected
    )


@pytest.mark.parametrize(
    "tool", ["create_watch", "resume_watch", "rotate_watch_key", "update_watch"]
)
async def test_recorded_watch_card_is_proven_with_tool_safety_off(monkeypatch, tool):
    from daimon.core.tool_safety import OPEN_TOOL_SAFETY, WATCH_CONFIRMED_TOOLS

    runtime = _runtime()
    runtime.settings.tool_safety = OPEN_TOOL_SAFETY
    monkeypatch.setattr(_session_gate, "daimon_rule_tools_for", AsyncMock(return_value=frozenset()))
    recorded = session_tools(
        AGENT,
        frozenset(),
        tool_safety=OPEN_TOOL_SAFETY,
        public_url="https://mcp.example/mcp",
        confirmed_tools=WATCH_CONFIRMED_TOOLS,
    )
    live = SimpleNamespace(effective_config=SimpleNamespace(tools_sha256=hash_tools(recorded)))
    assert await _session_gate._recorded_as_gated(
        runtime,
        AUTH,
        live,
        tool_name=tool,
        ma_agent_id=AGENT.id,
        reported_tools_sha256=hash_tools(AGENT.tools),
    )


@pytest.mark.parametrize(
    ("tool", "proven"),
    [("create_watch", True), ("resume_watch", True), ("update_watch", False)],
)
async def test_a_session_bound_before_edits_were_gated_never_proves_an_edit(
    monkeypatch, tool, proven
):
    from daimon.core.tool_safety import OPEN_TOOL_SAFETY, WATCH_CONFIRMED_TOOLS

    runtime = _runtime()
    runtime.settings.tool_safety = OPEN_TOOL_SAFETY
    monkeypatch.setattr(_session_gate, "daimon_rule_tools_for", AsyncMock(return_value=frozenset()))
    recorded = session_tools(
        AGENT,
        frozenset(),
        tool_safety=OPEN_TOOL_SAFETY,
        public_url="https://mcp.example/mcp",
        confirmed_tools=WATCH_CONFIRMED_TOOLS - {"update_watch"},
    )
    live = SimpleNamespace(effective_config=SimpleNamespace(tools_sha256=hash_tools(recorded)))
    assert (
        await _session_gate._recorded_as_gated(
            runtime,
            AUTH,
            live,
            tool_name=tool,
            ma_agent_id=AGENT.id,
            reported_tools_sha256=hash_tools(AGENT.tools),
        )
        is proven
    )


@pytest.mark.parametrize("policy_enabled", [False, True])
@pytest.mark.parametrize(
    "requested",
    ["request_agent_backend_switch", "create_watch", "resume_watch", "rotate_watch_key"],
)
@pytest.mark.parametrize("modern", [False, True])
async def test_recorded_pre_watch_cards_preserve_non_watch_actions_only(
    monkeypatch, policy_enabled, requested, modern
):
    """An old switch card remains proof for switching, never for watch enrollment."""
    from daimon.core.tool_safety import WATCH_CONFIRMED_TOOLS

    runtime = _runtime()
    runtime.settings.tool_safety = ToolSafetyPolicy(enabled=policy_enabled)
    monkeypatch.setattr(_session_gate, "backend_selection_enabled", lambda _: True)
    monkeypatch.setattr(_session_gate, "daimon_rule_tools_for", AsyncMock(return_value=frozenset()))
    # A literal historical bind, independent of today's session_tools builder.
    names = ["add_skill"] if policy_enabled else []
    if modern:
        names.extend(sorted(WATCH_CONFIRMED_TOOLS))
    names.append("request_agent_backend_switch")
    if modern and not policy_enabled:
        names.sort()
    recorded_agent = ma_agent(
        tools=[
            {
                "type": "mcp_toolset",
                "mcp_server_name": "daimon-mcp",
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
                "configs": [
                    {"name": name, "enabled": True, "permission_policy": {"type": "always_ask"}}
                    for name in names
                ],
            }
        ],
    )
    live = SimpleNamespace(
        effective_config=SimpleNamespace(tools_sha256=hash_tools(recorded_agent.tools))
    )
    assert await _session_gate._recorded_as_gated(
        runtime,
        AUTH,
        live,
        tool_name=requested,
        ma_agent_id=AGENT.id,
        reported_tools_sha256=hash_tools(AGENT.tools),
    ) is (modern or requested == "request_agent_backend_switch")


@pytest.mark.parametrize(
    ("tool", "proven"),
    [("add_skill", True), ("add_server_skill", False), ("approve_server_skill_update", False)],
)
async def test_a_bind_from_before_server_skills_proves_other_cards_never_theirs(
    monkeypatch, tool, proven
):
    from daimon.core.tool_safety import CARDED_TOOLS, CONFIRMED_DAIMON_TOOLS, WATCH_CONFIRMED_TOOLS

    policy = ToolSafetyPolicy(enabled=True)
    runtime = _runtime()
    runtime.settings.tool_safety = policy
    monkeypatch.setattr(_session_gate, "daimon_rule_tools_for", AsyncMock(return_value=frozenset()))
    before = {"add_server_skill", "approve_server_skill_update"}
    recorded = session_tools(
        AGENT,
        frozenset(),
        tool_safety=policy,
        public_url="https://mcp.example/mcp",
        confirmed_tools=(WATCH_CONFIRMED_TOOLS | set(CARDED_TOOLS) | {"approve_reach"}) - before,
    )
    # Policy on asks on every confirmed tool; the older bind held all but the new two.
    older = _session_gate._without_configs(recorded, frozenset(before))  # pyright: ignore[reportPrivateUsage]
    assert before <= set(CONFIRMED_DAIMON_TOOLS)
    live = SimpleNamespace(effective_config=SimpleNamespace(tools_sha256=hash_tools(older)))
    assert (
        await _session_gate._recorded_as_gated(  # pyright: ignore[reportPrivateUsage]
            runtime,
            AUTH,
            live,
            tool_name=tool,
            ma_agent_id=AGENT.id,
            reported_tools_sha256=hash_tools(AGENT.tools),
        )
        is proven
    )


@pytest.mark.parametrize("tamper", ["recorded_hash", "reported_tools", "missing_target_gate"])
async def test_pre_watch_card_compatibility_requires_the_exact_scoped_target_gate(
    monkeypatch, tamper
):
    runtime = _runtime()
    runtime.settings.tool_safety = ToolSafetyPolicy(enabled=False)
    monkeypatch.setattr(_session_gate, "backend_selection_enabled", lambda _: True)
    monkeypatch.setattr(_session_gate, "daimon_rule_tools_for", AsyncMock(return_value=frozenset()))
    recorded_agent = ma_agent(
        tools=[
            {
                "type": "mcp_toolset",
                "mcp_server_name": "daimon-mcp",
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
                "configs": []
                if tamper == "missing_target_gate"
                else [
                    {
                        "name": "request_agent_backend_switch",
                        "enabled": True,
                        "permission_policy": {"type": "always_ask"},
                    }
                ],
            }
        ]
    )
    live = SimpleNamespace(
        effective_config=SimpleNamespace(
            tools_sha256="unrelated"
            if tamper == "recorded_hash"
            else hash_tools(recorded_agent.tools)
        )
    )
    assert not await _session_gate._recorded_as_gated(
        runtime,
        AUTH,
        live,
        tool_name="request_agent_backend_switch",
        ma_agent_id=AGENT.id,
        reported_tools_sha256="out-of-band"
        if tamper == "reported_tools"
        else hash_tools(AGENT.tools),
    )


async def test_recorded_suppression_still_proves_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = ma_agent(
        id="agent",
        tools=[
            *AGENT.tools,
            {
                "type": "mcp_toolset",
                "mcp_server_name": "legacy",
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
                "configs": [],
            },
        ],
        mcp_servers=[
            *AGENT.mcp_servers,
            {
                "name": "legacy",
                "type": "url",
                "url": "https://mcp.example/legacy",
            },
        ],
    )
    monkeypatch.setattr(_session_gate, "retrieve_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(
        _session_gate,
        "daimon_rule_tools_for",
        AsyncMock(return_value=frozenset()),
    )
    runtime = _runtime()
    runtime.settings.repo_memory = RepoMemorySettings(
        enabled=True, legacy_mcp_server_names=("legacy",)
    )
    recorded = hash_tools(
        session_tools(
            agent,
            frozenset(),
            tool_safety=POLICY,
            public_url="https://mcp.example/mcp",
            asks_before_publishing=True,
            legacy_memory_servers=("legacy",),
        )
    )
    assert await _session_gate._recorded_as_gated(  # pyright: ignore[reportPrivateUsage]
        runtime,
        AUTH,
        cast(Any, SimpleNamespace(effective_config=SimpleNamespace(tools_sha256=recorded))),
        tool_name="publish_report",
        ma_agent_id="agent",
        reported_tools_sha256=hash_tools(agent.tools),
    )


def _without_carded(tools: list[Any]) -> list[Any]:
    """The tools a session bound before the carded tools existed was sent."""
    from daimon.core.tool_safety import CARDED_TOOLS

    return [
        tool.model_copy(
            update={"configs": [c for c in tool.configs or [] if c.name not in CARDED_TOOLS]}
        )
        if tool.type == "mcp_toolset" and tool.mcp_server_name == "daimon-mcp"
        else tool
        for tool in tools
    ]


@pytest.mark.parametrize(
    ("tool_name", "proven"),
    [("add_skill", True), ("publish_report", True), ("remove_my_data", False)],
)
async def test_a_session_bound_before_the_carded_tools_proves_other_cards_never_a_carded_one(
    monkeypatch: pytest.MonkeyPatch, tool_name: str, proven: bool
) -> None:
    """The old hash still proves add_skill and publish, but no old hash authorizes a carded tool."""
    monkeypatch.setattr(_session_gate, "daimon_rule_tools_for", AsyncMock(return_value=frozenset()))
    recorded = _without_carded(
        session_tools(
            AGENT,
            frozenset[str](),
            tool_safety=POLICY,
            public_url="https://mcp.example/mcp",
            asks_before_publishing=True,
        )
    )
    live = cast(
        Any, SimpleNamespace(effective_config=SimpleNamespace(tools_sha256=hash_tools(recorded)))
    )
    assert (
        await _session_gate._recorded_as_gated(  # pyright: ignore[reportPrivateUsage]
            _runtime(),
            AUTH,
            live,
            tool_name=tool_name,
            ma_agent_id="agent",
            reported_tools_sha256=hash_tools(AGENT.tools),
        )
        is proven
    ), f"{tool_name} should{'' if proven else ' not'} be proven by a pre-carded bind"


@pytest.mark.parametrize("policy_enabled", [False, True])
@pytest.mark.parametrize("with_reach", [False, True], ids=["pre-reach", "current"])
@pytest.mark.parametrize("requested", ["request_agent_backend_switch", "approve_reach"])
async def test_recorded_cards_with_or_before_the_reach_gate(
    monkeypatch, policy_enabled, with_reach, requested
):
    """Today's binds carry the reach gate; older ones prove every card but that one."""
    from daimon.core.tool_safety import REACH_APPROVAL_TOOLS, WATCH_CONFIRMED_TOOLS

    runtime = _runtime()
    runtime.settings.tool_safety = ToolSafetyPolicy(enabled=policy_enabled)
    monkeypatch.setattr(_session_gate, "backend_selection_enabled", lambda _: True)
    monkeypatch.setattr(_session_gate, "daimon_rule_tools_for", AsyncMock(return_value=frozenset()))
    names = {"request_agent_backend_switch", *WATCH_CONFIRMED_TOOLS}
    if policy_enabled:
        names.add("add_skill")
    if with_reach:
        names |= REACH_APPROVAL_TOOLS
    ordered = (
        ["add_skill", *sorted(names - {"add_skill", "request_agent_backend_switch"})]
        + ["request_agent_backend_switch"]
        if policy_enabled
        else sorted(names)
    )
    recorded_agent = ma_agent(
        tools=[
            {
                "type": "mcp_toolset",
                "mcp_server_name": "daimon-mcp",
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
                "configs": [
                    {"name": name, "enabled": True, "permission_policy": {"type": "always_ask"}}
                    for name in ordered
                ],
            }
        ],
    )
    live = SimpleNamespace(
        effective_config=SimpleNamespace(tools_sha256=hash_tools(recorded_agent.tools))
    )

    proven = await _session_gate._recorded_as_gated(
        runtime,
        AUTH,
        live,
        tool_name=requested,
        ma_agent_id=AGENT.id,
        reported_tools_sha256=hash_tools(AGENT.tools),
    )

    assert proven is (with_reach or requested != "approve_reach"), (
        "a bind proves the cards it gated, and only a current one proves a reach approval"
    )


@pytest.mark.parametrize("requested", ["add_skill", "remove_my_data", "approve_reach"])
async def test_a_session_bound_before_both_the_carded_and_reach_gates_proves_only_other_cards(
    monkeypatch: pytest.MonkeyPatch, requested: str
) -> None:
    """The two older formats are independent: a bind lacking both proves neither new kind."""
    from daimon.core.tool_safety import CARDED_TOOLS, REACH_APPROVAL_TOOLS

    monkeypatch.setattr(_session_gate, "daimon_rule_tools_for", AsyncMock(return_value=frozenset()))
    newer = frozenset(CARDED_TOOLS) | REACH_APPROVAL_TOOLS
    recorded = session_tools(
        AGENT,
        frozenset[str](),
        tool_safety=POLICY,
        public_url="https://mcp.example/mcp",
        asks_before_publishing=True,
    )
    recorded = [
        tool.model_copy(update={"configs": [c for c in tool.configs or [] if c.name not in newer]})
        if tool.type == "mcp_toolset" and tool.mcp_server_name == "daimon-mcp"
        else tool
        for tool in recorded
    ]
    live = cast(
        Any, SimpleNamespace(effective_config=SimpleNamespace(tools_sha256=hash_tools(recorded)))
    )

    proven = await _session_gate._recorded_as_gated(  # pyright: ignore[reportPrivateUsage]
        _runtime(),
        AUTH,
        live,
        tool_name=requested,
        ma_agent_id="agent",
        reported_tools_sha256=hash_tools(AGENT.tools),
    )

    assert proven is (requested == "add_skill"), (
        f"{requested} should{'' if requested == 'add_skill' else ' not'} be proven by an older bind"
    )
