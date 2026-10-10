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

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Money = Annotated[Decimal, Field(ge=0, max_digits=32, decimal_places=18)]
Rate = Annotated[Decimal, Field(ge=0, max_digits=16, decimal_places=8)]
Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,127}$")]
Count = Annotated[int, Field(strict=True, ge=0, le=1_000_000_000)]
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


class ProbePlan(ProbeModel):
    provider: Identifier
    model: Identifier
    fixture_id: str = Field(pattern=r"^(C(0[1-9]|1[0-8])|F1)$")
    limits: TokenLimits = TokenLimits(input_tokens=20_000, output_tokens=2_000)
    container_allowance: ContainerAllowance | None = None

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


class SpendReceipt(ProbeModel):
    version: Literal[1, 2] = 1
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    provider: Identifier
    model: Identifier
    fixture_id: str = Field(pattern=r"^(C(0[1-9]|1[0-8])|F1)$")
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
    ]

    @model_validator(mode="after")
    def accounting(self) -> SpendReceipt:
        # An overrun latches admission independently of its current billable total.
        if self.status == "overrun":
            object.__setattr__(self, "admission_blocked", True)
        if self.overrun_evidence is not None and not self.admission_blocked:
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
        if self.accounting_status == "actual":
            if (
                self.actual_usd is None
                or self.held_usd != 0
                or self.cost_estimate_usd != self.actual_usd
            ):
                raise ValueError("actual spend releases all held dollars")
        elif self.actual_usd is not None or self.held_usd != self.cost_estimate_usd:
            raise ValueError("unverified spend must remain separately held")
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
                if previous is not None and previous.admission_blocked:
                    if "admission_blocked" not in receipt.model_fields_set:
                        # Old rows have no independent latch field. Carry the
                        # proven history in memory without rewriting their bytes.
                        object.__setattr__(receipt, "admission_blocked", True)
                    elif not receipt.admission_blocked:
                        raise BudgetLedgerError("overrun admission latch cannot be cleared")
                if previous is None:
                    if receipt.status != "reserved":
                        raise BudgetLedgerError("spend receipt has no reservation")
                elif receipt.status == "reconciled":
                    approval = receipt.reconcile
                    if (
                        previous.status in ("blocked", "reconciled")
                        or previous.actual_usd is not None
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
        except (ValidationError, ValueError):
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
        if any(sensitive_identifier(name) for name in (plan.provider, plan.model)):
            raise BudgetRefused("probe metadata must not contain credentials")
        with self._locked() as (file, lock, checkpoint, runs, config):
            budget = config.providers.get(plan.provider)
            allowed = plan.model in LIVE_MODEL_ALLOWLIST.get(plan.provider, frozenset())
            price = budget.models.get(plan.model) if budget is not None and allowed else None
            today = datetime.now(UTC).date()
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
            estimate = price.reserve(plan.limits) if price is not None else Decimal(0)
            if allowance is not None and session_price is not None:
                estimate += allowance.sessions * session_price.cost(allowance.seconds_per_session)
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

    @staticmethod
    def _price_current(price: SessionPrice, at: datetime) -> bool:
        return (
            at.tzinfo is not None
            and at.astimezone(UTC).date() >= price.effective_from
            and (price.effective_until is None or at.astimezone(UTC).date() < price.effective_until)
        )

    @staticmethod
    def receipt_digest(receipt: SpendReceipt) -> str:
        # Hash the schema actually present on disk. Adding read-time defaults or
        # inheriting an admission latch must not invalidate a pre-upgrade signature.
        absent = {"overrun_evidence", "admission_blocked"} - receipt.model_fields_set
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
        return self._settle(reservation, status=status, limits=limits, usage=usage, actual=actual)

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
        otherwise known bounds stay held. Native freshness checks belong to adapters.
        """
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
                request_ids = {request.id for request in actual.requests}
                container_ids = {container.id for container in actual.containers or ()}
                uncovered = ActualSpend(
                    requests=tuple(r for r in overrun_evidence.requests if r.id not in request_ids),
                    containers=tuple(
                        c for c in overrun_evidence.containers or () if c.id not in container_ids
                    ),
                )
                _, _, _, missing_tokens, missing_cost = self._measured(original, uncovered)
                known_cost += missing_cost
                lower_tokens = TokenUsage(
                    input_tokens=(lower_tokens.minimum_input_tokens if lower_tokens else 0)
                    + missing_tokens.minimum_input_tokens,
                    output_tokens=((lower_tokens.output_tokens or 0) if lower_tokens else 0)
                    + (missing_tokens.output_tokens or 0),
                )
        if exceeded:
            terminal, reason = "overrun", "overrun"
        elif measured is not None:
            reason = "settled"
        elif status != "completed":
            reason = "probe_error"
        elif estimate is None or actual is not None or original.container_allowance is not None:
            terminal, reason = "uncertain", "unknown_usage"
        charged = measured if measured is not None else original.reserved_usd
        # Backward-compatible undated estimates are explicitly unverified holds.
        if (
            actual is None
            and original.container_allowance is None
            and status == "completed"
            and estimate is not None
        ):
            charged = estimate
        if terminal == "overrun" and measured is None:
            charged = max(charged, original.reserved_usd, known_cost, proof_cost)
            if lower_tokens is not None:
                charged = max(charged, reservation.price.reserve(limits, usage=lower_tokens))
            if proof_tokens is not None:
                charged = max(charged, reservation.price.reserve(limits, usage=proof_tokens))
        receipt = SpendReceipt.model_validate(
            {
                **original.model_dump(),
                "version": 2,
                "timestamp": datetime.now(UTC),
                "status": terminal,
                "tokens": usage,
                "cost_estimate_usd": charged,
                "actual_usd": measured,
                "held_usd": Decimal(0) if measured is not None else charged,
                "accounting_status": "actual" if measured is not None else "estimated_unverified",
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
            if current is None or current.actual_usd is not None or current.status == "reconciled":
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
        A reserved run may be reconciled for lead-approved crash recovery. This
        fences any later worker settlement as already settled; do not reconcile
        a run whose worker is still expected to finish.
        """
        proposal = Reconciliation.model_validate(proposal.model_dump())
        with self._locked() as (file, lock, checkpoint, runs, config):
            if proposal.digest not in config.approved_reconciliations:
                raise BudgetRefused("reconciliation needs the lead-signed proposal digest")
            current = runs.get(proposal.run_id)
            if current is None or current.actual_usd is not None or current.status == "reconciled":
                raise BudgetLedgerError("reconciliation requires a held run")
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
