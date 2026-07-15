"""mrkdwn entity-escaper for Slack.

Slack's mrkdwn format uses three HTML entities to prevent literal text from
being interpreted as links or mentions. The escape order is **load-bearing**:

  1. ``&`` → ``&amp;``  — MUST be first; if < or > are replaced first, the
     ``&`` already present in ``&lt;``/``&gt;`` would get double-escaped.
  2. ``<`` → ``&lt;``
  3. ``>`` → ``&gt;``

This module is stdlib-only — no ``slack_sdk``, ``anthropic``, or ``daimon.core``
imports. It forms part of the functional-core rendering layer (SREND-02, D-07).

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
