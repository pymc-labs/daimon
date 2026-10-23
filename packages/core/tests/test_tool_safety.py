"""The attached-tool read/write model: classification, decisions, toolset policy."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from daimon.core.defaults.mcp_merge import DAIMON_MCP_SERVER_NAME
from daimon.core.tool_safety import (
    CONFIRMED_DAIMON_TOOLS,
    DAIMON_SERVER_NAME,
    OPEN_TOOL_SAFETY,
    PUBLISH_TOOLS,
    ResolvedAgentToolRule,
    ToolAnnotations,
    ToolCall,
    ToolSafetyPolicy,
    classify_tool,
    decide_tool_call,
    has_confirmation_gate,
    heal_reserved_server,
    session_tools_for_policy,
    toolset_permission_policy,
    trusted_servers_for,
    unwrap_daimon_call,
)
from daimon.core.turn.approvals import interactive_decider, unattended_decider

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
        # Question-and-answer and checking tools carded as writes on 2026-10-09.
        ("ask_wiki_question", "read"),
        ("eap_briefing", "read"),
        ("eap_verify_answer", "read"),
        ("verifyAnswer", "read"),
        ("ask_question", "read"),
        ("askQuestion", "read"),
        # GitHub's "<object>_read" tools, whose verb comes last.
        ("pull_request_read", "read"),
        ("pullRequestRead", "read"),
        ("issue_read", "read"),
        # A trailing "read" alone is no rule.
        ("mark_all_notifications_read", "write"),
        ("mark_as_read", "write"),
        ("pull_request_review_write", "write"),
        ("issue_write", "write"),
        ("create_pull_request", "write"),
        ("push_files", "write"),
        ("merge_pull_request", "write"),
        ("pull_request_read_and_merge", "write"),
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
        ("ask_and_post", "write"),
        ("verify_and_send", "write"),
        ("run_verify", "write"),
        ("briefing_publish", "write"),
        # "ask" and "verify" read only in a question-and-answer shape: a bare
        # "ask" is an action (agent_chat's `ask` starts a turn in another agent).
        ("ask", "write"),
        ("ask_agent", "write"),
        ("other_ask", "write"),
        ("ask_question_and_post", "write"),
        ("verify", "write"),
        ("verify_email", "write"),
        ("verifyPhone", "write"),
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
    assert (
        classify_tool(
            _ON,
            server_name="s",
            tool_name="ask_wiki_question",
            annotations=ToolAnnotations(read_only_hint=False),
        )
        == "write"
    ), "a server that says its tool is not read-only is believed over a read-sounding name"


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
        "configs": [
            {"name": "add_skill", "enabled": True, "permission_policy": {"type": "always_ask"}}
        ]
    }, "daimon's own toolset stays trusted, except the one tool that confirms"
    assert out[2]["default_config"] == {"permission_policy": {"type": "always_ask"}}
    assert out[2]["configs"] == [
        {"name": "create_issue", "permission_policy": {"type": "always_ask"}}
    ], "a per-tool always_allow would let that tool skip the pause"


def test_session_tools_report_no_change_once_gated() -> None:
    trusted = frozenset({DAIMON_SERVER_NAME})
    once = session_tools_for_policy(_ON, _tools(), trusted_servers=trusted)
    assert once is not None
    assert session_tools_for_policy(_ON, once, trusted_servers=trusted) is None


def test_new_confirmed_tool_is_gated_without_an_explicit_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.core import tool_safety

    tool_name = "install_workspace_asset"
    monkeypatch.setattr(
        tool_safety,
        "CONFIRMED_DAIMON_TOOLS",
        {**tool_safety.CONFIRMED_DAIMON_TOOLS, tool_name: "review_digest"},
    )
    call = ToolCall(
        tool_use_id="new-confirmed-tool",
        server_name=DAIMON_SERVER_NAME,
        tool_name=tool_name,
        input={"review_digest": "a" * 64},
    )
    policy = ToolSafetyPolicy(enabled=True, unattended_writes=("*",))
    trusted = frozenset({DAIMON_SERVER_NAME})
    assert (
        decide_tool_call(
            policy, call, attended=True, trusted_servers=trusted, confirmed_tools=frozenset()
        ).outcome
        == "ask"
    )
    assert (
        decide_tool_call(
            policy, call, attended=False, trusted_servers=trusted, confirmed_tools=frozenset()
        ).outcome
        == "deny"
    )
    gated = session_tools_for_policy(
        policy, _tools(), trusted_servers=trusted, confirmed_tools=frozenset()
    )
    assert gated is not None and has_confirmation_gate(gated, tool_name=tool_name)
    preview = call.model_copy(update={"input": {}})
    assert (
        decide_tool_call(policy, preview, attended=True, trusted_servers=trusted).outcome == "allow"
    )
    assert (
        decide_tool_call(OPEN_TOOL_SAFETY, call, attended=True, trusted_servers=trusted).outcome
        == "allow"
    )
    assert session_tools_for_policy(OPEN_TOOL_SAFETY, _tools(), trusted_servers=trusted) is None


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
    for allowed in (("*",), ("daimon-mcp",), ("daimon-mcp/add_skill",)):
        open_policy = _ON.model_copy(update={"unattended_writes": allowed})
        for servers in (trusted, frozenset[str]()):
            verdict = decide(open_policy, upload, attended=False, trusted_servers=servers)
            assert verdict.outcome == "deny", f"{allowed}: nobody watching never confirms"
    assert decide(OPEN_TOOL_SAFETY, upload, attended=True, trusted_servers=trusted).reason == (
        "disabled"
    ), "off by default: nothing about add_skill changes until the policy is on"


def test_only_a_session_created_gated_holds_add_skill_for_a_person() -> None:
    """The server reads the session's own tools: the gate as created, never the model's word."""
    trusted = frozenset({DAIMON_SERVER_NAME})
    gated = session_tools_for_policy(_ON, _tools(), trusted_servers=trusted)
    assert gated is not None
    assert has_confirmation_gate(gated, tool_name="add_skill"), "the per-tool ask"
    untrusted = session_tools_for_policy(_ON, _tools())
    assert untrusted is not None
    assert has_confirmation_gate(untrusted, tool_name="add_skill"), "the whole toolset asks"
    assert not has_confirmation_gate(_tools(), tool_name="add_skill"), (
        "a session created before the gate, or without tool safety, never asks"
    )
    assert not has_confirmation_gate(_tools()[2:], tool_name="add_skill"), "no daimon toolset"


def test_a_per_tool_allow_under_an_asking_toolset_is_gated() -> None:
    """A toolset already stored `always_ask` with one tool on `always_allow` still
    changes: left raw, that tool would skip the card."""
    ask, allow = {"type": "always_ask"}, {"type": "always_allow"}
    stored = [
        {
            "type": "mcp_toolset",
            "mcp_server_name": "linear",
            "default_config": {"permission_policy": ask},
            "configs": [{"name": "create_issue", "permission_policy": allow}],
        }
    ]
    out = session_tools_for_policy(_ON, stored)
    assert out is not None, "the per-tool allow is a change"
    assert out[0]["configs"] == [{"name": "create_issue", "permission_policy": ask}]


def test_a_disabled_daimon_toolset_keeps_add_skill_disabled() -> None:
    """The confirm config added to daimon's trusted toolset takes the toolset's own
    `enabled`, so a server the agent turned off stays off for add_skill too."""
    stored = [
        {
            "type": "mcp_toolset",
            "mcp_server_name": DAIMON_SERVER_NAME,
            "default_config": {"enabled": False, "permission_policy": {"type": "always_allow"}},
        }
    ]
    out = session_tools_for_policy(_ON, stored, trusted_servers=frozenset({DAIMON_SERVER_NAME}))
    assert out is not None
    assert out[0]["configs"] == [
        {"name": "add_skill", "enabled": False, "permission_policy": {"type": "always_ask"}}
    ], "add_skill is not switched on"


def test_publish_tools_ask_only_in_a_session_that_asks_before_publishing() -> None:
    """With tool safety off or on, a session whose agent may not publish freely holds
    daimon's publish tools for the requester's Approve; other sessions keep them open."""
    trusted = frozenset({DAIMON_SERVER_NAME})
    assert session_tools_for_policy(OPEN_TOOL_SAFETY, _tools(), trusted_servers=trusted) is None
    asking = session_tools_for_policy(
        OPEN_TOOL_SAFETY, _tools(), trusted_servers=trusted, asks_before_publishing=True
    )
    assert asking is not None
    assert [c["name"] for c in asking[1]["configs"]] == sorted(PUBLISH_TOOLS)
    assert all(has_confirmation_gate(asking, tool_name=name) for name in PUBLISH_TOOLS)
    assert not has_confirmation_gate(asking, tool_name="add_skill"), "tool safety is still off"
    assert asking[2] == _tools()[2], "and third-party toolsets are untouched"
    gated = session_tools_for_policy(_ON, _tools(), trusted_servers=trusted)
    assert gated is not None and not has_confirmation_gate(gated, tool_name="publish_report")


def test_a_publish_call_asks_in_chat_and_is_denied_unattended_with_safety_off_or_on() -> None:
    trusted = trusted_servers_for("https://mcp.example.com/mcp")
    call = _call(DAIMON_SERVER_NAME, "publish_report")
    for policy in (OPEN_TOOL_SAFETY, _ON):
        asked = decide_tool_call(policy, call, attended=True, trusted_servers=trusted)
        assert (asked.outcome, asked.reason) == ("ask", "publish_needs_confirmation")
        denied = decide_tool_call(policy, call, attended=False, trusted_servers=trusted)
        assert (denied.outcome, denied.reason) == ("deny", "unattended_publish")


def test_the_approval_card_timeout_defaults_to_ten_minutes_and_is_bounded() -> None:
    import pytest
    from pydantic import ValidationError

    assert ToolSafetyPolicy().confirmation_timeout_s == 600
    assert ToolSafetyPolicy(confirmation_timeout_s=60).confirmation_timeout_s == 60
    assert ToolSafetyPolicy(confirmation_timeout_s=86_400).confirmation_timeout_s == 86_400
    for bad in (0, 29, 86_401):
        with pytest.raises(ValidationError):
            ToolSafetyPolicy(confirmation_timeout_s=bad)


_READ_TOOLS = [
    ("deepwiki", "ask_wiki_question"),
    ("eap", "eap_briefing"),
    ("eap", "eap_verify_answer"),
]


@pytest.mark.parametrize(("server", "tool"), _READ_TOOLS)
@pytest.mark.parametrize("attended", [True, False])
async def test_question_and_answer_tools_run_without_a_card(
    server: str, tool: str, attended: bool
) -> None:
    card = AsyncMock(return_value="denied")
    decider = (
        interactive_decider(_ON, requester_platform_user_id="u1", confirm=card)
        if attended
        else unattended_decider(_ON)
    )
    result = await decider(ToolCall(tool_use_id="tu_1", server_name=server, tool_name=tool))
    assert result.allow
    card.assert_not_awaited()


@pytest.mark.parametrize("attended", [True, False])
async def test_github_pull_request_read_runs_without_a_card(attended: bool) -> None:
    card = AsyncMock(return_value="denied")
    decider = (
        interactive_decider(_ON, requester_platform_user_id="u1", confirm=card)
        if attended
        else unattended_decider(_ON)
    )
    call = ToolCall(
        tool_use_id="tu_1",
        server_name="github",
        tool_name="pull_request_read",
        input={"method": "get_diff", "owner": "o", "repo": "r", "pullNumber": 1},
    )
    verdict = decide_tool_call(_ON, call, attended=attended)
    assert (verdict.outcome, verdict.reason) == ("allow", "read")
    assert (await decider(call)).allow
    card.assert_not_awaited()


@pytest.mark.parametrize(
    "tool",
    [
        "pull_request_review_write",
        "create_pull_request",
        "push_files",
        "merge_pull_request",
        "mark_all_notifications_read",
    ],
)
def test_github_write_tools_still_ask_in_chat_and_are_denied_unattended(tool: str) -> None:
    call = _call("github", tool)
    assert decide_tool_call(_ON, call, attended=True).outcome == "ask"
    assert decide_tool_call(_ON, call, attended=False).outcome == "deny"


@pytest.mark.parametrize("attended", [True, False])
async def test_another_daimons_agent_ask_still_needs_approval(attended: bool) -> None:
    """agent_chat's `ask` starts or continues a turn in another agent: an action.
    Attached from another Daimon endpoint, it must wait for the card in chat
    and be refused in a routine."""
    card = AsyncMock(return_value="denied")
    decider = (
        interactive_decider(_ON, requester_platform_user_id="u1", confirm=card)
        if attended
        else unattended_decider(_ON)
    )
    call = ToolCall(
        tool_use_id="tu_1",
        server_name="other-daimon",
        tool_name="ask",
        input={"message": "do the next task"},
    )
    assert decide_tool_call(_ON, call, attended=attended).effect == "write"
    result = await decider(call)
    assert not result.allow
    if attended:
        card.assert_awaited_once()


# --- agent tool rules on daimon's own tools -------------------------------------

_RULES_ON = ToolSafetyPolicy(enabled=True, agent_rules=True)
_TRUSTED = frozenset({DAIMON_SERVER_NAME})


def _proxy(inner: str, **arguments: object) -> ToolCall:
    """What MA reports for `call_tool(name=inner, arguments=...)` on daimon's server."""
    return ToolCall(
        tool_use_id="tu_p",
        server_name=DAIMON_SERVER_NAME,
        tool_name="call_tool",
        input={"name": inner, "arguments": arguments},
    )


