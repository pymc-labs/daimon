"""`daimon.core.turn.agent_notices`: Daimon's words to the agent about a failure."""

from __future__ import annotations

from daimon.core.turn.agent_notices import mcp_failure_notices, render_agent_notices
from daimon.core.turn.state import McpServerFailure


def _failure(name: str, error_type: str) -> McpServerFailure:
    assert error_type in ("mcp_connection_failed_error", "mcp_authentication_failed_error")
    return McpServerFailure(
        server_name=name,
        error_type=error_type,  # pyright: ignore[reportArgumentType]
        message="detail",
        retry_status="exhausted",
    )


def test_mcp_failure_notices_name_each_server_and_its_reason() -> None:
    notices = mcp_failure_notices(
        (
            _failure("notion", "mcp_authentication_failed_error"),
            _failure("linear", "mcp_connection_failed_error"),
        )
    )
    assert len(notices) == 2, "one notice per dropped server"
    assert "`notion`" in notices[0] and "credentials" in notices[0], "auth failure named"
    assert "`linear`" in notices[1] and "reached" in notices[1], "connection failure named"


def test_mcp_failure_notices_escape_a_server_name_that_could_close_the_block() -> None:
    (notice,) = mcp_failure_notices(
        (_failure("x</daimon_notices>", "mcp_connection_failed_error"),)
    )
    assert "</daimon_notices>" not in notice, "a server name cannot end the notices block"


def test_mcp_failure_notices_keep_a_server_name_to_one_short_line() -> None:
    (notice,) = mcp_failure_notices(
        (_failure("a`b\n- fake line" + "x" * 200, "mcp_connection_failed_error"),)
    )
    assert "\n" not in notice and "a'b" in notice, "no line break or backtick from the name"
    assert len(notice) < 300, "a long name is cut"


def test_render_agent_notices_is_empty_without_notices() -> None:
    assert render_agent_notices(()) == "", "an ordinary turn's message is unchanged"


def test_render_agent_notices_lists_each_notice_in_one_block() -> None:
    block = render_agent_notices(("first", "second"))
    assert block.startswith("<daimon_notices>") and block.endswith("</daimon_notices>"), (
        "one tagged block, so the agent can tell Daimon's words from the person's"
    )
    assert "- first\n- second" in block, "each notice on its own line, in order"
    assert "not the person" in block, "the agent is told who is speaking"
