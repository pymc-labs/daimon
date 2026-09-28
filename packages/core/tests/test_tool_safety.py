"""The attached-tool read/write model: classification, decisions, toolset policy."""

from __future__ import annotations

import pytest
from daimon.core.defaults.mcp_merge import DAIMON_MCP_SERVER_NAME
from daimon.core.tool_safety import (
    DAIMON_SERVER_NAME,
    OPEN_TOOL_SAFETY,
    ToolAnnotations,
    ToolCall,
    ToolSafetyPolicy,
    classify_tool,
    decide_tool_call,
    session_tools_for_policy,
    toolset_permission_policy,
)

_ON = ToolSafetyPolicy(enabled=True)


def _call(server: str | None, tool: str) -> ToolCall:
    return ToolCall(tool_use_id="tu_1", server_name=server, tool_name=tool)


def test_daimon_server_name_matches_the_defaults_constant() -> None:
    assert DAIMON_SERVER_NAME == DAIMON_MCP_SERVER_NAME


@pytest.mark.parametrize(
    ("tool", "effect"),
    [
        ("get_issue", "read"),
        ("list_teams", "read"),
        ("search", "read"),
        ("notion_get_page", "read"),
        ("query-database", "read"),
        ("create_issue", "write"),
        ("update_deal", "write"),
        ("delete_list", "write"),
        ("send_email", "write"),
        ("run_report", "write"),
        ("something_unfamiliar", "write"),
    ],
)
def test_the_tool_name_decides_when_nothing_else_does(tool: str, effect: str) -> None:
    assert classify_tool(_ON, server_name="linear", tool_name=tool) == effect


def test_annotations_win_over_the_name() -> None:
    assert (
        classify_tool(
            _ON,
            server_name="s",
            tool_name="create_preview",
            annotations=ToolAnnotations(read_only_hint=True),
        )
        == "read"
    )
    assert (
        classify_tool(
            _ON,
            server_name="s",
            tool_name="get_and_purge",
            annotations=ToolAnnotations(destructive_hint=True),
        )
        == "write"
    )


def test_an_operator_override_wins_and_a_tool_key_beats_its_server_key() -> None:
    policy = ToolSafetyPolicy(
        enabled=True,
        effects={"notion": "write", "notion/get_page": "read"},
    )
    assert classify_tool(policy, server_name="notion", tool_name="get_page") == "read"
    assert classify_tool(policy, server_name="notion", tool_name="get_database") == "write"
    assert (
        classify_tool(
            policy,
            server_name="notion",
            tool_name="search",
            annotations=ToolAnnotations(read_only_hint=True),
        )
        == "write"
    )


def test_disabled_allows_everything() -> None:
    verdict = decide_tool_call(OPEN_TOOL_SAFETY, _call("hubspot", "delete_deal"), attended=False)
    assert (verdict.outcome, verdict.reason) == ("allow", "disabled")


def test_sandbox_and_daimon_tools_are_never_gated() -> None:
    assert decide_tool_call(_ON, _call(None, "bash"), attended=False).outcome == "allow"
    assert (
        decide_tool_call(_ON, _call(DAIMON_SERVER_NAME, "routine_delete"), attended=False).outcome
        == "allow"
    )


def test_reads_run_attended_or_not() -> None:
    for attended in (True, False):
        verdict = decide_tool_call(_ON, _call("linear", "get_issue"), attended=attended)
        assert (verdict.outcome, verdict.effect) == ("allow", "read")


def test_a_write_asks_in_chat_and_is_denied_unattended() -> None:
    call = _call("linear", "create_issue")
    assert decide_tool_call(_ON, call, attended=True).outcome == "ask"
    unattended = decide_tool_call(_ON, call, attended=False)
    assert (unattended.outcome, unattended.reason) == ("deny", "unattended_write")


@pytest.mark.parametrize("key", ["linear", "linear/create_issue", "*"])
def test_unattended_writes_can_be_allowed(key: str) -> None:
    policy = ToolSafetyPolicy(enabled=True, unattended_writes=(key,))
    verdict = decide_tool_call(policy, _call("linear", "create_issue"), attended=False)
    assert (verdict.outcome, verdict.reason) == ("allow", "unattended_write_allowed")


def test_the_deny_list_beats_reads_and_attendance() -> None:
    policy = ToolSafetyPolicy(
        enabled=True, denied=("hubspot/get_deal", "stripe"), unattended_writes=("*",)
    )
    for attended in (True, False):
        assert decide_tool_call(
            policy, _call("hubspot", "get_deal"), attended=attended
        ).outcome == ("deny")
        assert decide_tool_call(policy, _call("stripe", "refund"), attended=attended).reason == (
            "denied_by_operator"
        )
    assert decide_tool_call(policy, _call("hubspot", "get_contact"), attended=True).outcome == (
        "allow"
    )


def test_toolset_policy_asks_only_for_gated_servers_when_enabled() -> None:
    assert toolset_permission_policy(OPEN_TOOL_SAFETY, server_name="linear") == {
        "type": "always_allow"
    }
    assert toolset_permission_policy(_ON, server_name="linear") == {"type": "always_ask"}
    assert toolset_permission_policy(_ON, server_name=DAIMON_SERVER_NAME) == {
        "type": "always_allow"
    }
    assert toolset_permission_policy(_ON, server_name=None) == {"type": "always_allow"}


def _tools() -> list[dict[str, object]]:
    return [
        {
            "type": "agent_toolset_20260401",
            "default_config": {"permission_policy": {"type": "always_allow"}},
        },
        {
            "type": "mcp_toolset",
            "mcp_server_name": DAIMON_SERVER_NAME,
            "default_config": {"permission_policy": {"type": "always_allow"}},
        },
        {
            "type": "mcp_toolset",
            "mcp_server_name": "linear",
            "default_config": {"permission_policy": {"type": "always_allow"}},
            "configs": [{"name": "create_issue", "permission_policy": {"type": "always_allow"}}],
        },
    ]


def test_session_tools_are_untouched_when_disabled() -> None:
    assert session_tools_for_policy(OPEN_TOOL_SAFETY, _tools()) is None


def test_session_tools_gate_third_party_toolsets_only() -> None:
    out = session_tools_for_policy(_ON, _tools())
    assert out is not None
    assert out[0] == _tools()[0]
    assert out[1] == _tools()[1]
    assert out[2]["default_config"] == {"permission_policy": {"type": "always_ask"}}
    assert out[2]["configs"] == [{"name": "create_issue"}], (
        "a per-tool always_allow would let that tool skip the pause"
    )


def test_session_tools_report_no_change_once_gated() -> None:
    once = session_tools_for_policy(_ON, _tools())
    assert once is not None
    assert session_tools_for_policy(_ON, once) is None