def _rule(rule: str, scope: str = "chat") -> ResolvedAgentToolRule:
    return ResolvedAgentToolRule.model_validate({"rule": rule, "scope": scope})


def test_unwrapping_a_proxy_call_names_the_inner_tool_and_its_arguments() -> None:
    inner = unwrap_daimon_call(_proxy("hubspot_update_objects", title="Q3"), _TRUSTED)
    assert inner.key == "daimon-mcp/hubspot_update_objects"
    assert inner.input == {"title": "Q3"}
    assert inner.tool_use_id == "tu_p"


@pytest.mark.parametrize(
    "call",
    [
        _call("linear", "call_tool"),
        _call(DAIMON_SERVER_NAME, "get_session"),
        ToolCall(
            tool_use_id="x",
            server_name=DAIMON_SERVER_NAME,
            tool_name="call_tool",
            input={"name": 7},
        ),
        ToolCall(tool_use_id="x", server_name=DAIMON_SERVER_NAME, tool_name="call_tool"),
        _proxy("a/b"),
        _proxy("call_tool"),
        _proxy("search_tools"),
    ],
)
def test_a_call_that_is_not_a_plain_proxy_call_is_left_as_it_is(call: ToolCall) -> None:
    assert unwrap_daimon_call(call, _TRUSTED) == call


