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
    heal_reserved_server,
    session_tools_for_policy,
    toolset_permission_policy,
    trusted_servers_for,
)

_ON = ToolSafetyPolicy(enabled=True)


def _call(server: str | None, tool: str) -> ToolCall:
    return ToolCall(tool_use_id="tu_1", server_name=server, tool_name=tool)


def test_daimon_server_name_matches_the_defaults_constant() -> None:
    assert DAIMON_SERVER_NAME == DAIMON_MCP_SERVER_NAME


@pytest.mark.parametrize(
    ("tool", "effect"),
    [
        # Plain reads.
        ("get_issue", "read"),
        ("list_teams", "read"),
        ("search", "read"),
        ("notion_get_page", "read"),
        ("fetch-page", "read"),
        ("getContact", "read"),
        ("find_user", "read"),
        # Plain writes and unknowns.
        ("create_issue", "write"),
        ("update_deal", "write"),
        ("delete_list", "write"),
        ("send_email", "write"),
        ("run_report", "write"),
        ("something_unfamiliar", "write"),
        # Evaluator finding 1: compound names that used to read as reads.
        ("get_or_create_contact", "write"),
        ("find_or_create", "write"),
        ("search_and_replace", "write"),
        ("fetch_and_delete", "write"),
        ("lookup_and_update", "write"),
        ("retrieve_and_cancel", "write"),
        ("view_and_edit", "write"),
        ("count_and_reset", "write"),
        ("linear_search_and_delete", "write"),
        ("api_get_and_post", "write"),
        ("list_delete", "write"),
        ("run_query", "write"),
        ("sql_query", "write"),
        ("getOrCreateContact", "write"),
        ("call_get_page", "write"),
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


def test_sandbox_tools_and_trusted_servers_are_not_gated() -> None:
    trusted = trusted_servers_for("https://mcp.example.com/mcp")
    assert decide_tool_call(_ON, _call(None, "bash"), attended=False).outcome == "allow"
    assert (
        decide_tool_call(
            _ON,
            _call(DAIMON_SERVER_NAME, "routine_delete"),
            attended=False,
            trusted_servers=trusted,
        ).outcome
        == "allow"
    )


def test_the_reserved_name_alone_earns_no_exemption() -> None:
    assert trusted_servers_for(None) == frozenset()
    verdict = decide_tool_call(_ON, _call(DAIMON_SERVER_NAME, "delete_records"), attended=False)
    assert (verdict.outcome, verdict.reason) == ("deny", "unattended_write")


def test_a_foreign_url_under_the_reserved_name_is_healed() -> None:
    servers = [
        {"name": DAIMON_SERVER_NAME, "type": "url", "url": "https://third-party.example/mcp"},
        {"name": "linear", "type": "url", "url": "https://mcp.linear.app/mcp"},
    ]
    healed = heal_reserved_server(_ON, servers, public_url="https://mcp.example.com/mcp")
    assert healed is not None
    assert healed[0]["url"] == "https://mcp.example.com/mcp"
    assert healed[1] == servers[1]
    assert heal_reserved_server(OPEN_TOOL_SAFETY, servers, public_url="https://x/mcp") is None
    assert heal_reserved_server(_ON, healed, public_url="https://mcp.example.com/mcp/") is None


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
    assert toolset_permission_policy(
        _ON, server_name=DAIMON_SERVER_NAME, trusted_servers=frozenset({DAIMON_SERVER_NAME})
    ) == {"type": "always_allow"}
    assert toolset_permission_policy(_ON, server_name=DAIMON_SERVER_NAME) == {"type": "always_ask"}
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
    out = session_tools_for_policy(_ON, _tools(), trusted_servers=frozenset({DAIMON_SERVER_NAME}))
    assert out is not None
    assert out[0] == _tools()[0]
    assert out[1] == _tools()[1] | {
        "configs": [{"name": "add_skill", "permission_policy": {"type": "always_ask"}}]
    }, "daimon's own toolset stays trusted, except the one tool that confirms"
    assert out[2]["default_config"] == {"permission_policy": {"type": "always_ask"}}
    assert out[2]["configs"] == [{"name": "create_issue"}], (
        "a per-tool always_allow would let that tool skip the pause"
    )


def test_session_tools_report_no_change_once_gated() -> None:
    trusted = frozenset({DAIMON_SERVER_NAME})
    once = session_tools_for_policy(_ON, _tools(), trusted_servers=trusted)
    assert once is not None
    assert session_tools_for_policy(_ON, once, trusted_servers=trusted) is None


def test_daimon_add_skill_asks_only_when_it_uploads() -> None:
    trusted = trusted_servers_for("https://mcp.example.com/mcp")
    preview = _call(DAIMON_SERVER_NAME, "add_skill")
    upload = ToolCall(
        tool_use_id="tu_2",
        server_name=DAIMON_SERVER_NAME,
        tool_name="add_skill",
        input={"agent_name": "a", "content_hash": "abc"},
    )
    decide = decide_tool_call
    assert decide(_ON, preview, attended=True, trusted_servers=trusted).outcome == "allow"
    assert decide(_ON, upload, attended=True, trusted_servers=trusted).outcome == "ask"
    unattended = decide(_ON, upload, attended=False, trusted_servers=trusted)
    assert (unattended.outcome, unattended.reason) == ("deny", "unattended_write")
    assert decide(OPEN_TOOL_SAFETY, upload, attended=True, trusted_servers=trusted).reason == (
        "disabled"
    ), "off by default: nothing about add_skill changes until the policy is on"
