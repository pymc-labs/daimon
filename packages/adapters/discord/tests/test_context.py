"""Tests for the Discord thread history XML context builder."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.context import (
    CHANNEL_BACKFILL_LIMIT,
    build_channel_context_xml,
    build_context_xml,
    build_delta_xml,
)
from daimon.adapters.discord.vision import MAX_VISION_IMAGE_BYTES


class _AsyncIter:
    """Async iterator adapter for mocked ``thread.history()``."""

    def __init__(self, items: list[discord.Message]) -> None:
        self._items = iter(items)

    def __aiter__(self) -> _AsyncIter:
        return self

    async def __anext__(self) -> discord.Message:
        try:
            return next(self._items)
        except StopIteration as err:
            raise StopAsyncIteration from err


def _make_message(
    *,
    msg_id: int = 1,
    content: str = "hello",
    author_name: str = "Alice",
    author_id: int = 100,
    is_bot: bool = False,
    timestamp: datetime | None = None,
    attachments: list[discord.Attachment] | None = None,
) -> discord.Message:
    """Build a mock discord.Message with the fields context.py uses."""
    msg = MagicMock(spec=discord.Message)
    msg.id = msg_id
    msg.content = content
    msg.author = MagicMock()
    msg.author.display_name = author_name
    msg.author.id = author_id
    msg.author.bot = is_bot
    msg.created_at = timestamp or datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
    msg.attachments = attachments or []
    return msg


def _make_attachment(
    *,
    filename: str = "image.png",
    url: str = "https://cdn.discord.com/image.png",
    content_type: str = "image/png",
    size: int = 1024,
    width: int | None = 800,
    height: int | None = 600,
) -> discord.Attachment:
    att = MagicMock(spec=discord.Attachment)
    att.filename = filename
    att.url = url
    att.content_type = content_type
    att.size = size
    att.width = width
    att.height = height
    return att


def _make_thread(
    messages: list[discord.Message],
    *,
    thread_id: int = 900,
    parent_id: int = 800,
) -> discord.Thread:
    thread = MagicMock(spec=discord.Thread)
    thread.history = MagicMock(return_value=_AsyncIter(messages))
    thread.id = thread_id
    thread.parent_id = parent_id
    # Default: no starter message and no parent — build_context_xml skips starter prepend.
    thread.starter_message = None
    thread.parent = None
    return thread


def _make_text_channel(messages: list[discord.Message]) -> discord.TextChannel:
    """Build a mock discord.TextChannel with a history async iterator."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.history = MagicMock(return_value=_AsyncIter(messages))
    return channel


