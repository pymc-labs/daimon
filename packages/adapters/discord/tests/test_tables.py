import io

from daimon.adapters.discord.tables import render_discord_tables
from PIL import Image


async def test_wide_table_is_a_readable_bounded_png():
    headers = [f"Column {i} wide heading" for i in range(12)]
    table = "| " + " | ".join(headers) + " |\n"
    table += "| " + " | ".join(["---"] * 12) + " |\n"
    table += "| " + " | ".join(["123.45"] * 12) + " |\n"
    text, files = await render_discord_tables("Before\n" + table + "After", enabled=True)
    assert "Before" in text and "After" in text
    assert "| ---" not in text
    assert len(files) == 1
    image = Image.open(io.BytesIO(files[0].fp.read()))
    assert image.format == "PNG"
    assert image.width == 2400
    assert image.height >= 98
    assert image.getpixel((0, 0)) == (12, 31, 64)
    assert "table-1.png" in text


async def test_non_table_text_is_unchanged():
    assert await render_discord_tables("ordinary **answer**") == ("ordinary **answer**", [])


async def test_disabled_rendering_keeps_table_text():
    table = "| A | B |\n| --- | --- |\n| a | b |"
    assert await render_discord_tables(table) == (table, [])


async def test_unsupported_glyphs_preserve_distinct_original_values():
    outputs = []
    for value in ("中文", "日本"):
        table = f"| Value |\n| --- |\n| {value} |\n"
        text, files = await render_discord_tables(table, enabled=True)
        assert text == table
        assert value in text
        assert files == []
        outputs.append(text)
    assert outputs[0] != outputs[1]


async def test_unsupported_header_preserves_table_but_supported_neighbor_renders():
    raw = "| 中文 |\n| --- |\n| 123 |\n"
    supported = "| Value |\n| --- |\n| 456 |\n"
    text, files = await render_discord_tables(raw + "\n" + supported, enabled=True)
    assert raw in text
    assert len(files) == 1
