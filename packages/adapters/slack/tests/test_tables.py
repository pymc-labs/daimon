import pytest
from daimon.adapters.slack.tables import render_slack_tables


async def test_slack_url_links_render_as_standard_markdown_without_enabling_broadcasts():
    parts = await render_slack_tables("See <https://example.com|example.com> <!channel>")
    assert parts[0][1]["text"] == "See [example.com](https://example.com) &lt;!channel&gt;"


@pytest.mark.parametrize(
    "text",
    [
        "`<https://example.com|label>`",
        "``<https://example.com|label>``",
        "```\n<https://example.com|label>\n```",
        "~~~\n<https://example.com|label>\n~~~",
    ],
)
async def test_slack_link_examples_in_code_are_sent_as_written(text):
    parts = await render_slack_tables(text)
    assert parts[0][1]["text"] == text, (
        "Slack shows entities inside code literally, so code must not be escaped"
    )


async def test_code_and_prose_forms_of_one_link_render_differently():
    link = "<https://example.com|label>"
    parts = await render_slack_tables(f"{link} and `{link}`")
    assert parts[0][1]["text"] == f"[label](https://example.com) and `{link}`"


async def test_literal_entity_text_in_code_is_not_decoded():
    parts = await render_slack_tables("`&lt;` and &lt;")
    assert parts[0][1]["text"] == "`&lt;` and &amp;lt;"


async def test_mentions_in_code_reach_slack_only_inside_code():
    parts = await render_slack_tables("`<@U123> <!channel>` <!channel>")
    fallback, block = parts[0]
    assert block["text"] == "`<@U123> <!channel>` &lt;!channel&gt;"
    assert fallback == block["text"], "the chunk stays the block text the lifecycle re-sends"


async def test_table_markdown_kept_for_a_rejected_table_leaves_code_unescaped():
    text = "| Code | Person |\n| --- | --- |\n| `a<b` | <@U123> |"
    parts = await render_slack_tables(text, enabled=True)
    fallback, block = parts[0]
    assert block["type"] == "table"
    assert "`a<b`" in fallback and "&lt;@U123&gt;" in fallback


async def test_an_unclosed_backtick_does_not_shield_links_in_later_paragraphs():
    parts = await render_slack_tables("a stray ` here\n\nsee <https://example.com|docs>\n\n`x`")
    assert "[docs](https://example.com)" in parts[0][1]["text"], (
        "an inline code span ends at a blank line, so the next paragraph's link renders"
    )


async def test_wide_table_uses_native_wrapped_cells_between_prose():
    headers = [f"Column {i}" for i in range(12)]
    table = "| " + " | ".join(headers) + " |\n"
    table += "| " + " | ".join(["---"] * 12) + " |\n"
    table += "| " + " | ".join(["123"] * 12) + " |\n"
    parts = await render_slack_tables("Before\n" + table + "After", enabled=True)
    assert [block["type"] for _, block in parts] == ["markdown", "table", "markdown"]
    block = parts[1][1]
    assert len(block["rows"]) == 2
    assert len(block["rows"][0]) == 12
    assert block["rows"][0][11]["text"] == "Column 11"
    assert all(
        column == {"align": "right", "is_wrapped": True} for column in block["column_settings"]
    )


async def test_plain_message_preserves_existing_mentions():
    parts = await render_slack_tables("Hello <@U123>")
    assert parts == [("Hello <@U123>", {"type": "markdown", "text": "Hello <@U123>"})]


async def test_disabled_rendering_keeps_table_text():
    table = "| A | B |\n| --- | --- |\n| a | b |"
    assert await render_slack_tables(table) == [(table, {"type": "markdown", "text": table})]


async def test_table_notification_fallback_preserves_data_without_mentions():
    text = "| Person | Value |\n| --- | --- |\n| <@U123> | 42 |"
    parts = await render_slack_tables(text, enabled=True)
    fallback, block = parts[0]
    assert "42" in fallback and "&lt;@U123&gt;" in fallback
    assert block["rows"][1][0]["text"] == "<@U123>"


async def test_prose_urls_are_linkified_but_table_cells_are_not():
    text = (
        "See **https://example.com/a**\n"
        "| Link |\n| --- |\n| **https://example.com/b** |\n"
        "Then **https://example.com/c**"
    )
    parts = await render_slack_tables(text, enabled=True)
    assert [block["type"] for _, block in parts] == ["markdown", "table", "markdown"]
    assert "[https://example.com/a](https://example.com/a)" in parts[0][1]["text"], (
        "prose before the table must have its emphasized URL linkified"
    )
    assert parts[1][1]["rows"][1][0]["text"] == "**https://example.com/b**", (
        "a table cell is raw_text: rewriting it would show the markdown literally"
    )
    assert "[https://example.com/c](https://example.com/c)" in parts[2][1]["text"], (
        "prose after the table must have its emphasized URL linkified"
    )


async def test_long_answer_keeps_code_unescaped_in_every_chunk():
    code = "<https://example.com|label>\n" * 800
    parts = await render_slack_tables(f"Intro <!channel>\n\n```\n{code}```")
    assert len(parts) > 1, "the answer should need several messages"
    assert "&lt;!channel&gt;" in parts[0][1]["text"]
    code_chunks = [block["text"] for _, block in parts[1:]]
    assert len(code_chunks) > 1, "the fenced block itself should span several messages"
    for chunk in code_chunks:
        assert chunk.startswith("```") and chunk.endswith("```"), "each chunk repairs the fence"
        assert "&lt;https" not in chunk, "code stays raw across the split"


async def test_a_mention_in_code_stays_inert_when_a_split_leaves_it_outside_its_fence():
    code = "x\n" * 6000 + "<@U123>\n"
    parts = await render_slack_tables(f"Hi <@U9>\n\n~~~\n{code}~~~")
    texts = [block["text"] for _, block in parts]
    assert len(texts) > 1, "the tilde fence should span several messages"
    assert "<@U9>" in texts[0], "a mention written in prose stays live"
    assert not any("<@U123>" in text for text in texts[1:]), (
        "a tilde fence is not repaired across a split, so its tail is prose and "
        "a mention written in code must be escaped there"
    )