def test_the_proxy_is_not_unwrapped_where_the_server_is_not_trusted() -> None:
    call = _proxy("get_session")
    assert unwrap_daimon_call(call, frozenset()) == call


@pytest.mark.parametrize(
    ("rule", "attended", "outcome", "reason"),
    [
        ("deny", True, "deny", "agent_rule_deny"),
        ("deny", False, "deny", "agent_rule_deny"),
        ("ask", True, "ask", "agent_rule_ask"),
        ("ask", False, "deny", "agent_rule_ask"),
        ("allow", True, "allow", "agent_rule_allow"),
        ("allow", False, "allow", "agent_rule_allow"),
        (None, True, "allow", "not_attached"),
        (None, False, "allow", "not_attached"),
    ],
)
def test_a_daimon_tool_follows_its_agent_rule(
    rule: str | None, attended: bool, outcome: str, reason: str
) -> None:
    call = unwrap_daimon_call(_proxy("hubspot_update_objects"), _TRUSTED)
    verdict = decide_tool_call(
        _RULES_ON,
        call,
        attended=attended,
        trusted_servers=_TRUSTED,
        agent_rule=_rule(rule) if rule else None,
    )
    assert (verdict.outcome, verdict.reason) == (outcome, reason)


def test_daimon_tool_rules_need_both_the_policy_and_the_rules_switch() -> None:
    call = _call(DAIMON_SERVER_NAME, "hubspot_update_objects")
    for policy in (OPEN_TOOL_SAFETY, _ON):
        verdict = decide_tool_call(
            policy, call, attended=True, trusted_servers=_TRUSTED, agent_rule=_rule("deny")
        )
        assert verdict.outcome == "allow", "a stored rule is inert until both switches are on"


