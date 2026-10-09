"""Manual probe admission and spend receipts; no SDK, recorder or live calls."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Annotated, Literal, TextIO
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Money = Annotated[Decimal, Field(ge=0, max_digits=32, decimal_places=18)]
Rate = Annotated[Decimal, Field(ge=0, max_digits=16, decimal_places=8)]
Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,127}$")]
Count = Annotated[int, Field(strict=True, ge=0, le=1_000_000_000)]
PREFIX = "<!-- mux-probe "
HEADER = "# Managed probe spend"
LEDGER_PREFIX = "<!-- mux-ledger "


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

    @property
    def minimum_input_tokens(self) -> int:
        return max(
            self.input_tokens or 0,
            (self.input_cached_tokens or 0) + (self.input_cache_write_tokens or 0),
        )

    @model_validator(mode="after")
    def subsets(self) -> TokenUsage:
        if self.input_tokens is not None:
            known = (self.input_cached_tokens or 0) + (self.input_cache_write_tokens or 0)
            if known > self.input_tokens:
                raise ValueError("cached/write tokens exceed inclusive input")
        return self


class ModelPrice(ProbeModel):
    """Reviewed USD per million tokens. Reasoning is included in output."""

    input: Rate
    cached_input: Rate
    cache_write_input: Rate
    output: Rate

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


class ProviderBudget(ProbeModel):
    cap_usd: Money
    opening_spend_usd: Money = Decimal(0)
    models: dict[Identifier, ModelPrice] = Field(default_factory=dict[str, ModelPrice])


class BudgetConfig(ProbeModel):
    version: Literal[1] = 1
    total_cap_usd: Money = Decimal(150)
    ledger_path: Path
    providers: dict[Identifier, ProviderBudget]

    @model_validator(mode="after")
    def total(self) -> BudgetConfig:
        if not self.ledger_path.is_absolute():
            raise ValueError("ledger path must be an absolute pin")
        if any(
            sensitive_identifier(name)
            for provider, budget in self.providers.items()
            for name in (provider, *budget.models)
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
    fixture_id: str = Field(pattern=r"^C(0[1-9]|1[0-8])$")
    limits: TokenLimits


class SpendReceipt(ProbeModel):
    version: Literal[1] = 1
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    provider: Identifier
    model: Identifier
    fixture_id: str = Field(pattern=r"^C(0[1-9]|1[0-8])$")
    timestamp: datetime
    status: Literal[
        "reserved", "completed", "uncertain", "failed", "cancelled", "blocked", "overrun"
    ]
    tokens: TokenUsage | None = None
    reserved_usd: Money
    cost_estimate_usd: Money
    reason: Literal[
        "admitted",
        "settled",
        "budget",
        "unconfigured",
        "probe_error",
        "unknown_usage",
        "overrun",
        "fixture_exists",
    ]

    @model_validator(mode="after")
    def accounting(self) -> SpendReceipt:
        if self.status in ("reserved", "uncertain", "failed", "cancelled"):
            if self.cost_estimate_usd != self.reserved_usd:
                raise ValueError("unknown spend must retain its reservation")
        elif self.status == "blocked":
            if self.cost_estimate_usd != 0 or self.tokens is not None:
                raise ValueError("refused runs have no usage or cost")
        elif self.status == "overrun":
            if self.cost_estimate_usd < self.reserved_usd:
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
                if previous is None:
                    if receipt.status != "reserved":
                        raise BudgetLedgerError("spend receipt has no reservation")
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
            price = budget.models.get(plan.model) if budget is not None else None
            estimate = price.reserve(plan.limits) if price is not None else Decimal(0)
            receipt = SpendReceipt(
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
            if budget is None or price is None:
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
                        r.status == "overrun" and r.provider == plan.provider for r in runs.values()
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

    def settle(
        self,
        reservation: Reservation,
        *,
        status: Literal["completed", "failed", "cancelled"],
        limits: TokenLimits,
        usage: TokenUsage | None = None,
    ) -> SpendReceipt:
        original = reservation.receipt
        estimate = reservation.price.estimate(usage) if usage is not None else None
        reason: Literal["settled", "probe_error", "unknown_usage", "overrun"] = "settled"
        terminal: Literal["completed", "uncertain", "failed", "cancelled", "overrun"] = status
        if status != "completed":
            reason = "probe_error"
        elif usage is not None and (
            usage.minimum_input_tokens > limits.input_tokens
            or (usage.output_tokens or 0) > limits.output_tokens
            or (estimate is not None and estimate > original.reserved_usd)
        ):
            terminal, reason = "overrun", "overrun"
        elif estimate is None:
            terminal, reason = "uncertain", "unknown_usage"
        charged = (
            estimate if status == "completed" and estimate is not None else original.reserved_usd
        )
        if terminal == "overrun":
            charged = max(charged, original.reserved_usd)
            if estimate is None and usage is not None:
                charged = reservation.price.reserve(limits, usage=usage)
        receipt = SpendReceipt.model_validate(
            {
                **original.model_dump(),
                "timestamp": datetime.now(UTC),
                "status": terminal,
                "tokens": usage,
                "cost_estimate_usd": charged,
                "reason": reason,
            }
        )
        with self._locked() as (file, lock, checkpoint, runs, _config):
            current = runs.get(original.run_id)
            if current != original:
                raise BudgetLedgerError("reservation was already settled or changed")
            self._append(file, receipt, checkpoint, checkpoint.opening_spend, lock)
        return receipt
