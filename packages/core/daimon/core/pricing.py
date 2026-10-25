"""Static per-model pricing table + cost computation + formatter.

Anthropic's API doesn't expose rates; update this module when prices change
or new models ship. Rates are in USD per million tokens.

Pure module — no I/O or DB; provider tariffs are immutable dated list rates.
Per `guideline:architecture` "Functional core, imperative shell".
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from types import MappingProxyType
from typing import Protocol

from mux.contracts.config import ResolvedBackend
from mux.contracts.ids import ModelRef
from mux.contracts.usage import UsageObservation
from mux.profiles import (
    CONVERSATION_ONLY,
    INLINE_REUSE,
    PERSISTENT_WORKSPACE,
    get_profile,
    meters_cache_writes_for,
)


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
    # Rates for a request whose prompt is over LONG_CONTEXT_PROMPT_TOKENS, for
    # models priced by prompt length (Haiku 5.5). None: one rate at any length.
    long_context: ModelRates | None = None


# Haiku 5.5's long-context threshold. The pricing page counts a request's
# prompt as all its input tokens, cache reads and cache writes included, and
# prices each request on its own.
LONG_CONTEXT_PROMPT_TOKENS = 100_000


class UsageCounts(Protocol):
    """Token fields shared by SDK request usage and turn totals."""

    @property
    def input_tokens(self) -> int: ...

    @property
    def output_tokens(self) -> int: ...

    @property
    def cache_creation_input_tokens(self) -> int: ...

    @property
    def cache_read_input_tokens(self) -> int: ...


# Agent models — what an agent's `model` may be set to. List price in USD per
# 1M tokens, every row checked against AGENT_PRICING_SOURCE on
# AGENT_PRICING_CHECKED_ON; `cache_write` is the five-minute rate. Any margin
# belongs in `DAIMON_BILLING__MARKUP`, never in these rows: reports read them as
# provider cost. `test_pricing.py` pins every row and has an opt-in live check
# (`pytest -m contract packages/core/tests/test_pricing.py`) against the page.
AGENT_PRICING_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
AGENT_PRICING_CHECKED_ON = "2026-10-10"
AGENT_MODEL_PRICING: dict[str, ModelRates] = {
    "claude-opus-5-5": ModelRates(input=4.0, output=20.0, cache_write=5.0, cache_read=0.20),
    # Local preset/tariff key, resolved before provider I/O.
    "claude-opus-5-5-fast": ModelRates(input=8.0, output=40.0, cache_write=10.0, cache_read=0.40),
    "claude-opus-5": ModelRates(input=5.0, output=25.0, cache_write=6.25, cache_read=0.50),
    "claude-opus-4-8": ModelRates(input=5.0, output=25.0, cache_write=6.25, cache_read=0.50),
    "claude-opus-4-7": ModelRates(input=5.0, output=25.0, cache_write=6.25, cache_read=0.50),
    "claude-sonnet-5-5": ModelRates(input=2.0, output=10.0, cache_write=2.50, cache_read=0.10),
    "claude-sonnet-5": ModelRates(input=2.0, output=10.0, cache_write=2.50, cache_read=0.20),
    "claude-sonnet-4-6": ModelRates(input=3.0, output=15.0, cache_write=3.75, cache_read=0.30),
    "claude-haiku-5-5": ModelRates(
        input=0.10,
        output=0.50,
        cache_write=0.125,
        cache_read=0.01,
        long_context=ModelRates(input=0.50, output=2.50, cache_write=0.625, cache_read=0.05),
    ),
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

_SNAPSHOT_SUFFIX = re.compile(r"-\d{8}$")


class _ModelPricing(dict[str, ModelRates]):
    """Price table that also prices a dated snapshot id at its alias's row.

    Managed Agents echoes snapshot ids such as `claude-haiku-4-5-20251001`;
    the table lists aliases (`claude-haiku-4-5`). Without this fallback a
    turn on a snapshot id priced at None and went unbilled.
    """

    def __missing__(self, key: str) -> ModelRates:
        alias = _SNAPSHOT_SUFFIX.sub("", key)
        if alias != key and super().__contains__(alias):
            return super().__getitem__(alias)
        raise KeyError(key)

    def get(self, key: str, default: ModelRates | None = None) -> ModelRates | None:  # type: ignore[override]
        try:
            return self[key]
        except KeyError:
            return default


# Every metered model, for cost lookups. Agent selectability comes from
# AGENT_MODEL_PRICING alone (see `constants.ALLOWED_MODEL_IDS`).
MODEL_PRICING: dict[str, ModelRates] = _ModelPricing({**AGENT_MODEL_PRICING, **TOOL_MODEL_PRICING})


def cost_of(
    usage: UsageObservation | LegacyUsage,
    rates: ModelRates | None,
) -> float | None:
    """Compute USD cost for one reported request at its prompt-length tier.

    Neutral observations project to disjoint counts first. Unknown rates or
    token stages stay unknown; float operation order retains historical parity.
    Multiple requests use cost_of_totals or an explicitly selected tier bucket.
    """
    if rates is None:
        return None
    if isinstance(usage, UsageObservation):
        tokens = usage_tokens(usage)
        if tokens is None:
            return None
        usage = tokens
    prompt_tokens = (
        usage.input_tokens + usage.cache_creation_input_tokens + usage.cache_read_input_tokens
    )
    return cost_of_bucket(usage, rates, long_prompt=prompt_tokens > LONG_CONTEXT_PROMPT_TOKENS)


def cost_of_bucket(
    usage: UsageCounts,
    rates: ModelRates | None,
    *,
    long_prompt: bool,
) -> float | None:
    """USD cost of usage summed over requests that all sit on one side of the threshold.

    `long_prompt` says which side: each request's prompt was over
    LONG_CONTEXT_PROMPT_TOKENS. Ignored for models with a single rate.
    """
    if rates is None:
        return None
    if long_prompt and rates.long_context is not None:
        rates = rates.long_context
    return (
        usage.input_tokens * rates.input / 1_000_000
        + usage.output_tokens * rates.output / 1_000_000
        + usage.cache_creation_input_tokens * rates.cache_write / 1_000_000
        + usage.cache_read_input_tokens * rates.cache_read / 1_000_000
    )


def cost_of_totals(
    all_usage: UsageCounts,
    long_usage: UsageCounts,
    rates: ModelRates | None,
) -> float | None:
    """Price a turn's totals: `long_usage` is the subset from long-prompt requests.

    The rest is priced at the base rates and the subset at the long-context
    rates, so the sum equals pricing each request on its own.
    """
    short = UsageTokens(
        input_tokens=all_usage.input_tokens - long_usage.input_tokens,
        output_tokens=all_usage.output_tokens - long_usage.output_tokens,
        cache_creation_input_tokens=all_usage.cache_creation_input_tokens
        - long_usage.cache_creation_input_tokens,
        cache_read_input_tokens=all_usage.cache_read_input_tokens
        - long_usage.cache_read_input_tokens,
    )
    short_cost = cost_of_bucket(short, rates, long_prompt=False)
    long_cost = cost_of_bucket(long_usage, rates, long_prompt=True)
    if short_cost is None or long_cost is None:
        return None
    return short_cost + long_cost


def format_cost(amount: float | None) -> str | None:
    """Max 3 decimals, trailing zeros stripped. Amounts < $0.001 render as `<$0.001`."""
    if amount is None:
        return None
    if amount < 0.001:
        return "<$0.001"
    s = f"{amount:.3f}".rstrip("0").rstrip(".")
    return f"${s}"


@dataclass(frozen=True)
class ProviderPrice:
    """Dated USD/M rates for an explicitly selected backend.

    A None cache-write rate declares that billing stage inapplicable, rather
    than asserting that the provider reported a zero count. Reasoning is
    already included in output. Actual infrastructure cost is supplied
    separately for the observation's grain and revision. A host-admitted tariff
    pins its validity check to admitted_on; ordinary prices use the receipt date.
    """

    provider: str
    model: str
    checked_on: date
    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal | None = None
    verified: bool = True
    max_priced_input_tokens: int | None = None
    # (input and cache, output) multipliers above max_priced_input_tokens. Applied
    # to a whole aggregate, which may mix tiers, so the cost is an upper bound.
    long_context_multipliers: tuple[Decimal, Decimal] | None = None
    valid_before: date | None = None
    admitted_on: date | None = None

    def current_on(self, on: date) -> bool:
        """Check the frozen admission date when the host admitted this tariff."""
        on = self.admitted_on or on
        return (
            self.verified
            and self.checked_on <= on
            and (self.valid_before is None or on < self.valid_before)
        )

    def __post_init__(self) -> None:
        if self.max_priced_input_tokens is not None and (
            type(self.max_priced_input_tokens) is not int or self.max_priced_input_tokens < 0
        ):
            raise ValueError("pricing input threshold requires a nonnegative integer")
        if self.long_context_multipliers is not None and (
            self.max_priced_input_tokens is None
            or any(not factor.is_finite() or factor < 1 for factor in self.long_context_multipliers)
        ):
            raise ValueError("long-context multipliers need a threshold and are at least 1")
        if self.valid_before is not None and self.valid_before <= self.checked_on:
            raise ValueError("pricing validity must extend beyond its checked date")
        if not self.provider or not self.model:
            raise ValueError("provider pricing requires provider and model identity")
        for rate in (self.input, self.output, self.cache_read, self.cache_write):
            if rate is not None and (not rate.is_finite() or rate < 0):
                raise ValueError("provider rates must be finite nonnegative Decimals")


def unreported_cache_writes(usage: UsageObservation, price: ProviderPrice) -> bool:
    """True when the tariff bills cache writes the meter never counts.

    Such a turn is priced as if it wrote nothing to the cache (ROOT D2=B): its
    non-cached input at the uncached rate, never above what its counts prove. The
    debit is an estimate, marked `cache_writes_unreported` on its ledger row.
    """
    return (
        price.cache_write is not None
        and usage.input_cache_write_tokens is None
        and not meters_cache_writes_for(usage.session.provider)
    )


def provider_uncached_input_tokens(usage: UsageObservation, price: ProviderPrice) -> int | None:
    """Validate attribution and project the input stage independently."""
    if usage.model is None or (usage.session.provider, usage.model.provider, usage.model.id) != (
        price.provider,
        price.provider,
        price.model,
    ):
        raise ValueError("usage and dated provider pricing identity differ")
    written = usage.input_cache_write_tokens
    if price.cache_write is None:
        if written not in (None, 0):
            raise ValueError("provider reported an inapplicable cache-write stage")
        written = 0
    elif unreported_cache_writes(usage, price):
        written = 0
    if usage.input_tokens is None or usage.input_cached_tokens is None or written is None:
        return None
    uncached = usage.input_tokens - usage.input_cached_tokens - written
    if uncached < 0:
        raise ValueError("cached input exceeds inclusive input tokens")
    return uncached


def provider_usage_tokens(usage: UsageObservation, price: ProviderPrice) -> UsageTokens | None:
    """Project measured stages to host columns without altering the native DTO."""
    uncached = provider_uncached_input_tokens(usage, price)
    if uncached is None or usage.output_tokens is None:
        return None
    return UsageTokens(
        uncached,
        usage.output_tokens,
        usage.input_cache_write_tokens or 0,
        usage.input_cached_tokens or 0,
    )


def provider_cost_of(
    usage: UsageObservation,
    price: ProviderPrice,
    *,
    infrastructure_usd: Decimal | None,
) -> Decimal | None:
    """Verified total cost, including actual session/container charges.

    Non-measured completeness, missing counts or infrastructure remain
    unverified, never a free turn.
    A caller may explicitly supply zero for inapplicable infrastructure.
    Equal cached/uncached rates permit exact inclusive-input pricing even
    when the split is unreported. No missing native count is filled in.
    """
    tokens = provider_usage_tokens(usage, price)
    if (
        usage.completeness != "measured"
        or not price.current_on(usage.observed_at.date())
        or infrastructure_usd is None
    ):
        return None
    above = price.max_priced_input_tokens is not None and (
        usage.input_tokens is None or usage.input_tokens > price.max_priced_input_tokens
    )
    if above and (usage.input_tokens is None or price.long_context_multipliers is None):
        # An aggregate cannot establish the pricing tier of its requests.
        return None
    if above and price.long_context_multipliers is not None:
        # Conservative: the whole aggregate at the long-context rates.
        incoming_factor, output_factor = price.long_context_multipliers
        price = replace(
            price,
            input=price.input * incoming_factor,
            cache_read=price.cache_read * incoming_factor,
            cache_write=(
                None if price.cache_write is None else price.cache_write * incoming_factor
            ),
            output=price.output * output_factor,
        )
    if not infrastructure_usd.is_finite() or infrastructure_usd < 0:
        raise ValueError("actual infrastructure cost must be finite and nonnegative")
    with localcontext() as context:
        context.prec = 80
        if tokens is None:
            if (
                usage.input_tokens is None
                or usage.output_tokens is None
                or usage.input_cached_tokens is not None
                or price.cache_read != price.input
                or price.cache_write is not None
            ):
                return None
            incoming = Decimal(usage.input_tokens) * price.input
        else:
            incoming = (
                Decimal(tokens.input_tokens) * price.input
                + Decimal(tokens.cache_read_input_tokens) * price.cache_read
                + Decimal(tokens.cache_creation_input_tokens) * (price.cache_write or Decimal(0))
            )
        return (incoming + Decimal(usage.output_tokens or 0) * price.output) / Decimal(
            1_000_000
        ) + infrastructure_usd


def minimum_cost_of(
    usage: UsageObservation,
    rates: ModelRates | ProviderPrice | None,
) -> Decimal | None:
    """Price a reported token floor, never an exact bill or a spend ceiling.

    The caller supplies the resolved tariff. Inclusive input with an unknown
    cache split is priced at the cheapest applicable input stage. Known cache
    stages are retained even when another category is unknown. Reasoning is
    included in output, so those counts are never summed. Unknown runtime is
    outside this token floor; it still prevents complete settlement.
    """
    if rates is None:
        return None
    if isinstance(rates, ProviderPrice):
        if usage.model is None or (
            usage.session.provider,
            usage.model.provider,
            usage.model.id,
        ) != (rates.provider, rates.provider, rates.model):
            raise ValueError("usage and dated provider pricing identity differ")
        if not rates.current_on(usage.observed_at.date()):
            return None
        variants = (rates,)
    elif rates.long_context is None:
        variants = (rates,)
    elif usage.input_tokens is None or usage.grain != "model_request":
        # A turn/session total can contain many short prompts. Its total
        # tokens cannot establish a request's long-context pricing tier.
        variants = (rates, rates.long_context)
    else:
        variants = (
            rates.long_context if usage.input_tokens > LONG_CONTEXT_PROMPT_TOKENS else rates,
        )

    cached = usage.input_cached_tokens or 0
    written = usage.input_cache_write_tokens or 0
    incoming = usage.input_tokens
    if incoming is not None and incoming < cached + written:
        raise ValueError("cached input exceeds inclusive input tokens")
    # The inclusive total owns output when present. A contradictory reasoning
    # subset cannot inflate its valid floor; an absent total can still retain
    # the independently reported reasoning floor.
    output = (
        usage.output_tokens
        if usage.output_tokens is not None
        else usage.output_reasoning_tokens or 0
    )
    floors: list[Decimal] = []
    with localcontext() as context:
        context.prec = 80
        for variant in variants:
            prices = [
                Decimal(str(variant.input)),
                Decimal(str(variant.cache_read)),
                Decimal(str(variant.output)),
            ]
            write_rate = None if variant.cache_write is None else Decimal(str(variant.cache_write))
            if any(not value.is_finite() or value < 0 for value in prices) or (
                write_rate is not None and (not write_rate.is_finite() or write_rate < 0)
            ):
                raise ValueError("token floors require finite nonnegative rates")
            if written and write_rate is None:
                raise ValueError("provider reported an inapplicable cache-write stage")
            token_cost = Decimal(cached) * prices[1] + Decimal(written) * (write_rate or Decimal(0))
            if incoming is not None:
                unknown_stage_rates = [prices[0]]
                if usage.input_cached_tokens is None:
                    unknown_stage_rates.append(prices[1])
                if usage.input_cache_write_tokens is None and write_rate is not None:
                    unknown_stage_rates.append(write_rate)
                token_cost += Decimal(incoming - cached - written) * min(unknown_stage_rates)
            floors.append((token_cost + Decimal(output) * prices[2]) / Decimal(1_000_000))
        return min(floors)


def list_price_preview(model: str, rates: ModelRates | ProviderPrice | None) -> str:
    """Describe supplied list rates without promising an invoice ceiling.

    The profile/service resolves the actual model and existing tariff. Native
    rates describe the supplied request tier only; no prices are discovered or
    synthesized here. A context/output limit alone cannot bound a whole turn.
    """
    bound = "Worst case per reply: unknown; requests, tools and hosted time have no cap."
    exclusions = "Excludes billing markup, hosted runtime, external tools and cache storage."
    if rates is None:
        return f"{model}: list price unknown. {bound} {exclusions}"
    if isinstance(rates, ProviderPrice):
        if rates.model != model:
            raise ValueError("preview model and supplied pricing identity differ")
        if not rates.verified:
            return (
                f"{model}: list price unverified (checked {rates.checked_on}). {bound} {exclusions}"
            )
        quoted = _list_rate_text(rates)
        label = f"list price (USD/1M tokens; checked {rates.checked_on}; supplied request tier)"
    else:
        quoted = _list_rate_text(rates)
        label = f"list price (USD/1M tokens; checked {AGENT_PRICING_CHECKED_ON})"
        if rates.long_context is not None:
            quoted = (
                f"prompt <= {LONG_CONTEXT_PROMPT_TOKENS:,}: {quoted}; "
                f"prompt > {LONG_CONTEXT_PROMPT_TOKENS:,}: {_list_rate_text(rates.long_context)}"
            )
        quoted += "; cache-write is the 5-minute rate"
    return f"{model}: {label}: {quoted}. {bound} {exclusions}"


def _list_rate_text(rates: ModelRates | ProviderPrice) -> str:
    def amount(value: float | Decimal | None) -> str:
        if value is None:
            return "inapplicable"
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise ValueError("preview rates must be finite and nonnegative")
        # Fixed-point formatting preserves list-price precision independently of
        # the caller's Decimal context; format_cost rounds actual display costs.
        rendered = format(number, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return f"${rendered}"

    return (
        f"input {amount(rates.input)}, output {amount(rates.output)}, "
        f"cache-read {amount(rates.cache_read)}, cache-write {amount(rates.cache_write)}"
    )


def resolved_worker_prices(
    choices: Sequence[tuple[ResolvedBackend, ModelRef]],
    *,
    native_prices: Mapping[tuple[str, str], ProviderPrice],
) -> Mapping[tuple[str, str], ModelRates | ProviderPrice]:
    """Resolve host-owned tariffs; missing prices stay missing for admission.

    This context grants no model authority and proves no final provider bill.
    Native rates come from the registered preparation owners, never tool input.
    Financial validation remains in the shared admission producer.
    """
    prices: dict[tuple[str, str], ModelRates | ProviderPrice] = {}
    for selection, model in choices:
        profile = get_profile(selection.profile)
        key = (model.provider, model.id)
        rate = (
            native_prices.get(key)
            if profile.accounting_mode.settles_natively
            else MODEL_PRICING.get(model.id)
        )
        if rate is not None:
            prices[key] = rate
    return MappingProxyType(prices)


# Paid Standard direct-API text rates, verified against these official sources.
# Agents inference follows the underlying model rates; tool/storage/container
# charges remain separate. Luna's base row is a floor at either context tier;
# exact pricing above the threshold requires per-request tier attribution.
PROVIDER_PRICING_CHECKED_ON = date(2026, 10, 14)
PROVIDER_PRICING_SOURCES = (
    "https://developers.openai.com/api/docs/models/gpt-6-luna",
    "https://developers.openai.com/api/docs/models/gpt-6-astra",
    "https://developers.openai.com/api/docs/guides/agents-api/observability",
    "https://ai.google.dev/gemini-api/docs/pricing",
    "https://ai.google.dev/gemini-api/docs/antigravity-agent",
)
_PROVIDER_PRICING: Mapping[str, tuple[ProviderPrice, ...]] = MappingProxyType(
    {
        PERSISTENT_WORKSPACE.profile_id: (
            ProviderPrice(
                PERSISTENT_WORKSPACE.provider,
                "gpt-6-luna",
                PROVIDER_PRICING_CHECKED_ON,
                Decimal("0.10"),
                Decimal("0.50"),
                Decimal("0.01"),
                Decimal("0.125"),
                max_priced_input_tokens=272_000,
            ),
            # Checked 2026-10-22 (official model page). Above 272k input tokens
            # input/cache double and output is 1.5x; an aggregate above it is
            # priced wholly at those rates, an upper bound.
            ProviderPrice(
                PERSISTENT_WORKSPACE.provider,
                "gpt-6-astra",
                date(2026, 10, 22),
                Decimal("10.00"),
                Decimal("50.00"),
                Decimal("1.00"),
                Decimal("12.50"),
                max_priced_input_tokens=272_000,
                long_context_multipliers=(Decimal(2), Decimal("1.5")),
            ),
        ),
        INLINE_REUSE.profile_id: (
            ProviderPrice(
                INLINE_REUSE.provider,
                "gemini-3.5-flash-lite",
                PROVIDER_PRICING_CHECKED_ON,
                Decimal("0.30"),
                Decimal("2.50"),
                Decimal("0.03"),
            ),
            ProviderPrice(
                INLINE_REUSE.provider,
                "gemini-3.8-flash",
                PROVIDER_PRICING_CHECKED_ON,
                Decimal("0.75"),
                Decimal("3.75"),
                Decimal("0.075"),
                valid_before=date(2027, 1, 1),
            ),
            # Intro tariff only (GA 2026-10-13, through 2026-11-30). The post-intro
            # price is unpublished here, so from 2026-12-01 Argon is unpriced and
            # refused at admission until a dated row is added. Cached input: 95% off.
            # FOLLOW-UP before 2026-12-01: add the post-intro row, or the daily model
            # cap also refuses every Argon turn and worker (provider_price_unavailable).
            ProviderPrice(
                INLINE_REUSE.provider,
                "gemini-4-argon",
                date(2026, 10, 22),
                Decimal("2.00"),
                Decimal("10.00"),
                Decimal("0.10"),
                valid_before=date(2026, 12, 1),
            ),
        ),
    }
)


def provider_price_expired_on(profile: str, provider: str, model: str) -> date | None:
    """The last day of the model's newest tariff once every dated row has expired.

    None while a row is current or not yet checked, or when the model has no row.
    """
    on = datetime.now(UTC).date()
    if profile == CONVERSATION_ONLY.profile_id:
        profile = PERSISTENT_WORKSPACE.profile_id
    rows = [
        price
        for price in _PROVIDER_PRICING.get(profile, ())
        if (price.provider, price.model) == (provider, model)
    ]
    if not rows or any(price.valid_before is None or on < price.valid_before for price in rows):
        return None
    return max(price.valid_before for price in rows if price.valid_before) - timedelta(days=1)


def provider_prices(profile: str) -> Mapping[tuple[str, str], ProviderPrice]:
    """Cold immutable native tariffs, shared by admission and usage owners.

    Unsupported profiles/models are absent. This supplies Standard token
    rates, not a whole-bill proof, native request limit or authority grant.
    """
    on = datetime.now(UTC).date()
    if profile == CONVERSATION_ONLY.profile_id:
        profile = PERSISTENT_WORKSPACE.profile_id
    return MappingProxyType(
        {
            (price.provider, price.model): price
            for price in _PROVIDER_PRICING.get(profile, ())
            if price.current_on(on)
        }
    )
