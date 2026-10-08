"""`daimon.core.turn.status_lines`: the words on the in-progress turn card."""

from __future__ import annotations

from typing import Literal

from daimon.core.turn.state import ContentBlock, TextBlock, ToolUseBlock
from daimon.core.turn.status_lines import (
    format_draft,
    format_duration,
    format_headline,
    format_tool_lines,
    has_running_tool,
)

ToolType = Literal["agent.tool_use", "agent.custom_tool_use", "agent.mcp_tool_use"]


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
    assert len(clipped) == 301 and clipped.endswith("…"), "clipped to 300 chars plus an ellipsis"


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
