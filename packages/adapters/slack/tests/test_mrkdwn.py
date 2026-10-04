"""Tests for the mrkdwn entity-escaper.

Escape order is load-bearing:
  & → &amp;  FIRST (so the & in &lt;/&gt; is not double-escaped)
  < → &lt;
  > → &gt;

These tests verify the ordering invariant.
"""

from __future__ import annotations

import pytest
from daimon.adapters.slack.mrkdwn import (
    escape_markdown_block,
    escape_mrkdwn,
    escape_mrkdwn_preserving_mentions,
    linkify_emphasized_urls,
    markdown_block_fallback,
    prose_mentions,
    seal_markdown_block,
)


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


# ---------------------------------------------------------------------------
# linkify_emphasized_urls — bare URLs wrapped in emphasis
# ---------------------------------------------------------------------------


def test_linkify_bold_wrapped_bare_url_becomes_markdown_link() -> None:
    """A bare URL immediately before a closing ** must be rewritten to a
    [url](url) markdown link, so Slack's autolinker cannot absorb the
    asterisks into the link target (reported: notebook URL rendered with a
    trailing '*')."""
    text = "Here's the notebook: **🔗 https://x.up.railway.app/n/abc**"
    expected = (
        "Here's the notebook: "
        "**🔗 [https://x.up.railway.app/n/abc](https://x.up.railway.app/n/abc)**"
    )
    assert linkify_emphasized_urls(text) == expected, (
        "URL followed by ** must be converted to an explicit markdown link"
    )


def test_linkify_single_asterisk_wrapped_url() -> None:
    text = "see *https://example.com/a* now"
    assert linkify_emphasized_urls(text) == (
        "see *[https://example.com/a](https://example.com/a)* now"
    ), "URL followed by a single * must also be converted"


def test_linkify_plain_url_unchanged() -> None:
    text = "see https://example.com/a for details"
    assert linkify_emphasized_urls(text) == text, (
        "a bare URL not followed by emphasis must be left untouched"
    )


def test_linkify_existing_markdown_link_unchanged() -> None:
    text = "**[notebook](https://example.com/a)**"
    assert linkify_emphasized_urls(text) == text, (
        "a URL already inside a [label](url) link must not be rewritten"
    )


def test_linkify_url_with_interior_asterisk_only_strips_trailing_emphasis() -> None:
    text = "**https://example.com/a*b**"
    assert linkify_emphasized_urls(text) == (
        "**[https://example.com/a*b](https://example.com/a*b)**"
    ), "asterisks inside the URL path stay in the URL; only trailing emphasis is excluded"


def test_linkify_url_in_parentheses() -> None:
    text = "(see **https://example.com/a**)"
    assert linkify_emphasized_urls(text) == (
        "(see **[https://example.com/a](https://example.com/a)**)"
    ), "an emphasis closer followed by ')' must still trigger linkification"


def test_linkify_url_followed_by_quote_and_dash() -> None:
    assert linkify_emphasized_urls('"**https://example.com/a**"') == (
        '"**[https://example.com/a](https://example.com/a)**"'
    ), "an emphasis closer followed by a quote must still trigger linkification"
    assert linkify_emphasized_urls("**https://example.com/a**—done") == (
        "**[https://example.com/a](https://example.com/a)**—done"
    ), "an emphasis closer followed by an em-dash must still trigger linkification"


def test_linkify_leaves_four_asterisk_runs_alone() -> None:
    text = "**https://example.com/a****"
    assert linkify_emphasized_urls(text) == text, (
        "a 4+ asterisk run is not a 1-3 emphasis closer; the URL must not be "
        "rewritten (previously the regex backtracked an asterisk into the URL)"
    )


def test_linkify_skips_fenced_code_blocks() -> None:
    text = "before\n```\n**https://example.com/a**\n```\nafter **https://example.com/b**"
    assert linkify_emphasized_urls(text) == (
        "before\n```\n**https://example.com/a**\n```\n"
        "after **[https://example.com/b](https://example.com/b)**"
    ), "text inside a fenced code block must not be rewritten"


def test_linkify_skips_inline_code_spans() -> None:
    text = "use `**https://example.com/a** ` verbatim"
    assert linkify_emphasized_urls(text) == text, (
        "text inside an inline code span must not be rewritten"
    )


def test_linkify_treats_unterminated_fence_as_code() -> None:
    text = "```\n**https://example.com/a**"
    assert linkify_emphasized_urls(text) == text, (
        "an opened-but-unclosed fence renders as code in Slack; its content must not be rewritten"
    )


def test_linkify_leaves_an_emphasized_url_link_label_alone() -> None:
    text = "[**https://example.com/a**](https://example.com/a)"
    assert linkify_emphasized_urls(text) == text, (
        "an emphasized URL that is already a [label](url) link's label must not be "
        "rewritten into a link nested inside that link"
    )


def test_linkify_keeps_trailing_sentence_punctuation_out_of_the_link() -> None:
    assert linkify_emphasized_urls("**see https://example.com/a.**") == (
        "**see [https://example.com/a](https://example.com/a).**"
    ), "a period before the emphasis closer is sentence punctuation, not part of the URL"
    assert linkify_emphasized_urls("**is it https://example.com/a?**") == (
        "**is it [https://example.com/a](https://example.com/a)?**"
    ), "a question mark before the emphasis closer must stay outside the link"


