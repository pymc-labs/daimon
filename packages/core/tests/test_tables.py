import pytest
from daimon.core.tables import MarkdownTable, parse_tables, render_tables

TABLE = "| Name | Value |\n| --- | ---: |\n| a \\| b | 42 |\n"


def test_parser_preserves_text_and_escaped_pipes():
    parts = parse_tables("Before\n\n" + TABLE + "\nAfter\n")
    assert parts[0] == "Before\n\n"
    assert parts[-1] == "\nAfter\n"
    table = parts[1]
    assert isinstance(table, MarkdownTable)
    assert table.rows == (("a | b", "42"),)
    assert table.alignments == ("left", "right")
    assert table.raw == TABLE


@pytest.mark.parametrize("fence", ["```", "~~~~"])
def test_fenced_examples_stay_literal(fence):
    text = f"{fence}\n{TABLE}{fence}\n"
    assert parse_tables(text) == [text]


async def test_default_and_failing_hooks_preserve_original_text():
    text = "Before\n" + TABLE + "After"
    assert await render_tables(text) == [text]

    async def fail(table):
        raise ValueError("renderer unavailable")

    assert "".join(await render_tables(text, hook=fail)) == text


def test_oversized_table_stays_plain_text():
    row = "| " + " | ".join(["x"] * 21) + " |\n"
    separator = "| " + " | ".join(["---"] * 21) + " |\n"
    text = row + separator + row
    assert parse_tables(text) == [text]


def test_inline_code_pipe_is_not_a_column_boundary():
    table = parse_tables("| command | value |\n| --- | --- |\n| `a|b` | 1 |\n")[0]
    assert isinstance(table, MarkdownTable)
    assert table.rows[0] == ("`a|b`", "1")


def test_literal_backslashes_in_paths_are_not_lost():
    table = parse_tables("| Path | Value |\n| --- | --- |\n| C:\\data\\file.csv | 42 |\n")[0]
    assert isinstance(table, MarkdownTable)
    assert table.rows[0][0] == "C:\\data\\file.csv"
