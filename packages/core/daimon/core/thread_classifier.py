"""Auto-respond classifier: one small model call, fail closed to silence.

The shell half of `daimon.core.thread_participation`. This is a plain
`messages.create` call, not a Managed Agents session, so it is not metered
into `usage_events`; at the pinned model's rates one call costs a fraction of
a cent. It runs only for threads whose resolved mode is `on`, after the burst
of messages has gone quiet, so a deployment that never turns the feature on
never pays for it. Any error, including malformed output, yields
`SILENCE_ON_ERROR`: a classifier outage must never turn into a chattier bot.
"""

from __future__ import annotations

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


async def classify(
    anthropic: AsyncAnthropic,
    *,
    model: str,
    bot_display_name: str,
    recent: list[ClassifierMessage],
    candidates: list[ClassifierMessage],
) -> ClassifierVerdict:
    prompt = build_classifier_prompt(recent, candidates, bot_display_name=bot_display_name)
    try:
        response = await anthropic.messages.create(
            model=model,
            max_tokens=_MAX_OUTPUT_TOKENS,
            system=classifier_system_prompt(bot_display_name),
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in response.content if isinstance(b, TextBlock))
        return parse_classifier_response(text)
    except Exception:  # noqa: BLE001 -- fail-closed boundary: any failure means silence
        log.warning("thread_participation.classifier_failed", exc_info=True)
        return SILENCE_ON_ERROR
