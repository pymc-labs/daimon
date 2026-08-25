"""Tests for the pure Slack permalink parser.

Mirrors ``tests/tools/test_discord_read.py::TestParseLink`` in shape: no
fixtures, no monkeypatch, no aioresponses, no DB — ``_slack_parse_link_impl``
is pure, so importing it and calling it directly is the whole test.
"""

from __future__ import annotations

import pytest
from daimon.adapters.mcp.tools.slack._parse_link import (
    _slack_parse_link_impl,  # pyright: ignore[reportPrivateUsage]
)
from fastmcp.exceptions import ToolError


class TestSlackParseLink:
    def test_root_message_permalink(self) -> None:
        result = _slack_parse_link_impl(
            "https://acme.slack.com/archives/C0123456789/p1358546515000008"
        )
        assert result.channel_id == "C0123456789", "channel id must come from the path segment"
        assert result.message_ts == "1358546515.000008", (
            "the p-value's digits must be dotted before the last six digits"
        )
        assert result.thread_ts is None, "a root permalink carries no thread_ts"
        assert "read_thread" in result.hint, "the hint must name read_thread"

    def test_docs_example_is_malformed_test_the_rule_not_that_example(self) -> None:
        """Slack's own published permalink example (p135854651500008, for the
        ts 1358546515.000008) is missing a digit — 15 digits instead of 16.
        This test pins the parser's actual rule (a dot before the last six
        digits of whatever digit string is present), not that broken
        example. A future reader must NOT "fix" the parser to reproduce
        Slack's docs bug — the docs are wrong, not this test."""
        result = _slack_parse_link_impl(
            "https://acme.slack.com/archives/C0123456789/p135854651500008"
        )
        assert result.message_ts == "135854651.500008", (
            "a dot inserted before the last six digits of the 15-digit p-value "
            "is the mechanically-correct output of the parser's rule, even "
            "though it does not match Slack's own (malformed) docs example"
        )

    def test_reply_permalink_thread_ts_is_already_dotted(self) -> None:
        result = _slack_parse_link_impl(
            "https://acme.slack.com/archives/C0123456789/p1717171717000200"
            "?thread_ts=1717171717.000100&cid=C0123456789"
        )
        assert result.thread_ts == "1717171717.000100", (
            "thread_ts arrives already dotted in the query string — no conversion"
        )
        assert result.message_ts == "1717171717.000200", (
            "message_ts is still the converted p-value, distinct from thread_ts"
        )
        assert "C0123456789:1717171717.000100" in result.hint, (
            "the hint's read_thread composite must use the PARENT ts (thread_ts), "
            "not the reply's own message_ts"
        )

    def test_dm_permalink_parses_identically(self) -> None:
        result = _slack_parse_link_impl(
            "https://acme.slack.com/archives/D0123456789/p1717171717000100"
        )
        assert result.channel_id == "D0123456789", (
            "a D-prefixed (DM) channel id needs no special-casing"
        )
        assert result.message_ts == "1717171717.000100"

    def test_foreign_workspace_subdomain_parses_fine(self) -> None:
        """The parser is workspace-agnostic; a wrong workspace fails downstream
        at the read tool via map_slack_api_error, not here."""
        result = _slack_parse_link_impl(
            "https://other-workspace.slack.com/archives/C0123456789/p1717171717000100"
        )
        assert result.channel_id == "C0123456789"
        assert result.message_ts == "1717171717.000100"

    def test_non_slack_url_raises_tool_error(self) -> None:
        with pytest.raises(ToolError, match="not a recognized slack"):
            _slack_parse_link_impl("https://example.com/not-slack")

    def test_no_archives_segment_raises_tool_error(self) -> None:
        with pytest.raises(ToolError, match="not a recognized slack"):
            _slack_parse_link_impl("https://acme.slack.com/messages/C0123456789/p1717171717000100")

    def test_p_value_too_short_to_split_raises_tool_error(self) -> None:
        with pytest.raises(ToolError, match="not a recognized slack"):
            _slack_parse_link_impl("https://acme.slack.com/archives/C0123456789/p12345")

    def test_root_permalink_hint_names_get_message_as_fallback(self) -> None:
        result = _slack_parse_link_impl(
            "https://acme.slack.com/archives/C0123456789/p1717171717000100"
        )
        assert "get_message" in result.hint, (
            "a root permalink's hint must name get_message as the read_thread fallback, "
            "mirroring Discord's ambiguity-tolerant hint"
        )
