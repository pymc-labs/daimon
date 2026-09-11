"""Thread titles from an opening message, via one metered Haiku call.

Discord-only in effect: Slack threads have no title, so neither the
automatic rename nor the ``rename_thread`` MCP tool has a Slack half. The
executable record of that split is
``tests/parity/test_thread_naming_discord_only.py``.

Functional core, imperative shell (`guideline:architecture`):
``parse_thread_name`` is pure; ``suggest_thread_name`` is the single I/O
call, a plain ``messages.create`` like ``thread_classifier``. Metering is the caller's
job (``usage_recording.record_thread_naming_usage``) because the caller
owns the tenant and runs after the balance and cap gates.
"""

from __future__ import annotations

from dataclasses import dataclass

from anthropic import AsyncAnthropic
from anthropic.types import TextBlock

# Priced in AGENT_MODEL_PRICING; a rename must never run on an unmetered model.
THREAD_NAMING_MODEL = "claude-haiku-4-5"
THREAD_NAME_MAX_CHARS = 80
_MAX_OUTPUT_TOKENS = 60
_NONE_SENTINEL = "NONE"

_SYSTEM_PROMPT = f"""\
Generate a concise Discord thread title from the user's message.

Guidelines:
- Capture the core topic or question, not a summary of the full message
- Use title case
- Max {THREAD_NAME_MAX_CHARS} characters (shorter is better)
- Strip @mentions, URLs, and Discord formatting
- If the message is too vague to title (e.g. "hey", "hello"), return {_NONE_SENTINEL}

Examples:
- "Can someone help me set up the Bayesian model for our A/B test?"
  -> "Bayesian Model for A/B Test Setup"
- "I'm getting a weird error when I try to run PyMC on my M2 Mac" -> "PyMC Error on M2 Mac"
- "What's the difference between NUTS and Metropolis samplers?" -> "NUTS vs Metropolis Samplers"

Return ONLY the title, or {_NONE_SENTINEL} if the message has no clear topic."""


@dataclass(frozen=True)
class ThreadNamingUsage:
    """Token counts of one naming call, for metering."""

    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int


@dataclass(frozen=True)
class ThreadNameSuggestion:
    """``name`` is None when the model declined to title the message."""

    name: str | None
    usage: ThreadNamingUsage


def parse_thread_name(raw: str) -> str | None:
    """Normalise model output into a usable title, or None.

    Takes the first non-empty line, drops wrapping quotes, and cuts at a
    word boundary so a runaway completion never yields a mid-word title.
    """
    lines = [line.strip() for line in raw.splitlines()]
    first = next((line for line in lines if line), "")
    title = first.strip("\"'“”‘’ ")
    if not title or title.upper() == _NONE_SENTINEL:
        return None
    if len(title) <= THREAD_NAME_MAX_CHARS:
        return title
    head = title[:THREAD_NAME_MAX_CHARS]
    at_word = head.rsplit(" ", 1)[0].rstrip()
    return at_word or head


async def suggest_thread_name(
    anthropic: AsyncAnthropic,
    *,
    message_text: str,
    max_input_chars: int,
) -> ThreadNameSuggestion:
    """One Haiku call over the (truncated) opening message.

    ``message_text`` must be non-empty; the caller skips attachment-only
    messages rather than paying for a call that can only answer NONE.
    SDK errors propagate: the adapter boundary decides what a failed
    naming means (keep the placeholder title).
    """
    response = await anthropic.messages.create(
        model=THREAD_NAMING_MODEL,
        max_tokens=_MAX_OUTPUT_TOKENS,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": message_text[:max_input_chars]}],
    )
    raw = "".join(block.text for block in response.content if isinstance(block, TextBlock))
    return ThreadNameSuggestion(
        name=parse_thread_name(raw),
        usage=ThreadNamingUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_input_tokens=response.usage.cache_read_input_tokens or 0,
        ),
    )
