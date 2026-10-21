"""`daimon.core.turn.status_lines`: the words on the in-progress turn card."""

from __future__ import annotations

from typing import Literal

import pytest
from daimon.core.tool_safety import DAIMON_SERVER_NAME
from daimon.core.turn.state import ContentBlock, TextBlock, ToolUseBlock
from daimon.core.turn.status_lines import (
    DRAFT_LOG_LINES,
    MAX_PLAN_LINES,
    draft_log,
    format_draft,
    format_duration,
    format_headline,
    format_plan_lines,
    format_summary,
    format_tool_lines,
    has_running_tool,
    roll_draft,
)

ToolType = Literal["agent.tool_use", "agent.custom_tool_use", "agent.mcp_tool_use"]
# A pending plan step has no icon: an em and an en space keep it in line.
_PENDING = "  "


def _call(
    name: str,
    status: Literal["pending", "complete", "failed"] = "pending",
    *,
    type: ToolType = "agent.tool_use",
    server: str | None = None,
    input: dict[str, object] | None = None,
) -> ToolUseBlock:
    return ToolUseBlock(
        kind="tool_use",
        id=f"tu_{name}_{status}",
        type=type,
        name=name,
        input=input or {},
        mcp_server_name=server,
        status=status,
    )


def _plan(
    steps: list[tuple[str, str]],
    *,
    status: Literal["pending", "complete", "failed"] = "pending",
    via_search: bool = False,
) -> ToolUseBlock:
    arguments: dict[str, object] = {
        "steps": [{"step": step, "status": state} for step, state in steps]
    }
    return ToolUseBlock(
        kind="tool_use",
        id=f"tu_plan_{len(steps)}_{status}",
        type="agent.mcp_tool_use",
        name="call_tool" if via_search else "update_plan",
        input={"name": "update_plan", "arguments": arguments} if via_search else arguments,
        mcp_server_name=DAIMON_SERVER_NAME,
        status=status,
        is_error=status == "failed",
    )


def test_format_summary_is_one_line_set_apart_by_space_alone() -> None:
    line = format_summary(agent_name="Ada", elapsed_seconds=194, cost="$0.014", left="$24.93 left")
    assert line == "Ada\u2003\u20033m 14s\u2003\u2003$0.014 used\u2003\u2003$24.93 left"
    assert "\n" not in line and "·" not in line and "|" not in line, "one line, no separators"


def test_format_summary_drops_missing_fields_with_their_gap() -> None:
    assert format_summary(agent_name=None, elapsed_seconds=5, cost=None, left=None) == "5s"
    assert (
        format_summary(agent_name="", elapsed_seconds=5, cost=None, left="$1.00 left")
        == "5s\u2003\u2003$1.00 left"
    )


def test_format_duration_covers_seconds_minutes_and_hours() -> None:
    assert format_duration(12.9) == "12s", "under a minute shows whole seconds"
    assert format_duration(65) == "1m 5s", "minutes then seconds, unpadded"
    assert format_duration(3 * 3600 + 7 * 60 + 30) == "3h 7m", "hours drop the seconds"
    assert format_duration(-4) == "0s", "a clock skew never shows a negative duration"


def test_format_headline_bolds_the_state_word_with_the_adapter_markup() -> None:
    assert format_headline(is_working=False, elapsed_seconds=12, bold=lambda s: f"**{s}**") == (
        "**Working on it…**"
    )
    assert format_headline(is_working=True, elapsed_seconds=None, bold=lambda s: f"*{s}*") == (
        "*Working on it…*"
    ), "without a clock the headline is the word alone"


def test_format_draft_flattens_whitespace_and_clips() -> None:
    assert format_draft("Checking\n\nthe   logs") == "Checking the logs", (
        "one line, so a single quote marker covers the draft"
    )
    clipped = format_draft("x" * 400)
    assert len(clipped) == 121 and clipped.endswith("…"), "clipped to 120 chars plus an ellipsis"


