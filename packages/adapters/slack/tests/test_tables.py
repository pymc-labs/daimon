from daimon.adapters.slack.tables import render_slack_tables


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
