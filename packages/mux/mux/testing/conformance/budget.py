"""Manual probe admission and spend receipts; no SDK, recorder or live calls."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import os
import re
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal, localcontext
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Final, Literal, TextIO
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

Money = Annotated[Decimal, Field(ge=0, max_digits=32, decimal_places=18)]
Rate = Annotated[Decimal, Field(ge=0, max_digits=16, decimal_places=8)]
Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,127}$")]
FIXTURE_ID_PATTERN = (
    r"(?:C(?:0[1-9]|1[0-8])|F1|H6-(?:ANTHROPIC|OPENAI|GEMINI)"
    r"|I7-(?:ROOT|W[1-4]|RESTORE-SMOKE|CANDIDATE-SMOKE))"
)
H6_MODELS = {"anthropic": "claude-haiku-5-5", "openai": "gpt-6-luna", "gemini": "gemini-3.8-flash"}
Count = Annotated[int, Field(strict=True, ge=0, le=1_000_000_000)]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
RequestPath = Annotated[str, Field(pattern=r"^/[a-zA-Z0-9_./:-]*$")]
RequestRoute = tuple[Literal["GET", "POST", "PUT", "PATCH", "DELETE"], RequestPath]
SourceURL = Annotated[
    str,
    Field(
        pattern=r"^https://(?:developers\.openai\.com|platform\.claude\.com|ai\.google\.dev|cloud\.google\.com)/[A-Za-z0-9/_#.-]+$"
    ),
]
PREFIX = "<!-- mux-probe "
HEADER = "# Managed probe spend"
LEDGER_PREFIX = "<!-- mux-ledger "

# Probe policy is independent of the pricing map: reviewed rates never grant
# permission to use a larger model. Unknown providers refuse admission too.
LIVE_MODEL_ALLOWLIST: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "openai": frozenset({"gpt-6-luna"}),
        "anthropic": frozenset({"claude-haiku-5-5"}),
        # The Gemini harness owns 3.8 Flash-first and 503-only fallback.
        "gemini": frozenset({"gemini-3.8-flash", "gemini-flash-latest", "gemini-3.5-flash-lite"}),
    }
)


_KEY = re.compile(r"(?:sk-|AIza)[A-Za-z0-9_-]{32,}")
_BASE64 = re.compile(r"[A-Za-z0-9+/_-]{8,}={0,2}")


def sensitive_identifier(value: str, depth: int = 0) -> bool:
    """Refuse credential-shaped identifiers without depending on tape auditing.

    Config/plan names are validated Identifier strings: no whitespace, percent,
    backslash or JSON syntax. Decoded names with such structure are ambiguous
    and refused, rather than accepting an encoded credential in spend metadata.
    """
    if _KEY.search(value):
        return True
    for match in _BASE64.finditer(value):
        try:
            decoded = base64.b64decode(
                match[0] + "=" * (-len(match[0]) % 4), altchars=b"-_", validate=True
            )
        except ValueError:
            continue
        if depth >= 4:
            return True
        texts = [decoded.decode("latin1")]
        if b"\0" in decoded:
            texts.extend(
                decoded.decode(encoding, errors="ignore") for encoding in ("utf-16-le", "utf-16-be")
            )
        for text in texts:
            if _KEY.search(text):
                return True
            if not all(char.isprintable() or char in "\r\n\t" for char in text):
                continue
            if any(char in text for char in ("\\", "%", "{", "[")) or re.search(
                r"\b(?:bearer|basic)\s", text, re.IGNORECASE
            ):
                return True
            if sensitive_identifier(text, depth + 1):
                return True
    return False


class BudgetRefused(Exception):
    pass


class BudgetLedgerError(Exception):
    pass


class ProbeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TokenLimits(ProbeModel):
    input_tokens: Count
    output_tokens: Count


class TokenUsage(ProbeModel):
    input_tokens: Count | None = None
    output_tokens: Count | None = None
    input_cached_tokens: Count | None = None
    input_cache_write_tokens: Count | None = None
    input_cache_write_5m_tokens: Count | None = None
    input_cache_write_1h_tokens: Count | None = None

    @property
    def minimum_input_tokens(self) -> int:
        return max(
            self.input_tokens or 0,
            (self.input_cached_tokens or 0)
            + max(
                self.input_cache_write_tokens or 0,
                (self.input_cache_write_5m_tokens or 0) + (self.input_cache_write_1h_tokens or 0),
            ),
        )

    @model_validator(mode="after")
    def subsets(self) -> TokenUsage:
        if self.input_tokens is not None and self.minimum_input_tokens > self.input_tokens:
            raise ValueError("cached/write tokens exceed inclusive input")
        durations = (self.input_cache_write_5m_tokens, self.input_cache_write_1h_tokens)
        if (
            self.input_cache_write_tokens is not None
            and sum(v or 0 for v in durations) > self.input_cache_write_tokens
        ):
            raise ValueError("cache-write durations exceed write total")
        if (
            all(v is not None for v in durations)
            and sum(v or 0 for v in durations) != self.input_cache_write_tokens
        ):
            raise ValueError("cache-write durations disagree with write total")
        return self


class PromptPrice(ProbeModel):
    through_input_tokens: Count
    input: Rate
    cached_input: Rate
    cache_write_input: Rate
    cache_write_5m_input: Rate
    output: Rate


class ModelPrice(ProbeModel):
    """Reviewed USD per million tokens. Reasoning is included in output."""

    input: Rate
    cached_input: Rate
    cache_write_input: Rate
    output: Rate
    cache_write_5m_input: Rate | None = None
    effective_from: date | None = None
    effective_until: date | None = None
    source: SourceURL | None = None
    short_prompt: PromptPrice | None = None
    actual_input_limit: Count | None = None
    pricing_basis: Identifier = "standard-global"

    @model_validator(mode="after")
    def dated(self) -> ModelPrice:
        if self.effective_until is not None and (
            self.effective_from is None or self.effective_until <= self.effective_from
        ):
            raise ValueError("invalid price effective interval")
        if self.short_prompt is not None and any(
            getattr(self.short_prompt, name) > getattr(self, name)
            for name in ("input", "cached_input", "cache_write_input", "output")
        ):
            raise ValueError("reservation rates must bound the short prompt tier")
        if (
            self.short_prompt is not None
            and self.short_prompt.cache_write_5m_input > self.cache_write_input
        ):
            raise ValueError("reservation must bound short-tier cache writes")
        if (
            self.cache_write_5m_input is not None
            and self.cache_write_5m_input > self.cache_write_input
        ):
            raise ValueError("reservation must bound cache-write rates")
        return self

    def actual(self, usage: TokenUsage, at: datetime) -> Decimal | None:
        if at.tzinfo is None or self.effective_from is None or self.source is None:
            return None
        if at.astimezone(UTC).date() < self.effective_from or (
            self.effective_until is not None and at.astimezone(UTC).date() >= self.effective_until
        ):
            return None
        if any(
            v is None
            for v in (
                usage.input_tokens,
                usage.output_tokens,
                usage.input_cached_tokens,
                usage.input_cache_write_tokens,
            )
        ):
            return None
        incoming = usage.input_tokens or 0
        if self.actual_input_limit is not None and incoming > self.actual_input_limit:
            return None
        cached, written = usage.input_cached_tokens or 0, usage.input_cache_write_tokens or 0
        rate = (
            self.short_prompt
            if self.short_prompt is not None and incoming <= self.short_prompt.through_input_tokens
            else self
        )
        five = (
            rate.cache_write_5m_input
            if rate.cache_write_5m_input is not None
            else rate.cache_write_input
        )
        if written and five != rate.cache_write_input:
            if (
                usage.input_cache_write_5m_tokens is None
                or usage.input_cache_write_1h_tokens is None
            ):
                return None
            write_cost = (
                usage.input_cache_write_5m_tokens * five
                + usage.input_cache_write_1h_tokens * rate.cache_write_input
            )
        else:
            write_cost = written * rate.cache_write_input
        with localcontext() as context:
            context.prec = 80
            return (
                (incoming - cached - written) * rate.input
                + cached * rate.cached_input
                + write_cost
                + (usage.output_tokens or 0) * rate.output
            ) / Decimal(1_000_000)

    def minimum_charge(self, usage: TokenUsage, at: datetime) -> Decimal:
        """Price known components as lower bounds, without claiming an actual total.

        Unknown categories use the cheapest compatible reviewed rate. An unknown
        prompt size uses the cheaper tier; known long prompts use the long tier.
        Undated/out-of-window evidence contributes no proven dollar lower bound.
        """
        if (
            at.tzinfo is None
            or self.effective_from is None
            or self.source is None
            or at.astimezone(UTC).date() < self.effective_from
            or self.effective_until is not None
            and at.astimezone(UTC).date() >= self.effective_until
            or self.actual_input_limit is not None
            and usage.minimum_input_tokens > self.actual_input_limit
        ):
            return Decimal(0)
        rate = (
            self.short_prompt
            if self.short_prompt is not None
            and usage.minimum_input_tokens <= self.short_prompt.through_input_tokens
            else self
        )
        five = (
            rate.cache_write_5m_input
            if rate.cache_write_5m_input is not None
            else rate.cache_write_input
        )
        input_rate, cached_rate, write_rate, output_rate = (
            rate.input,
            rate.cached_input,
            rate.cache_write_input,
            rate.output,
        )
        if usage.input_tokens is None and rate is not self:
            input_rate = min(input_rate, self.input)
            cached_rate = min(cached_rate, self.cached_input)
            write_rate = min(write_rate, self.cache_write_input)
            five = min(
                five,
                self.cache_write_5m_input
                if self.cache_write_5m_input is not None
                else self.cache_write_input,
            )
            output_rate = min(output_rate, self.output)
        cached = usage.input_cached_tokens or 0
        five_count = usage.input_cache_write_5m_tokens or 0
        hour_count = usage.input_cache_write_1h_tokens or 0
        written = max(usage.input_cache_write_tokens or 0, five_count + hour_count)
        # When both cache totals are known, the remainder is uncached input.
        remainder_rate = (
            input_rate
            if usage.input_cached_tokens is not None and usage.input_cache_write_tokens is not None
            else min(input_rate, cached_rate, write_rate, five)
        )
        with localcontext() as context:
            context.prec = 80
            return (
                (usage.minimum_input_tokens - cached - written) * remainder_rate
                + cached * cached_rate
                + five_count * five
                + hour_count * write_rate
                + (written - five_count - hour_count) * min(five, write_rate)
                + (usage.output_tokens or 0) * output_rate
            ) / Decimal(1_000_000)

    def reserve(self, limits: TokenLimits, *, usage: TokenUsage | None = None) -> Decimal:
        with localcontext() as context:
            context.prec = 80
            return (
                max(limits.input_tokens, usage.minimum_input_tokens if usage is not None else 0)
                * max(self.input, self.cached_input, self.cache_write_input)
                + max(limits.output_tokens, (usage.output_tokens or 0) if usage is not None else 0)
                * self.output
            ) / Decimal(1_000_000)

    def estimate(self, usage: TokenUsage) -> Decimal | None:
        if usage.input_tokens is None or usage.output_tokens is None:
            return None
        with localcontext() as context:
            context.prec = 80
            if usage.input_cached_tokens is None or usage.input_cache_write_tokens is None:
                incoming = usage.input_tokens * max(
                    self.input, self.cached_input, self.cache_write_input
                )
            else:
                incoming = (
                    (
                        usage.input_tokens
                        - usage.input_cached_tokens
                        - usage.input_cache_write_tokens
                    )
                    * self.input
                    + usage.input_cached_tokens * self.cached_input
                    + usage.input_cache_write_tokens * self.cache_write_input
                )
            return (incoming + usage.output_tokens * self.output) / Decimal(1_000_000)


class SessionPrice(ProbeModel):
    meter: Literal["hosted_container", "agent_session"] = "hosted_container"
    memory_gb: Literal[1, 4, 16, 64] | None = None
    billing: Literal["quantum", "proportional"] = "quantum"
    unit_seconds: int = Field(strict=True, gt=0)
    minimum_seconds: Count = 0
    usd_per_unit: Rate
    effective_from: date
    effective_until: date | None = None
    source: SourceURL

    @model_validator(mode="after")
    def interval(self) -> SessionPrice:
        if (self.meter == "hosted_container") != (self.memory_gb is not None):
            raise ValueError("container memory applies only to hosted-container meters")
        if self.effective_until is not None and self.effective_until <= self.effective_from:
            raise ValueError("invalid session price interval")
        return self

    def cost(self, seconds: int | Decimal) -> Decimal:
        with localcontext() as context:
            context.prec = 80
            units = Decimal(max(seconds, self.minimum_seconds)) / self.unit_seconds
            if self.billing == "quantum":
                units = units.to_integral_value(rounding=ROUND_CEILING)
            return (units * self.usd_per_unit).quantize(Decimal(".000000000000000001"))


class ContainerAllowance(ProbeModel):
    meter: Literal["hosted_container", "agent_session"] = "hosted_container"
    memory_gb: Literal[1, 4, 16, 64] | None = 1
    sessions: Count = 1
    seconds_per_session: Count = 1200


class ContainerUsage(ProbeModel):
    id: Identifier
    meter: Literal["hosted_container", "agent_session"] = "hosted_container"
    memory_gb: Literal[1, 4, 16, 64] | None = None
    seconds: Annotated[Decimal, Field(ge=0, max_digits=16, decimal_places=3)]
    started_at: datetime


class MeasuredRequest(ProbeModel):
    id: Identifier
    observed_at: datetime
    tokens: TokenUsage
    pricing_basis: Identifier | None = None


class ActualSpend(ProbeModel):
    """A complete, non-overlapping request walk plus known container lifetimes."""

    requests: tuple[MeasuredRequest, ...] = ()
    containers: tuple[ContainerUsage, ...] | None = None
    usage_complete: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def unique(self) -> ActualSpend:
        for ids in ([r.id for r in self.requests], [c.id for c in self.containers or ()]):
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate usage evidence")
        return self


class ProviderBudget(ProbeModel):
    cap_usd: Money
    opening_spend_usd: Money = Decimal(0)
    models: dict[Identifier, ModelPrice] = Field(default_factory=dict[str, ModelPrice])
    session_prices: tuple[SessionPrice, ...] = ()


class BudgetConfig(ProbeModel):
    version: Literal[1] = 1
    total_cap_usd: Money = Decimal(150)
    ledger_path: Path
    providers: dict[Identifier, ProviderBudget]
    approved_reconciliations: frozenset[Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]] = (
        frozenset()
    )

    approved_latch_recoveries: frozenset[Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]] = (
        frozenset()
    )
    approved_column_holds: frozenset[Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]] = frozenset()

    @model_validator(mode="after")
    def total(self) -> BudgetConfig:
        if not self.ledger_path.is_absolute():
            raise ValueError("ledger path must be an absolute pin")
        if any(
            sensitive_identifier(name)
            for provider, budget in self.providers.items()
            for name in (
                provider,
                *budget.models,
                *(price.pricing_basis for price in budget.models.values()),
                *(price.source for price in budget.models.values() if price.source is not None),
                *(price.source for price in budget.session_prices),
            )
        ):
            raise ValueError("budget metadata must not contain credentials")
        with localcontext() as context:
            context.prec = 80
            if (
                self.total_cap_usd > 150
                or sum(p.cap_usd for p in self.providers.values()) > self.total_cap_usd
            ):
                raise ValueError("provider caps exceed the approved total")
        return self

    @classmethod
    def load(cls, path: Path) -> BudgetConfig:
        try:
            return cls.model_validate_json(path.read_text())
        except (OSError, ValidationError, ValueError):
            raise BudgetRefused("invalid budget configuration") from None


def omit_none(value: object) -> bool:
    return value is None


class StagingBinding(ProbeModel):
    """Caller-verified deployment/GO/quote pins; hashes are not authorization."""

    attempt_id: Identifier
    leg_id: Identifier
    phase: (
        Literal[
            "baseline", "switched_new", "switched_existing", "reverted_new", "reverted_existing"
        ]
        | Literal["setup", "admin", "smoke", "cleanup"]
    )
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    deployment_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    go_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    envelope_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    quote_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    scope_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    config_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    ledger_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    deadline: datetime
    maximum_hold_usd: Money = Field(gt=0)
    origin: str = Field(pattern=r"^https://[a-zA-Z0-9.-]+(?::[0-9]+)?$")
    requests: tuple[RequestRoute, ...]
    request_limit: Count = Field(gt=0)

    @model_validator(mode="after")
    def bound(self) -> StagingBinding:
        if (
            self.deadline.tzinfo is None
            or not self.requests
            or len(set(self.requests)) != len(self.requests)
        ):
            raise ValueError("staging request binding needs an aware deadline and exact routes")
        names = (self.attempt_id, self.leg_id, self.origin)
        if any(map(sensitive_identifier, names + tuple(path for _, path in self.requests))):
            raise ValueError("staging binding must not contain credentials")
        return self


H6Binding = StagingBinding

# Signed conditional cash; this is not an unconditional native token bound.
I7_TERMS: Final[Mapping[str, tuple[str, str, Decimal, int]]] = MappingProxyType(
    {
        "ROOT": ("anthropic", "claude-haiku-5-5", Decimal(".06"), 3),
        "W1": ("anthropic", "claude-haiku-5-5", Decimal(".04"), 1),
        "W2": ("openai", "gpt-6-luna", Decimal(".055"), 1),
        "W3": ("gemini", "gemini-3.8-flash", Decimal(".49"), 1),
        "W4": ("openai", "gpt-6-luna", Decimal(".055"), 1),
        "RESTORE-SMOKE": ("anthropic", "claude-haiku-5-5", Decimal(".03"), 1),
        "CANDIDATE-SMOKE": ("anthropic", "claude-haiku-5-5", Decimal(".03"), 1),
    }
)


class ProbePlan(ProbeModel):
    provider: Identifier
    model: Identifier
    fixture_id: str = Field(pattern=rf"^(?:{FIXTURE_ID_PATTERN}|A7)$")
    limits: TokenLimits = TokenLimits(input_tokens=20_000, output_tokens=2_000)
    container_allowance: ContainerAllowance | None = None
    h6: H6Binding | None = Field(default=None, exclude_if=omit_none)
    staging: StagingBinding | None = Field(default=None, exclude_if=omit_none)

    @property
    def binding(self) -> StagingBinding | None:
        return self.staging or self.h6

    @model_validator(mode="after")
    def staging_binding(self) -> ProbePlan:
        if self.fixture_id.startswith("H6-") != (self.h6 is not None):
            raise ValueError("staging fixture identity requires its own binding")
        if self.fixture_id.startswith("I7-") != (self.staging is not None):
            raise ValueError("fleet receipt identity requires its own staging binding")
        if self.staging is not None:
            provider, model, hold, _ = I7_TERMS[self.fixture_id.removeprefix("I7-")]
            if (self.provider, self.model, self.staging.maximum_hold_usd) != (
                provider,
                model,
                hold,
            ) or self.staging.leg_id != self.fixture_id.removeprefix("I7-"):
                raise ValueError("fleet identity differs from signed terms")
        return self

    @classmethod
    def create_only(
        cls,
        *,
        provider: str,
        model: str,
        fixture_id: str,
        container_allowance: ContainerAllowance | None = None,
    ) -> ProbePlan:
        return cls(
            provider=provider,
            model=model,
            fixture_id=fixture_id,
            limits=TokenLimits(input_tokens=0, output_tokens=0),
            container_allowance=container_allowance,
        )


class Reconciliation(ProbeModel):
    """Lead signs by pinning digest in trusted config, never by a caller boolean."""

    ledger_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    previous_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    basis: Literal["billed_export", "provider_usage"]
    actual_usd: Money
    token_usd: Money
    container_usd: Money
    price_effective_from: date | None = None

    @model_validator(mode="after")
    def amount(self) -> Reconciliation:
        if self.actual_usd != self.token_usd + self.container_usd:
            raise ValueError("reconciliation components disagree")
        if self.basis == "provider_usage" and self.price_effective_from is None:
            raise ValueError("usage reconciliation needs a dated price table")
        return self

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class LatchRecovery(ProbeModel):
    """Lead-chosen conservative hold; never a billable actual or settlement."""

    operation: Literal["LATCH-RECOVERY"] = "LATCH-RECOVERY"
    ledger_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    previous_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    held_usd: Money

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class RecordedCellReceipt(ProbeModel):
    """Offline execution counters, never evidence of live billable actuals."""

    cell_id: Identifier
    provider: Identifier
    model: Identifier
    receipt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    full_cell_executable: bool = Field(strict=True)
    request_count: Count
    turn_count: Count
    tokens: TokenUsage
    session_count: Count = 0
    max_session_seconds: Count | None = None
    tool_policy: Literal["no_separate_tool_charges", "unknown"] = "unknown"


class ColumnCellPlan(ProbeModel):
    recorded: RecordedCellReceipt
    per_request_limits: TokenLimits
    request_limit: int = Field(strict=True, ge=1, le=1_000_000)
    container_allowance: ContainerAllowance | None = None


class CellCostEstimate(ProbeModel):
    plan: ColumnCellPlan
    limits: TokenLimits
    price: ModelPrice
    session_prices: tuple[SessionPrice, ...]
    estimated_usd: Money
    upper_bound_usd: Money


class ColumnPlan(ProbeModel):
    column_id: Identifier
    provider: Identifier
    model: Identifier
    cells: tuple[ColumnCellPlan, ...] = Field(min_length=1, max_length=53)


class ColumnHoldApproval(ProbeModel):
    """One lead-signed admission, bound to a ledger and its exact cell ceilings."""

    operation: Literal["COLUMN-HOLD"] = "COLUMN-HOLD"
    ledger_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    column_id: Identifier
    provider: Identifier
    model: Identifier
    created_at: datetime
    expires_at: datetime
    cells: tuple[CellCostEstimate, ...] = Field(min_length=1, max_length=53)

    @model_validator(mode="after")
    def scoped(self) -> ColumnHoldApproval:
        if (
            self.created_at.tzinfo is None
            or self.expires_at.tzinfo is None
            or not self.created_at < self.expires_at <= self.created_at + timedelta(days=1)
        ):
            raise ValueError("column approval needs a bounded aware time window")
        names = [cell.plan.recorded.cell_id for cell in self.cells]
        if len(names) != len(set(names)):
            raise ValueError("column cell IDs must be unique")
        if any(
            cell.plan.recorded.provider != self.provider or cell.plan.recorded.model != self.model
            for cell in self.cells
        ):
            raise ValueError("column must use one provider and model")
        if any(
            sensitive_identifier(name)
            for name in (self.column_id, self.provider, self.model, *names)
        ):
            raise ValueError("column metadata must not contain credentials")
        return self

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    @property
    def bound_usd(self) -> Decimal:
        with localcontext() as context:
            context.prec = 80
            return sum((cell.upper_bound_usd for cell in self.cells), Decimal(0))


class ColumnCellReceipt(ProbeModel):
    cell_id: Identifier
    state: Literal["pending", "running", "closed", "not_started"]
    started_at: datetime | None = None
    verdict: Literal["completed", "failed", "cancelled"] | None = None
    actual_evidence: ActualSpend | None = None
    actual_usd: Money | None = None
    token_usd: Money | None = None
    container_usd: Money | None = None
    held_usd: Money
    admission_blocked: bool = Field(default=False, strict=True)


class ColumnState(ProbeModel):
    approval: ColumnHoldApproval
    revision: Count = 0
    terminal_status: Literal["completed", "failed", "cancelled"] | None = None
    cells: tuple[ColumnCellReceipt, ...]


class SpendReceipt(ProbeModel):
    version: Literal[1, 2] = 1
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    provider: Identifier
    model: Identifier
    fixture_id: str = Field(pattern=rf"^(?:{FIXTURE_ID_PATTERN}|A7)$")
    timestamp: datetime
    status: Literal[
        "reserved",
        "completed",
        "uncertain",
        "failed",
        "cancelled",
        "blocked",
        "overrun",
        "reconciled",
        "latch_recovered",
    ]
    tokens: TokenUsage | None = None
    reserved_usd: Money
    cost_estimate_usd: Money
    actual_usd: Money | None = None
    held_usd: Money | None = None
    accounting_status: Literal["actual", "estimated_unverified", "pending"] | None = None
    price: ModelPrice | None = None
    limits: TokenLimits | None = None
    container_allowance: ContainerAllowance | None = None
    session_prices: tuple[SessionPrice, ...] = ()
    actual_evidence: ActualSpend | None = None
    overrun_evidence: ActualSpend | None = None
    admission_blocked: bool = Field(default=False, strict=True)
    token_usd: Money | None = None
    container_usd: Money | None = None
    reconcile: Reconciliation | None = None
    latch_recovery: LatchRecovery | None = None
    column: ColumnState | None = Field(default=None, exclude_if=omit_none)
    h6: H6Binding | None = Field(default=None, exclude_if=omit_none)
    staging: StagingBinding | None = Field(default=None, exclude_if=omit_none)
    dispatches_used: Count = Field(default=0, exclude_if=lambda value: value == 0)

    @property
    def binding(self) -> StagingBinding | None:
        return self.staging or self.h6

    reason: Literal[
        "admitted",
        "settled",
        "budget",
        "unconfigured",
        "probe_error",
        "unknown_usage",
        "overrun",
        "fixture_exists",
        "reconciled",
        "latch_recovery",
    ]

    @model_validator(mode="after")
    def accounting(self) -> SpendReceipt:
        if self.fixture_id.startswith("H6-") != (self.h6 is not None):
            raise ValueError("staging receipt identity requires its own binding")
        if self.fixture_id.startswith("I7-") != (self.staging is not None):
            raise ValueError("fleet receipt identity requires its own staging binding")
        if self.staging is not None:
            provider, model, hold, dispatch_limit = I7_TERMS[self.fixture_id.removeprefix("I7-")]
            if (self.provider, self.model, self.staging.maximum_hold_usd) != (
                provider,
                model,
                hold,
            ) or self.dispatches_used > dispatch_limit:
                raise ValueError("fleet receipt differs from signed terms")
        elif self.dispatches_used:
            raise ValueError("dispatch claims require fleet identity")
        if self.column is not None:
            BudgetGuard.validate_column_receipt(self)
            return self
        # An overrun latches admission independently of its current billable total.
        if self.status == "overrun":
            object.__setattr__(self, "admission_blocked", True)
        if (
            self.overrun_evidence is not None
            and not self.admission_blocked
            and not (
                self.latch_recovery is not None and self.status in ("latch_recovered", "reconciled")
            )
        ):
            raise ValueError("overrun evidence must retain the admission latch")
        # Legacy rows remain byte-for-byte untouched and never gain an actual claim.
        if self.held_usd is None:
            object.__setattr__(self, "held_usd", self.cost_estimate_usd)
        if self.accounting_status is None:
            object.__setattr__(
                self,
                "accounting_status",
                "pending" if self.status == "reserved" else "estimated_unverified",
            )
        if self.status in ("reserved", "uncertain") and self.actual_usd is not None:
            raise ValueError("unsettled runs cannot claim actual spend")
        if self.actual_usd is not None:
            if self.cost_estimate_usd != self.actual_usd + (self.held_usd or Decimal(0)):
                raise ValueError("budget debit must include settled and held dollars")
            if self.held_usd:
                if (
                    self.accounting_status != "estimated_unverified"
                    or self.status != "overrun"
                    or self.overrun_evidence is None
                ):
                    raise ValueError("residual hold requires independent overrun evidence")
            elif self.accounting_status != "actual":
                raise ValueError("fully settled spend must be actual")
        elif self.accounting_status == "actual" or self.held_usd != self.cost_estimate_usd:
            raise ValueError("unverified spend must remain separately held")
        if self.status == "latch_recovered":
            if (
                self.latch_recovery is None
                or self.admission_blocked
                or self.actual_usd is not None
                or self.accounting_status != "estimated_unverified"
                or self.reason != "latch_recovery"
                or self.held_usd != self.latch_recovery.held_usd
                or self.held_usd is None
                or self.held_usd < self.reserved_usd
            ):
                raise ValueError("latch recovery requires an unverified conservative hold")
            return self
        if self.status == "reconciled":
            if (
                self.reconcile is None
                or self.accounting_status != "actual"
                or self.actual_usd != self.reconcile.actual_usd
            ):
                raise ValueError("reconciliation requires signed actual evidence")
            return self
        if (
            self.status in ("reserved", "uncertain", "failed", "cancelled")
            and self.actual_usd is None
        ):
            if self.cost_estimate_usd != self.reserved_usd:
                raise ValueError("unknown spend must retain its reservation")
        elif self.status == "blocked":
            if self.cost_estimate_usd != 0 or self.tokens is not None:
                raise ValueError("refused runs have no usage or cost")
        elif self.status == "overrun":
            if self.actual_usd is None and self.cost_estimate_usd < self.reserved_usd:
                raise ValueError("overruns cannot reduce reserved spend")
        elif (
            self.tokens is None
            or self.tokens.input_tokens is None
            or self.tokens.output_tokens is None
            or self.cost_estimate_usd > self.reserved_usd
        ):
            raise ValueError("completed spend needs measured totals within its reservation")
        return self


class Reservation(ProbeModel):
    receipt: SpendReceipt
    price: ModelPrice


class LedgerBinding(ProbeModel):
    version: Literal[1] = 1
    ledger_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    ledger_path: Path


class LedgerCheckpoint(ProbeModel):
    binding: LedgerBinding
    sequence: Count
    ledger_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    opening_spend: dict[Identifier, Money]
    provider_totals: dict[Identifier, Money]


class LedgerAnchor(ProbeModel):
    binding: LedgerBinding
    sequence: Count


class BudgetGuard:
    """Persist reservations BEFORE I/O; crashed runs remain fully charged.

    All cooperating providers/processes use the same spend.md. flock protects
    both admission and settlement; terminal receipts replace their reservation
    in the accounting view, but both remain in the append-only audit file.
    """

    def __init__(self, config_path: Path, spend_path: Path) -> None:
        self.config_path = config_path
        self.spend_path = spend_path
        self.lock_path = spend_path.with_name(spend_path.name + ".lock")
        self.checkpoint_path = spend_path.with_name(spend_path.name + ".checkpoint.json")

    def _pin(self, config: BudgetConfig) -> None:
        if (
            not self.spend_path.is_absolute()
            or self.spend_path != config.ledger_path
            or self.spend_path.resolve() != self.spend_path
        ):
            raise BudgetLedgerError("ledger path must match the configured absolute pin")

    @staticmethod
    def _sync_directory(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _create(path: Path, content: str) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())

    @classmethod
    def initialize(cls, config_path: Path, spend_path: Path) -> None:
        """Lead-only one-time bootstrap. Existing or partial state is never reset."""
        guard = cls(config_path, spend_path)
        config = BudgetConfig.load(config_path)
        guard._pin(config)
        if any(
            path.exists() or path.is_symlink()
            for path in (guard.lock_path, spend_path, guard.checkpoint_path)
        ):
            raise BudgetLedgerError("ledger state already exists; reconcile instead of resetting")
        binding = LedgerBinding(ledger_id=uuid4().hex, ledger_path=spend_path)
        content = HEADER + "\n" + LEDGER_PREFIX + binding.model_dump_json() + " -->\n\n"
        opening = {
            provider: budget.opening_spend_usd for provider, budget in config.providers.items()
        }
        checkpoint = LedgerCheckpoint(
            binding=binding,
            sequence=0,
            ledger_sha256=hashlib.sha256(content.encode()).hexdigest(),
            opening_spend=opening,
            provider_totals=opening,
        )
        try:
            # O_EXCL on the stable lock arbitrates competing initializers too.
            # A failed initialization leaves its markers for explicit repair.
            guard._create(
                guard.lock_path, LedgerAnchor(binding=binding, sequence=0).model_dump_json() + "\n"
            )
            guard._create(spend_path, content)
            guard._create(guard.checkpoint_path, checkpoint.model_dump_json() + "\n")
            guard._sync_directory(spend_path.parent)
        except OSError:
            raise BudgetLedgerError(
                "ledger initialization failed; reconcile partial state"
            ) from None

    @staticmethod
    def _same_file(file: TextIO, path: Path) -> None:
        try:
            opened = os.fstat(file.fileno())
            named = path.stat(follow_symlinks=False)
        except OSError:
            raise BudgetLedgerError("ledger state missing or replaced") from None
        if not stat.S_ISREG(named.st_mode) or (opened.st_dev, opened.st_ino) != (
            named.st_dev,
            named.st_ino,
        ):
            raise BudgetLedgerError("ledger state missing or replaced")

    @classmethod
    def _existing(cls, path: Path) -> TextIO:
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        except OSError:
            raise BudgetLedgerError(
                "ledger state missing or unsafe; initialize explicitly"
            ) from None
        file = os.fdopen(fd, "r+", encoding="utf-8", newline="")
        try:
            cls._same_file(file, path)
        except BudgetLedgerError:
            file.close()
            raise
        return file

    @staticmethod
    def _digest(file: TextIO) -> str:
        file.seek(0)
        return hashlib.sha256(file.read().encode("utf-8")).hexdigest()

    @staticmethod
    def _totals(runs: dict[str, SpendReceipt], opening: dict[str, Decimal]) -> dict[str, Decimal]:
        totals = dict(opening)
        with localcontext() as context:
            context.prec = 80
            for receipt in runs.values():
                totals[receipt.provider] = (
                    totals.get(receipt.provider, Decimal(0)) + receipt.cost_estimate_usd
                )
        return totals

    @contextmanager
    def _locked(
        self,
    ) -> Iterator[tuple[TextIO, TextIO, LedgerCheckpoint, dict[str, SpendReceipt], BudgetConfig]]:
        with self._existing(self.lock_path) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                self._same_file(lock, self.lock_path)
                config = BudgetConfig.load(self.config_path)
                self._pin(config)
                with self._existing(self.spend_path) as file:
                    with self._existing(self.checkpoint_path) as checkpoint_file:
                        try:
                            checkpoint = LedgerCheckpoint.model_validate_json(
                                checkpoint_file.read()
                            )
                            lock.seek(0)
                            anchor = LedgerAnchor.model_validate_json(lock.read())
                        except (ValidationError, ValueError):
                            raise BudgetLedgerError(
                                "invalid ledger checkpoint or binding"
                            ) from None
                    binding = anchor.binding
                    if (
                        binding != checkpoint.binding
                        or binding.ledger_path != self.spend_path
                        or anchor.sequence != checkpoint.sequence
                    ):
                        raise BudgetLedgerError("ledger binding or checkpoint sequence changed")
                    runs = self._read(file, binding)
                    if (
                        self._digest(file) != checkpoint.ledger_sha256
                        or self._totals(runs, checkpoint.opening_spend)
                        != checkpoint.provider_totals
                    ):
                        raise BudgetLedgerError("ledger disagrees with provider-total checkpoint")
                    yield file, lock, checkpoint, runs, config
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _read(file: TextIO, binding: LedgerBinding) -> dict[str, SpendReceipt]:
        file.seek(0)
        runs: dict[str, SpendReceipt] = {}
        try:
            if file.readline().rstrip("\n") != HEADER:
                raise BudgetLedgerError("ledger missing header; initialize explicitly")
            identity = file.readline().strip()
            if not identity.startswith(LEDGER_PREFIX) or not identity.endswith(" -->"):
                raise BudgetLedgerError("ledger missing its initialization binding")
            if LedgerBinding.model_validate_json(identity[len(LEDGER_PREFIX) : -4]) != binding:
                raise BudgetLedgerError("ledger initialization binding changed")
            for line in file:
                line = line.strip()
                if not line:
                    continue
                if not line.startswith(PREFIX) or not line.endswith(" -->"):
                    raise BudgetLedgerError("unrecognized spend entry; reconcile before running")
                receipt = SpendReceipt.model_validate_json(line[len(PREFIX) : -4])
                if receipt.status == "blocked":
                    continue
                previous = runs.get(receipt.run_id)
                if previous is not None and previous.binding != receipt.binding:
                    raise BudgetLedgerError("staging receipt binding changed")
                if (
                    receipt.column is not None
                    and receipt.column.approval.ledger_id != binding.ledger_id
                ):
                    raise BudgetLedgerError("column hold belongs to another ledger")
                if (
                    previous is not None
                    and previous.admission_blocked
                    and receipt.status != "latch_recovered"
                ):
                    if "admission_blocked" not in receipt.model_fields_set:
                        # Old rows have no independent latch field. Carry the
                        # proven history in memory without rewriting their bytes.
                        object.__setattr__(receipt, "admission_blocked", True)
                    elif not receipt.admission_blocked:
                        raise BudgetLedgerError("overrun admission latch cannot be cleared")
                if previous is None:
                    if receipt.status != "reserved" or receipt.dispatches_used:
                        raise BudgetLedgerError("spend receipt has no reservation")
                    if receipt.column is not None and (
                        receipt.column.revision != 0
                        or receipt.column.terminal_status is not None
                        or any(cell.state != "pending" for cell in receipt.column.cells)
                    ):
                        raise BudgetLedgerError("column needs its original complete hold")
                elif previous.column is not None or receipt.column is not None:
                    BudgetGuard._check_column_transition(previous, receipt)
                elif receipt.status == "latch_recovered":
                    approval = receipt.latch_recovery
                    if approval is None:
                        raise BudgetLedgerError("missing latch recovery approval")
                    BudgetGuard._check_latch_recovery(previous, approval, binding.ledger_id)
                    preserved = (
                        "provider",
                        "model",
                        "fixture_id",
                        "reserved_usd",
                        "tokens",
                        "price",
                        "limits",
                        "container_allowance",
                        "session_prices",
                        "actual_evidence",
                        "overrun_evidence",
                        "token_usd",
                        "container_usd",
                        "reconcile",
                    )
                    if any(getattr(receipt, name) != getattr(previous, name) for name in preserved):
                        raise BudgetLedgerError(
                            "latch recovery cannot change known accounting evidence"
                        )
                elif receipt.status == "reconciled":
                    approval = receipt.reconcile
                    if (
                        receipt.latch_recovery != previous.latch_recovery
                        or previous.status in ("blocked", "reconciled")
                        or previous.actual_usd is not None
                        and not previous.held_usd
                        or previous.actual_usd is not None
                        and approval is not None
                        and approval.actual_usd < previous.actual_usd
                        or approval is None
                        or approval.ledger_id != binding.ledger_id
                        or approval.run_id != previous.run_id
                        or approval.previous_sha256 != BudgetGuard.receipt_digest(previous)
                        or receipt.provider != previous.provider
                        or receipt.model != previous.model
                        or receipt.fixture_id != previous.fixture_id
                        or receipt.reserved_usd != previous.reserved_usd
                    ):
                        raise BudgetLedgerError("invalid reconciliation transition")
                elif previous.staging is not None and receipt.status == "reserved":
                    if previous.status != "reserved" or receipt != previous.model_copy(
                        update={"dispatches_used": previous.dispatches_used + 1}
                    ):
                        raise BudgetLedgerError("invalid fleet dispatch claim")
                elif (
                    previous.status != "reserved"
                    or receipt.status == "reserved"
                    or receipt.provider != previous.provider
                    or receipt.model != previous.model
                    or receipt.fixture_id != previous.fixture_id
                    or receipt.reserved_usd != previous.reserved_usd
                ):
                    raise BudgetLedgerError("invalid spend receipt transition")
                runs[receipt.run_id] = receipt
        except (ValidationError, ValueError, BudgetRefused):
            raise BudgetLedgerError("invalid spend receipt") from None
        return runs

    def _append(
        self,
        file: TextIO,
        receipt: SpendReceipt,
        checkpoint: LedgerCheckpoint,
        opening: dict[str, Decimal],
        lock: TextIO,
    ) -> None:
        receipt = SpendReceipt.model_validate(receipt.model_dump())
        self._same_file(file, self.spend_path)
        self._same_file(lock, self.lock_path)
        anchor = LedgerAnchor(binding=checkpoint.binding, sequence=checkpoint.sequence + 1)
        # Advance the stable inode BEFORE append. Restoring an older ledger and
        # checkpoint together cannot match this independently retained sequence.
        lock.seek(0)
        lock.write(anchor.model_dump_json() + "\n")
        lock.truncate()
        lock.flush()
        os.fsync(lock.fileno())
        self._same_file(lock, self.lock_path)
        file.seek(0, os.SEEK_END)
        file.write(PREFIX + receipt.model_dump_json() + " -->\n")
        file.flush()
        os.fsync(file.fileno())
        runs = self._read(file, checkpoint.binding)
        updated = LedgerCheckpoint(
            binding=checkpoint.binding,
            sequence=anchor.sequence,
            ledger_sha256=self._digest(file),
            opening_spend=opening,
            provider_totals=self._totals(runs, opening),
        )
        temporary = self.checkpoint_path.with_name(self.checkpoint_path.name + "." + uuid4().hex)
        try:
            # A crash between ledger and checkpoint updates refuses future I/O;
            # it cannot roll an uncheckpointed reservation back to zero.
            self._create(temporary, updated.model_dump_json() + "\n")
            with self._existing(self.checkpoint_path):
                os.replace(temporary, self.checkpoint_path)
            self._sync_directory(self.spend_path.parent)
            self._same_file(file, self.spend_path)
        finally:
            temporary.unlink(missing_ok=True)

    def reserve(self, plan: ProbePlan, *, fixture_exists: bool = False) -> Reservation:
        """Preserve the ordinary probe and adapter override admission signature."""
        return self.reserve_with_hold(plan, fixture_exists=fixture_exists)

    def reserve_with_hold(
        self,
        plan: ProbePlan,
        *,
        fixture_exists: bool = False,
        minimum_hold_usd: Decimal = Decimal(0),
        maximum_hold_usd: Decimal | None = None,
        expected_config_sha256: str | None = None,
        expected_ledger_id: str | None = None,
    ) -> Reservation:
        """Persist the larger of the priced bound and an explicit cash floor.

        A floor neither grants live authorization nor changes token/runtime
        limits. The runner verifies its signed envelope before calling this API
        and reads credentials only after successful admission. Unknown spend
        retains the full resulting reservation; complete actual releases it.
        Optional pins and a ceiling bind admission to the runner's verified
        installation and signed financial maximum, under the ledger lock.
        """
        try:
            minimum_hold_usd = TypeAdapter[Decimal](Money).validate_python(
                minimum_hold_usd, strict=True
            )
            if maximum_hold_usd is not None:
                maximum_hold_usd = TypeAdapter[Decimal](Money).validate_python(
                    maximum_hold_usd, strict=True
                )
        except ValidationError:
            raise BudgetRefused("invalid reservation cash floor") from None
        if (
            expected_config_sha256 is not None
            and re.fullmatch(r"[a-f0-9]{64}", expected_config_sha256) is None
        ) or (
            expected_ledger_id is not None
            and re.fullmatch(r"[a-f0-9]{32}", expected_ledger_id) is None
        ):
            raise BudgetRefused("invalid reservation installation pins")
        if any(sensitive_identifier(name) for name in (plan.provider, plan.model)):
            raise BudgetRefused("probe metadata must not contain credentials")
        if plan.binding is not None:
            binding = plan.binding
            if datetime.now(UTC) >= binding.deadline:
                raise BudgetRefused("staging deadline expired")
            if expected_config_sha256 not in (
                None,
                binding.config_sha256,
            ) or expected_ledger_id not in (None, binding.ledger_id):
                raise BudgetRefused("staging financial pins changed")
            expected_config_sha256, expected_ledger_id = binding.config_sha256, binding.ledger_id
            minimum_hold_usd = max(minimum_hold_usd, binding.maximum_hold_usd)
            maximum_hold_usd = (
                binding.maximum_hold_usd
                if maximum_hold_usd is None
                else min(maximum_hold_usd, binding.maximum_hold_usd)
            )
        if maximum_hold_usd is not None and minimum_hold_usd > maximum_hold_usd:
            raise BudgetRefused("reservation cash floor exceeds its ceiling")
        with self._locked() as (file, lock, checkpoint, runs, config):
            if plan.binding is not None:
                for previous in runs.values():
                    if (
                        previous.binding is not None
                        and previous.binding.attempt_id == plan.binding.attempt_id
                    ):
                        if previous.fixture_id == plan.fixture_id and (
                            plan.staging is not None
                            or previous.binding.leg_id == plan.binding.leg_id
                        ):
                            raise BudgetRefused("staging leg already consumed")
                        if plan.staging is None and (
                            previous.status != "completed"
                            or previous.accounting_status != "actual"
                            or previous.admission_blocked
                        ):
                            raise BudgetRefused(
                                "staging attempt has active, failed or unresolved spend"
                            )
            if (
                expected_ledger_id is not None
                and checkpoint.binding.ledger_id != expected_ledger_id
            ):
                raise BudgetRefused("reservation ledger pin changed")
            if expected_config_sha256 is not None:
                try:
                    source = self.config_path.read_bytes()
                    pinned_config = BudgetConfig.model_validate_json(source)
                except (OSError, ValueError):
                    raise BudgetRefused("reservation config pin changed") from None
                if (
                    hashlib.sha256(source).hexdigest() != expected_config_sha256
                    or pinned_config != config
                ):
                    raise BudgetRefused("reservation config pin changed")
            budget = config.providers.get(plan.provider)
            allowed = plan.model in LIVE_MODEL_ALLOWLIST.get(plan.provider, frozenset())
            price = budget.models.get(plan.model) if budget is not None and allowed else None
            today = datetime.now(UTC).date()
            if (
                plan.binding is not None
                and price is not None
                and (
                    (plan.staging is None and plan.model != H6_MODELS.get(plan.provider))
                    or (plan.staging is not None and price.source is None)
                    or price.effective_from is None
                    or price.effective_until is None
                )
            ):
                price = None
            if price is not None and (
                (price.effective_from is not None and today < price.effective_from)
                or (price.effective_until is not None and today >= price.effective_until)
            ):
                price = None
            session_prices = budget.session_prices if budget is not None else ()
            allowance = plan.container_allowance
            session_price = next(
                (
                    p
                    for p in session_prices
                    if allowance is not None
                    and p.meter == allowance.meter
                    and p.memory_gb == allowance.memory_gb
                    and self._price_current(p, datetime.now(UTC))
                ),
                None,
            )
            with localcontext() as context:
                context.prec = 80
                estimate = price.reserve(plan.limits) if price is not None else Decimal(0)
                if allowance is not None and session_price is not None:
                    estimate += allowance.sessions * session_price.cost(
                        allowance.seconds_per_session
                    )
                estimate = max(estimate, minimum_hold_usd)
                if plan.staging is not None:
                    estimate = plan.staging.maximum_hold_usd
            receipt = SpendReceipt(
                version=2,
                price=price,
                limits=plan.limits,
                container_allowance=allowance,
                session_prices=session_prices,
                run_id=uuid4().hex,
                provider=plan.provider,
                model=plan.model,
                fixture_id=plan.fixture_id,
                h6=plan.h6,
                staging=plan.staging,
                timestamp=datetime.now(UTC),
                status="reserved",
                reserved_usd=estimate,
                cost_estimate_usd=estimate,
                reason="admitted",
            )
            opening = dict(checkpoint.opening_spend)
            for provider, configured in config.providers.items():
                opening[provider] = max(
                    opening.get(provider, Decimal(0)), configured.opening_spend_usd
                )
            totals = self._totals(runs, opening)
            reason: Literal["budget", "unconfigured", "fixture_exists"] | None = None
            if (
                budget is None
                or price is None
                or (allowance is not None and session_price is None)
                or (
                    price.actual_input_limit is not None
                    and plan.limits.input_tokens > price.actual_input_limit
                )
            ):
                reason = "unconfigured"
            elif maximum_hold_usd is not None and estimate > maximum_hold_usd:
                reason = "budget"
            elif fixture_exists:
                reason = "fixture_exists"
            else:
                with localcontext() as context:
                    context.prec = 80
                    used = totals.get(plan.provider, Decimal(0))
                    total_used = sum(totals.values())
                    if (
                        used + estimate > budget.cap_usd * Decimal("0.8")
                        or total_used + estimate > config.total_cap_usd * Decimal("0.8")
                    ) or any(
                        r.admission_blocked and r.provider == plan.provider for r in runs.values()
                    ):
                        reason = "budget"
            if reason is not None:
                self._append(
                    file,
                    SpendReceipt.model_validate(
                        {
                            **receipt.model_dump(),
                            "status": "blocked",
                            "cost_estimate_usd": Decimal(0),
                            "held_usd": Decimal(0),
                            "accounting_status": "actual",
                            "actual_usd": Decimal(0),
                            "reason": reason,
                        }
                    ),
                    checkpoint,
                    opening,
                    lock,
                )
                raise BudgetRefused("probe refused before invocation")
            self._append(file, receipt, checkpoint, opening, lock)
        if price is None:
            raise BudgetRefused("model pricing is required")
        return Reservation(receipt=receipt, price=price)

    def require_reservation(
        self, reservation: Reservation, plan: ProbePlan, *, claim_dispatch: bool = False
    ) -> Reservation | None:
        """Recheck an admitted staging leg immediately before transport I/O."""
        binding = plan.binding
        if binding is None or datetime.now(UTC) >= binding.deadline:
            raise BudgetRefused("staging request binding absent or expired")
        with self._locked() as (file, lock, checkpoint, runs, config):
            current = runs.get(reservation.receipt.run_id)
            if (
                current != reservation.receipt
                or current is None
                or current.status != "reserved"
                or (
                    current.fixture_id,
                    current.provider,
                    current.model,
                    current.limits,
                    current.binding,
                )
                != (plan.fixture_id, plan.provider, plan.model, plan.limits, binding)
                or checkpoint.binding.ledger_id != binding.ledger_id
                or hashlib.sha256(self.config_path.read_bytes()).hexdigest()
                != binding.config_sha256
            ):
                raise BudgetRefused("staging reservation changed or was released")
            price = config.providers[plan.provider].models[plan.model]
            if price.effective_until is None or not self._price_current(price, datetime.now(UTC)):
                raise BudgetRefused("staging prices expired")
            if any(
                row.admission_blocked and row.provider == plan.provider for row in runs.values()
            ):
                raise BudgetRefused("staging provider admission is latched")
            if plan.staging is None and any(
                row.binding is not None
                and row.binding.attempt_id == binding.attempt_id
                and row.run_id != reservation.receipt.run_id
                and (
                    row.status != "completed"
                    or row.accounting_status != "actual"
                    or row.admission_blocked
                )
                for row in runs.values()
            ):
                raise BudgetRefused("staging attempt has unresolved spend")
            if plan.staging is not None and claim_dispatch:
                role = plan.fixture_id.removeprefix("I7-")
                dispatch_limit = I7_TERMS[role][3]
                if current.dispatches_used >= dispatch_limit:
                    raise BudgetRefused("fleet dispatch already consumed")
                operations = role.endswith("SMOKE")
                same_scope = [
                    r
                    for r in runs.values()
                    if r.staging is not None
                    and r.staging.attempt_id == binding.attempt_id
                    and r.fixture_id.endswith("SMOKE") == operations
                ]
                with localcontext() as context:
                    context.prec = 80
                    provider_used = sum(
                        (r.cost_estimate_usd for r in same_scope if r.provider == plan.provider),
                        Decimal(0),
                    )
                    stop = (
                        Decimal(".08")
                        if operations
                        else {
                            "anthropic": Decimal(".11"),
                            "openai": Decimal(".12"),
                            "gemini": Decimal(".49"),
                        }[plan.provider]
                    )
                    initial_w3 = (
                        role == "W3"
                        and current.dispatches_used == 0
                        and provider_used == Decimal(".49")
                    )
                    if provider_used >= stop and not initial_w3:
                        raise BudgetRefused("fleet dispatch stop reached")
                updated = current.model_copy(
                    update={"dispatches_used": current.dispatches_used + 1}
                )
                self._append(file, updated, checkpoint, checkpoint.opening_spend, lock)
                return Reservation(receipt=updated, price=reservation.price)

    @staticmethod
    def _price_current(price: SessionPrice | ModelPrice, at: datetime) -> bool:
        return (
            at.tzinfo is not None
            and price.effective_from is not None
            and at.astimezone(UTC).date() >= price.effective_from
            and (price.effective_until is None or at.astimezone(UTC).date() < price.effective_until)
        )

    @staticmethod
    def receipt_digest(receipt: SpendReceipt) -> str:
        # Hash the schema actually present on disk. Adding read-time defaults or
        # inheriting an admission latch must not invalidate a pre-upgrade signature.
        absent = {
            "overrun_evidence",
            "admission_blocked",
            "latch_recovery",
        } - receipt.model_fields_set
        return hashlib.sha256(receipt.model_dump_json(exclude=absent).encode()).hexdigest()

    @staticmethod
    def _measured(
        original: SpendReceipt, actual: ActualSpend
    ) -> tuple[TokenUsage, Decimal | None, Decimal | None, TokenUsage, Decimal]:
        actual = ActualSpend.model_validate(actual.model_dump())
        if any(
            sensitive_identifier(name)
            for name in (
                *(item.id for item in (*actual.requests, *(actual.containers or ()))),
                *(
                    request.pricing_basis
                    for request in actual.requests
                    if request.pricing_basis is not None
                ),
            )
        ):
            raise BudgetRefused("usage metadata must not contain credentials")
        totals: dict[str, int | None] = {}
        for field in TokenUsage.model_fields:
            values = [getattr(request.tokens, field) for request in actual.requests]
            totals[field] = (
                None
                if any(value is None for value in values)
                else sum(value or 0 for value in values)
            )
        tokens = TokenUsage.model_validate(totals)
        # Disjoint requests retain their proven bounds even if another request
        # makes the corresponding actual aggregate unknown.
        lower_tokens = TokenUsage(
            input_tokens=sum(request.tokens.minimum_input_tokens for request in actual.requests),
            output_tokens=sum(request.tokens.output_tokens or 0 for request in actual.requests),
        )
        known_cost = Decimal(0)
        token_cost: Decimal | None = (
            Decimal(0) if actual.usage_complete and original.price is not None else None
        )
        for request in actual.requests:
            if original.price is not None and request.pricing_basis == original.price.pricing_basis:
                known_cost += original.price.minimum_charge(request.tokens, request.observed_at)
            cost = (
                original.price.actual(request.tokens, request.observed_at)
                if original.price is not None
                and request.pricing_basis == original.price.pricing_basis
                else None
            )
            if cost is None:
                token_cost = None
            elif token_cost is not None:
                token_cost += cost
        container_cost: Decimal | None = Decimal(0) if actual.containers is not None else None
        for container in actual.containers or ():
            price = next(
                (
                    p
                    for p in original.session_prices
                    if p.meter == container.meter
                    and p.memory_gb == container.memory_gb
                    and BudgetGuard._price_current(p, container.started_at)
                    and BudgetGuard._price_current(
                        p,
                        container.started_at
                        + timedelta(milliseconds=int(container.seconds * 1000)),
                    )
                ),
                None,
            )
            if price is None:
                container_cost = None
            else:
                cost = price.cost(container.seconds)
                known_cost += cost
                if container_cost is not None:
                    container_cost += cost
        return tokens, token_cost, container_cost, lower_tokens, known_cost

    @staticmethod
    def _exceeded(
        original: SpendReceipt,
        limits: TokenLimits,
        actual: ActualSpend | None,
        lower_tokens: TokenUsage | None,
        known_cost: Decimal,
    ) -> bool:
        container_exceeded = (
            actual is not None
            and original.container_allowance is not None
            and (
                len(actual.containers or ()) > original.container_allowance.sessions
                or any(
                    c.seconds > original.container_allowance.seconds_per_session
                    or c.memory_gb != original.container_allowance.memory_gb
                    or c.meter != original.container_allowance.meter
                    for c in actual.containers or ()
                )
            )
        )
        return (
            container_exceeded
            or known_cost > original.reserved_usd
            or lower_tokens is not None
            and (
                lower_tokens.minimum_input_tokens > limits.input_tokens
                or (lower_tokens.output_tokens or 0) > limits.output_tokens
            )
        )

    def settle(
        self,
        reservation: Reservation,
        *,
        status: Literal["completed", "failed", "cancelled"],
        limits: TokenLimits,
        usage: TokenUsage | None = None,
        actual: ActualSpend | None = None,
    ) -> SpendReceipt:
        """Release holds on complete dated evidence; retain unknown spend.

        Existing adapter overrides keep this signature. For an earlier admission
        overrun independent of the latest billable usage, use settle_with_overrun.
        """
        with localcontext() as context:
            context.prec = 80
            return self._settle(
                reservation, status=status, limits=limits, usage=usage, actual=actual
            )

    def settle_with_overrun(
        self,
        reservation: Reservation,
        *,
        status: Literal["completed", "failed", "cancelled"],
        limits: TokenLimits,
        overrun_evidence: ActualSpend,
        actual: ActualSpend | None = None,
    ) -> SpendReceipt:
        """Settle newest accepted actual while retaining an earlier overrun latch.

        The adapter supplies its newest root-bound, verified, freshness-checked
        actual snapshot, or None when none is accepted. Earlier overrun_evidence
        proves admission refusal; it never replaces actual or adds duplicate
        billable requests. Rejected stale responses must not supersede an already
        accepted verified measurement. Actual must also cover every proof request
        at a non-older observation time and every proven container lifetime;
        otherwise known bounds stay held. Bounds take the maximum per identity
        across both snapshots before summing. Even complete actual retains any
        residual floor above settled dollars. Native freshness checks belong to adapters.
        """
        with localcontext() as context:
            context.prec = 80
            return self._settle(
                reservation,
                status=status,
                limits=limits,
                actual=actual,
                overrun_evidence=overrun_evidence,
            )

    @staticmethod
    def _covers_proof(actual: ActualSpend, proof: ActualSpend) -> bool:
        requests = {request.id: request for request in actual.requests}
        for prior in proof.requests:
            current = requests.get(prior.id)
            if (
                current is None
                or prior.observed_at.tzinfo is None
                or current.observed_at.tzinfo is None
                or current.observed_at < prior.observed_at
            ):
                return False
        containers = {container.id: container for container in actual.containers or ()}
        return all(
            (current := containers.get(prior.id)) is not None
            and current.meter == prior.meter
            and current.memory_gb == prior.memory_gb
            and current.started_at == prior.started_at
            for prior in proof.containers or ()
        )

    @staticmethod
    def _union_floor(
        original: SpendReceipt, proof: ActualSpend, actual: ActualSpend | None
    ) -> Decimal:
        request_bounds: dict[str, Decimal] = {}
        container_bounds: dict[str, Decimal] = {}
        for snapshot in (proof, actual):
            if snapshot is None:
                continue
            for request in snapshot.requests:
                one = ActualSpend(requests=(request,), containers=(), usage_complete=True)
                _, cost, _, lower_tokens, known = BudgetGuard._measured(original, one)
                bound = (
                    cost
                    if cost is not None
                    else max(
                        known,
                        original.price.reserve(
                            TokenLimits(input_tokens=0, output_tokens=0), usage=lower_tokens
                        )
                        if original.price is not None
                        else Decimal(0),
                    )
                )
                previous = request_bounds.get(request.id)
                request_bounds[request.id] = bound if previous is None else max(previous, bound)
            for container in snapshot.containers or ():
                one = ActualSpend(containers=(container,))
                _, _, _, _, bound = BudgetGuard._measured(original, one)
                previous = container_bounds.get(container.id)
                container_bounds[container.id] = bound if previous is None else max(previous, bound)
        with localcontext() as context:
            context.prec = 80
            return sum(request_bounds.values(), Decimal(0)) + sum(
                container_bounds.values(), Decimal(0)
            )

    def _settle(
        self,
        reservation: Reservation,
        *,
        status: Literal["completed", "failed", "cancelled"],
        limits: TokenLimits,
        usage: TokenUsage | None = None,
        actual: ActualSpend | None = None,
        overrun_evidence: ActualSpend | None = None,
    ) -> SpendReceipt:
        """Release holds immediately on complete dated usage and container evidence.

        Failed/cancelled runs can have known actual spend too. Partial walks,
        undated prices and unknown runtime never become actual zero dollars.
        The adapter selects the newest root-bound, verified, freshness-checked
        snapshot for actual. Earlier overrun_evidence proves an admission latch;
        it never replaces actual or adds duplicate billable usage. Unknown/stale
        snapshots must not be supplied as actual. Retain their accepted overrun
        evidence separately, including when a fresh correction lowers the total.
        """
        reservation = Reservation.model_validate(reservation.model_dump())
        original = reservation.receipt
        if original.column is not None:
            raise BudgetLedgerError("use the per-cell column settlement API")
        if original.price is not None and (
            reservation.price != original.price or limits != original.limits
        ):
            raise BudgetLedgerError("reservation price or limits changed")
        if actual is not None and usage is not None:
            raise BudgetLedgerError("provide one non-overlapping usage source")
        token_cost: Decimal | None = None
        container_cost: Decimal | None = None
        lower_tokens = usage
        known_cost = Decimal(0)
        if actual is not None:
            usage, token_cost, container_cost, lower_tokens, known_cost = self._measured(
                original, actual
            )
        estimate = reservation.price.estimate(usage) if usage is not None else None
        measured = (
            token_cost + container_cost
            if token_cost is not None and container_cost is not None
            else None
        )
        reason: Literal["settled", "probe_error", "unknown_usage", "overrun"] = "settled"
        terminal: Literal["completed", "uncertain", "failed", "cancelled", "overrun"] = status
        known_cost = max(known_cost, (token_cost or Decimal(0)) + (container_cost or Decimal(0)))
        exceeded = self._exceeded(original, limits, actual, lower_tokens, known_cost) or (
            actual is None and estimate is not None and estimate > original.reserved_usd
        )
        proof_tokens: TokenUsage | None = None
        proof_cost = Decimal(0)
        if overrun_evidence is not None:
            _, proof_token_cost, proof_container_cost, proof_tokens, proof_cost = self._measured(
                original, overrun_evidence
            )
            proof_cost = max(
                proof_cost,
                (proof_token_cost or Decimal(0)) + (proof_container_cost or Decimal(0)),
            )
            if not self._exceeded(original, limits, overrun_evidence, proof_tokens, proof_cost):
                raise BudgetLedgerError("overrun evidence does not prove a reservation overrun")
            exceeded = True
            if actual is not None and not self._covers_proof(actual, overrun_evidence):
                # A different request or an older revision cannot erase proven
                # spend, even if that partial walk claims to be complete.
                measured = None
        if exceeded:
            terminal, reason = "overrun", "overrun"
        elif measured is not None:
            reason = "settled"
        elif status != "completed":
            reason = "probe_error"
        elif (
            estimate is None
            or actual is not None
            or original.container_allowance is not None
            or original.binding is not None
        ):
            terminal, reason = "uncertain", "unknown_usage"
        charged = measured if measured is not None else original.reserved_usd
        # Backward-compatible undated estimates are explicitly unverified holds.
        # Staging cash bounds also cover setup, admin and cleanup exposure;
        # token counts alone cannot establish their actual spend or release room.
        if (
            actual is None
            and original.binding is None
            and original.container_allowance is None
            and status == "completed"
            and estimate is not None
        ):
            charged = estimate
        if terminal == "overrun" and measured is None and overrun_evidence is None:
            charged = max(charged, original.reserved_usd, known_cost, proof_cost)
            if lower_tokens is not None:
                charged = max(charged, reservation.price.reserve(limits, usage=lower_tokens))
            if proof_tokens is not None:
                charged = max(charged, reservation.price.reserve(limits, usage=proof_tokens))
        held = Decimal(0) if measured is not None else charged
        if overrun_evidence is not None:
            floor = self._union_floor(original, overrun_evidence, actual)
            if measured is None:
                # Unknown final usage retains its reservation as well as the
                # identity union's floor. No missing measurement becomes actual zero.
                floor = max(floor, original.reserved_usd)
            held = max(floor - (measured if measured is not None else Decimal(0)), Decimal(0))
            charged = (measured if measured is not None else Decimal(0)) + held
        receipt = SpendReceipt.model_validate(
            {
                **original.model_dump(),
                "version": 2,
                "timestamp": datetime.now(UTC),
                "status": terminal,
                "tokens": usage,
                "cost_estimate_usd": charged,
                "actual_usd": measured,
                "held_usd": held,
                "accounting_status": "actual"
                if measured is not None and held == 0
                else "estimated_unverified",
                "actual_evidence": actual,
                "overrun_evidence": overrun_evidence,
                "admission_blocked": exceeded,
                "token_usd": token_cost,
                "container_usd": container_cost,
                "reason": reason,
            }
        )
        with self._locked() as (file, lock, checkpoint, runs, _config):
            current = runs.get(original.run_id)
            if current != original:
                raise BudgetLedgerError("reservation was already settled or changed")
            self._append(file, receipt, checkpoint, checkpoint.opening_spend, lock)
        return receipt

    def report(self) -> tuple[SpendReceipt, ...]:
        """Current run rows, each with actual_usd, held_usd and accounting_status."""
        with self._locked() as (_file, _lock, _checkpoint, runs, _config):
            return tuple(runs.values())

    def propose_reconciliation(
        self,
        run_id: str,
        *,
        evidence_sha256: str,
        basis: Literal["billed_export", "provider_usage"],
        token_usd: Decimal,
        container_usd: Decimal,
        price_effective_from: date | None = None,
    ) -> Reconciliation:
        """Read-only proposal; evidence hash binds the reviewed export/calculation."""
        with self._locked() as (_file, _lock, checkpoint, runs, _config):
            current = runs.get(run_id)
            if current is not None and current.column is not None:
                raise BudgetLedgerError("use the per-cell column settlement API")
            if (
                current is None
                or current.actual_usd is not None
                and not current.held_usd
                or current.status == "reconciled"
            ):
                raise BudgetLedgerError("reconciliation requires a held run")
            return Reconciliation(
                ledger_id=checkpoint.binding.ledger_id,
                run_id=run_id,
                previous_sha256=self.receipt_digest(current),
                evidence_sha256=evidence_sha256,
                basis=basis,
                actual_usd=token_usd + container_usd,
                token_usd=token_usd,
                container_usd=container_usd,
                price_effective_from=price_effective_from,
            )

    def reconcile(self, proposal: Reconciliation) -> SpendReceipt:
        """Lead-only RECONCILE append. The operator pins proposal.digest in config.

        No private key, provider key, caller boolean, deletion or ledger rewrite.
        Config is trusted operator state, as for existing caps/initialization.
        A residual overrun hold may coexist with verified settled actual; the
        approved total cannot remove that known settled amount.
        A reserved run may be reconciled for lead-approved crash recovery. This
        fences any later worker settlement as already settled; do not reconcile
        a run whose worker is still expected to finish.
        """
        proposal = Reconciliation.model_validate(proposal.model_dump())
        with self._locked() as (file, lock, checkpoint, runs, config):
            if proposal.digest not in config.approved_reconciliations:
                raise BudgetRefused("reconciliation needs the lead-signed proposal digest")
            current = runs.get(proposal.run_id)
            if current is not None and current.column is not None:
                raise BudgetLedgerError("use the per-cell column settlement API")
            if (
                current is None
                or current.actual_usd is not None
                and not current.held_usd
                or current.status == "reconciled"
            ):
                raise BudgetLedgerError("reconciliation requires a held run")
            if current.actual_usd is not None and proposal.actual_usd < current.actual_usd:
                raise BudgetRefused("reconciliation cannot remove verified settled dollars")
            if (
                proposal.ledger_id != checkpoint.binding.ledger_id
                or proposal.previous_sha256 != self.receipt_digest(current)
            ):
                raise BudgetLedgerError("reconciliation is foreign or stale")
            receipt = SpendReceipt.model_validate(
                {
                    **current.model_dump(),
                    "version": 2,
                    "status": "reconciled",
                    "reason": "reconciled",
                    "timestamp": datetime.now(UTC),
                    "accounting_status": "actual",
                    "actual_usd": proposal.actual_usd,
                    "held_usd": Decimal(0),
                    "cost_estimate_usd": proposal.actual_usd,
                    "token_usd": proposal.token_usd,
                    "container_usd": proposal.container_usd,
                    "reconcile": proposal,
                }
            )
            self._append(file, receipt, checkpoint, checkpoint.opening_spend, lock)
            return receipt

    @staticmethod
    def _latch_recovery_floor(current: SpendReceipt) -> Decimal:
        """Reservation and every known spend bound survive an operator recovery."""
        floor = max(current.reserved_usd, current.held_usd or Decimal(0))
        if current.tokens is not None and current.price is not None:
            floor = max(
                floor,
                current.price.reserve(
                    TokenLimits(input_tokens=0, output_tokens=0), usage=current.tokens
                ),
            )
        proof = current.overrun_evidence or current.actual_evidence
        if proof is not None:
            floor = max(floor, BudgetGuard._union_floor(current, proof, current.actual_evidence))
        return floor

    @staticmethod
    def _check_latch_recovery(
        current: SpendReceipt, proposal: LatchRecovery, ledger_id: str
    ) -> None:
        if current.column is not None:
            raise BudgetLedgerError(
                "column overruns require owner review; recovery is not supported"
            )
        if (
            current.status != "overrun"
            or not current.admission_blocked
            or current.actual_usd is not None
        ):
            raise BudgetLedgerError("latch recovery requires a latched run with incomplete actuals")
        if (
            proposal.run_id != current.run_id
            or proposal.ledger_id != ledger_id
            or proposal.previous_sha256 != BudgetGuard.receipt_digest(current)
        ):
            raise BudgetLedgerError("latch recovery is foreign or stale")
        if proposal.held_usd < BudgetGuard._latch_recovery_floor(current):
            raise BudgetLedgerError("latch recovery hold is below reservation or proven spend")

    def propose_latch_recovery(
        self, run_id: str, *, held_usd: Decimal, evidence_sha256: str
    ) -> LatchRecovery:
        """Read-only proposal; lead reviews the evidence and pins the exact digest."""
        with self._locked() as (_file, _lock, checkpoint, runs, _config):
            current = runs.get(run_id)
            if current is None:
                raise BudgetLedgerError("latch recovery requires an existing run")
            proposal = LatchRecovery(
                ledger_id=checkpoint.binding.ledger_id,
                run_id=run_id,
                previous_sha256=self.receipt_digest(current),
                evidence_sha256=evidence_sha256,
                held_usd=held_usd,
            )
            self._check_latch_recovery(current, proposal, checkpoint.binding.ledger_id)
            return proposal

    def recover_latch(self, proposal: LatchRecovery) -> SpendReceipt:
        """Append a lead-signed LATCH-RECOVERY, retaining unknown actuals and evidence.

        Only incomplete latched runs qualify. This fences a later settlement by
        the old worker; it never invents or removes settled dollars. The chosen
        hold cannot be below the reservation, current hold or any proven bound.
        Other provider latches and normal budget caps still govern admission.
        RECONCILE remains the separate path for verified billable actuals.
        """
        proposal = LatchRecovery.model_validate(proposal.model_dump())
        with self._locked() as (file, lock, checkpoint, runs, config):
            if proposal.digest not in config.approved_latch_recoveries:
                raise BudgetRefused("latch recovery needs the lead-signed proposal digest")
            current = runs.get(proposal.run_id)
            if current is None:
                raise BudgetLedgerError("latch recovery requires an existing run")
            self._check_latch_recovery(current, proposal, checkpoint.binding.ledger_id)
            receipt = SpendReceipt.model_validate(
                {
                    **current.model_dump(),
                    "version": 2,
                    "timestamp": datetime.now(UTC),
                    "status": "latch_recovered",
                    "reason": "latch_recovery",
                    "accounting_status": "estimated_unverified",
                    "actual_usd": None,
                    "held_usd": proposal.held_usd,
                    "cost_estimate_usd": proposal.held_usd,
                    "admission_blocked": False,
                    "latch_recovery": proposal,
                }
            )
            self._append(file, receipt, checkpoint, checkpoint.opening_spend, lock)
            return receipt

    @staticmethod
    def _estimate_column_cell(
        plan: ColumnCellPlan,
        price: ModelPrice,
        session_prices: tuple[SessionPrice, ...],
        at: datetime,
    ) -> CellCostEstimate:
        recorded = plan.recorded
        if (
            not recorded.full_cell_executable
            or recorded.tool_policy != "no_separate_tool_charges"
            or plan.request_limit < recorded.request_count
            or price.effective_from is None
            or price.source is None
            or at.tzinfo is None
            or at.astimezone(UTC).date() < price.effective_from
            or price.effective_until is not None
            and at.astimezone(UTC).date() >= price.effective_until
            or price.actual_input_limit is not None
            and plan.per_request_limits.input_tokens > price.actual_input_limit
        ):
            raise BudgetRefused("cell needs executable receipts and current bounded pricing")
        limits = TokenLimits(
            input_tokens=plan.per_request_limits.input_tokens * plan.request_limit,
            output_tokens=plan.per_request_limits.output_tokens * plan.request_limit,
        )
        if (
            recorded.tokens.minimum_input_tokens > limits.input_tokens
            or (recorded.tokens.output_tokens or 0) > limits.output_tokens
        ):
            raise BudgetRefused("cell ceilings cannot undercount recorded execution")
        bound = price.reserve(limits)
        estimate = price.estimate(recorded.tokens)
        allowance = plan.container_allowance
        if allowance is None:
            if (
                recorded.provider in ("openai", "anthropic")
                or recorded.session_count
                or recorded.max_session_seconds
            ):
                raise BudgetRefused("recorded infrastructure needs an explicit allowance")
        else:
            session_price = next(
                (
                    p
                    for p in session_prices
                    if p.meter == allowance.meter
                    and p.memory_gb == allowance.memory_gb
                    and BudgetGuard._price_current(p, at)
                ),
                None,
            )
            if (
                session_price is None
                or recorded.session_count > allowance.sessions
                or (recorded.max_session_seconds or 0) > allowance.seconds_per_session
            ):
                raise BudgetRefused(
                    "cell infrastructure needs current pricing and sufficient ceilings"
                )
            with localcontext() as context:
                context.prec = 80
                bound += allowance.sessions * session_price.cost(allowance.seconds_per_session)
                if estimate is not None:
                    estimate += recorded.session_count * session_price.cost(
                        recorded.max_session_seconds
                        if recorded.max_session_seconds is not None
                        else allowance.seconds_per_session
                    )
        return CellCostEstimate(
            plan=plan,
            limits=limits,
            price=price,
            session_prices=session_prices,
            estimated_usd=bound if estimate is None else estimate,
            upper_bound_usd=bound,
        )

    def propose_column_hold(self, plan: ColumnPlan) -> ColumnHoldApproval:
        """Read-only quote. The lead pins its digest before any hold or provider I/O."""
        plan = ColumnPlan.model_validate(plan.model_dump())
        with self._locked() as (_file, _lock, checkpoint, _runs, config):
            budget = config.providers.get(plan.provider)
            if budget is None or plan.model not in LIVE_MODEL_ALLOWLIST.get(plan.provider, ()):
                raise BudgetRefused("column provider/model is not allowlisted")
            price = budget.models.get(plan.model)
            if price is None:
                raise BudgetRefused("column model needs reviewed pricing")
            now = datetime.now(UTC)
            cells = tuple(
                self._estimate_column_cell(cell, price, budget.session_prices, now)
                for cell in plan.cells
            )
            return ColumnHoldApproval(
                ledger_id=checkpoint.binding.ledger_id,
                run_id=uuid4().hex,
                column_id=plan.column_id,
                provider=plan.provider,
                model=plan.model,
                created_at=now,
                expires_at=now + timedelta(hours=1),
                cells=cells,
            )

    def reserve_column(self, approval: ColumnHoldApproval) -> SpendReceipt:
        """Admit the entire column atomically, including other runs' actuals and holds."""
        approval = ColumnHoldApproval.model_validate(approval.model_dump())
        with self._locked() as (file, lock, checkpoint, runs, config):
            if approval.digest not in config.approved_column_holds:
                raise BudgetRefused("column hold needs the lead-signed proposal digest")
            if approval.ledger_id != checkpoint.binding.ledger_id:
                raise BudgetLedgerError("column hold belongs to another ledger")
            prior = runs.get(approval.run_id)
            if prior is not None:
                if prior.column is None or prior.column.approval != approval:
                    raise BudgetLedgerError("column hold ID already belongs to another run")
                if prior.column.terminal_status is not None:
                    raise BudgetRefused("terminal column approval cannot be reused")
                return prior
            if any(
                r.column is not None and r.column.approval.column_id == approval.column_id
                for r in runs.values()
            ):
                raise BudgetRefused("column ID already admitted")
            now = datetime.now(UTC)
            if not approval.created_at <= now < approval.expires_at:
                raise BudgetRefused("column approval is expired or not yet valid")
            budget = config.providers.get(approval.provider)
            if budget is None or approval.model not in LIVE_MODEL_ALLOWLIST.get(
                approval.provider, ()
            ):
                raise BudgetRefused("column provider/model is not allowlisted")
            price = budget.models.get(approval.model)
            if price is None or any(
                self._estimate_column_cell(c.plan, price, budget.session_prices, now) != c
                for c in approval.cells
            ):
                raise BudgetRefused("column pricing or bounds changed since approval")
            opening = dict(checkpoint.opening_spend)
            for provider, configured in config.providers.items():
                opening[provider] = max(
                    opening.get(provider, Decimal(0)), configured.opening_spend_usd
                )
            totals = self._totals(runs, opening)
            with localcontext() as context:
                context.prec = 80
                if (
                    totals.get(approval.provider, Decimal(0)) + approval.bound_usd
                    > budget.cap_usd * Decimal(".8")
                    or sum(totals.values(), Decimal(0)) + approval.bound_usd
                    > config.total_cap_usd * Decimal(".8")
                    or any(
                        r.admission_blocked and r.provider == approval.provider
                        for r in runs.values()
                    )
                ):
                    raise BudgetRefused("column bound exceeds the remaining provider/total line")
            state = ColumnState(
                approval=approval,
                cells=tuple(
                    ColumnCellReceipt(
                        cell_id=c.plan.recorded.cell_id,
                        state="pending",
                        held_usd=c.upper_bound_usd,
                    )
                    for c in approval.cells
                ),
            )
            receipt = self._column_receipt(state, now)
            self._append(file, receipt, checkpoint, opening, lock)
            return receipt

    @staticmethod
    def _column_cell_price(
        quote: CellCostEstimate, cell: ColumnCellReceipt, approval: ColumnHoldApproval
    ) -> ColumnCellReceipt:
        if cell.state in ("pending", "running"):
            if cell.verdict is not None or cell.actual_evidence is not None:
                raise ValueError("unsettled column cells cannot carry terminal evidence")
            if cell.state == "pending" and cell.started_at is not None:
                raise ValueError("pending cells have no claim timestamp")
            if cell.state == "running" and (
                cell.started_at is None
                or cell.started_at.tzinfo is None
                or cell.started_at < approval.created_at
            ):
                raise ValueError("running cell needs its persisted claim timestamp")
            return ColumnCellReceipt(
                cell_id=cell.cell_id,
                state=cell.state,
                started_at=cell.started_at,
                held_usd=quote.upper_bound_usd,
            )
        if cell.state == "not_started":
            if (
                cell.actual_evidence is not None
                or cell.verdict != "cancelled"
                or cell.started_at is not None
            ):
                raise ValueError("never-started cells cannot claim usage")
            return ColumnCellReceipt(
                cell_id=cell.cell_id,
                state="not_started",
                verdict="cancelled",
                held_usd=Decimal(0),
                actual_usd=Decimal(0),
                token_usd=Decimal(0),
                container_usd=Decimal(0),
            )
        if (
            cell.verdict is None
            or cell.started_at is None
            or cell.started_at.tzinfo is None
            or cell.started_at < approval.created_at
        ):
            raise ValueError("closed cell needs its execution verdict")
        original = SpendReceipt(
            version=2,
            run_id=approval.run_id,
            provider=approval.provider,
            model=approval.model,
            fixture_id="F1",
            timestamp=approval.created_at,
            status="reserved",
            reason="admitted",
            reserved_usd=quote.upper_bound_usd,
            cost_estimate_usd=quote.upper_bound_usd,
            price=quote.price,
            limits=quote.limits,
            container_allowance=quote.plan.container_allowance,
            session_prices=quote.session_prices,
        )
        actual = cell.actual_evidence
        measured: Decimal | None = None
        token: Decimal | None = None
        container: Decimal | None = None
        blocked = False
        floor = quote.upper_bound_usd
        if actual is not None:
            if any(
                r.observed_at.tzinfo is None or r.observed_at < cell.started_at
                for r in actual.requests
            ) or any(
                c.started_at.tzinfo is None or c.started_at < cell.started_at
                for c in actual.containers or ()
            ):
                raise ValueError("usage is stale or predates the cell's I/O claim")
            with localcontext() as context:
                context.prec = 80
                # Measurement accumulates costs too: preserve its digits before
                # pricing totals, comparing bounds or persisting the receipt.
                _, token, container, lower, known = BudgetGuard._measured(original, actual)
                measured = (
                    token + container if token is not None and container is not None else None
                )
                blocked = (
                    BudgetGuard._exceeded(original, quote.limits, actual, lower, known)
                    or len(actual.requests) > quote.plan.request_limit
                    or any(
                        r.tokens.minimum_input_tokens > quote.plan.per_request_limits.input_tokens
                        or (r.tokens.output_tokens or 0)
                        > quote.plan.per_request_limits.output_tokens
                        for r in actual.requests
                    )
                    or bool(actual.containers)
                    and quote.plan.container_allowance is None
                )
                floor = max(
                    floor,
                    known,
                    quote.upper_bound_usd
                    - quote.price.reserve(quote.limits)
                    + quote.price.reserve(quote.limits, usage=lower),
                )
        return ColumnCellReceipt(
            cell_id=cell.cell_id,
            state="closed",
            started_at=cell.started_at,
            verdict=cell.verdict,
            actual_evidence=actual,
            actual_usd=measured,
            token_usd=token,
            container_usd=container,
            held_usd=Decimal(0) if measured is not None else floor,
            admission_blocked=blocked,
        )

    @staticmethod
    def _column_receipt_fields(state: ColumnState, timestamp: datetime) -> dict[str, object]:
        """Pure totals shared by receipt construction and independent validation."""
        with localcontext() as context:
            context.prec = 80
            known = [c.actual_usd for c in state.cells if c.actual_usd is not None]
            actual = sum(known, Decimal(0)) if known else None
            held = sum((c.held_usd for c in state.cells), Decimal(0))
            token = sum(
                (c.token_usd or Decimal(0) for c in state.cells if c.actual_usd is not None),
                Decimal(0),
            )
            container = sum(
                (c.container_usd or Decimal(0) for c in state.cells if c.actual_usd is not None),
                Decimal(0),
            )
            charged = (actual or Decimal(0)) + held
        blocked = any(c.admission_blocked for c in state.cells)
        unknown = any(c.state == "closed" and c.actual_usd is None for c in state.cells)
        status = (
            "reserved"
            if state.terminal_status is None
            else "overrun"
            if blocked
            else "uncertain"
            if unknown and state.terminal_status == "completed"
            else state.terminal_status
        )
        return dict(
            version=2,
            run_id=state.approval.run_id,
            provider=state.approval.provider,
            model=state.approval.model,
            fixture_id="F1",
            timestamp=timestamp,
            status=status,
            reserved_usd=state.approval.bound_usd,
            cost_estimate_usd=charged,
            actual_usd=actual,
            held_usd=held,
            token_usd=token if known else None,
            container_usd=container if known else None,
            admission_blocked=blocked,
            accounting_status="actual"
            if actual is not None and held == 0
            else "estimated_unverified"
            if unknown or state.terminal_status is not None
            else "pending",
            reason="overrun"
            if blocked
            else "admitted"
            if state.revision == 0
            else "unknown_usage"
            if unknown
            else "settled",
            column=state,
        )

    @staticmethod
    def _column_receipt(state: ColumnState, timestamp: datetime) -> SpendReceipt:
        return SpendReceipt.model_validate(BudgetGuard._column_receipt_fields(state, timestamp))

    @staticmethod
    def validate_column_receipt(receipt: SpendReceipt) -> None:
        """Validate a column's pinned pricing, disjoint usage and exact accounting."""
        state = receipt.column
        if state is None:
            raise ValueError("missing column state")
        approval = state.approval
        if tuple(c.cell_id for c in state.cells) != tuple(
            c.plan.recorded.cell_id for c in approval.cells
        ):
            raise ValueError("column cells changed")
        if state.terminal_status is not None and any(
            c.state in ("pending", "running") for c in state.cells
        ):
            raise ValueError("terminal column cannot leave a cell claim open")
        requests: set[str] = set()
        containers: set[str] = set()
        for quote, cell in zip(approval.cells, state.cells, strict=True):
            if cell.started_at is not None and cell.started_at > receipt.timestamp:
                raise ValueError("claim timestamp is newer than the ledger receipt")
            if (
                BudgetGuard._estimate_column_cell(
                    quote.plan, quote.price, quote.session_prices, approval.created_at
                )
                != quote
                or BudgetGuard._column_cell_price(quote, cell, approval) != cell
            ):
                raise ValueError("column estimate or settlement disagrees with pinned evidence")
            if cell.actual_evidence is not None:
                ids = {r.id for r in cell.actual_evidence.requests}
                infra = {c.id for c in cell.actual_evidence.containers or ()}
                if requests & ids or containers & infra:
                    raise ValueError("column cell usage must be disjoint")
                requests.update(ids)
                containers.update(infra)
        expected = {
            name: field.get_default(call_default_factory=True)
            for name, field in SpendReceipt.model_fields.items()
            if field.exclude_if is None
            or not field.exclude_if(field.get_default(call_default_factory=True))
        }
        expected.update(BudgetGuard._column_receipt_fields(state, receipt.timestamp))
        expected["column"] = state.model_dump()
        if receipt.model_dump() != expected or receipt.timestamp.tzinfo is None:
            raise ValueError("column accounting disagrees with cell totals")

    @staticmethod
    def _check_column_transition(previous: SpendReceipt, current: SpendReceipt) -> None:
        before, after = previous.column, current.column
        if (
            before is None
            or after is None
            or before.approval != after.approval
            or after.revision != before.revision + 1
            or current.timestamp < previous.timestamp
            or (current.actual_usd or 0) < (previous.actual_usd or 0)
            or before.terminal_status is not None
            and after.terminal_status != before.terminal_status
        ):
            raise BudgetLedgerError("column identity, revision or settled actual regressed")
        finishing = before.terminal_status is None and after.terminal_status is not None
        changed = 0
        for quote, old, new in zip(after.approval.cells, before.cells, after.cells, strict=True):
            if old == new:
                continue
            if old.started_at is not None and old.started_at != new.started_at:
                raise BudgetLedgerError("column claim timestamp cannot change")
            changed += 1
            if finishing:
                allowed = (
                    old.state == "pending"
                    and new.state == "not_started"
                    or (
                        old.state == "running"
                        and new.state == "closed"
                        and new.verdict == "cancelled"
                        and new.actual_evidence is None
                    )
                )
            else:
                allowed = (
                    before.terminal_status is None
                    and not previous.admission_blocked
                    and current.timestamp < before.approval.expires_at
                    and old.state == "pending"
                    and new.state == "running"
                    or old.state == "running"
                    and new.state == "closed"
                    or old.state == new.state == "closed"
                    and old.actual_usd is None
                    and new.actual_evidence is not None
                )
            if not allowed or old.admission_blocked and not new.admission_blocked:
                raise BudgetLedgerError("column claim or admission latch regressed")
            if old.actual_evidence is not None and (
                new.actual_evidence is None
                or not BudgetGuard._column_evidence_covers(
                    quote, new.actual_evidence, old.actual_evidence
                )
            ):
                raise BudgetLedgerError("cell evidence cannot drop charges or become stale")
        if not finishing and changed != 1:
            raise BudgetLedgerError("column update must change exactly one cell")

    @staticmethod
    def _column_evidence_covers(
        quote: CellCostEstimate, actual: ActualSpend, proof: ActualSpend
    ) -> bool:
        if (
            not BudgetGuard._covers_proof(actual, proof)
            or proof.usage_complete
            and not actual.usage_complete
            or proof.containers is not None
            and actual.containers is None
        ):
            return False
        requests = {r.id: r for r in actual.requests}
        for old in proof.requests:
            new = requests[old.id]
            if any(
                (prior := getattr(old.tokens, field)) is not None
                and ((current := getattr(new.tokens, field)) is None or current < prior)
                for field in TokenUsage.model_fields
            ):
                return False
            if old.pricing_basis == quote.price.pricing_basis and (
                new.pricing_basis != old.pricing_basis
                or quote.price.minimum_charge(new.tokens, new.observed_at)
                < quote.price.minimum_charge(old.tokens, old.observed_at)
            ):
                return False
        containers = {c.id: c for c in actual.containers or ()}
        return all(containers[c.id].seconds >= c.seconds for c in proof.containers or ())

    def _update_column_cell(
        self,
        run_id: str,
        cell_id: str,
        *,
        start: bool,
        actual: ActualSpend | None = None,
        status: Literal["completed", "failed", "cancelled"] = "completed",
    ) -> SpendReceipt:
        with self._locked() as (file, lock, checkpoint, runs, config):
            original = runs.get(run_id)
            if original is None or original.column is None:
                raise BudgetLedgerError("column hold does not exist")
            state = original.column
            index = next((i for i, c in enumerate(state.cells) if c.cell_id == cell_id), None)
            if index is None:
                raise BudgetLedgerError("cell does not belong to this column hold")
            old = state.cells[index]
            opening = checkpoint.opening_spend
            if start:
                budget = config.providers.get(original.provider)
                opening = dict(checkpoint.opening_spend)
                for provider, configured in config.providers.items():
                    opening[provider] = max(
                        opening.get(provider, Decimal(0)), configured.opening_spend_usd
                    )
                totals = self._totals(runs, opening)
                with localcontext() as context:
                    context.prec = 80
                    line_exceeded = (
                        budget is None
                        or totals.get(original.provider, Decimal(0))
                        > budget.cap_usd * Decimal(".8")
                        or sum(totals.values(), Decimal(0)) > config.total_cap_usd * Decimal(".8")
                    )
                if (
                    state.terminal_status is not None
                    or datetime.now(UTC) >= state.approval.expires_at
                    or any(
                        r.admission_blocked and r.provider == original.provider
                        for r in runs.values()
                    )
                    or budget is None
                    or budget.models.get(original.model) != state.approval.cells[0].price
                    or budget.session_prices != state.approval.cells[0].session_prices
                    or any(
                        self._estimate_column_cell(
                            quote.plan, quote.price, quote.session_prices, datetime.now(UTC)
                        )
                        != quote
                        for quote in state.approval.cells
                    )
                    or line_exceeded
                ):
                    raise BudgetRefused("terminal/overrun column refuses new cell claims")
                if old.state != "pending":
                    raise BudgetLedgerError("column cell already claimed")
                new = old.model_copy(update={"state": "running", "started_at": datetime.now(UTC)})
            else:
                if old.state not in ("running", "closed"):
                    raise BudgetLedgerError("cell must be claimed before provider I/O")
                if actual is not None:
                    actual = ActualSpend.model_validate(actual.model_dump())
                candidate = ColumnCellReceipt(
                    cell_id=cell_id,
                    state="closed",
                    verdict=status,
                    started_at=old.started_at,
                    actual_evidence=actual,
                    held_usd=Decimal(0),
                )
                try:
                    new = self._column_cell_price(
                        state.approval.cells[index], candidate, state.approval
                    )
                except (ValueError, BudgetRefused):
                    raise BudgetLedgerError("invalid or stale column usage evidence") from None
                if old == new:
                    return original
            cells = list(state.cells)
            cells[index] = new
            updated = state.model_copy(
                update={"cells": tuple(cells), "revision": state.revision + 1}
            )
            try:
                receipt = SpendReceipt.model_validate(
                    self._column_receipt(updated, datetime.now(UTC)).model_dump()
                )
            except (ValueError, BudgetRefused):
                raise BudgetLedgerError("invalid or overlapping column usage evidence") from None
            self._check_column_transition(original, receipt)
            self._append(file, receipt, checkpoint, opening, lock)
            return receipt

    def start_column_cell(self, run_id: str, cell_id: str) -> SpendReceipt:
        """Claim once BEFORE I/O. Pending cells alone can later release unused holds."""
        return self._update_column_cell(run_id, cell_id, start=True)

    def settle_column_cell(
        self,
        run_id: str,
        cell_id: str,
        *,
        actual: ActualSpend | None = None,
        status: Literal["completed", "failed", "cancelled"] = "completed",
    ) -> SpendReceipt:
        """Settle complete dated evidence; uncertain cells retain their bound.

        Exact replays are idempotent. Unknown cells may accept a newer covering
        snapshot after column termination; settled actuals cannot be revised away.
        """
        return self._update_column_cell(run_id, cell_id, start=False, actual=actual, status=status)

    def finish_column(
        self, run_id: str, *, status: Literal["completed", "failed", "cancelled"]
    ) -> SpendReceipt:
        """Fence new claims, release never-started cells, preserve uncertain charges."""
        with self._locked() as (file, lock, checkpoint, runs, _config):
            original = runs.get(run_id)
            if original is None or original.column is None:
                raise BudgetLedgerError("column hold does not exist")
            state = original.column
            if state.terminal_status is not None:
                if state.terminal_status == status:
                    return original
                raise BudgetLedgerError("column already terminated")
            cells = tuple(
                self._column_cell_price(
                    quote,
                    ColumnCellReceipt(
                        cell_id=cell.cell_id,
                        state="not_started" if cell.state == "pending" else "closed",
                        verdict="cancelled",
                        started_at=cell.started_at,
                        held_usd=Decimal(0),
                    ),
                    state.approval,
                )
                if cell.state in ("pending", "running")
                else cell
                for quote, cell in zip(state.approval.cells, state.cells, strict=True)
            )
            updated = state.model_copy(
                update={
                    "cells": cells,
                    "revision": state.revision + 1,
                    "terminal_status": status,
                }
            )
            receipt = SpendReceipt.model_validate(
                self._column_receipt(updated, datetime.now(UTC)).model_dump()
            )
            self._check_column_transition(original, receipt)
            self._append(file, receipt, checkpoint, checkpoint.opening_spend, lock)
            return receipt