def test_has_running_tool_only_when_a_call_is_pending() -> None:
    assert not has_running_tool([TextBlock(kind="text", text="hi")]), "text alone is thinking"
    assert not has_running_tool([_call("bash", "complete")]), "a finished call is not running"
    assert has_running_tool([_call("bash", "complete"), _call("read")]), "a pending call works"


def test_built_in_tools_read_present_tense_while_running_and_past_when_done() -> None:
    lines = format_tool_lines(
        [_call("bash", "complete"), _call("web_fetch", "failed"), _call("read")]
    )
    assert lines == ("✔️ Ran a command", "🚫 Fetched a web page", "🔍 Reading a file"), (
        "finished calls take a result icon and the past tense; running ones the present"
    )


def test_mcp_and_custom_tools_get_humanized_names_and_verb_icons() -> None:
    lines = format_tool_lines(
        [
            _call("search_issues", "complete", type="agent.mcp_tool_use", server="tracker"),
            _call("createPage", type="agent.custom_tool_use"),
            _call("list-SQL-tables", type="agent.mcp_tool_use", server="warehouse"),
            _call("read", type="agent.custom_tool_use"),
        ]
    )
    assert lines == (
        "✔️ Search issues (tracker)",
        "🖋️ Create page",
        "📜 List SQL tables (warehouse)",
        "🔍 Read",
    ), "a custom tool named like a built-in is still humanized, not narrated"


def test_tool_lines_never_show_arguments() -> None:
    """A tool line names the tool and never shows its arguments."""
    lines = format_tool_lines(
        [
            _call(
                "search_issues",
                type="agent.mcp_tool_use",
                server="tracker",
                input={"query": "mcp-secret"},
            ),
            _call("create_page", type="agent.custom_tool_use", input={"title": "custom-secret"}),
        ]
    )
    shown = "".join(lines)
    assert "mcp-secret" not in shown, "an MCP call's arguments must never reach the card"
    assert "custom-secret" not in shown, "a custom call's arguments must never reach the card"


def test_only_allowlisted_built_in_fields_reach_the_card() -> None:
    """Details are an allowlist: bash, other built-in fields and same-named tools stay hidden."""
    lines = format_tool_lines(
        [
            _call("bash", input={"command": "curl -H 'Authorization: bash-secret'"}),
            _call("grep", input={"pattern": "grep-secret"}),
            _call("web_fetch", input={"url": "https://x.example.com/?key=url-secret"}),
            _call("write", input={"file_path": "/a/b.txt", "content": "write-secret"}),
            _call("web_search", type="agent.mcp_tool_use", server="s", input={"query": "s-secret"}),
            _call("read", type="agent.custom_tool_use", input={"file_path": "/custom-secret"}),
        ]
    )
    shown = "".join(lines)
    for secret in ("bash-secret", "grep-secret", "url-secret", "write-secret"):
        assert secret not in shown, f"{secret} must never reach the card"
    for secret in ("s-secret", "custom-secret"):
        assert secret not in shown, f"a non-built-in tool named like one leaks nothing: {secret}"


def test_finished_tool_lines_follow_the_order_calls_finished() -> None:
    content: list[ContentBlock] = [_call("bash", "complete"), _call("grep", "failed")]
    lines = format_tool_lines(content, finished_ids=("tu_grep_failed", "tu_bash_complete"))
    assert lines == ("🚫 Searched files", "✔️ Ran a command"), "the call that finished first leads"


def test_tool_lines_keep_running_calls_and_fold_older_ones() -> None:
    content: list[ContentBlock] = [_call(f"step_{i}", "complete") for i in range(5)]
    content += [_call("slow_import"), _call("bash"), _call("read")]
    lines = format_tool_lines(content)
    assert lines[0] == "+2 earlier", "calls beyond the six shown fold into one line on top"
    assert lines[1:4] == ("✔️ Step 2", "✔️ Step 3", "✔️ Step 4"), "latest finished calls kept"
    assert lines[4:] == ("🖋️ Slow import", "🖋️ Running a command", "🔍 Reading a file"), (
        "running calls always show, below the finished ones"
    )


