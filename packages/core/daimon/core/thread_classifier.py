"""Thread-participation classifier: one small model call, fail closed to silence.

The shell half of `daimon.core.thread_participation`. This is a plain
completion through `auxiliary_calls`, not a Managed Agents session; the caller meters it to
the tenant through `usage_recording.record_classifier_usage` from the token
counts returned here. It runs only for threads whose resolved mode is `on`,
after the burst of messages has gone quiet and after the balance and cap
gates, so a deployment that never turns the feature on never pays for it.
Any error, including malformed output or a refusal, yields `SILENCE_ON_ERROR`: a
classifier outage must never turn into a chattier bot.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from anthropic import AsyncAnthropic
from daimon.core.anthropic_guard import require_anthropic_allowed
from daimon.core.auxiliary_calls import auxiliary_complete
from daimon.core.thread_participation import (
    SILENCE_ON_ERROR,
    ClassifierMessage,
    ClassifierVerdict,
    build_classifier_prompt,
    classifier_system_prompt,
    parse_classifier_response,
)
from mux.contracts.config import DEFAULT_BACKEND
from mux.contracts.ids import Provider

log = structlog.get_logger()

# A 100-token cap truncated Haiku 5.5's JSON verdict (newer tokenizer, longer
# reasons), and truncated JSON fails closed to silence. The cap also leaves
# room for the rare adaptive-thinking episode.
_MAX_OUTPUT_TOKENS = 1024


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
    provider: Provider = DEFAULT_BACKEND
    model_id: str | None = None
    """What the call ran on, for metering."""


async def classify(
    anthropic: AsyncAnthropic,
    *,
    model: str,
    bot_display_name: str,
    recent: list[ClassifierMessage],
    candidates: list[ClassifierMessage],
    provider: Provider | None = None,
) -> ClassifierOutcome:
    require_anthropic_allowed()
    prompt = build_classifier_prompt(recent, candidates, bot_display_name=bot_display_name)
    try:
        done = await auxiliary_complete(
            anthropic,
            provider=provider,
            purpose="classifier",
            system=classifier_system_prompt(bot_display_name),
            prompt=prompt,
            max_tokens=_MAX_OUTPUT_TOKENS,
            # Adaptive thinking at low effort judged ambiguous bursts better than
            # thinking off, and rarely thinks at all.
            effort="low",
            anthropic_model=model,
        )
    except Exception:  # fail-closed boundary: any failure means silence
        log.warning("thread_participation.classifier_failed", exc_info=True)
        return ClassifierOutcome(SILENCE_ON_ERROR, None)
    usage = ClassifierUsage(
        input_tokens=done.usage.input_tokens,
        output_tokens=done.usage.output_tokens,
        cache_read_input_tokens=done.usage.cache_read_input_tokens,
    )
    try:
        if done.refused:
            raise ValueError("classifier call was refused")
        verdict = parse_classifier_response(done.text)
    except Exception:  # fail-closed boundary: malformed output or a refusal means silence
        log.warning("thread_participation.classifier_failed", exc_info=True)
        verdict = SILENCE_ON_ERROR
    return ClassifierOutcome(verdict, usage, done.provider, done.model_id)
