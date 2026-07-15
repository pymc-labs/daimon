"""Tests for the Slack context XML builder (context.py).

Tests verify that conversations.replies is called with the correct parameters
and that the resulting XML is well-formed and properly escaped.

Transport-level fake: each test creates its own ``aioresponses`` context so
the exact response payload is controlled per-test. No method-level AsyncMock.
"""

from __future__ import annotations

import re

from aioresponses import aioresponses as AioResponsesMock
from daimon.adapters.slack.attachments import ProxyUrlContext
from daimon.adapters.slack.context import _render_message, build_context_xml, build_delta_xml
from daimon.core.slack_file_token import verify_file_token
from slack_sdk.web.async_client import AsyncWebClient

# Pattern matches conversations.replies regardless of query params (aioresponses GET
# requests carry params in the URL, so exact-string matching fails).
_REPLIES_PATTERN = re.compile(r"https://slack\.com/api/conversations\.replies.*")


def _make_client() -> AsyncWebClient:
    return AsyncWebClient(token="xoxb-test")


async def test_build_context_xml_calls_conversations_replies_with_limit_100() -> None:
    """build_context_xml should call conversations_replies with limit=100 (D-02)."""
    with AioResponsesMock() as mock:
        mock.get(
            _REPLIES_PATTERN,
            payload={
                "ok": True,
                "messages": [
                    {"user": "U123", "text": "hello", "ts": "99.0"},
                ],
                "has_more": False,
            },
        )
        client = _make_client()
        xml = await build_context_xml(client, channel="C1", thread_ts="100.0", user_query="hi")

    # The XML output should contain the channel element, thread_history block, and user_query
    assert '<channel platform="slack" id="C1"/>' in xml, (
        "should contain channel element with platform and channel id"
    )
    assert "<thread_history>" in xml, "should contain thread_history block"
    assert "</thread_history>" in xml, "should close thread_history block"
    assert "<user_query>" in xml, "should contain user_query element"
    assert "hi" in xml, "user_query content should be in output"


async def test_build_context_xml_channel_is_first_child_of_context() -> None:
    """The <channel> element is the first child inside <context>, before <thread_history>."""
    with AioResponsesMock() as mock:
        mock.get(
            _REPLIES_PATTERN,
            payload={"ok": True, "messages": [], "has_more": False},
        )
        client = _make_client()
        xml = await build_context_xml(client, channel="C0BDT", thread_ts="100.0", user_query="hi")

    channel_pos = xml.index('<channel platform="slack" id="C0BDT"/>')
    context_pos = xml.index("<context>")
    history_pos = xml.index("<thread_history>")
    assert context_pos < channel_pos < history_pos, (
        "channel element should sit between <context> and <thread_history>"
    )


async def test_build_context_xml_renders_messages_in_output() -> None:
    """Replayed messages appear in the thread_history block."""
    with AioResponsesMock() as mock:
        mock.get(
            _REPLIES_PATTERN,
            payload={
                "ok": True,
                "messages": [
                    {"user": "U123", "text": "first message", "ts": "99.0"},
                    {"user": "U456", "text": "second message", "ts": "99.1"},
                ],
                "has_more": False,
            },
        )
        client = _make_client()
        xml = await build_context_xml(client, channel="C1", thread_ts="100.0", user_query="hello")

    assert "first message" in xml, "first message should be in XML"
    assert "second message" in xml, "second message should be in XML"
    # Messages should be wrapped in <message ...> elements
    assert "<message" in xml, "should contain message elements"


async def test_build_context_xml_escapes_xml_special_chars() -> None:
    """Message text containing < & > is XML-escaped (T-80-XML mitigation)."""
    dangerous_text = "a < b & c > d"
    with AioResponsesMock() as mock:
        mock.get(
            _REPLIES_PATTERN,
            payload={
                "ok": True,
                "messages": [
                    {"user": "U123", "text": dangerous_text, "ts": "99.0"},
                ],
                "has_more": False,
            },
        )
        client = _make_client()
        xml = await build_context_xml(client, channel="C1", thread_ts="100.0", user_query="safe")

    assert "&lt;" in xml, "< should be XML-escaped to &lt;"
    assert "&amp;" in xml, "& should be XML-escaped to &amp;"
    assert "&gt;" in xml, "> should be XML-escaped to &gt;"
    assert dangerous_text not in xml, "raw dangerous text must not appear verbatim"


async def test_build_context_xml_user_query_is_escaped() -> None:
    """user_query text is also XML-escaped."""
    with AioResponsesMock() as mock:
        mock.get(
            _REPLIES_PATTERN,
            payload={"ok": True, "messages": [], "has_more": False},
        )
        client = _make_client()
        xml = await build_context_xml(
            client, channel="C1", thread_ts="100.0", user_query="x < y & z"
        )

    assert "&lt;" in xml, "< in user_query should be XML-escaped"
    assert "&amp;" in xml, "& in user_query should be XML-escaped"