def test_a_long_mcp_tool_name_is_clipped_before_its_server() -> None:
    name = "export_every_quarterly_revenue_report_for_all_regions_and_teams"
    (line,) = format_tool_lines([_call(name, type="agent.mcp_tool_use", server="finance")])
    assert line.endswith("… (finance)"), "the clip shortens the name and keeps the server"


def test_tool_lines_strip_backticks_so_the_code_block_holds() -> None:
    lines = format_tool_lines([_call("run", type="agent.mcp_tool_use", server="ops`evil")])
    assert "`" not in "".join(lines), "a backtick would close the adapter's code block early"


def test_default_summary_does_not_display_a_backend_inside_a_turn() -> None:
    import uuid

    from daimon.core.turn.outcomes import TurnObservation
    from daimon.core.turn.status_lines import backend_footer_text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    observation = TurnObservation(async_sessionmaker(), uuid.UUID(int=701), "slack")
    assert backend_footer_text() is None
    with observation.activate():
        assert backend_footer_text() is None
        assert format_summary(agent_name="Ada", elapsed_seconds=5, cost=None, left=None) == (
            "Ada\u2003\u20035s"
        )
    assert backend_footer_text() is None


@pytest.mark.parametrize(
    "profile,expected",
    [
        ("anthropic.managed_agents", "anthropic"),
        ("openai.persistent_workspace", "openai"),
        ("gemini.inline_reuse", "gemini"),
    ],
)
def test_configured_summary_uses_only_the_admitted_profile_metadata(profile, expected) -> None:
    import uuid

    from daimon.core.turn.outcomes import TurnObservation
    from daimon.core.turn.status_lines import backend_footer_text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    observation = TurnObservation(
        async_sessionmaker(), uuid.UUID(int=702), "slack", backend_profile=profile
    )
    with observation.activate():
        assert backend_footer_text() == expected
        assert (
            format_summary(agent_name="Ada", elapsed_seconds=5, cost="Cost unknown", left="$1 left")
            == f"Ada\u2003\u20035s\u2003\u2003Cost unknown\u2003\u2003$1 left\u2003\u2003{expected}"
        )
    assert backend_footer_text() is None


async def test_concurrent_turns_keep_their_backend_footers_isolated() -> None:
    import asyncio
    import uuid

    from daimon.core.turn.outcomes import TurnObservation
    from sqlalchemy.ext.asyncio import async_sessionmaker

    first = asyncio.Event()
    second = asyncio.Event()

    async def render(profile, ready, peer):
        observation = TurnObservation(
            async_sessionmaker(), uuid.UUID(int=703), "slack", backend_profile=profile
        )
        with observation.activate():
            ready.set()
            await peer.wait()
            return format_summary(agent_name=None, elapsed_seconds=5, cost=None, left=None)

    rows = await asyncio.gather(
        render("openai.persistent_workspace", first, second),
        render("gemini.inline_reuse", second, first),
    )
    assert rows == ["5s\u2003\u2003openai", "5s\u2003\u2003gemini"]
    assert format_summary(agent_name=None, elapsed_seconds=5, cost=None, left=None) == "5s"