class TestBuildContextXml:
    """Tests for build_context_xml()."""

    @pytest.mark.asyncio
    async def test_build_context_xml_empty_thread_returns_user_query_only(self) -> None:
        trigger = _make_message(msg_id=10, content="what's up?", author_name="Bob", author_id=200)
        thread = _make_thread([trigger])  # only the trigger in history

        result, _ = await build_context_xml(thread, trigger)

        assert '<channel platform="discord" id="800" role="parent_channel"/>' in result, (
            "should have channel element with discord platform and parent-channel id"
        )
        assert '<thread platform="discord" id="900" role="current_thread"' in result, (
            "should have thread element carrying the current thread id"
        )
        channel_pos = result.index('<channel platform="discord" id="800" role="parent_channel"/>')
        context_pos = result.index("<context>")
        history_pos = result.index("<thread_history>")
        assert context_pos < channel_pos < history_pos, (
            "channel element should sit between <context> and <thread_history>"
        )
        assert "<thread_history>" in result, "should have thread_history element"
        assert "</thread_history>" in result, "should close thread_history"
        assert "<message" not in result, "no messages should appear in empty thread"
        assert "<user_query" in result, "should have user_query element"
        assert "what's up?" in result, "trigger content should appear in user_query"

    @pytest.mark.asyncio
    async def test_build_context_xml_channel_id_is_parent_channel_id(self) -> None:
        """The channel id is the thread's parent channel id, not the thread id."""
        trigger = _make_message(msg_id=10, content="hi", author_id=200)
        thread = _make_thread([trigger], thread_id=900, parent_id=42)

        result, _ = await build_context_xml(thread, trigger)

        assert '<channel platform="discord" id="42" role="parent_channel"/>' in result, (
            "channel id should be the thread's parent_id"
        )
        assert '<thread platform="discord" id="900" role="current_thread"' in result, (
            "thread element id should be the thread's own id, distinct from the channel"
        )

    @pytest.mark.asyncio
    async def test_build_context_xml_three_messages_produces_correct_structure(self) -> None:
        m1 = _make_message(
            msg_id=1,
            content="first",
            author_name="Alice",
            timestamp=datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC),
        )
        m2 = _make_message(
            msg_id=2,
            content="second",
            author_name="Bob",
            author_id=200,
            timestamp=datetime(2026, 4, 28, 12, 1, 0, tzinfo=UTC),
        )
        m3 = _make_message(
            msg_id=3,
            content="third",
            author_name="Charlie",
            author_id=300,
            timestamp=datetime(2026, 4, 28, 12, 2, 0, tzinfo=UTC),
        )
        trigger = _make_message(
            msg_id=10,
            content="trigger",
            author_name="Dave",
            author_id=400,
            timestamp=datetime(2026, 4, 28, 12, 3, 0, tzinfo=UTC),
        )
        thread = _make_thread([m1, m2, m3, trigger])

        result, _ = await build_context_xml(thread, trigger)

        assert result.count("<message") == 3, "should have 3 message elements"
        assert "<user_query" in result, "should have user_query"
        # user_query should be outside context
        context_end = result.index("</context>")
        user_query_start = result.index("<user_query")
        assert user_query_start > context_end, "user_query should be after </context>"

    @pytest.mark.asyncio
    async def test_build_context_xml_messages_sorted_chronologically(self) -> None:
        """Messages sorted oldest-first regardless of fetch order."""
        m_late = _make_message(
            msg_id=2, content="late", timestamp=datetime(2026, 4, 28, 12, 5, 0, tzinfo=UTC)
        )
        m_early = _make_message(
            msg_id=1, content="early", timestamp=datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
        )
        trigger = _make_message(
            msg_id=10, content="trigger", timestamp=datetime(2026, 4, 28, 12, 10, 0, tzinfo=UTC)
        )
        # Fetch order: late, early (reversed)
        thread = _make_thread([m_late, m_early, trigger])

        result, _ = await build_context_xml(thread, trigger)

        early_pos = result.index("early")
        late_pos = result.index("late")
        assert early_pos < late_pos, "early message should appear before late message"

    @pytest.mark.asyncio
    async def test_build_context_xml_trigger_excluded_from_history(self) -> None:
        m1 = _make_message(msg_id=1, content="prior")
        trigger = _make_message(msg_id=10, content="trigger-content")
        thread = _make_thread([m1, trigger])

        result, _ = await build_context_xml(thread, trigger)

        # trigger-content appears only in user_query, not in thread_history
        history_section = result[
            result.index("<thread_history>") : result.index("</thread_history>")
        ]
        assert "trigger-content" not in history_section, "trigger should not be in thread_history"
        assert "trigger-content" in result, "trigger should appear in user_query"

    @pytest.mark.asyncio
    async def test_build_context_xml_escapes_special_characters(self) -> None:
        m1 = _make_message(msg_id=1, content="a < b & c > d")
        trigger = _make_message(msg_id=10, content="x < y")
        thread = _make_thread([m1, trigger])

        result, _ = await build_context_xml(thread, trigger)

        assert "a &lt; b &amp; c &gt; d" in result, "special chars in messages should be escaped"
        assert "x &lt; y" in result, "special chars in trigger should be escaped"

    @pytest.mark.asyncio
    async def test_build_context_xml_quoteattr_handles_quotes_in_names(self) -> None:
        m1 = _make_message(msg_id=1, content="hi", author_name='O"Brien')
        trigger = _make_message(msg_id=10, content="yo")
        thread = _make_thread([m1, trigger])

        result, _ = await build_context_xml(thread, trigger)

        # quoteattr should handle the double quote in the display name
        assert "O" in result, "author name should appear"
        # The name should not break XML parsing — quoteattr wraps in quotes
        assert 'author_name="O' not in result or "author_name='" in result, (
            "quoteattr should use single quotes or escape double quotes"
        )

    @pytest.mark.asyncio
    async def test_build_context_xml_includes_bot_messages(self) -> None:
        bot_msg = _make_message(msg_id=1, content="I am a bot", is_bot=True, author_name="Daimon")
        human_msg = _make_message(
            msg_id=2, content="thanks", timestamp=datetime(2026, 4, 28, 12, 1, 0, tzinfo=UTC)
        )
        trigger = _make_message(
            msg_id=10, content="more", timestamp=datetime(2026, 4, 28, 12, 2, 0, tzinfo=UTC)
        )
        thread = _make_thread([bot_msg, human_msg, trigger])

        result, _ = await build_context_xml(thread, trigger)

        assert "I am a bot" in result, "bot messages should be included"
        assert 'is_bot="true"' in result, "bot message should have is_bot=true"
        assert 'is_bot="false"' in result, "human message should have is_bot=false"

    @pytest.mark.asyncio
    async def test_build_context_xml_attachments_rendered(self) -> None:
        att = _make_attachment(
            filename="report.pdf",
            url="https://cdn.discord.com/report.pdf",
            content_type="application/pdf",
            size=2048,
        )
        m1 = _make_message(msg_id=1, content="see attached", attachments=[att])
        trigger = _make_message(msg_id=10, content="got it")
        thread = _make_thread([m1, trigger])

        result, image_atts = await build_context_xml(thread, trigger)

        assert "<attachments>" in result, "should have attachments block"
        assert "<attachment" in result, "should have attachment element"
        assert 'filename="report.pdf"' in result, "should include filename attribute"
        assert 'content_type="application/pdf"' in result, "should include content_type"
        assert 'size="2048"' in result, "should include size"
        assert image_atts == [], "pdf attachment should not be collected as image"

    @pytest.mark.asyncio
    async def test_build_context_xml_image_attachments_collected(self) -> None:
        """Image attachments in thread history are returned for vision content blocks."""
        img = _make_attachment(
            filename="photo.png",
            url="https://cdn.discord.com/photo.png",
            content_type="image/png",
            size=1024,
        )
        doc = _make_attachment(
            filename="notes.pdf",
            url="https://cdn.discord.com/notes.pdf",
            content_type="application/pdf",
            size=512,
        )
        m1 = _make_message(msg_id=1, content="here are my files", attachments=[img, doc])
        trigger = _make_message(msg_id=10, content="thanks")
        thread = _make_thread([m1, trigger])

        _, image_atts = await build_context_xml(thread, trigger)

        assert len(image_atts) == 1, "only the image/png attachment should be collected"
        assert image_atts[0] is img, "collected attachment should be the image object"

    @pytest.mark.asyncio
    async def test_build_context_xml_non_vision_images_not_collected(self) -> None:
        """History images the API can't consume (unsupported type, oversized)
        are excluded from the vision collection — they'd fail the whole
        user.message event if sent."""
        svg = _make_attachment(
            filename="diagram.svg",
            url="https://cdn.discord.com/diagram.svg",
            content_type="image/svg+xml",
            size=512,
        )
        huge = _make_attachment(
            filename="huge.png",
            url="https://cdn.discord.com/huge.png",
            content_type="image/png",
            size=MAX_VISION_IMAGE_BYTES + 1,
        )
        m1 = _make_message(msg_id=1, content="big files", attachments=[svg, huge])
        trigger = _make_message(msg_id=10, content="see those?")
        thread = _make_thread([m1, trigger])

        result, image_atts = await build_context_xml(thread, trigger)

        assert image_atts == [], "svg and oversized png should not be collected for vision"
        assert 'filename="diagram.svg"' in result, "non-vision attachment still rendered in XML"

    @pytest.mark.asyncio
    async def test_build_context_xml_bot_mention_replaced(self) -> None:
        m1 = _make_message(msg_id=1, content="hey <@999> what do you think?")
        trigger = _make_message(msg_id=10, content="<@!999> help me")
        thread = _make_thread([m1, trigger])

        result, _ = await build_context_xml(thread, trigger, bot_user_id=999)

        assert "<@999>" not in result, "raw bot mention should be replaced"
        assert "<@!999>" not in result, "raw bot mention with ! should be replaced"
        assert "@daimon" in result, "bot mentions should become @daimon"

    @pytest.mark.asyncio
    async def test_build_context_xml_prepends_starter_when_distinct_from_trigger(self) -> None:
        """Thread starter present and distinct from trigger is prepended once at front of history."""
        starter = _make_message(
            msg_id=5,
            content="starter message",
            author_name="Alice",
            timestamp=datetime(2026, 4, 28, 11, 0, 0, tzinfo=UTC),
        )
        m1 = _make_message(
            msg_id=6,
            content="reply one",
            timestamp=datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC),
        )
        trigger = _make_message(
            msg_id=10,
            content="trigger content",
            timestamp=datetime(2026, 4, 28, 12, 1, 0, tzinfo=UTC),
        )
        thread = _make_thread([m1, trigger])
        thread.id = 5  # thread.id matches starter.id (used by fetch_message fallback)
        thread.starter_message = starter
        thread.parent = MagicMock(spec=discord.TextChannel)
        thread.parent.fetch_message = AsyncMock(return_value=starter)

        result, _ = await build_context_xml(thread, trigger)

        history_section = result[
            result.index("<thread_history>") : result.index("</thread_history>")
        ]
        assert "starter message" in history_section, "starter should appear in thread_history"
        starter_pos = result.index("starter message")
        reply_pos = result.index("reply one")
        assert starter_pos < reply_pos, "starter should appear before other messages"
        assert result.count("starter message") == 1, "starter should appear exactly once"

    @pytest.mark.asyncio
    async def test_build_context_xml_skips_starter_when_starter_is_trigger(self) -> None:
        """When starter.id == trigger.id, starter must NOT be prepended (trigger is already user_query)."""
        trigger = _make_message(
            msg_id=10,
            content="trigger is the starter",
            timestamp=datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC),
        )
        m1 = _make_message(
            msg_id=11,
            content="reply",
            timestamp=datetime(2026, 4, 28, 12, 1, 0, tzinfo=UTC),
        )
        thread = _make_thread([m1, trigger])
        thread.id = 10  # thread.id matches trigger.id
        thread.starter_message = trigger  # starter IS the trigger
        thread.parent = MagicMock(spec=discord.TextChannel)
        thread.parent.fetch_message = AsyncMock(return_value=trigger)

        result, _ = await build_context_xml(thread, trigger)

        history_section = result[
            result.index("<thread_history>") : result.index("</thread_history>")
        ]
        assert "trigger is the starter" not in history_section, (
            "starter==trigger should not appear in thread_history (it's in user_query)"
        )
        # Should still appear in user_query
        assert "trigger is the starter" in result, "trigger content should still be in user_query"

    @pytest.mark.asyncio
    async def test_build_context_xml_skips_starter_when_already_in_history(self) -> None:
        """When starter is already in the fetched history, it must NOT be duplicated."""
        starter = _make_message(
            msg_id=5,
            content="starter in history",
            timestamp=datetime(2026, 4, 28, 11, 0, 0, tzinfo=UTC),
        )
        trigger = _make_message(
            msg_id=10,
            content="trigger",
            timestamp=datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC),
        )
        # starter is already in the thread history returned by discord
        thread = _make_thread([starter, trigger])
        thread.id = 5
        thread.starter_message = starter
        thread.parent = MagicMock(spec=discord.TextChannel)
        thread.parent.fetch_message = AsyncMock(return_value=starter)

        result, _ = await build_context_xml(thread, trigger)

        assert result.count("starter in history") == 1, (
            "starter already in history should appear exactly once, not duplicated"
        )

    @pytest.mark.asyncio
    async def test_build_context_xml_handles_http_exception_on_starter_fetch(self) -> None:
        """When fetch_message raises discord.HTTPException, build succeeds with no starter prepended."""
        m1 = _make_message(
            msg_id=6,
            content="reply",
            timestamp=datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC),
        )
        trigger = _make_message(
            msg_id=10,
            content="trigger",
            timestamp=datetime(2026, 4, 28, 12, 1, 0, tzinfo=UTC),
        )
        thread = _make_thread([m1, trigger])
        thread.id = 5
        thread.starter_message = None  # force the fetch_message path
        thread.parent = MagicMock(spec=discord.TextChannel)
        thread.parent.fetch_message = AsyncMock(
            side_effect=discord.HTTPException(response=MagicMock(), message="Not Found")
        )

        # Must not raise
        result, _ = await build_context_xml(thread, trigger)

        history_section = result[
            result.index("<thread_history>") : result.index("</thread_history>")
        ]
        assert result.count("<message") == 1, (
            "only reply should appear; starter fetch failed silently"
        )
        assert "reply" in history_section, "non-starter history still rendered"
        assert "<user_query" in result, "user_query still present after starter fetch failure"


