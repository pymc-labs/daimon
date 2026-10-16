"""Dollar signs in Teams Extended Markdown, shared by the Teams adapter and the MCP server. Pure.

Extended Markdown renders `$…$` and `$$…$$` as KaTeX math, so an answer
quoting prices ("$5 and $10") could turn into a formula. Every `$` that does
not open or close a math span by pandoc's `tex_math_dollars` rules is escaped
as `\\$`: an opening `$` is followed by a non-space, a closing one follows a
non-space and is not followed by a digit, no `$` inside follows a space, and
the span ends at a blank line. Code spans, fences and `<scheme:…>` autolinks
are left as they are: CommonMark reads no backslash escape in any of them.
"""

from __future__ import annotations

import re

__all__ = ["escape_dollars"]

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_TICKS = re.compile(r"`+")
# CommonMark's URI autolink: a 2-32 character scheme, then no space, `<` or `>`.
_AUTOLINK = re.compile(r"<[A-Za-z][A-Za-z0-9+.-]{1,31}:[^\x00-\x20<>]*>")


def _inline_end(text: str, start: int) -> int | None:
    """Where the `$…$` span opened at `start` ends (its closing `$`), or None."""
    if start + 1 >= len(text) or text[start + 1].isspace() or text[start + 1] == "$":
        return None
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
        elif char == "$":
            following = text[index + 1 : index + 2]
            return None if following.isdigit() else index
        elif char.isspace():
            gap = index
            while index < len(text) and text[index].isspace():
                index += 1
            if text.count("\n", gap, index) > 1 or text[index : index + 1] == "$":
                return None
        else:
            index += 1
    return None


def _escape_prose(text: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            out.append(text[index : index + 2])
            index += 2
        elif char == "<" and (link := _AUTOLINK.match(text, index)) is not None:
            out.append(link.group())
            index = link.end()
        elif char != "$":
            out.append(char)
            index += 1
        elif text.startswith("$$", index):
            end = text.find("$$", index + 2)
            if end > index + 2:
                out.append(text[index : end + 2])
                index = end + 2
            else:
                out.append("\\$\\$")
                index += 2
        elif (end := _inline_end(text, index)) is not None:
            out.append(text[index : end + 1])
            index = end + 1
        else:
            out.append("\\$")
            index += 1
    return "".join(out)


def _escape_outside_code_spans(text: str) -> str:
    out: list[str] = []
    prose_start = index = 0
    while (match := _TICKS.search(text, index)) is not None:
        ticks = match.group()
        close = re.compile(rf"(?<!`){ticks}(?!`)").search(text, match.end())
        if close is None:
            index = match.end()  # An unmatched run is literal text.
            continue
        out.append(_escape_prose(text[prose_start : match.start()]))
        out.append(text[match.start() : close.end()])
        prose_start = index = close.end()
    out.append(_escape_prose(text[prose_start:]))
    return "".join(out)


def escape_dollars(text: str) -> str:
    """`text` with every `$` outside math spans and code escaped as `\\$`."""
    out: list[str] = []
    prose: list[str] = []
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        match = _FENCE.match(line)
        if fence is None and match is not None:
            out.append(_escape_outside_code_spans("".join(prose)))
            prose = []
            fence = match.group(1)
            out.append(line)
        elif fence is not None:
            out.append(line)
            # A closing fence carries no info string: "```python" inside stays code.
            if (
                match is not None
                and match.group(1)[0] == fence[0]
                and len(match.group(1)) >= len(fence)
                and not line[match.end() :].strip()
            ):
                fence = None
        else:
            prose.append(line)
    out.append(_escape_outside_code_spans("".join(prose)))
    return "".join(out)