def test_without_trusted_servers_the_reserved_name_is_gated_like_any_third_party() -> None:
    verdict = decide_tool_call(
        _RULES_ON,
        _call(DAIMON_SERVER_NAME, "delete_records"),
        attended=True,
        agent_rule=_rule("allow"),
    )
    assert (verdict.outcome, verdict.reason) == ("allow", "agent_rule_allow")


def test_an_agent_deny_beats_the_fixed_daimon_asks() -> None:
    publish = _call(DAIMON_SERVER_NAME, "publish_report")
    upload = ToolCall(
        tool_use_id="tu_2",
        server_name=DAIMON_SERVER_NAME,
        tool_name="add_skill",
        input={"agent_name": "a", "content_hash": "abc"},
    )
    for call in (publish, upload):
        for attended in (True, False):
            verdict = decide_tool_call(
                _RULES_ON,
                call,
                attended=attended,
                trusted_servers=_TRUSTED,
                agent_rule=_rule("deny"),
            )
            assert (verdict.outcome, verdict.reason) == ("deny", "agent_rule_deny")


def test_an_agent_allow_or_ask_never_loosens_a_fixed_daimon_gate() -> None:
    publish = _call(DAIMON_SERVER_NAME, "publish_report")
    upload = ToolCall(
        tool_use_id="tu_2",
        server_name=DAIMON_SERVER_NAME,
        tool_name="add_skill",
        input={"agent_name": "a", "content_hash": "abc"},
    )
    for rule in ("allow", "ask"):
        asked = decide_tool_call(
            _RULES_ON, publish, attended=True, trusted_servers=_TRUSTED, agent_rule=_rule(rule)
        )
        assert (asked.outcome, asked.reason) == ("ask", "publish_needs_confirmation")
        refused = decide_tool_call(
            _RULES_ON, publish, attended=False, trusted_servers=_TRUSTED, agent_rule=_rule(rule)
        )
        assert (refused.outcome, refused.reason) == ("deny", "unattended_publish")
        write = decide_tool_call(
            _RULES_ON, upload, attended=True, trusted_servers=_TRUSTED, agent_rule=_rule(rule)
        )
        assert write.outcome == "ask"
        unattended = decide_tool_call(
            _RULES_ON,
            upload,
            attended=False,
            trusted_servers=_TRUSTED,
            agent_rule=_rule(rule, "chat_and_routines"),
        )
        assert (unattended.outcome, unattended.reason) == ("deny", "unattended_write")
    assert set(CONFIRMED_DAIMON_TOOLS) >= {"add_skill"}


