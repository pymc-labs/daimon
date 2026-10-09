"""Static per-model pricing table + cost computation + formatter.

Anthropic's API doesn't expose rates; update this module when prices change
or new models ship. Rates are in USD per million tokens.

Pure module — no I/O, no DB, no module-level state beyond MODEL_PRICING.
Per `guideline:architecture` "Functional core, imperative shell".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from mux.contracts.usage import UsageObservation


class LegacyUsage(Protocol):
    """Temporary structural boundary for unchanged M0 SDK/adapter callers."""

    @property
    def input_tokens(self) -> int: ...
    @property
    def output_tokens(self) -> int: ...
    @property
    def cache_creation_input_tokens(self) -> int: ...
    @property
    def cache_read_input_tokens(self) -> int: ...


@dataclass(frozen=True)
class UsageTokens:
    """The existing four disjoint billing/telemetry columns."""

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int


def uncached_input_tokens(usage: UsageObservation) -> int | None:
    """Project the input stage when all three input counts were reported."""
    if (
        usage.input_tokens is None
        or usage.input_cached_tokens is None
        or usage.input_cache_write_tokens is None
    ):
        return None
    uncached = usage.input_tokens - usage.input_cached_tokens - usage.input_cache_write_tokens
    if uncached < 0:
        raise ValueError("cached input exceeds inclusive input tokens")
    return uncached


def usage_tokens(usage: UsageObservation) -> UsageTokens | None:
    """Project inclusive neutral counts without treating unknown as zero."""
    uncached = uncached_input_tokens(usage)
    if uncached is None or usage.output_tokens is None:
        return None
    assert usage.input_cached_tokens is not None and usage.input_cache_write_tokens is not None
    return UsageTokens(
        input_tokens=uncached,
        output_tokens=usage.output_tokens,
        cache_creation_input_tokens=usage.input_cache_write_tokens,
        cache_read_input_tokens=usage.input_cached_tokens,
    )


@dataclass(frozen=True)
class ModelRates:
    """USD per 1,000,000 tokens, by stage."""

    input: float
    output: float
    cache_write: float
    cache_read: float


# Agent models — what an agent's `model` may be set to. List price in USD per
# 1M tokens, every row checked against AGENT_PRICING_SOURCE on
# AGENT_PRICING_CHECKED_ON; `cache_write` is the five-minute rate. Any margin
# belongs in `DAIMON_BILLING__MARKUP`, never in these rows: reports read them as
# provider cost. `test_pricing.py` pins every row and has an opt-in live check
# (`pytest -m contract packages/core/tests/test_pricing.py`) against the page.
AGENT_PRICING_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
AGENT_PRICING_CHECKED_ON = "2026-10-08"
AGENT_MODEL_PRICING: dict[str, ModelRates] = {
    "claude-opus-5-5": ModelRates(input=4.0, output=20.0, cache_write=5.0, cache_read=0.20),
    "claude-opus-5": ModelRates(input=5.0, output=25.0, cache_write=6.25, cache_read=0.50),
    "claude-opus-4-8": ModelRates(input=5.0, output=25.0, cache_write=6.25, cache_read=0.50),
    "claude-opus-4-7": ModelRates(input=5.0, output=25.0, cache_write=6.25, cache_read=0.50),
    "claude-sonnet-5-5": ModelRates(input=2.0, output=10.0, cache_write=2.50, cache_read=0.10),
    "claude-sonnet-5": ModelRates(input=2.0, output=10.0, cache_write=2.50, cache_read=0.20),
    "claude-sonnet-4-6": ModelRates(input=3.0, output=15.0, cache_write=3.75, cache_read=0.30),
    "claude-haiku-4-5": ModelRates(input=1.0, output=5.0, cache_write=1.25, cache_read=0.10),
}

# Models pinned by MCP tools (media generation, etc.) — metered like agent
# models but never selectable as an agent's model. USD per 1M tokens, sourced
# from https://ai.google.dev/gemini-api/docs/pricing (2026-07-06). Gemini's
# published rates are modality-split (e.g. audio input vs text input); each
# entry below prices the pinned tool's dominant modality rather than modeling
# every modality split. gemini-3-pro-image's thinking tokens fold into `output`
# at the $120/M image rate rather than Google's cheaper ~$12/M text/thinking
# rate — a deliberate conservative approximation (over-charges slightly rather
# than under-metering).
TOOL_MODEL_PRICING: dict[str, ModelRates] = {
    "gemini-3.1-flash-tts-preview": ModelRates(
        input=1.00, output=20.00, cache_write=0.0, cache_read=0.0
    ),
    "gemini-3-pro-image-preview": ModelRates(
        input=2.00, output=120.00, cache_write=0.0, cache_read=0.0
    ),
    "gemini-2.5-flash": ModelRates(input=0.30, output=2.50, cache_write=0.0, cache_read=0.03),
}

# Every metered model, for cost lookups. Agent selectability comes from
# AGENT_MODEL_PRICING alone (see `constants.ALLOWED_MODEL_IDS`).
MODEL_PRICING: dict[str, ModelRates] = {**AGENT_MODEL_PRICING, **TOOL_MODEL_PRICING}


def cost_of(
    usage: UsageObservation | LegacyUsage,
    rates: ModelRates | None,
) -> float | None:
    """Compute USD cost for a neutral measurement against `rates`.

    Returns None for unknown rates or an unreported token stage. The float
    operation order is retained verbatim for historical debit parity.
    """
    if rates is None:
        return None
    if isinstance(usage, UsageObservation):
        tokens = usage_tokens(usage)
        if tokens is None:
            return None
        usage = tokens
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
