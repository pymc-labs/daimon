"""Static per-model pricing table + cost computation + formatter.

Anthropic's API doesn't expose rates; update this module when prices change
or new models ship. Rates are in USD per million tokens.

Pure module — no I/O, no DB, no module-level state beyond MODEL_PRICING.
Per `guideline:architecture` "Functional core, imperative shell".
"""

from __future__ import annotations

from dataclasses import dataclass

from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)


@dataclass(frozen=True)
class ModelRates:
    """USD per 1,000,000 tokens, by stage."""

    input: float
    output: float
    cache_write: float
    cache_read: float


# USD per 1M tokens. Sourced from https://www.anthropic.com/pricing (2026-04-19).
MODEL_PRICING: dict[str, ModelRates] = {
    "claude-opus-4-8": ModelRates(input=5.0, output=25.0, cache_write=6.25, cache_read=0.50),
    "claude-opus-4-7": ModelRates(input=15.0, output=75.0, cache_write=18.75, cache_read=1.50),
    "claude-sonnet-5": ModelRates(input=3.0, output=15.0, cache_write=3.75, cache_read=0.30),
    "claude-sonnet-4-6": ModelRates(input=3.0, output=15.0, cache_write=3.75, cache_read=0.30),
    "claude-haiku-4-5": ModelRates(input=1.0, output=5.0, cache_write=1.25, cache_read=0.10),
}

# USD per 1M tokens. Sourced from https://ai.google.dev/gemini-api/docs/pricing
# (2026-07-06). Gemini's published rates are modality-split (e.g. audio input
# vs text input); each entry below prices the pinned tool's dominant modality
# rather than modeling every modality split. gemini-3-pro-image's thinking
# tokens fold into `output` at the $120/M image rate rather than Google's
# cheaper ~$12/M text/thinking rate — a deliberate conservative approximation
# (over-charges slightly rather than under-metering).
MODEL_PRICING["gemini-3.1-flash-tts-preview"] = ModelRates(
    input=1.00, output=20.00, cache_write=0.0, cache_read=0.0
)
MODEL_PRICING["gemini-3-pro-image-preview"] = ModelRates(
    input=2.00, output=120.00, cache_write=0.0, cache_read=0.0
)
MODEL_PRICING["gemini-2.5-flash"] = ModelRates(
    input=0.30, output=2.50, cache_write=0.0, cache_read=0.03
)


def cost_of(
    usage: BetaManagedAgentsSpanModelUsage,
    rates: ModelRates | None,
) -> float | None:
    """Compute USD cost for one model_usage payload against `rates`.

    Returns None when rates is None (unknown model id).
    """
    if rates is None:
        return None
    return (
        usage.input_tokens * rates.input / 1_000_000
        + usage.output_tokens * rates.output / 1_000_000
        + usage.cache_creation_input_tokens * rates.cache_write / 1_000_000
        + usage.cache_read_input_tokens * rates.cache_read / 1_000_000
    )


def format_cost(amount: float | None) -> str | None:
    """Max 3 decimals, trailing zeros stripped. Amounts < $0.001 render as `<$0.001`."""
    if amount is None:
        return None
    if amount < 0.001:
        return "<$0.001"
    s = f"{amount:.3f}".rstrip("0").rstrip(".")
    return f"${s}"
