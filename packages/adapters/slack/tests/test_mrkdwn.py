"""Tests for the mrkdwn entity-escaper.

Escape order is load-bearing:
  & → &amp;  FIRST (so the & in &lt;/&gt; is not double-escaped)
  < → &lt;
  > → &gt;

These tests verify the ordering invariant.
"""

from __future__ import annotations

from daimon.adapters.slack.mrkdwn import escape_mrkdwn, escape_mrkdwn_preserving_mentions


def test_plain_text_unchanged() -> None:
    """Text with no special chars is returned verbatim."""
    assert escape_mrkdwn("plain text") == "plain text", (
        "escape_mrkdwn must be a no-op when no special chars are present"
    )


def test_ampersand_escapes_to_entity() -> None:
    """& alone must become &amp;."""
    assert escape_mrkdwn("a & b") == "a &amp; b", "& must escape to &amp;"


def test_less_than_escapes_to_entity() -> None:
    """< must become &lt;."""
    assert escape_mrkdwn("x < y > z") == "x &lt; y &gt; z", (
        "< must escape to &lt; and > must escape to &gt;"
    )


def test_angle_brackets_and_ampersand_no_double_escape() -> None:
    """& inside angle brackets must not be double-escaped.

    Input:  <a & b>
    After & → &amp;:  <a &amp; b>
    After < → &lt;:  &lt;a &amp; b>
    After > → &gt;:  &lt;a &amp; b&gt;

    The & inside the angle brackets must appear as &amp;, not as &amp;amp;.
    """
    result = escape_mrkdwn("<a & b>")
    assert result == "&lt;a &amp; b&gt;", (
        "& must be escaped before < and > so the & in entities is not double-escaped; "
        f"got {result!r}"
    )


def test_only_greater_than() -> None:
    """> alone must become &gt;."""
    assert escape_mrkdwn("a > b") == "a &gt; b", "> must escape to &gt;"


def test_multiple_ampersands() -> None:
    """Multiple & chars are all escaped."""
    assert escape_mrkdwn("a & b & c") == "a &amp; b &amp; c", "all & chars must be escaped"


def test_empty_string() -> None:
    """Empty string is returned unchanged."""
    assert escape_mrkdwn("") == "", "empty string must round-trip unchanged"


def test_preserves_user_mention() -> None:
    """A well-formed <@ID> survives as a live mention."""
    assert escape_mrkdwn_preserving_mentions("hi <@U0BDWSMCB26>") == "hi <@U0BDWSMCB26>", (
        "user mention token must be restored after escaping so Slack renders it"
    )


def test_preserves_channel_link() -> None:
    """A well-formed <#ID> survives as a live channel link."""
    assert escape_mrkdwn_preserving_mentions("see <#C0BENGC6C2W>") == "see <#C0BENGC6C2W>", (
        "channel link token must be restored after escaping"
    )


def test_preserves_mention_with_label() -> None:
    """The <@ID|label> form is restored, label intact."""
    assert (
        escape_mrkdwn_preserving_mentions("<@U123|Joshua> and <#C123|general>")
        == "<@U123|Joshua> and <#C123|general>"
    ), "labeled mention/link forms must be restored"


def test_blocks_channel_broadcast() -> None:
    """<!channel> stays escaped — no mass ping."""
    assert escape_mrkdwn_preserving_mentions("hey <!channel>") == "hey &lt;!channel&gt;", (
        "broadcast token must remain escaped so the agent cannot mass-ping"
    )


def test_blocks_here_and_everyone_broadcasts() -> None:
    """<!here> and <!everyone> stay escaped."""
    result = escape_mrkdwn_preserving_mentions("<!here> <!everyone>")
    assert result == "&lt;!here&gt; &lt;!everyone&gt;", "here/everyone broadcasts must stay escaped"


def test_still_escapes_stray_brackets_and_ampersand() -> None:
    """Non-mention < > & are still escaped (injection-safe)."""
    assert escape_mrkdwn_preserving_mentions("a < b & c > d") == "a &lt; b &amp; c &gt; d", (
        "stray control chars must still be escaped"
    )


def test_escapes_arbitrary_tag() -> None:
    """An arbitrary <tag> is not a mention and stays escaped."""
    assert (
        escape_mrkdwn_preserving_mentions("<script>x</script>") == "&lt;script&gt;x&lt;/script&gt;"
    ), "arbitrary angle-bracket tags must stay escaped"


def test_literal_entity_text_is_not_falsely_restored() -> None:
    """Agent text that literally contains &lt;@U1&gt; must not become a mention.

    escape_mrkdwn turns the literal '&' into '&amp;', so the restore regex
    (which matches '&lt;') cannot fire on it.
    """
    assert escape_mrkdwn_preserving_mentions("&lt;@U1&gt;") == "&amp;lt;@U1&amp;gt;", (
        "pre-escaped literal entity text must not be mistaken for a real mention"
    )
