"""Bounded Markdown table parsing with an optional platform rendering hook."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

Alignment = Literal["left", "center", "right"]


@dataclass(frozen=True)
class MarkdownTable:
    raw: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    alignments: tuple[Alignment, ...]


def _cells(line: str) -> tuple[str, ...]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    cells: list[str] = []
    cell = ""
    escaped = False
    code = False
    for char in line:
        if escaped:
            cell += char if char in "|\\`" else "\\" + char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "`":
            code = not code
            cell += char
        elif char == "|" and not code:
            cells.append(cell.strip())
            cell = ""
        else:
            cell += char
    cells.append((cell + ("\\" if escaped else "")).strip())
    return tuple(cells)


def parse_tables(text: str) -> list[str | MarkdownTable]:
    """Keep surrounding text and fenced code intact; retain raw fallback bytes."""
    lines = text.splitlines(keepends=True)
    result: list[str | MarkdownTable] = []
    plain: list[str] = []
    fence: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            plain.append(line)
            i += 1
            continue
        if fence is not None or "|" not in line or i + 1 >= len(lines):
            plain.append(line)
            i += 1
            continue
        headers, separators = _cells(line), _cells(lines[i + 1])
        if len(headers) != len(separators) or not all(
            re.fullmatch(r":?-{3,}:?", cell) for cell in separators
        ):
            plain.append(line)
            i += 1
            continue
        end = i + 2
        rows: list[tuple[str, ...]] = []
        while end < len(lines) and "|" in lines[end] and lines[end].strip():
            row = _cells(lines[end])
            if len(row) != len(headers):
                break
            rows.append(row)
            end += 1
        raw = "".join(lines[i:end])
        # Slack's native table limits also bound image allocation/work on Discord.
        if (
            len(headers) > 20
            or len(rows) > 99
            or sum(map(len, headers)) + sum(len(cell) for row in rows for cell in row) > 10000
        ):
            plain.append(raw)
            i = end
            continue
        if plain:
            result.append("".join(plain))
            plain = []
        alignments: list[Alignment] = []
        for column, separator in enumerate(separators):
            if separator.startswith(":") and separator.endswith(":"):
                alignments.append("center")
            elif separator.endswith(":") or (
                rows and all(re.fullmatch(r"[-+]?[$€£]?[\d,.]+%?", row[column]) for row in rows)
            ):
                alignments.append("right")
            else:
                alignments.append("left")
        result.append(MarkdownTable(raw, headers, tuple(rows), tuple(alignments)))
        i = end
    if plain:
        result.append("".join(plain))
    return result


async def render_tables[T](
    text: str, *, hook: Callable[[MarkdownTable], Awaitable[T]] | None = None
) -> list[str | T]:
    """No hook means exact passthrough. A failed hook falls back to the raw table.

    At most ten tables are rendered in one answer; further tables remain text.
    Cancellation propagates. Adapters own their native output types and delivery.
    """
    if hook is None:
        return [text]
    output: list[str | T] = []
    rendered = 0
    for part in parse_tables(text):
        if isinstance(part, str):
            output.append(part)
        elif rendered >= 10:
            output.append(part.raw)
        else:
            try:
                output.append(await hook(part))
                rendered += 1
            except Exception:
                output.append(part.raw)
    return output
