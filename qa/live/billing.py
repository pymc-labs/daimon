"""Independent QA prices and complete request-to-ledger evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, Field

from qa.live.models import ModelPolicy
from qa.live.types import Message, Pending, Turn, obj, objects


class Rates(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    input: Decimal = Field(gt=0, allow_inf_nan=False)
    write: Decimal = Field(gt=0, allow_inf_nan=False)
    read: Decimal = Field(gt=0, allow_inf_nan=False)
    output: Decimal = Field(gt=0, allow_inf_nan=False)


class BillingSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    short: Rates
    long: Rates
    prompt_threshold: int = Field(default=100000, gt=0)
    source: str = Field(min_length=1)


def approved_rates() -> dict[str, BillingSchedule]:
    # Driver hack/GO-pricing-check.md, official rates verified 2026-10-10.
    # These are independent QA inputs, never Daimon's MODEL_PRICING table.
    return {
        "claude-haiku-5-5": BillingSchedule(
            short=Rates(
                input=Decimal("0.1"),
                write=Decimal("0.125"),
                read=Decimal("0.01"),
                output=Decimal("0.5"),
            ),
            long=Rates(
                input=Decimal("0.5"),
                write=Decimal("0.625"),
                read=Decimal("0.05"),
                output=Decimal("2.5"),
            ),
            source="https://platform.claude.com/docs/en/about-claude/pricing",
        )
    }


@dataclass
class BillingEvidence:
    outcome: Message
    requests: list[Message]
    markup: Decimal
    schedules: dict[str, BillingSchedule]
    models: ModelPolicy


def amount(value: object) -> Decimal:
    if isinstance(value, bool):
        raise Pending("billing amount is malformed")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise Pending("billing amount is unavailable") from exc
    if not result.is_finite():
        raise Pending("billing amount is nonfinite")
    return result


def request_refs(outcome: Message) -> set[tuple[str, str]]:
    raw = outcome.get("usage_refs")
    if not isinstance(raw, list) or not raw:
        raise Pending("billing usage references are missing")
    refs: set[tuple[str, str]] = set()
    for entry in raw:
        row = obj(entry)
        session, event = row.get("session_id"), row.get("event_id")
        if not isinstance(session, str) or not session or not isinstance(event, str) or not event:
            raise Pending("billing usage reference is malformed")
        pair = (session, event)
        if pair in refs:
            raise Pending("billing usage reference is duplicated")
        refs.add(pair)
    if type(outcome.get("model_calls")) is not int or outcome.get("model_calls") != len(refs):
        raise Pending("billing model-call references are incomplete")
    return refs


def totals(evidence: BillingEvidence) -> tuple[Decimal, Decimal]:
    refs = request_refs(evidence.outcome)
    seen: set[tuple[str, str]] = set()
    expected = Decimal(0)
    ledger = Decimal(0)
    if not evidence.markup.is_finite() or evidence.markup <= 0:
        raise Pending("billing markup is unavailable")
    for row in evidence.requests:
        session, event = str(row.get("managed_session_id") or ""), str(row.get("event_id") or "")
        pair = (session, event)
        if pair not in refs or pair in seen:
            raise Pending("billing request is foreign or duplicated")
        seen.add(pair)
        if (
            row.get("idempotency_key") != f"turn:{session}:{event}"
            or not row.get("ledger_id")
            or row.get("ledger_reason") not in {"turn_debit", "checkpoint_debit"}
        ):
            raise Pending("billing ledger debit is missing or uncorrelated")
        model = str(row.get("model") or "")
        primary = evidence.models.policy("anthropic").primary
        if not evidence.models.policy("anthropic").matches_primary(model):
            raise Pending("billing request model has no approved independent rates")
        schedule = evidence.schedules.get(primary)
        if schedule is None:
            raise Pending("billing independent price schedule is unavailable")
        counts: list[int] = []
        for field in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
        ):
            value = row.get(field)
            if type(value) is not int or value < 0:
                raise Pending("billing per-request tokens are unavailable")
            counts.append(value)
        rates = schedule.long if sum(counts[:3]) > schedule.prompt_threshold else schedule.short
        provider = sum(
            (
                Decimal(n) * rate
                for n, rate in zip(
                    counts, (rates.input, rates.write, rates.read, rates.output), strict=True
                )
            ),
            Decimal(0),
        ) / Decimal(1000000)
        expected += provider * evidence.markup
        delta = amount(row.get("delta_usd"))
        if delta > 0:
            raise Pending("billing ledger row is a credit, not a debit")
        ledger -= delta
    if seen != refs:
        raise Pending("billing per-request evidence is incomplete")
    return expected, ledger


def footer_cost(turn: Turn) -> tuple[Decimal, bool]:
    matches: list[tuple[str, str]] = []
    for message in turn.messages:
        # Parse embed footers only: answer text and '$X left' aren't spend.
        for embed in objects(message.get("embeds")):
            text = str(obj(embed.get("footer")).get("text") or "")
            matches.extend(re.findall(r"(<?)\$([0-9]+(?:\.[0-9]+)?)\s+used\b", text))
    if len(matches) != 1:
        raise Pending("billing footer spend is missing or ambiguous")
    less, value = matches[0]
    return amount(value), bool(less)


def footer_matches(turn: Turn, ledger: Decimal, tolerance: float) -> bool:
    value, upper_bound = footer_cost(turn)
    if upper_bound:
        return Decimal(0) <= ledger < value
    return abs(value - ledger) <= Decimal(str(tolerance))