def test_the_operators_deny_of_a_confirmed_daimon_write_still_comes_first() -> None:
    policy = ToolSafetyPolicy(enabled=True, agent_rules=True, denied=("daimon-mcp/add_skill",))
    upload = ToolCall(
        tool_use_id="tu_2",
        server_name=DAIMON_SERVER_NAME,
        tool_name="add_skill",
        input={"agent_name": "a", "content_hash": "abc"},
    )
    for rule in ("deny", "ask", "allow"):
        verdict = decide_tool_call(
            policy, upload, attended=True, trusted_servers=_TRUSTED, agent_rule=_rule(rule)
        )
        assert (verdict.outcome, verdict.reason) == ("deny", "denied_by_operator")


def test_session_tools_for_an_agent_without_daimon_rules_are_exactly_as_before() -> None:
    before = session_tools_for_policy(_RULES_ON, _tools(), trusted_servers=_TRUSTED)
    assert before is not None
    assert (
        session_tools_for_policy(
            _RULES_ON, _tools(), trusted_servers=_TRUSTED, daimon_rule_tools=frozenset()
        )
        == before
    )
    assert session_tools_for_policy(OPEN_TOOL_SAFETY, _tools(), trusted_servers=_TRUSTED) is None


def test_a_daimon_rule_makes_call_tool_and_the_named_tool_ask() -> None:
    out = session_tools_for_policy(
        _RULES_ON,
        _tools(),
        trusted_servers=_TRUSTED,
        daimon_rule_tools=frozenset({"hubspot_update_objects", "search_tools"}),
    )
    assert out is not None
    daimon = out[1]
    assert daimon["default_config"] == {"permission_policy": {"type": "always_allow"}}
    asked = {c["name"] for c in daimon["configs"] if c["permission_policy"]["type"] == "always_ask"}
    assert asked == {"add_skill", "call_tool", "hubspot_update_objects"}
    assert has_confirmation_gate(out, tool_name="call_tool")
    assert not has_confirmation_gate(out, tool_name="search_tools"), "search never asks"
    plain = session_tools_for_policy(_ON, _tools(), trusted_servers=_TRUSTED)
    assert plain is not None and out[2] == plain[2], "third-party toolsets are unchanged"


