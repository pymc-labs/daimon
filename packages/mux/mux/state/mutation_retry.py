"""Bounded retries of a claimed provider mutation: the vocabulary and the policy.

A driver classifies each failed mutation at its provider boundary into a
`MutationFailure` (see `mux.drivers.anthropic.classify_mutation_failure`):

- `rejected`: the provider provably did not execute the request.
- `repeatable`: the request may have executed, but repeating it is
  deduplicated by a verified provider contract (for example an idempotency
  key the endpoint honours).
- `unknown`: anything else. It is never retried; replay or reconcile it.

`retryable` is separate: a definite 400 or 401 is rejected but repeating it
cannot help. Only the host's persistence layer acts on a classification, by
taking a fresh durable attempt; SDK client retries stay off for claimed
mutations, so the policy here is the only retry layer.

These are pure rules over values (no I/O, no clock), like the rest of
`mux.state`: the caller supplies elapsed time, the remaining deadline and a
random source.
"""

from __future__ import annotations

import email.utils
import math
import random as _random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from pydantic import JsonValue

Delivery = Literal["rejected", "repeatable", "unknown"]

RetryStop = Literal[
    "not_retryable",
    "unclassified_refusal",
    "attempts_exhausted",
    "retry_after_too_long",
    "budget_exhausted",
    "cancelled",
]


@dataclass(frozen=True)
class MutationFailure:
    """What a failed mutation attempt proves, in credential-free terms.

    `reason` is a bounded identifier such as `http_429` or `connect_error`,
    safe to persist and log. `retry_after_s` is the provider's requested wait.
    """

    delivery: Delivery
    retryable: bool
    reason: str
    retry_after_s: float | None = None

    def __post_init__(self) -> None:
        if self.retryable and self.delivery == "unknown":
            raise ValueError("an unknown outcome is never retryable")

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "delivery": self.delivery,
            "retryable": self.retryable,
            "reason": self.reason,
            "retry_after_s": self.retry_after_s,
        }

    @classmethod
    def from_json(cls, value: object) -> MutationFailure | None:
        """A persisted classification, or None when it is absent or malformed."""
        if not isinstance(value, Mapping):
            return None
        fields = cast(Mapping[str, object], value)
        delivery, retryable = fields.get("delivery"), fields.get("retryable")
        reason, retry_after = fields.get("reason"), fields.get("retry_after_s")
        if (
            delivery not in ("rejected", "repeatable", "unknown")
            or not isinstance(retryable, bool)
            or not isinstance(reason, str)
            or not (retry_after is None or isinstance(retry_after, int | float))
        ):
            return None
        try:
            return cls(
                delivery,
                retryable=retryable,
                reason=reason,
                retry_after_s=None if retry_after is None else float(retry_after),
            )
        except ValueError:
            return None


UNCLASSIFIED = MutationFailure("unknown", retryable=False, reason="unclassified")
"""The conservative default for a failure no driver vouches for."""


@dataclass(frozen=True)
class RetryDecision:
    """Retry after `wait_s`, or stop for `stop`."""

    wait_s: float = 0.0
    stop: RetryStop | None = None

    @property
    def retry(self) -> bool:
        return self.stop is None


@dataclass(frozen=True)
class MutationRetryPolicy:
    """Limits for retrying one logical mutation.

    `max_attempts` counts every durable attempt of the mutation, including the
    first and those taken by earlier invocations, so a restart cannot reset it.
    `max_elapsed_s` bounds the time since the first attempt began. The backoff
    is exponential from `base_delay_s`, capped at `max_delay_s`, with up to 25%
    jitter below the cap. A provider wait longer than `max_retry_after_s` stops
    instead of waiting. Every wait also has to fit the host's remaining deadline.
    """

    max_attempts: int = 5
    max_elapsed_s: float = 90.0
    base_delay_s: float = 0.5
    max_delay_s: float = 8.0
    max_retry_after_s: float = 60.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must allow the first attempt")
        if min(self.max_elapsed_s, self.base_delay_s, self.max_delay_s) < 0:
            raise ValueError("retry delays and budgets cannot be negative")

    def backoff_s(self, retry_index: int, *, random: Callable[[], float]) -> float:
        """The jittered delay before retry number `retry_index` (0-based)."""
        delay = min(self.base_delay_s * 2.0 ** min(retry_index, 32), self.max_delay_s)
        return delay * (1.0 - 0.25 * random())

    def decide(
        self,
        failure: MutationFailure,
        *,
        attempts_made: int,
        first_began: datetime,
        refused_at: datetime,
        now: datetime,
        remaining_s: float | None = None,
        random: Callable[[], float] = _random.random,
    ) -> RetryDecision:
        """Whether attempt `attempts_made + 1` may follow `failure`, and how long to wait.

        All times come from the durable records, so a fresh invocation decides
        exactly as the one that saw the refusal: `first_began` is when attempt
        0 began, `refused_at` when the last refusal was recorded. The retry is
        never earlier than `refused_at` plus Retry-After (and the backoff), and
        stops when that moment, or `now`, is past the elapsed budget, or when
        the wait would not end inside the host deadline (`remaining_s`).
        """
        if not failure.retryable or failure.delivery == "unknown":
            return RetryDecision(stop="not_retryable")
        if attempts_made >= self.max_attempts:
            return RetryDecision(stop="attempts_exhausted")
        retry_after = failure.retry_after_s
        if retry_after is not None and retry_after > self.max_retry_after_s:
            return RetryDecision(stop="retry_after_too_long")
        delay = self.backoff_s(attempts_made - 1, random=random)
        if retry_after is not None:
            delay = max(delay, retry_after)
        not_before = refused_at + timedelta(seconds=delay)
        if (max(not_before, now) - first_began).total_seconds() >= self.max_elapsed_s:
            return RetryDecision(stop="budget_exhausted")
        wait = max((not_before - now).total_seconds(), 0.0)
        if remaining_s is not None and wait >= remaining_s:
            return RetryDecision(stop="budget_exhausted")
        return RetryDecision(wait_s=wait)


def parse_retry_after(headers: Mapping[str, str], *, now: datetime) -> float | None:
    """The provider's requested wait in seconds, or None when it gave none.

    Reads `retry-after-ms` (Anthropic and OpenAI send it), then `retry-after`
    as delta-seconds or as an HTTP-date (RFC 9110 section 10.2.3). A date in
    the past means no further wait. Malformed values are ignored.
    """
    lowered = {name.lower(): value for name, value in headers.items()}
    milliseconds = _finite(lowered.get("retry-after-ms"))
    if milliseconds is not None:
        return max(milliseconds / 1000.0, 0.0)
    value = lowered.get("retry-after")
    if value is None:
        return None
    seconds = _finite(value)
    if seconds is not None:
        return max(seconds, 0.0)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - now).total_seconds(), 0.0)


def _finite(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value.strip())
    except ValueError:
        return None
    return number if math.isfinite(number) else None


__all__ = [
    "UNCLASSIFIED",
    "Delivery",
    "MutationFailure",
    "MutationRetryPolicy",
    "RetryDecision",
    "RetryStop",
    "parse_retry_after",
]
