"""Standing unattended readers always require a human confirmation card."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from daimon.core.confirmation import confirmed_tools_for_turn, prompt_for_tool_call
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall, ToolSafetyPolicy, decide_tool_call
from daimon.core.turn.deps import TurnDeps


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("create_watch", {"source": "poll", "arguments": {"thread_id": "4343"}}),
        ("resume_watch", {"watch_id": "example"}),
        ("rotate_watch_key", {"watch_id": "example"}),
    ],
)
def test_watch_enrollment_requires_owner_card_even_without_tool_safety(enabled, tool, arguments):
    policy = ToolSafetyPolicy(enabled=enabled, unattended_writes=("*",))
    selected = confirmed_tools_for_turn(
        cast(
            TurnDeps,
            SimpleNamespace(
                backend_selection_enabled=False, microsoft_send=False, tool_safety=policy
            ),
        )
    )
    call = ToolCall(
        tool_use_id="watch", server_name=DAIMON_SERVER_NAME, tool_name=tool, input=arguments
    )
    assert tool in selected
    for attended, expected in ((True, "ask"), (False, "deny")):
        assert (
            decide_tool_call(
                policy,
                call,
                attended=attended,
                trusted_servers=frozenset({DAIMON_SERVER_NAME}),
                confirmed_tools=selected,
            ).outcome
            == expected
        )
    prompt = prompt_for_tool_call(call, requester_platform_user_id="owner", now=datetime.now(UTC))
    assert prompt.requester_platform_user_id == "owner"


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("create_watch", {"source": "poll"}),
        ("resume_watch", {"watch_id": "example"}),
        ("rotate_watch_key", {"watch_id": "example"}),
    ],
)
def test_watch_confirmation_table_protects_policy_enabled_callers(tool, arguments):
    call = ToolCall(
        tool_use_id="watch", server_name=DAIMON_SERVER_NAME, tool_name=tool, input=arguments
    )
    assert (
        decide_tool_call(
            ToolSafetyPolicy(enabled=True),
            call,
            attended=True,
            trusted_servers=frozenset({DAIMON_SERVER_NAME}),
        ).outcome
        == "ask"
    )


@pytest.mark.parametrize(
    "delivery,shown",
    [
        (None, "this thread"),
        ("thread", "this thread"),
        ("dm", "your DM"),
        ("new_thread", "a new public thread each time"),
    ],
)
def test_create_watch_card_always_says_where_reports_go(delivery, shown):
    # Many fields before `delivery` must not push it off the card.
    arguments: dict[str, object] = {
        "instructions": "Tell me about new rows.",
        "source": "poll",
        "projection": ".",
        "rule": "change",
        "interval": 300,
        "lifetime_days": 7,
    }
    if delivery is not None:
        arguments["delivery"] = delivery
    call = ToolCall(
        tool_use_id="watch",
        server_name=DAIMON_SERVER_NAME,
        tool_name="create_watch",
        input=arguments,
    )
    prompt = prompt_for_tool_call(call, requester_platform_user_id="owner", now=datetime.now(UTC))
    assert prompt.detail_lines[0] == f"Reports to: {shown}"
    assert sum(line.startswith("Reports to:") for line in prompt.detail_lines) == 1
    assert not any(line.startswith("Delivery:") for line in prompt.detail_lines)
    assert prompt.title == "Create this watch?"