@pytest.mark.parametrize(
    "profile,enabled,expected",
    [
        (None, True, "5s"),
        ("openai.persistent_workspace", False, "5s"),
        ("openai.persistent_workspace", True, "5s\u2003\u2003openai"),
        ("gemini.inline_reuse", True, "5s\u2003\u2003gemini"),
        ("anthropic.managed_agents", True, "5s\u2003\u2003anthropic"),
    ],
)
async def test_prepared_turn_activates_only_its_admitted_footer(
    monkeypatch, profile, enabled, expected
) -> None:
    import asyncio
    from dataclasses import replace
    from types import SimpleNamespace

    from daimon.core.turn import run as run_module
    from daimon.core.turn.outcomes import TurnObservation, current_outcome
    from daimon.core.turn.prepare import PreparedTurn
    from daimon.core.turn.run import RunOutcome
    from daimon.core.turn.running import RunningTurns
    from daimon.core.turn.state import TurnState
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from .test_provider_dispatch import ACCOUNT, TENANT, admission, record_usage

    # Only the outer context wrapper is controlled here; the admission and
    # preparation proofs exercise real selection and retained bindings.
    prepared = PreparedTurn(
        replace(admission(), backend_footer_profile=profile),
        "session",
        None,
        None,
        True,
        ACCOUNT,
        record_usage,
    )
    observed = []

    async def run(*args, **kwargs):
        assert current_outcome.get() is not None
        observed.append(format_summary(agent_name=None, elapsed_seconds=5, cost=None, left=None))
        return RunOutcome(TurnState(), "session", None, False)

    monkeypatch.setattr(run_module, "run_prepared_turn_impl", run)
    monkeypatch.setattr(TurnObservation, "finish", lambda *args, **kwargs: None)

    async def reseed():
        raise AssertionError("no recovery in a context proof")

    await run_module.run_prepared_turn(
        SimpleNamespace(
            sessionmaker=async_sessionmaker(),
            backend_selection_enabled=enabled,
            running_turns=RunningTurns(),
        ),
        prepared,
        tenant_id=TENANT,
        platform="slack",
        thread_id="thread",
        external_user_id="caller",
        user_message="offline",
        lifecycle=object(),
        cancel=asyncio.Event(),
        reseed_user_message=reseed,
        recovery_lifecycle=lambda _: object(),
    )
    assert observed == [expected]
    assert current_outcome.get() is None
    assert format_summary(agent_name=None, elapsed_seconds=5, cost=None, left=None) == "5s"


