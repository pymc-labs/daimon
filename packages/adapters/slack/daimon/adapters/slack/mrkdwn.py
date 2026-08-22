"""mrkdwn entity-escaper for Slack.

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
# The URL charset excludes ()[]<> and whitespace so URLs already inside
# [label](url) links (which end at the ')') never match.
_EMPHASIZED_URL = re.compile(r"(https?://[^\s<>()\[\]]*?[^\s<>()\[\]*])(?=\*{1,3}(?:[^\w*]|$))")

# Code segments the linkifier must never touch: a fenced block (closed, or
# open-to-end — Slack renders an unterminated fence as code), or an inline
# single-backtick span.
_CODE_SEGMENT = re.compile(r"```[\s\S]*?(?:```|$)|`[^`\n]*`")


def linkify_emphasized_urls(text: str) -> str:
    """Rewrite bare URLs that sit against an emphasis closer to ``[url](url)``.

    In Slack's native ``markdown`` block the bare-URL autolinker is greedy and
    ``*`` is a valid URL character, so ``**https://x**`` renders with the
    closing asterisks absorbed into the link target (broken link with a
    trailing ``*``, unclosed bold). Making the link explicit removes the
    ambiguity while leaving the surrounding emphasis intact.

    Fenced code blocks and inline code spans are passed through verbatim —
    emphasis has no meaning there and the content must render exactly as
    written.
    """

    def _rewrite(segment: str) -> str:
        return _EMPHASIZED_URL.sub(lambda m: f"[{m.group(1)}]({m.group(1)})", segment)

    parts: list[str] = []
    last = 0
    for code in _CODE_SEGMENT.finditer(text):
        parts.append(_rewrite(text[last : code.start()]))
        parts.append(code.group(0))
        last = code.end()
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