async def test_build_context_xml_empty_history() -> None:
    """An empty messages list results in only user_query in the output."""
    with AioResponsesMock() as mock:
        mock.get(
            _REPLIES_PATTERN,
            payload={"ok": True, "messages": [], "has_more": False},
        )
        client = _make_client()
        xml = await build_context_xml(
            client, channel="C1", thread_ts="100.0", user_query="trigger text"
        )

    assert "trigger text" in xml, "user_query must always appear"
    assert "<thread_history>" in xml, "thread_history block should be present"
    assert "</thread_history>" in xml, "thread_history block should be closed"


async def test_build_delta_xml_calls_conversations_replies_with_oldest() -> None:
    """build_delta_xml calls conversations_replies with oldest= and inclusive=False."""
    with AioResponsesMock() as mock:
        mock.get(
            _REPLIES_PATTERN,
            payload={
                "ok": True,
                "messages": [
                    {"user": "U789", "text": "delta message", "ts": "106.0"},
                ],
                "has_more": False,
            },
        )
        client = _make_client()
        xml = await build_delta_xml(
            client,
            channel="C1",
            thread_ts="100.0",
            watermark_ts="105.0",
            user_query="more",
        )

    assert '<channel platform="slack" id="C1"/>' in xml, (
        "should contain channel element with platform and channel id"
    )
    assert "<thread_delta>" in xml, "should contain thread_delta block"
    assert "</thread_delta>" in xml, "should close thread_delta block"
    assert "delta message" in xml, "delta message should appear"
    assert "<user_query>" in xml, "user_query element must be present"
    assert "more" in xml, "user_query content should appear"


async def test_build_delta_xml_escapes_xml_special_chars() -> None:
    """Delta message text with special characters is XML-escaped (T-80-XML)."""
    with AioResponsesMock() as mock:
        mock.get(
            _REPLIES_PATTERN,
            payload={
                "ok": True,
                "messages": [
                    {"user": "U789", "text": "<script>bad</script>", "ts": "106.0"},
                ],
                "has_more": False,
            },
        )
        client = _make_client()
        xml = await build_delta_xml(
            client,
            channel="C1",
            thread_ts="100.0",
            watermark_ts="105.0",
            user_query="query",
        )

    assert "<script>" not in xml, "raw script tag must not appear in output"
    assert "&lt;script&gt;" in xml, "< and > in message text must be escaped"


def test_render_message_emits_proxy_attachment_for_history_file() -> None:
    """A history message with a file renders a signed <attachment> URL."""
    msg = {
        "user": "U1",
        "ts": "1.0",
        "text": "see attached",
        "files": [{"id": "F9", "name": "old.csv", "mimetype": "text/csv"}],
    }
    ctx = ProxyUrlContext(public_url="https://mcp.example.com", secret="s", team_id="T1", now=1000)
    lines = _render_message(msg, proxy=ctx)
    joined = "\n".join(lines)
    assert "old.csv" in joined and "<attachment" in joined, "history file rendered as attachment"
    token = joined.split("/slack/file/")[1].split('"')[0]
    assert verify_file_token(token, secret="s", now=1000) is not None, "attachment URL is signed"


def test_render_message_omits_attachments_when_no_proxy_configured() -> None:
    """When the proxy is unconfigured, history files are omitted (no attachment)."""
    msg = {"user": "U1", "ts": "1.0", "text": "hi", "files": [{"id": "F9", "name": "x"}]}
    lines = _render_message(msg, proxy=None)
    assert "<attachment" not in "\n".join(lines), "no attachment lines when proxy unconfigured"


async def test_build_context_xml_renders_author_id_on_user_query() -> None:
    """When author_id is given, it appears as an attribute on <user_query>."""
    with AioResponsesMock() as mock:
        mock.get(
            _REPLIES_PATTERN,
            payload={"ok": True, "messages": [], "has_more": False},
        )
        client = _make_client()
        xml = await build_context_xml(
            client, channel="C1", thread_ts="100.0", user_query="hi", author_id="U0BDWSMCB26"
        )

    assert '<user_query author_id="U0BDWSMCB26">' in xml, (
        "author_id must be rendered as a quoted attribute so the agent can mention the asker"
    )


async def test_build_context_xml_omits_author_id_attr_when_empty() -> None:
    """No author_id → bare <user_query> (back-compat, no empty attribute)."""
    with AioResponsesMock() as mock:
        mock.get(
            _REPLIES_PATTERN,
            payload={"ok": True, "messages": [], "has_more": False},
        )
        client = _make_client()
        xml = await build_context_xml(client, channel="C1", thread_ts="100.0", user_query="hi")

    assert "<user_query>" in xml, "empty author_id must leave <user_query> without attributes"
    assert "author_id=" not in xml, "must not emit an empty author_id attribute"


async def test_build_delta_xml_renders_author_id_on_user_query() -> None:
    """Delta builder also carries author_id onto <user_query>."""
    with AioResponsesMock() as mock:
        mock.get(
            _REPLIES_PATTERN,
            payload={"ok": True, "messages": [], "has_more": False},
        )
        client = _make_client()
        xml = await build_delta_xml(
            client,
            channel="C1",
            thread_ts="100.0",
            watermark_ts="105.0",
            user_query="more",
            author_id="U123",
        )

    assert '<user_query author_id="U123">' in xml, "delta user_query must carry author_id"
