"""Approved stop and refusal words, including spacing and short references."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import get_args

import pytest
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.turn import TerminationReason, render_termination_notice
from daimon.core.turn.errors import AdmissionDenialReason, AdmissionDenied
from daimon.core.turn.notices import (
    RefusalNouns,
    admission_refusal_text,
    fit_notice,
    resolver_miss_text,
    short_ref,
)
from daimon.core.turn.state import McpServerFailure, ToolUseBlock, TurnState


def _tool(name: str, status: str) -> ToolUseBlock:
    return ToolUseBlock(
        kind="tool_use",
        id=f"tu_{name}",
        type="agent.tool_use",
        name=name,
        input={},
        status=status,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("reason", "title", "action"),
    [
        (
            TerminationReason.INTERRUPTED,
            "Stopped. Changes already made stay made.",
            "@mention Daimon to carry on.",
        ),
        (
            TerminationReason.INTERRUPT_TIMEOUT,
            "Stopping, but not confirmed. It may still be finishing.",
            "Wait a minute before you @mention Daimon again.",
        ),
        (
            TerminationReason.CONNECTION_LOST,
            "Lost the connection. Daimon may still be working.",
            "@mention Daimon to ask what it finished.",
        ),
        (
            TerminationReason.UPSTREAM,
            "Daimon's AI service failed on this reply.",
            "@mention Daimon to try again.",
        ),
        (
            TerminationReason.REFUSED,
            "Daimon's AI declined this request.",
            "Rephrase it and @mention Daimon again.",
        ),
        (TerminationReason.RATE_LIMITED, "Daimon's AI service is busy.", "Try again in a minute."),
        (
            TerminationReason.SESSION_TERMINATED,
            "Daimon stopped mid-reply. Its working files may be gone.",
            "@mention Daimon to carry on.",
        ),
        (
            TerminationReason.MCP_DEGRADED_EMPTY,
            "The connection to a tool failed, so there's no reply.",
            "Ask to reconnect it, or ask again without it.",
        ),
        (
            TerminationReason.RETRYING_UNSETTLED,
            "The AI kept failing, so there's no reply.",
            "Wait a moment, then @mention Daimon again.",
        ),
        (
            TerminationReason.REQUIRES_ACTION,
            "Daimon couldn't get the approval it needed.",
            "@mention Daimon to try again.",
        ),
        (
            TerminationReason.CEILING,
            "This took too long, so Daimon stopped waiting.",
            "@mention Daimon to try again.",
        ),
        (
            TerminationReason.RECOVERY_CANCELLED,
            "Stopped. Daimon lost its working files.",
            "@mention Daimon to carry on.",
        ),
        (
            TerminationReason.RECOVERY_FAILED,
            "Daimon lost its working files and couldn't continue.",
            "@mention Daimon to try again.",
        ),
        (
            TerminationReason.ADMISSION_CONCURRENCY_SHED,
            "Daimon is too busy right now.",
            "@mention Daimon again in a moment.",
        ),
        (
            TerminationReason.ADMISSION_BALANCE_DEPLETED,
            "Your team's Daimon credit has run out.",
            "An admin can top up with `/billing`.",
        ),
        (
            TerminationReason.ADMISSION_CAP_EXCEEDED,
            "You've used your monthly limit.",
            "Ask the team running Daimon to raise it.",
        ),
        (
            TerminationReason.ADMISSION_CHANNEL_BUDGET_EXCEEDED,
            "This channel's budget is used up.",
            "An admin can raise it.",
        ),
        (
            TerminationReason.ADMISSION_CHANNEL_PROTECTED,
            "Daimon can't reply in this channel.",
            "Ask somewhere else, or ask an admin.",
        ),
        (
            TerminationReason.ADMISSION_AGENT_PINNED_ELSEWHERE,
            "team-020-analyst only works in other channels.",
            "Ask it there.",
        ),
        (
            TerminationReason.ADMISSION_CHANNEL_ISOLATED,
            "This channel only uses its own agents.",
            "Ask an admin to pick one.",
        ),
        (
            TerminationReason.ADMISSION_DENIED,
            "Daimon couldn't accept this request.",
            "Ask an admin.",
        ),
        (
            TerminationReason.MISSING_CONFIG,
            "Daimon isn't set up in this channel yet.",
            "Ask an admin to run `/agent-setup`.",
        ),
        (
            TerminationReason.RESOLVER_MISS,
            "This channel's setup is out of date.",
            "Ask an admin to check `/agent-setup`.",
        ),
        (
            TerminationReason.SESSION_PREPARATION_FAILED,
            "A settings change hasn't applied yet.",
            "@mention Daimon again in a little while.",
        ),
        (
            TerminationReason.SESSION_BUSY,
            "Daimon is still working in this conversation.",
            "Wait for it to finish, then @mention Daimon.",
        ),
        (
            TerminationReason.SESSION_AGENT_MISMATCH,
            "team-020-analyst can't continue this conversation.",
            "Start a new conversation to talk to it.",
        ),
        (TerminationReason.UNKNOWN, "Something went wrong.", "@mention Daimon to try again."),
        (TerminationReason.REDUCER_BUG, "Something went wrong.", "@mention Daimon to try again."),
    ],
)
def test_approved_termination_rows(reason: TerminationReason, title: str, action: str) -> None:
    notice = render_termination_notice(
        reason, agent_name="team-020-analyst", request_id="01a7c3qx2"
    )
    assert notice is not None
    assert (notice.title, notice.next_step) == (title, action)
    assert notice.plain_text() == f"{title}\n\n{action}\n\nRef 7C3QX2"
    assert "The conversation and its workspace are kept." not in notice.plain_text()


def test_completed_has_no_notice() -> None:
    assert render_termination_notice(TerminationReason.COMPLETED) is None


@pytest.mark.parametrize(
    ("reason", "action"),
    [
        (TerminationReason.INTERRUPTED, "Send a message to carry on."),
        (TerminationReason.UPSTREAM, "Send your message again."),
        (TerminationReason.REFUSED, "Rephrase it and send it again."),
        (TerminationReason.CONNECTION_LOST, "Send a message to ask what it finished."),
        (TerminationReason.SESSION_BUSY, "Wait for it to finish, then send a message."),
    ],
)
def test_dm_actions(reason: TerminationReason, action: str) -> None:
    notice = render_termination_notice(reason, in_dm=True)
    assert notice is not None and notice.next_step == action


def test_work_and_ref_are_small_separate_lines() -> None:
    state = TurnState(
        content=[
            _tool("bash", "complete"),
            _tool("fetch", "failed"),
            _tool("fit_model", "pending"),
        ]
    )
    notice = render_termination_notice(
        TerminationReason.CONNECTION_LOST, state=state, request_id="a7c3qx2"
    )
    assert notice is not None
    assert notice.work_line(lambda name: f"`{name}`") == (
        "Still running when it ended: `fit_model`. Finished before that: 2 tool calls."
    )
    assert notice.plain_text().endswith(
        "Still running when it ended: fit_model. Finished before that: 2 tool calls.\n\nRef 7C3QX2"
    )


def test_server_name_and_rate_horizon() -> None:
    state = TurnState(
        mcp_failures=(
            McpServerFailure(
                server_name="Notion",
                error_type="mcp_connection_failed_error",
                message="down",
                retry_status="exhausted",
            ),
        )
    )
    notice = render_termination_notice(TerminationReason.MCP_DEGRADED_EMPTY, state=state)
    assert notice is not None
    assert notice.title == "The connection to Notion failed, so there's no reply."
    until = datetime(2026, 9, 28, 15, 42, tzinfo=UTC)
    rate = render_termination_notice(
        TerminationReason.RATE_LIMITED, state=TurnState(rate_limit_until=until)
    )
    assert rate is not None and rate.next_step == "Try again after 15:42 UTC."


def test_short_ref_and_fit_notice() -> None:
    assert short_ref("01a7c3qx2") == "7C3QX2"
    fitted = fit_notice(["x" * 5000, "y" * 5000], tail="Ref 7C3QX2", limit=3000)
    assert len(fitted) == 3000
    assert fitted.endswith("…\n\nRef 7C3QX2")
    assert fit_notice(["a", "b"], tail="Ref X", limit=100) == "a\n\nb\n\nRef X"


_NOUNS = RefusalNouns(scope="server", admin="a server admin", billing="`/billing`")


@pytest.mark.parametrize("reason", get_args(AdmissionDenialReason))
@pytest.mark.parametrize("in_dm", [False, True])
def test_refusals_have_two_spaced_lines(reason: AdmissionDenialReason, in_dm: bool) -> None:
    rendered = admission_refusal_text(reason, _NOUNS, bot_name="team-020-analyst", in_dm=in_dm)
    lines = rendered.split("\n\n")
    assert len(lines) == 2
    assert all(line.endswith(".") for line in lines)
    assert "{" not in rendered


def test_depleted_refusal_names_the_billing_command() -> None:
    teams = RefusalNouns(
        scope="organisation",
        admin="an admin",
        billing="`billing` in a 1:1 chat with me",
        setup="`setup`",
    )
    assert admission_refusal_text("balance_depleted", _NOUNS) == (
        "Your team's Daimon credit has run out.\n\nAn admin can top up with `/billing`."
    )
    assert admission_refusal_text("balance_depleted", teams).endswith(
        "An admin can top up with `billing` in a 1:1 chat with me."
    )
    notice = render_termination_notice(
        TerminationReason.ADMISSION_BALANCE_DEPLETED,
        billing_command="`billing` in a 1:1 chat with me",
    )
    assert notice is not None
    assert notice.next_step == "An admin can top up with `billing` in a 1:1 chat with me."


def test_nouns_for_a_platform_come_from_the_words_table() -> None:
    teams = RefusalNouns.for_platform("teams")
    assert (teams.scope, teams.admin) == ("organisation", "an admin")
    assert teams.billing == "`billing` in a 1:1 chat with me"
    assert teams.setup == "`setup` in a 1:1 chat with me"
    assert RefusalNouns.for_platform("teams", place="dm").setup == "`setup`"
    assert admission_refusal_text("balance_depleted", RefusalNouns.for_platform("discord")) == (
        "Your team's Daimon credit has run out.\n\nAn admin can top up with `/billing`."
    )


def test_nouns_for_a_caller_without_a_chat_platform_name_no_command() -> None:
    nouns = RefusalNouns.for_caller(None)
    assert nouns.admin == "an admin"
    assert "/" not in nouns.billing + nouns.setup


def test_dm_refusal_keeps_the_pinned_agent_separate_from_the_bot_name() -> None:
    channel = admission_refusal_text(
        "runs_elsewhere",
        _NOUNS,
        bot_name="daimon-staging",
        agent_name="team-020-analyst",
    )
    dm = admission_refusal_text(
        "runs_elsewhere",
        _NOUNS,
        bot_name="daimon-staging",
        agent_name="team-020-analyst",
        in_dm=True,
    )
    assert channel == "team-020-analyst only works in other channels.\n\nAsk it there."
    assert dm == "team-020-analyst only works in other channels.\n\nAsk it there."


@pytest.mark.parametrize(
    ("cause", "provider", "title", "fix"),
    [
        (
            "channel_pin",
            "openai",
            "This channel is pinned to OpenAI, which can't run here.",
            'Ask an admin to say "clear this channel\'s pin", so churn-explorer answers on its own'
            " provider.",
        ),
        (
            "provider_not_allowed",
            "gemini",
            "Gemini isn't turned on for this server.",
            'Ask an admin to turn it on, or to say "switch churn-explorer to Anthropic".',
        ),
        (
            "agent_backend",
            "gemini",
            "churn-explorer runs on Gemini, which isn't set up here yet.",
            'Ask an admin to say "switch churn-explorer to Anthropic", or to finish setting up'
            " Gemini.",
        ),
    ],
)
def test_a_provider_refusal_names_its_real_cause_and_the_chat_fix(cause, provider, title, fix):
    rendered = admission_refusal_text(
        "backend_unsupported", _NOUNS, agent_name="churn-explorer", cause=cause, provider=provider
    )
    assert rendered == f"{title}\n\n{fix}"


def test_an_unattributed_provider_refusal_never_blames_the_channel():
    rendered = admission_refusal_text("backend_unsupported", _NOUNS)
    assert "channel" not in rendered
    assert rendered == (
        "This agent's model provider isn't available here right now.\n\n"
        "Ask an admin to check which provider this agent uses."
    )


def test_an_unknown_provider_is_named_generically():
    rendered = admission_refusal_text(
        "backend_unsupported", _NOUNS, cause="agent_backend", provider=None
    )
    assert rendered.startswith("This agent runs on a model provider,")


def test_a_gemini_hold_says_it_is_temporary_and_names_no_one_to_ask() -> None:
    text = admission_refusal_text("provider_hold_cap_reached", _NOUNS)
    assert "temporary" in text and "few minutes" in text
    assert "admin" not in text and "release" not in text


@pytest.mark.parametrize(
    "reason",
    ["backend_unsupported", "provider_hold_cap_reached", "invoker_not_allowed", "client_requester"],
)
def test_a_denial_with_no_reason_of_its_own_keeps_its_specific_copy_mid_turn(
    reason: AdmissionDenialReason,
) -> None:
    """A denial raised after the turn started reads as at admission, never the generic one."""
    notice = render_termination_notice(
        TerminationReason.ADMISSION_DENIED,
        error=AdmissionDenied(reason=reason),
        agent_name="team-020-analyst",
        request_id="01a7c3qx2",
    )
    assert notice is not None
    expected = admission_refusal_text(
        reason, RefusalNouns.for_caller(None), agent_name="team-020-analyst"
    )
    assert f"{notice.title}\n\n{notice.next_step}" == expected
    assert "couldn't accept this request" not in notice.plain_text()
    assert notice.ref_line == "Ref 7C3QX2"


def test_a_pinned_agent_names_its_channels_where_the_platform_links_them() -> None:
    discord = admission_refusal_text(
        "runs_elsewhere",
        RefusalNouns.for_platform("discord"),
        agent_name="analyst",
        channels=("111", "222"),
    )
    assert discord == "analyst only works in <#111>, <#222>.\n\nAsk it there."
    many = admission_refusal_text(
        "runs_elsewhere",
        RefusalNouns.for_platform("slack"),
        agent_name="analyst",
        channels=("C1", "C2", "C3", "C4"),
    )
    assert many.startswith("analyst only works in <#C1>, <#C2>, <#C3> and 1 more.")
    teams = admission_refusal_text(
        "runs_elsewhere",
        RefusalNouns.for_platform("teams"),
        agent_name="analyst",
        channels=("19:x",),
    )
    assert teams == "analyst only works in other channels.\n\nAsk it there."


def test_a_deleted_agent_is_named_with_where_to_pick_another() -> None:
    nouns = RefusalNouns.for_platform("discord")
    assert resolver_miss_text(nouns, kind="agent", name="re`search") == (
        "This channel's agent `research` no longer exists.\n\n"
        "Ask an admin to pick another in `/agent-setup`."
    )
    assert resolver_miss_text(nouns, kind="environment", name="py") == (
        "This channel's setup is out of date.\n\nAsk an admin to check `/agent-setup`."
    )
    notice = render_termination_notice(
        TerminationReason.RESOLVER_MISS,
        error=MAResolverMissError(kind="agent", tenant_id=uuid.uuid4(), daimon_tag="research"),
        nouns=nouns,
    )
    assert notice is not None
    assert notice.title == "This channel's agent `research` no longer exists."
