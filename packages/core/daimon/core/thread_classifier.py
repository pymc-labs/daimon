"""Thread-participation classifier: one small model call, fail closed to silence.

The shell half of `daimon.core.thread_participation`. This is a plain
`messages.create` call, not a Managed Agents session; the caller meters it to
the tenant through `usage_recording.record_classifier_usage` from the token
counts returned here. It runs only for threads whose resolved mode is `on`,
after the burst of messages has gone quiet and after the balance and cap
gates, so a deployment that never turns the feature on never pays for it.
Any error, including malformed output, yields `SILENCE_ON_ERROR`: a
classifier outage must never turn into a chattier bot.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from anthropic import AsyncAnthropic
from anthropic.types import TextBlock
from daimon.core.thread_participation import (
    SILENCE_ON_ERROR,
    ClassifierMessage,
    ClassifierVerdict,
    build_classifier_prompt,
    classifier_system_prompt,
    parse_classifier_response,
)

log = structlog.get_logger()

_MAX_OUTPUT_TOKENS = 100


@dataclass(frozen=True)
class ClassifierUsage:
    """Token counts of one classifier call, for metering."""

    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int


@dataclass(frozen=True)
class ClassifierOutcome:
    verdict: ClassifierVerdict
    usage: ClassifierUsage | None
    """`None` when the call failed before the API answered: nothing to meter."""


async def classify(
    anthropic: AsyncAnthropic,
    *,
    model: str,
    bot_display_name: str,
    recent: list[ClassifierMessage],
    candidates: list[ClassifierMessage],
) -> ClassifierOutcome:
    prompt = build_classifier_prompt(recent, candidates, bot_display_name=bot_display_name)
    usage: ClassifierUsage | None = None
    try:
        response = await anthropic.messages.create(
            model=model,
            max_tokens=_MAX_OUTPUT_TOKENS,
            system=classifier_system_prompt(bot_display_name),
            messages=[{"role": "user", "content": prompt}],
        )
        usage = ClassifierUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_input_tokens=response.usage.cache_read_input_tokens or 0,
        )
        text = "".join(b.text for b in response.content if isinstance(b, TextBlock))
        return ClassifierOutcome(parse_classifier_response(text), usage)
    except Exception:  # noqa: BLE001 -- fail-closed boundary: any failure means silence
        log.warning("thread_participation.classifier_failed", exc_info=True)
        return ClassifierOutcome(SILENCE_ON_ERROR, usage)