class TestBuildDeltaXml:
    """Tests for build_delta_xml() — continuation turn delta builder."""

    @pytest.mark.asyncio
    async def test_delta_includes_only_post_watermark_messages(self) -> None:
        """Delta builder includes post-watermark messages and trigger in user_query."""
        m_a = _make_message(
            msg_id=51,
            content="first post-watermark",
            author_name="Alice",
            timestamp=datetime(2026, 4, 28, 12, 1, 0, tzinfo=UTC),
        )
        m_b = _make_message(
            msg_id=52,
            content="second post-watermark",
            author_name="Bob",
            author_id=200,
            timestamp=datetime(2026, 4, 28, 12, 2, 0, tzinfo=UTC),
        )
        # thread fake returns exactly these two messages (simulating discord's after= filter)
        thread = _make_thread([m_a, m_b])
        trigger = _make_message(
            msg_id=99,
            content="follow-up",
            author_name="Dave",
            author_id=400,
            timestamp=datetime(2026, 4, 28, 12, 3, 0, tzinfo=UTC),
        )

        result, _ = await build_delta_xml(thread, trigger, after_message_id=50, bot_user_id=None)

        assert '<channel platform="discord" id="800" role="parent_channel"/>' in result, (
            "delta should have channel element with discord platform and parent-channel id"
        )
        assert '<thread platform="discord" id="900" role="current_thread"' in result, (
            "delta should have thread element carrying the current thread id"
        )
        channel_pos = result.index('<channel platform="discord" id="800" role="parent_channel"/>')
        context_pos = result.index("<context>")
        delta_pos = result.index("<thread_delta>")
        assert context_pos < channel_pos < delta_pos, (
            "channel element should sit between <context> and <thread_delta>"
        )
        assert "first post-watermark" in result, "m_a content should appear in delta"
        assert "second post-watermark" in result, "m_b content should appear in delta"
        assert "<user_query" in result, "trigger should appear in user_query element"
        assert "follow-up" in result, "trigger content should appear in output"
        # trigger should NOT appear inside thread_delta region
        delta_start = result.index("<thread_delta>")
        delta_end = result.index("</thread_delta>")
        delta_region = result[delta_start:delta_end]
        assert "follow-up" not in delta_region, (
            "trigger content should not appear inside thread_delta"
        )

    @pytest.mark.asyncio
    async def test_build_delta_xml_includes_bot_replies_in_window(self) -> None:
        """Q1→(b): build_delta_xml INCLUDES the bot's own reply messages in the delta.

        When a caller resumes after other participants spoke, the cross-speaker delta
        must carry the bot's replies to those participants so the agent's per-caller
        session stays coherent with what the agent told others in the same thread.

        The trigger is still excluded. Image handling is unchanged (URL-only).
        """
        bot_reply = _make_message(
            msg_id=10,
            content="bot reply to other participant",
            author_name="Daimon",
            author_id=777,
            is_bot=True,
            timestamp=datetime(2026, 4, 28, 12, 1, 0, tzinfo=UTC),
        )
        human_msg = _make_message(
            msg_id=11,
            content="human follow-up",
            author_name="Alice",
            timestamp=datetime(2026, 4, 28, 12, 2, 0, tzinfo=UTC),
        )
        trigger = _make_message(
            msg_id=99,
            content="resuming caller's question",
            author_name="Bob",
            author_id=200,
            timestamp=datetime(2026, 4, 28, 12, 3, 0, tzinfo=UTC),
        )
        thread = _make_thread([bot_reply, human_msg])

        result, _ = await build_delta_xml(thread, trigger, after_message_id=5, bot_user_id=777)

        # Bot reply must appear in the delta (Q1→b coherence fix).
        assert "bot reply to other participant" in result, (
            "build_delta_xml must include the bot's own reply in the delta window "
            "so a resuming caller's per-caller session stays coherent with what the "
            "agent told other participants (Q1→b, Phase 88-05)"
        )
        # Human message still appears.
        assert "human follow-up" in result, "human messages in the delta window must still appear"
        # Trigger is still excluded from thread_delta (only in user_query).
        delta_start = result.index("<thread_delta>")
        delta_end = result.index("</thread_delta>")
        delta_region = result[delta_start:delta_end]
        assert "resuming caller's question" not in delta_region, (
            "trigger must still be excluded from thread_delta (only in user_query)"
        )
        assert "<user_query" in result, "trigger must still appear in user_query"

    @pytest.mark.asyncio
    async def test_build_delta_xml_bot_reply_present_single_speaker(self) -> None:
        """Single-speaker continuation: bot reply in delta is harmless (server-side memory
        already has it; the small token cost is accepted per SCOPING §3/§9 Q1).
        The function must not error or strip the bot reply in this case.
        """
        bot_reply = _make_message(
            msg_id=10,
            content="my previous answer",
            author_name="Daimon",
            author_id=777,
            is_bot=True,
            timestamp=datetime(2026, 4, 28, 12, 1, 0, tzinfo=UTC),
        )
        trigger = _make_message(
            msg_id=99,
            content="follow-up",
            author_name="Alice",
            timestamp=datetime(2026, 4, 28, 12, 2, 0, tzinfo=UTC),
        )
        thread = _make_thread([bot_reply])

        result, _ = await build_delta_xml(thread, trigger, after_message_id=5, bot_user_id=777)

        assert "my previous answer" in result, (
            "bot reply must appear in delta for single-speaker continuation — "
            "the token cost is accepted (Q1→b)"
        )
        assert "follow-up" in result, "trigger must appear in user_query"

    @pytest.mark.asyncio
    async def test_delta_none_after_falls_back_to_full(self) -> None:
        """When after_message_id is None, output matches build_context_xml (full snapshot)."""
        m1 = _make_message(
            msg_id=1,
            content="prior message",
            author_name="Alice",
            timestamp=datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC),
        )
        trigger = _make_message(
            msg_id=10,
            content="trigger message",
            author_name="Bob",
            author_id=200,
            timestamp=datetime(2026, 4, 28, 12, 1, 0, tzinfo=UTC),
        )
        # both builders get fresh threads from the same message list
        thread_for_delta = _make_thread([m1, trigger])
        thread_for_full = _make_thread([m1, trigger])

        delta_result, delta_atts = await build_delta_xml(
            thread_for_delta, trigger, after_message_id=None, bot_user_id=None
        )
        full_result, full_atts = await build_context_xml(thread_for_full, trigger, bot_user_id=None)

        assert delta_result == full_result, (
            "after_message_id=None should produce identical output to build_context_xml"
        )
        assert delta_atts == full_atts, (
            "after_message_id=None should produce identical image attachments to build_context_xml"
        )

    @pytest.mark.asyncio
    async def test_delta_empty_when_no_new_messages(self) -> None:
        """Empty delta (no messages since watermark) yields empty thread_delta + user_query."""
        thread = _make_thread([])  # no messages since the watermark
        trigger = _make_message(msg_id=99, content="wake up")

        result, image_atts = await build_delta_xml(
            thread, trigger, after_message_id=50, bot_user_id=None
        )

        assert "<thread_delta>" in result, "should have thread_delta open tag"
        assert "</thread_delta>" in result, "should have thread_delta close tag"
        delta_start = result.index("<thread_delta>")
        delta_end = result.index("</thread_delta>")
        delta_body = result[delta_start + len("<thread_delta>") : delta_end]
        assert delta_body.strip() == "", "thread_delta body should be empty when no new messages"
        assert "<user_query" in result, "trigger should still appear in user_query"
        assert "wake up" in result, "trigger content should appear in output"
        assert image_atts == [], "empty delta should return no image attachments"

    @pytest.mark.asyncio
    async def test_delta_path_includes_bot_replies_and_excludes_trigger(self) -> None:
        """Delta path filter after Q1→(b) fix: post-watermark messages INCLUDING bot replies
        appear in thread_delta; only the trigger is excluded.

        Bot messages were previously filtered out (old behavior); Phase 88-05 removes
        that exclusion for cross-speaker coherence. The trigger exclusion is unchanged.
        """
        bot_msg = _make_message(
            msg_id=10,
            content="bot reply now included",
            author_name="Daimon",
            author_id=777,
            is_bot=True,
            timestamp=datetime(2026, 4, 28, 12, 1, 0, tzinfo=UTC),
        )
        human_post_watermark = _make_message(
            msg_id=11,
            content="post-watermark human message",
            author_name="Alice",
            timestamp=datetime(2026, 4, 28, 12, 2, 0, tzinfo=UTC),
        )
        trigger = _make_message(
            msg_id=99,
            content="trigger should be only in user_query",
            author_name="Bob",
            timestamp=datetime(2026, 4, 28, 12, 3, 0, tzinfo=UTC),
        )
        # thread.history returns post-watermark messages (discord's after= filter already applied)
        thread = _make_thread([bot_msg, human_post_watermark, trigger])

        result, _ = await build_delta_xml(thread, trigger, after_message_id=5, bot_user_id=777)

        delta_start = result.index("<thread_delta>")
        delta_end = result.index("</thread_delta>")
        delta_region = result[delta_start:delta_end]

        # Q1→(b): bot reply NOW appears in thread_delta for cross-speaker coherence.
        assert "bot reply now included" in delta_region, (
            "bot replies must appear in thread_delta after Phase 88-05 fix (Q1→b)"
        )
        # Trigger is still excluded from thread_delta.
        assert "trigger should be only in user_query" not in delta_region, (
            "trigger must not appear in thread_delta"
        )
        assert "post-watermark human message" in delta_region, (
            "post-watermark human messages must appear in thread_delta"
        )
        assert "<user_query" in result, "trigger must still appear in user_query"
        assert "trigger should be only in user_query" in result, (
            "trigger content must appear in user_query"
        )