def test_a_rule_naming_a_confirmed_tool_does_not_duplicate_its_config() -> None:
    out = session_tools_for_policy(
        _RULES_ON,
        _tools(),
        trusted_servers=_TRUSTED,
        daimon_rule_tools=frozenset({"add_skill", "publish_report"}),
    )
    assert out is not None
    names = [c["name"] for c in out[1]["configs"]]
    assert sorted(names) == ["add_skill", "call_tool", "publish_report"]


def test_daimon_rules_are_inert_for_session_tools_until_both_switches_are_on() -> None:
    rules = frozenset({"hubspot_update_objects"})
    assert (
        session_tools_for_policy(
            OPEN_TOOL_SAFETY, _tools(), trusted_servers=_TRUSTED, daimon_rule_tools=rules
        )
        is None
    )
    plain = session_tools_for_policy(_ON, _tools(), trusted_servers=_TRUSTED)
    assert (
        session_tools_for_policy(_ON, _tools(), trusted_servers=_TRUSTED, daimon_rule_tools=rules)
        == plain
    )


def test_a_daimon_rule_config_takes_the_toolsets_enabled_and_is_stable() -> None:
    stored = [
        {
            "type": "mcp_toolset",
            "mcp_server_name": DAIMON_SERVER_NAME,
            "default_config": {"enabled": False, "permission_policy": {"type": "always_allow"}},
        }
    ]
    rules = frozenset({"get_session"})
    out = session_tools_for_policy(
        _RULES_ON, stored, trusted_servers=_TRUSTED, daimon_rule_tools=rules
    )
    assert out is not None
    assert all(c["enabled"] is False for c in out[0]["configs"])
    assert (
        session_tools_for_policy(_RULES_ON, out, trusted_servers=_TRUSTED, daimon_rule_tools=rules)
        is None
    ), "complete configs hash the same on the next bind"


def test_the_daimon_wildcard_gates_the_proxy_but_never_gets_a_config_of_its_own() -> None:
    only = session_tools_for_policy(
        _RULES_ON, _tools(), trusted_servers=_TRUSTED, daimon_rule_tools=frozenset({"*"})
    )
    assert only is not None
    assert sorted(c["name"] for c in only[1]["configs"]) == ["add_skill", "call_tool"]
    both = session_tools_for_policy(
        _RULES_ON,
        _tools(),
        trusted_servers=_TRUSTED,
        daimon_rule_tools=frozenset({"*", "get_session"}),
    )
    assert both is not None
    assert sorted(c["name"] for c in both[1]["configs"]) == [
        "add_skill",
        "call_tool",
        "get_session",
    ]
