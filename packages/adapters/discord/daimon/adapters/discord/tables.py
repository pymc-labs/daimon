"""Off-thread PNG table renderer using the bundled PyMC Labs Inter faces."""

from __future__ import annotations

import asyncio
import io
from functools import cache
from pathlib import Path
from typing import cast

from daimon.core.tables import MarkdownTable, render_tables
from fontTools.ttLib import TTFont  # pyright: ignore[reportMissingTypeStubs]
from PIL import Image, ImageDraw, ImageFont

import discord

_FONTS = Path(__file__).with_name("fonts")


@cache
def _supported_codepoints(face: str) -> frozenset[int]:
    with TTFont(_FONTS / face) as font:
        cmap = cast(dict[int, str], font.getBestCmap())
        return frozenset(cmap)


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if current and font.getlength(candidate) > width:
            space = candidate.rfind(" ")
            if space > 0:
                lines.append(candidate[:space])
                current = candidate[space + 1 :]
            else:
                lines.append(current)
                current = char
        else:
            current = candidate
    return [*lines, current]


def table_png(table: MarkdownTable) -> bytes:
    """Wrap every cell without clipping; reject excessive pixel allocation."""
    for index, row in enumerate((table.headers, *table.rows)):
        face = "Inter-Bold.ttf" if index == 0 else "Inter-Regular.ttf"
        supported = _supported_codepoints(face)
        if any(ord(char) not in supported for cell in row for char in cell):
            raise ValueError("table contains glyphs unavailable in the bundled font")
    regular = ImageFont.truetype(str(_FONTS / "Inter-Regular.ttf"), 18)
    bold = ImageFont.truetype(str(_FONTS / "Inter-Bold.ttf"), 18)
    width = min(300, 2400 // len(table.headers))
    cells = (table.headers, *table.rows)
    wrapped = [
        [_wrap(cell, bold if index == 0 else regular, width - 24) for cell in row]
        for index, row in enumerate(cells)
    ]
    heights = [max(map(len, row)) * 25 + 24 for row in wrapped]
    total_width, total_height = width * len(table.headers), sum(heights)
    if total_width * total_height > 12_000_000:
        raise ValueError("table image exceeds pixel budget")
    image = Image.new("RGB", (total_width, total_height), "white")
    draw = ImageDraw.Draw(image)
    y = 0
    for index, (row, height) in enumerate(zip(wrapped, heights, strict=True)):
        draw.rectangle(
            (0, y, total_width, y + height),
            fill="#0C1F40" if index == 0 else ("#F7F7F7" if index % 2 else "#FFFFFF"),
        )
        font = bold if index == 0 else regular
        for column, lines in enumerate(row):
            for offset, line in enumerate(lines):
                x = column * width + 12
                if table.alignments[column] == "right":
                    x = (column + 1) * width - 12 - font.getlength(line)
                elif table.alignments[column] == "center":
                    x = column * width + (width - font.getlength(line)) / 2
                draw.text(
                    (x, y + 12 + offset * 25),
                    line,
                    font=font,
                    fill="#FFFFFF" if index == 0 else "#0C1F40",
                )
        y += height
        draw.line((0, y - 1, total_width, y - 1), fill="#B4E7DD", width=1)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def render_discord_tables(
    text: str, *, enabled: bool = False
) -> tuple[str, list[discord.File]]:
    async def hook(table: MarkdownTable) -> bytes:
        return await asyncio.to_thread(table_png, table)

    parts = await render_tables(text, hook=hook if enabled else None)
    rendered: list[str] = []
    files: list[discord.File] = []
    for part in parts:
        if isinstance(part, str):
            rendered.append(part)
        else:
            name = f"table-{len(files) + 1}.png"
            files.append(discord.File(io.BytesIO(part), filename=name))
            rendered.append(f"[Table attached: {name}]\n")
    return "".join(rendered), files