def test_linkify_keeps_query_punctuation_inside_the_url() -> None:
    assert linkify_emphasized_urls("**https://example.com/a?b=1&c=2**") == (
        "**[https://example.com/a?b=1&c=2](https://example.com/a?b=1&c=2)**"
    ), "punctuation inside the URL (before its last character) stays in the link"


# ---------------------------------------------------------------------------
# Code-aware escaping for markdown blocks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "`<a> & é 日本 &lt;`",
        "``a `<b>` c``",
        "```py\nif a < b and c > d: pass\n```",
        "~~~\n<!channel> &amp;\n~~~",
        "- item\n\n  ```\n  <a>\n  ```",
        "```\n<unterminated>",
        "1. step\n\n   ```bash\n   echo <a>\n   ```",
        "```x<a>``` as inline code\n\n```\n<b>\n```",
    ],
)
def test_markdown_block_sends_code_as_written(code: str) -> None:
    assert escape_markdown_block(code) == code, "code is displayed verbatim, entities included"


def test_markdown_block_escapes_prose_around_code() -> None:
    assert escape_markdown_block("a < b `a < b` <!here> <@U1>") == (
        "a &lt; b `a < b` &lt;!here&gt; <@U1>"
    ), "prose keeps mrkdwn escaping and its user mentions"


def test_markdown_block_can_drop_mentions_in_prose() -> None:
    assert escape_markdown_block("<@U1> `<@U1>`", preserve_mentions=False) == (
        "&lt;@U1&gt; `<@U1>`"
    ), "without preserve_mentions only code keeps its brackets"


def test_a_stray_backtick_before_a_blank_line_leaves_later_code_alone() -> None:
    assert escape_markdown_block("a stray ` <x>\n\n`<y>`") == "a stray ` &lt;x&gt;\n\n`<y>`", (
        "an inline span never crosses a blank line"
    )


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("a ` b\n`<!channel>` c`", "an unpaired run on an earlier line may pair with this one"),
        ("\\`<!channel>`", "a backslash-escaped backtick does not open a span"),
        ("see https://example.com`<!channel>`", "GFM's autolinker takes the backtick"),
        ("[a](x`<!channel>`)", "a link destination takes the backtick"),
        ("[a\n](`<!channel>`)", "a link destination takes the backtick"),
        ("[a]: `<!channel>`", "a reference definition is not inline text"),
        ("x\n2. ```\n<!channel>\n```", "only an item numbered 1 interrupts a paragraph"),
        ("1. ```\n  ```\n```\n<!channel>", "a closer left of the content opens a new fence"),
        ("```\nx\n```\r<!channel>\n```", "a carriage return ends a line"),
        ("\xa0\n2) ```\n<!channel>\n```", "a no-break space makes a line non-blank"),
        ("para\n    ```\n    <!channel>\n    ```", "an indented fence continues the paragraph"),
        ("- item\n  ```\n<!channel>\n  ```", "a less indented line ends the list item"),
        ("| a |\n| --- |\n| `x|<!channel>|y` |", "table cells split on pipes inside code"),
        ("- ```\n  x\n  ```\n  ok <!channel>", "a list item's fence closes at its own indent"),
        ("> ```\n> x\n> ```\n```\n<!channel>\n```", "a quoted fence's closer may pair with ours"),
    ],
)
def test_markdown_block_escapes_what_slack_may_not_read_as_code(text: str, why: str) -> None:
    assert "<!channel>" not in escape_markdown_block(text), why


def test_fallback_escapes_code_and_keeps_prose_mentions() -> None:
    block = escape_markdown_block("<@U1> see `<!channel> <@U2> &lt;` & <#C1|general>")
    assert markdown_block_fallback(block) == (
        "<@U1> see `&lt;!channel&gt; &lt;@U2&gt; &amp;lt;` &amp; <#C1|general>"
    ), "mrkdwn parses code in the fallback, so only prose mentions stay live"


def test_fallback_escapes_stray_raw_angles_outside_detected_code() -> None:
    assert markdown_block_fallback("```\n<!here>\n") == "```\n&lt;!here&gt;\n"
    assert markdown_block_fallback("x <!here> y") == "x &lt;!here&gt; y"


def test_seal_escapes_what_a_split_moved_out_of_its_fence() -> None:
    assert seal_markdown_block("<a>\n```", mentions=frozenset()) == "&lt;a&gt;\n```", (
        "a chunk holding only the tail of a fenced block shows that tail as prose"
    )


def test_seal_keeps_code_and_existing_entities() -> None:
    assert seal_markdown_block("&lt; `<a> &lt;`", mentions=frozenset()) == "&lt; `<a> &lt;`", (
        "sealing is idempotent on escaped prose and leaves code alone"
    )


def test_seal_keeps_only_listed_mentions() -> None:
    text = "<@U1AUTHOR>\n<@U2> <#C1|general> <!here>"
    assert seal_markdown_block(text, mentions={"<@U1AUTHOR>"}) == (
        "<@U1AUTHOR>\n&lt;@U2&gt; &lt;#C1|general&gt; &lt;!here&gt;"
    ), "a mention not listed is escaped, however well-formed"


def test_prose_mentions_ignore_mentions_in_code() -> None:
    assert prose_mentions("<@U1>\r\n`<@U2>` <#C1|x>\n```\n<@U3>\n```") == {"<@U1>", "<#C1|x>"}, (
        "mentions inside inline code or a fence are not live"
    )
