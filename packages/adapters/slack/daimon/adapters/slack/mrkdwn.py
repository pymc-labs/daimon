"""mrkdwn entity-escaping for Slack, and the code-aware escaping of answers
sent as native ``markdown`` blocks.

Slack's mrkdwn format uses three HTML entities to prevent literal text from
being interpreted as links or mentions. The escape order is **load-bearing**:

  1. ``&`` → ``&amp;``  — MUST be first; if < or > are replaced first, the
     ``&`` already present in ``&lt;``/``&gt;`` would get double-escaped.
  2. ``<`` → ``&lt;``
  3. ``>`` → ``&gt;``

This module is stdlib-only — no ``slack_sdk``, ``anthropic``, or ``daimon.core``
imports. It forms part of the functional-core rendering layer.

Reference: https://docs.slack.dev/messaging/formatting-message-text
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection


def escape_mrkdwn(text: str) -> str:
    """Escape Slack mrkdwn control characters in *text*.

    Replaces ``&``, ``<``, and ``>`` with their HTML entity equivalents so
    that agent-generated text containing these characters renders literally
    rather than being interpreted as Slack links, mentions, or entities.

    Args:
        text: Raw agent text to escape.

    Returns:
        The escaped string safe for insertion into a Slack mrkdwn text field.
    """
    # & MUST be replaced first — otherwise the & in &lt;/&gt; would itself
    # be escaped on a subsequent pass, producing &amp;lt; / &amp;gt;.
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    return text


# Matches an escaped, well-formed user (@) or channel (#) token AFTER escape_mrkdwn
# has run: "&lt;@U123&gt;", "&lt;#C123|label&gt;". The label (optional) contains no
# entity/bracket chars, so it stops before the closing "&gt;". Only @ and # prefixes
# match — "&lt;!channel&gt;" and arbitrary tags are left escaped/literal.
_ESCAPED_MENTION = re.compile(r"&lt;([@#][A-Z0-9]+(?:\|[^&<>]*)?)&gt;")


# A bare http(s) URL whose very next characters are 1-3 asterisks followed by
# anything that cannot continue a URL or the asterisk run — i.e. an emphasis
# closer, not part of the URL. The URL must not itself end in an asterisk, so
# a 4+ asterisk run (not a valid closer) never donates its head to the URL.
# Trailing sentence punctuation between the URL and the closer stays outside
# the link, as GFM's autolinker leaves it: ``**see https://x/a.**`` links
# ``https://x/a``, not ``https://x/a.``. The URL charset excludes ()[]<> and
# whitespace so URLs already inside [label](url) links (which end at the ')')
# never match.
_EMPHASIZED_URL = re.compile(
    r"(https?://[^\s<>()\[\]]*?[^\s<>()\[\]*.,:;!?'\"])"
    r"(?=[.,:;!?'\"]*\*{1,3}(?:[^\w*]|$))"
)

# Segments the linkifier must never touch: a fenced block (closed, or
# open-to-end — Slack renders an unterminated fence as code), an inline
# backtick span, or an existing ``[label](url)`` link — its label may
# itself be an emphasized URL, and rewriting it would nest a link in a link.
# An inline span may wrap lines but, as in CommonMark, never crosses a blank
# line: a stray backtick would otherwise shield every paragraph up to the next.
# Looser than code_spans, which decides what goes out unescaped: a span wrongly
# shielded here only leaves a link unconverted.
_VERBATIM_SEGMENT = re.compile(
    r"(?P<fence>`{3,}|~{3,})[\s\S]*?(?:(?P=fence)|$)"
    r"|(?<!`)(?P<ticks>`+)(?!`)(?:(?!\n[ \t]*\n)[\s\S])*?(?P=ticks)(?!`)"
    r"|\[[^\]\n]*\]\([^)\s]*\)"
)
_SLACK_URL_LINK = re.compile(r"<(https?://[^\s<>|]+)(?:\|([^<>\n]*))?>")


def normalize_slack_url_links(text: str) -> str:
    """Convert Slack URL entities to standard Markdown outside code and links."""

    def rewrite(segment: str) -> str:
        def replace(match: re.Match[str]) -> str:
            raw_url = match.group(1)
            url = raw_url.translate(
                str.maketrans(
                    {
                        "(": "%28",
                        ")": "%29",
                        "[": "%5B",
                        "]": "%5D",
                        "\\": "%5C",
                    }
                )
            )
            label = match.group(2) or raw_url
            label = re.sub(r"([\\`*_[\]])", r"\\\1", label)
            return f"[{label}]({url})"

        return _SLACK_URL_LINK.sub(replace, segment)

    parts: list[str] = []
    last = 0
    for verbatim in _VERBATIM_SEGMENT.finditer(text):
        parts.append(rewrite(text[last : verbatim.start()]))
        parts.append(verbatim.group(0))
        last = verbatim.end()
    parts.append(rewrite(text[last:]))
    return "".join(parts)


def linkify_emphasized_urls(text: str) -> str:
    """Rewrite bare URLs that sit against an emphasis closer to ``[url](url)``.

    In Slack's native ``markdown`` block the bare-URL autolinker is greedy and
    ``*`` is a valid URL character, so ``**https://x**`` renders with the
    closing asterisks absorbed into the link target (broken link with a
    trailing ``*``, unclosed bold). Making the link explicit removes the
    ambiguity while leaving the surrounding emphasis intact.

    Fenced code blocks and inline code spans are passed through verbatim —
    emphasis has no meaning there and the content must render exactly as
    written. So are existing ``[label](url)`` links, which are already
    explicit.
    """

    def _rewrite(segment: str) -> str:
        return _EMPHASIZED_URL.sub(lambda m: f"[{m.group(1)}]({m.group(1)})", segment)

    parts: list[str] = []
    last = 0
    for verbatim in _VERBATIM_SEGMENT.finditer(text):
        parts.append(_rewrite(text[last : verbatim.start()]))
        parts.append(verbatim.group(0))
        last = verbatim.end()
    parts.append(_rewrite(text[last:]))
    return "".join(parts)


def escape_mrkdwn_preserving_mentions(text: str) -> str:
    """Escape mrkdwn control chars but keep live ``<@user>`` / ``<#channel>`` links.

    Runs :func:`escape_mrkdwn` (so all ``& < >`` become entities and no literal
    text can be interpreted as a link), then restores only well-formed user and
    channel mention tokens the agent emitted. Broadcast tokens
    (``<!channel>``/``<!here>``/``<!everyone>``) and arbitrary ``<tag>`` sequences
    stay escaped, so the agent can mention people and channels but cannot mass-ping
    or inject arbitrary Slack entities.
    """
    escaped = escape_mrkdwn(text)
    return _ESCAPED_MENTION.sub(lambda m: f"<{m.group(1)}>", escaped)


# A fence opener at a line start, after spaces and at most one list marker.
# ``indent`` is the column its content lines must keep.
_FENCE_OPEN = re.compile(
    r"(?P<indent>(?P<leading>[ ]*)(?:(?P<marker>[-+*]|(?P<number>\d{1,9})[.)])[ ]+)?)"
    r"(?P<fence>(?P<char>[`~])(?P=char){2,})(?P<info>.*)"
)
# A line that may open or close a fence Slack sees, in a blockquote or after
# nested markers included. One _fence_end cannot place stops later fences from
# counting as code.
_FENCE_LIKE = re.compile(r"[ >*+\-\d.)]*(?:`{3,}[^`]*|~{3,}.*)")
# A single-line inline code span. The opener must not be backslash-escaped or
# the tail of a longer run, and the closer is a run of exactly the same length,
# which pairs runs left to right as CommonMark does.
_INLINE_CODE = re.compile(r"(?<![`\\])(?P<ticks>`+)(?!`).*?(?<!`)(?P=ticks)(?!`)")
# A bare URL; GFM's autolinker runs it to the next space or tab.
_BARE_URL = re.compile(r"(?:://|www\.)(?P<rest>[^ \t]*)")
# A link destination holding a title or a nested parenthesis.
_DESTINATION_TITLE = re.compile(r"\]\([^)`]*?[\"'(]")
# A GFM table delimiter row. Table cells split on ``|`` before code spans are
# parsed, so a ``|`` inside a span in a table splits the span.
_TABLE_DELIMITER = re.compile(r"[ ]*\|?[ ]*:?-+:?[ ]*(?:\|[ ]*:?-+:?[ ]*)*\|?[ ]*")
# A user or channel mention, or any other raw angle bracket.
_RAW_MENTION_OR_ANGLE = re.compile(r"<[@#][A-Z0-9]+(?:\|[^&<>]*)?>|[<>]")


def _blank(line: str) -> bool:
    # CommonMark blank lines hold only spaces and tabs; str.strip() would also
    # drop no-break and other Unicode spaces, which make a line non-blank.
    return not line.strip(" \t")


def _fence_end(lines: list[str], start: int) -> tuple[int, bool] | None:
    """Where the fence opened on ``lines[start]`` ends, as ``(end, sure)``.

    ``end`` is one past the closer, or the end of *lines* for an unterminated
    fence, which CommonMark renders as code to the end. ``sure`` is False when
    the closer sits left of the opener's content, where CommonMark may instead
    end a list item and open a new fence. None when the opener may not be a
    fence.
    """
    match = _FENCE_OPEN.fullmatch(lines[start])
    if match is None:
        return None
    # Some parsers read a "|" line followed by a delimiter row as a table.
    if "<" in match["info"] or "|" in match["info"]:
        return None
    if match["char"] == "`" and "`" in match["info"]:
        return None
    under_paragraph = start > 0 and not _blank(lines[start - 1])
    # Indented four or more under a paragraph line, the opener continues that
    # paragraph unless both sit in a list item.
    if under_paragraph and len(match["leading"]) >= 4:
        return None
    # Only an ordered item numbered 1 can interrupt a paragraph.
    if under_paragraph and match["number"] is not None and int(match["number"]) != 1:
        return None
    indent, fence = len(match["indent"]), match["fence"]
    closer = re.compile(
        rf"(?P<at>[ ]{{0,{indent + 3}}}){re.escape(fence)}{re.escape(fence[0])}*[ \t]*"
    )
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if closed := closer.fullmatch(line):
            return index + 1, len(closed["at"]) >= indent
        # A line left of the content ends a list item, and so the fence.
        if not _blank(line) and not line.startswith(" " * indent):
            return None
    return len(lines), True


def _swallowed(line: str) -> list[bool]:
    """Whether a backtick at each offset of *line* may be taken by another construct.

    A bare URL running into the backtick, an open link label (some parsers
    close it past a code span), an open link destination, or anything after a
    destination with a title or nested parenthesis. One extra entry, for the
    line end, is true when the line ends inside one. Coverage is summed from
    interval ends, so a line costs O(n) however many spans it holds.
    """
    delta = [0] * (len(line) + 2)

    def cover(start: int, end: int) -> None:
        if start <= end:
            delta[start] += 1
            delta[end + 1] -= 1

    for url in _BARE_URL.finditer(line):
        cover(url.start("rest"), url.end())
    next_bracket, next_paren = len(line), len(line)
    for index in range(len(line) - 1, -1, -1):
        char = line[index]
        if char == "]":
            next_bracket = index
        elif char == ")":
            next_paren = index
        elif char == "[":
            cover(index + 1, next_bracket)
        elif char == "(" and index > 0 and line[index - 1] == "]":
            cover(index + 1, next_paren)
    if title := _DESTINATION_TITLE.search(line):
        cover(title.end(), len(line))
    covered: list[bool] = []
    depth = 0
    for index in range(len(line) + 1):
        depth += delta[index]
        covered.append(depth > 0)
    return covered


def _inline_code_spans(lines: list[str]) -> list[list[tuple[int, int]]]:
    """Inline code spans on each line of one paragraph.

    A backtick run left unpaired on one line may pair with a run on a later
    line, and a backtick another construct takes shifts the pairing after it,
    so from either point on no span in the paragraph is trusted. Spans holding
    ``|`` are dropped when the paragraph has a table delimiter row, and none are
    kept in a paragraph that may hold a link reference definition, whose
    destination and title are not inline text.
    """
    if any("]:" in line for line in lines):
        return [[] for _ in lines]
    in_table = any("|" in line and _TABLE_DELIMITER.fullmatch(line) for line in lines)
    result: list[list[tuple[int, int]]] = []
    spans_trusted = True
    for line in lines:
        swallowed = _swallowed(line)
        spans: list[tuple[int, int]] = []
        last = 0
        for match in _INLINE_CODE.finditer(line):
            if "`" in line[last : match.start()] or swallowed[match.start()]:
                spans_trusted = False
            last = match.end()
            if spans_trusted and not (in_table and "|" in match[0]):
                spans.append(match.span())
        result.append(spans)
        if "`" in line[last:] or swallowed[len(line)]:
            spans_trusted = False
    return result


def code_spans(text: str) -> list[tuple[int, int]]:
    """Offsets of the fenced blocks and inline code spans in *text*, in order.

    *text* has ``\n`` line ends. Slack shows code verbatim, so these spans go
    out raw and everything else is escaped. A missed span only shows ``&lt;``;
    an invented one sends raw text Slack parses as prose, which can ping. The
    matcher therefore errs toward prose: inline spans stay on one line, a fence
    ends wherever CommonMark could end it, and after a fence-like line it
    cannot place, no later fence counts.
    """
    lines = text.split("\n")
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line) + 1)
    spans: list[tuple[int, int]] = []
    paragraph: list[int] = []

    def close_paragraph() -> None:
        for index, line_spans in zip(
            paragraph, _inline_code_spans([lines[i] for i in paragraph]), strict=True
        ):
            spans.extend((offsets[index] + a, offsets[index] + b) for a, b in line_spans)
        paragraph.clear()

    fences_trusted = True
    index = 0
    while index < len(lines):
        line = lines[index]
        if _blank(line):
            close_paragraph()
            index += 1
            continue
        if _FENCE_LIKE.fullmatch(line):
            fence = _fence_end(lines, index) if fences_trusted else None
            if fence is not None:
                end, sure = fence
                close_paragraph()
                spans.append((offsets[index], offsets[end] - 1))
                fences_trusted = sure
                index = end
                continue
            fences_trusted = False
        paragraph.append(index)
        index += 1
    close_paragraph()
    return spans


def _normalize_line_ends(text: str) -> str:
    # CommonMark ends a line at "\r" too; code_spans splits on "\n" only.
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _map_prose_and_code(text: str, prose: Callable[[str], str], code: Callable[[str], str]) -> str:
    parts: list[str] = []
    last = 0
    for start, end in code_spans(text):
        parts.append(prose(text[last:start]))
        parts.append(code(text[start:end]))
        last = end
    parts.append(prose(text[last:]))
    return "".join(parts)


def escape_markdown_block(text: str, *, preserve_mentions: bool = True) -> str:
    """Escape *text* for a native ``markdown`` block, leaving code as written.

    Slack does not decode entities inside code, so escaping there shows
    ``&lt;`` where the answer had ``<``. Prose is escaped as in mrkdwn, keeping
    user and channel mentions live when *preserve_mentions* is set. Run
    :func:`seal_markdown_block` on each message actually sent: splitting or
    prefixing the result can move a code line into prose.
    """
    escape = escape_mrkdwn_preserving_mentions if preserve_mentions else escape_mrkdwn
    return _map_prose_and_code(_normalize_line_ends(text), escape, lambda code: code)


def _escape_raw_angles(segment: str, mentions: Collection[str] | None) -> str:
    """Escape raw angle brackets, keeping mentions in *mentions*, or all when None."""
    pattern = _RAW_MENTION_OR_ANGLE
    if mentions:
        listed = "|".join(map(re.escape, sorted(mentions)))
        pattern = re.compile(f"{listed}|{_RAW_MENTION_OR_ANGLE.pattern}")

    def keep(token: str) -> bool:
        return token in mentions if mentions is not None else len(token) > 1

    return pattern.sub(lambda m: m[0] if keep(m[0]) else escape_mrkdwn(m[0]), segment)


def prose_mentions(text: str) -> frozenset[str]:
    """The user and channel mentions left live in the prose of *text*.

    Passed to :func:`seal_markdown_block` for the messages *text* is split
    into, so a mention written in code stays inert when a split moves it out of
    its fence.
    """
    text = _normalize_line_ends(text)
    found: set[str] = set()
    last = 0
    for start, end in code_spans(text):
        found.update(m[0] for m in _RAW_MENTION_OR_ANGLE.finditer(text, last, start))
        last = end
    found.update(m[0] for m in _RAW_MENTION_OR_ANGLE.finditer(text, last))
    return frozenset(token for token in found if len(token) > 1)


def seal_markdown_block(text: str, *, mentions: Collection[str]) -> str:
    """Escape raw ``<`` and ``>`` that Slack would read as prose in *text*.

    *text* is the exact ``markdown`` block text of one message. Code found in
    it is left alone; elsewhere, entities already in place are kept and any raw
    angle bracket is escaped, except the mention tokens in *mentions*.
    """
    return _map_prose_and_code(
        _normalize_line_ends(text), lambda prose: _escape_raw_angles(prose, mentions), lambda c: c
    )


def markdown_block_fallback(block_text: str) -> str:
    """Make ``markdown`` block text safe as a message's mrkdwn ``text`` fallback.

    The fallback is parsed as mrkdwn, where code is not exempt, so the raw code
    :func:`escape_markdown_block` leaves would be live there: ``<!channel>``
    in a code example would ping the channel from the notification text. Code is
    escaped here, and so is any other raw ``<`` or ``>`` that is not a user or
    channel mention.
    """
    return _map_prose_and_code(
        _normalize_line_ends(block_text),
        lambda prose: _escape_raw_angles(prose, None),
        escape_mrkdwn,
    )