def test_optional_backend_context_does_not_shift_existing_positional_fields() -> None:
    import inspect

    from daimon.core.turn.outcomes import TurnObservation

    signature = inspect.signature(TurnObservation)
    assert signature.parameters["backend_profile"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["id"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


def test_the_draft_log_keeps_the_last_three_lines_oldest_first() -> None:
    earlier: tuple[str, ...] = ()
    latest: str | None = None
    for line in ("one", "two", "three", "four", "five"):
        earlier, latest = roll_draft(earlier, latest, line)
    assert DRAFT_LOG_LINES == 3, "three lines share the card"
    assert draft_log(earlier, latest) == ("three", "four", "five"), "the newest three, oldest first"


def test_the_draft_log_grows_from_nothing_to_one_to_three_lines() -> None:
    assert draft_log((), None) == (), "no message yet, no draft"
    earlier, latest = roll_draft((), None, "one")
    assert draft_log(earlier, latest) == ("one",), "the first message is the only line"
    earlier, latest = roll_draft(earlier, latest, "two")
    earlier, latest = roll_draft(earlier, latest, "three")
    assert draft_log(earlier, latest) == ("one", "two", "three"), "lines add up to three"


def test_the_draft_log_does_not_repeat_the_latest_line() -> None:
    earlier, latest = roll_draft((), None, "one")
    assert roll_draft(earlier, latest, "one") == ((), "one"), "a repeated line is shown once"


@pytest.mark.usefixtures("status_log_on")
def test_the_draft_log_is_the_same_whether_answers_keep_messages_apart() -> None:
    """`turn.status_log` now only shapes the answer; the card logs either way."""
    earlier, latest = roll_draft((), None, "one")
    earlier, latest = roll_draft(earlier, latest, "two")
    assert draft_log(earlier, latest) == ("one", "two"), "the log does not depend on the switch"
    assert format_draft("x" * 121) == "x" * 120 + "…", "nor does the line length"


def test_built_in_tools_show_one_allowlisted_detail() -> None:
    """A search shows its query, a fetch its host, a file tool its base name."""
    lines = format_tool_lines(
        [
            _call("web_search", "complete", input={"query": "pymc  marketing\nmix"}),
            _call(
                "web_fetch",
                "complete",
                input={"url": "https://user:pw@Docs.Example.com:8443/a/b?token=t#x"},
            ),
            _call("read", "complete", input={"file_path": "/workspace/repo/src/model.py"}),
            _call("edit", input={"path": "C:\\work\\notes.txt"}),
        ]
    )
    assert lines == (
        "✔️ Searched the web: pymc marketing mix",
        "✔️ Fetched a web page: docs.example.com",
        "✔️ Read a file: model.py",
        "🖋️ Editing a file: notes.txt",
    ), "each built-in shows its one field, on one line"


def test_a_long_detail_is_clipped_and_a_bad_one_dropped() -> None:
    lines = format_tool_lines(
        [
            _call("web_search", input={"query": "q" * 80}),
            _call("web_fetch", input={"url": "not a url"}),
            _call("write", input={"file_path": 42}),
            _call("read", input={"file_path": "/"}),
        ]
    )
    assert lines[0] == f"🔍 Searching the web: {'q' * 40}…", "a long query is clipped"
    assert lines[1:] == ("🔍 Fetching a web page", "🖋️ Writing a file", "🔍 Reading a file"), (
        "an input that is not a usable value leaves the plain label"
    )


def test_tool_details_never_carry_backticks_or_line_breaks() -> None:
    lines = format_tool_lines([_call("web_search", input={"query": "`rm -rf`\r\n@everyone\u200b"})])
    assert "`" not in lines[0] and "\n" not in lines[0] and "\r" not in lines[0], (
        "a detail must stay inside the adapter's inline code"
    )


def test_plan_lines_draw_the_latest_plan_as_a_checklist() -> None:
    content: list[ContentBlock] = [
        _plan([("Load data", "in_progress"), ("Fit model", "pending")]),
        _plan(
            [("Load data", "completed"), ("Fit model", "in_progress"), ("Report", "pending")],
            status="complete",
            via_search=True,
        ),
    ]
    assert format_plan_lines(content) == ("✔️ Load data", "🖋️ Fit model", f"{_PENDING}Report"), (
        "the newest update wins, also when called through the MCP search interface"
    )


def test_a_long_plan_folds_finished_steps_then_cuts_the_tail() -> None:
    steps: list[tuple[str, str]] = [(f"step {i}", "completed") for i in range(4)]
    steps += [("now", "in_progress")] + [(f"later {i}", "pending") for i in range(5)]
    lines = format_plan_lines([_plan(steps)])
    assert len(lines) == MAX_PLAN_LINES, "the plan stays within its line budget"
    assert lines[0] == "✔️ 4 done", "finished steps at the top fold into one line"
    assert lines[1] == "🖋️ now", "the step in progress stays in view"
    assert lines[-1] == f"{_PENDING}+2 more", "what does not fit is counted"


def test_plan_lines_skip_failed_calls_and_malformed_steps() -> None:
    content: list[ContentBlock] = [
        _plan([("Good", "pending"), ("Odd", "maybe"), ("", "pending")]),
        _plan([("Rejected", "pending")], status="failed"),
    ]
    assert format_plan_lines(content) == (f"{_PENDING}Good",), (
        "a failed update is ignored and a malformed step left out"
    )


def test_a_plan_update_leaves_the_tool_lines_only_when_the_plan_is_shown() -> None:
    content: list[ContentBlock] = [_plan([("Load", "in_progress")]), _call("bash")]
    assert format_tool_lines(content, plan_shown=True) == ("🖋️ Running a command",), (
        "the plan is drawn as the checklist, not as a tool line"
    )
    assert format_tool_lines(content) == ("🖋️ Update plan (daimon-mcp)", "🖋️ Running a command"), (
        "with progress plans off the call is an ordinary tool line"
    )
