"""Statistics for eval results, in plain Python.

Results are paired by case. Cases cut from one source thread are not independent, so the
bootstrap resamples whole clusters (threads), never single cases.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from math import comb
from typing import Final

DEFAULT_SEED: Final = 20260101
DEFAULT_RESAMPLES: Final = 2000


@dataclass(frozen=True)
class Interval:
    mean: float
    low: float
    high: float
    n: int
    """Cases behind the mean."""
    clusters: int


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    position = q * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (position - lower)


def cluster_bootstrap(
    clusters: Sequence[Sequence[float]],
    *,
    confidence: float = 0.95,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
) -> Interval:
    """The mean of per-case values with a percentile interval from resampling clusters.

    `clusters` holds one list of case values per source thread. The mean weights every case
    equally; each resample draws as many clusters as there are, with replacement.
    """
    live = [list(c) for c in clusters if c]
    cases = sum(len(c) for c in live)
    if not live:
        return Interval(float("nan"), float("nan"), float("nan"), 0, 0)
    mean = sum(sum(c) for c in live) / cases
    sums = [sum(c) for c in live]
    sizes = [len(c) for c in live]
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(resamples):
        picked = rng.choices(range(len(live)), k=len(live))
        draws.append(sum(sums[i] for i in picked) / sum(sizes[i] for i in picked))
    draws.sort()
    tail = (1 - confidence) / 2
    return Interval(mean, _quantile(draws, tail), _quantile(draws, 1 - tail), cases, len(live))


def group_by_cluster(
    values: Mapping[str, float], clusters: Mapping[str, Hashable | None]
) -> list[list[float]]:
    """Per-case values grouped by their cluster key; a case with no key is its own cluster."""
    grouped: dict[Hashable, list[float]] = {}
    for case, value in values.items():
        grouped.setdefault(clusters.get(case) or ("case", case), []).append(value)
    return list(grouped.values())


def binomial_two_sided(successes: int, trials: int) -> float:
    """Exact two-sided p-value of `successes` out of `trials` fair coin flips."""
    if trials == 0:
        return 1.0
    tail = min(successes, trials - successes)
    return min(1.0, 2 * sum(comb(trials, k) for k in range(tail + 1)) / 2**trials)


def mcnemar(only_a: int, only_b: int) -> float:
    """Exact McNemar p-value from the discordant pairs: cases only A passed, only B passed."""
    return binomial_two_sided(only_a, only_a + only_b)


def pass_at_k(samples: int, passes: int, k: int) -> float:
    """Chance that at least one of `k` draws from `samples` repeats passes (unbiased)."""
    if k > samples:
        raise ValueError("k is larger than the number of repeats")
    return 1.0 - comb(samples - passes, k) / comb(samples, k)


def pass_pow_k(samples: int, passes: int, k: int) -> float:
    """Chance that all `k` draws from `samples` repeats pass (unbiased)."""
    if k > samples:
        raise ValueError("k is larger than the number of repeats")
    return comb(passes, k) / comb(samples, k)


@dataclass(frozen=True)
class NonInferiority:
    difference: float
    low: float
    high: float
    margin: float
    verdict: bool
    """Candidate is not worse: the lower bound of (candidate - reference) clears -margin."""


def non_inferiority(
    clusters: Sequence[Sequence[float]],
    *,
    margin: float = 0.05,
    confidence: float = 0.95,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
) -> NonInferiority:
    """Non-inferiority from per-case differences (candidate minus reference), by cluster."""
    ci = cluster_bootstrap(clusters, confidence=confidence, resamples=resamples, seed=seed)
    return NonInferiority(ci.mean, ci.low, ci.high, margin, ci.n > 0 and ci.low > -margin)


def cohen_kappa(a: Sequence[Hashable], b: Sequence[Hashable]) -> float:
    """Cohen's kappa between two graders' labels on the same items; NaN when undefined."""
    if len(a) != len(b):
        raise ValueError("label lists differ in length")
    n = len(a)
    if n == 0:
        return float("nan")
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    expected = sum(ca[label] * cb[label] for label in ca.keys() | cb.keys()) / n**2
    if expected == 1:
        return 1.0 if observed == 1 else float("nan")
    return (observed - expected) / (1 - expected)
