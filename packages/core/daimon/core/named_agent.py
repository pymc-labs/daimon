"""Parse and match an explicit agent name without changing ordinary messages."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from anthropic.types.beta import BetaManagedAgentsAgent


def normalized_name(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold()


def name_after_mention(text: str, mention: str | None = None) -> str | None:
    """Read the first `name:` token after the bot mention (or stripped Teams text)."""
    if mention is not None:
        match = re.search(re.escape(mention), text)
        if match is None:
            return None
        text = text[match.end() :]
    match = re.match(r"\s*([^\s:]+):(?=\s|$)", text)
    return match.group(1) if match else None


def matching_agent(
    agents: Sequence[BetaManagedAgentsAgent], name: str
) -> BetaManagedAgentsAgent | None:
    matches = [agent for agent in agents if normalized_name(agent.name) == normalized_name(name)]
    # A collision after Unicode normalization is ambiguous, so do not guess.
    return matches[0] if len(matches) == 1 else None