class TestBuildChannelContextXml:
    """Tests for build_channel_context_xml() — channel-mention backfill builder."""

    @pytest.mark.asyncio
    async def test_channel_context_count_equals_backfilled_message_count(self) -> None:
        """count attribute equals number of non-trigger messages backfilled."""
        m1 = _make_message(
            msg_id=1, content="first", timestamp=datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
        )
        m2 = _make_message(
            msg_id=2, content="second", timestamp=datetime(2026, 4, 28, 12, 1, 0, tzinfo=UTC)
        )
        trigger = _make_message(
            msg_id=10, content="<@999> hello", timestamp=datetime(2026, 4, 28, 12, 2, 0, tzinfo=UTC)
        )
        channel = _make_text_channel([m1, m2, trigger])

        result, _ = await build_channel_context_xml(channel, trigger)

        assert 'count="2"' in result, "count attribute should equal number of backfilled messages"

    @pytest.mark.asyncio
    async def test_channel_context_trigger_excluded_from_channel_context(self) -> None:
        """Trigger message must not appear inside <channel_context>."""
        m1 = _make_message(msg_id=1, content="prior")
        trigger = _make_message(msg_id=10, content="trigger-content")
        channel = _make_text_channel([m1, trigger])

        result, _ = await build_channel_context_xml(channel, trigger)

        ctx_start = result.index("<channel_context")
        ctx_end = result.index("</channel_context>")
        ctx_region = result[ctx_start:ctx_end]
        assert "trigger-content" not in ctx_region, (
            "trigger must not appear inside <channel_context>"
        )
        assert "trigger-content" in result, "trigger must appear in <user_query>"

    @pytest.mark.asyncio
    async def test_channel_context_includes_bot_replies(self) -> None:
        """Bot replies ARE included in channel_context (unlike delta which filters them out)."""
        bot_msg = _make_message(
            msg_id=1,
            content="bot prior reply",
            is_bot=True,
            author_name="Daimon",
            author_id=777,
            timestamp=datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC),
        )
        trigger = _make_message(
            msg_id=10,
            content="user follow-up",
            timestamp=datetime(2026, 4, 28, 12, 1, 0, tzinfo=UTC),
        )
        channel = _make_text_channel([bot_msg, trigger])

        result, _ = await build_channel_context_xml(channel, trigger)

        assert "bot prior reply" in result, "bot replies must be included in channel_context"
        assert 'is_bot="true"' in result, "bot message should have is_bot=true attribute"
        assert 'count="1"' in result, "count should include the bot message"

    @pytest.mark.asyncio
    async def test_channel_context_messages_sorted_oldest_first(self) -> None:
        """Messages in <channel_context> are sorted oldest-first regardless of fetch order."""
        m_late = _make_message(
            msg_id=2, content="late", timestamp=datetime(2026, 4, 28, 12, 5, 0, tzinfo=UTC)
        )
        m_early = _make_message(
            msg_id=1, content="early", timestamp=datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
        )
        trigger = _make_message(
            msg_id=10, content="trigger", timestamp=datetime(2026, 4, 28, 12, 10, 0, tzinfo=UTC)
        )
        channel = _make_text_channel([m_late, m_early, trigger])

        result, _ = await build_channel_context_xml(channel, trigger)

        early_pos = result.index("early")
        late_pos = result.index("late")
        assert early_pos < late_pos, "early message should appear before late message"

    @pytest.mark.asyncio
    async def test_channel_context_trigger_content_in_user_query(self) -> None:
        """Trigger content appears only in <user_query>, not in <channel_context>."""
        m1 = _make_message(msg_id=1, content="prior message")
        trigger = _make_message(msg_id=10, content="<@999> hello there")
        channel = _make_text_channel([m1, trigger])

        result, _ = await build_channel_context_xml(channel, trigger, bot_user_id=999)

        assert "<user_query" in result, "user_query element must be present"
        # user_query must come after </channel_context>
        ctx_end = result.index("</channel_context>")
        uq_start = result.index("<user_query")
        assert uq_start > ctx_end, "user_query must appear after </channel_context>"
        # bot mention replaced in user_query content
        assert "@daimon" in result, "bot mention should be replaced with @daimon"

    @pytest.mark.asyncio
    async def test_channel_context_vision_image_attachments_collected(self) -> None:
        """Vision image attachments in backfill are returned in the tuple."""
        img = _make_attachment(
            filename="photo.png",
            url="https://cdn.discord.com/photo.png",
            content_type="image/png",
            size=1024,
        )
        pdf = _make_attachment(
            filename="doc.pdf",
            url="https://cdn.discord.com/doc.pdf",
            content_type="application/pdf",
            size=512,
        )
        m1 = _make_message(msg_id=1, content="files", attachments=[img, pdf])
        trigger = _make_message(msg_id=10, content="got it")
        channel = _make_text_channel([m1, trigger])

        _, image_atts = await build_channel_context_xml(channel, trigger)

        assert len(image_atts) == 1, "only the image/png attachment should be collected"
        assert image_atts[0] is img, "collected attachment should be the image object"

    @pytest.mark.asyncio
    async def test_channel_context_empty_channel_yields_count_zero(self) -> None:
        """Empty channel (trigger is only message) produces count=0 and no <message> elements."""
        trigger = _make_message(msg_id=10, content="hello")
        channel = _make_text_channel([trigger])

        result, image_atts = await build_channel_context_xml(channel, trigger)

        assert 'count="0"' in result, "empty channel should produce count=0"
        assert "<message" not in result.split("</channel_context>")[0], (
            "no message elements in empty channel_context"
        )
        assert "<user_query" in result, "user_query still present for empty channel"
        assert image_atts == [], "empty channel returns no image attachments"

    @pytest.mark.asyncio
    async def test_channel_backfill_limit_constant_is_twenty_five(self) -> None:
        """CHANNEL_BACKFILL_LIMIT module constant must be 25."""
        assert CHANNEL_BACKFILL_LIMIT == 25, "CHANNEL_BACKFILL_LIMIT must equal 25"
