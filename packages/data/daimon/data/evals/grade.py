"""Grading and summary: pure functions over ranked record ids and their gains.

Only expected records the caller can read count: one it cannot see is access coverage, never
a miss, so `gains` holds the visible ones alone and a question with none is not graded.
"""

from __future__ import annotations

import math
import random
import statistics
import uuid
from collections.abc import Mapping, Sequence
from typing import Final

KS: Final = (5, 10)
RESAMPLES: Final = 2000
SEED: Final = 20260101

type Gains = Mapping[uuid.UUID, int]
type Metrics = dict[str, float]


def recall_at(ranked: Sequence[uuid.UUID], gains: Gains, k: int) -> float:
    """The share of the expected grade found in the top `k`."""
    return sum(gains.get(r, 0) for r in set(ranked[:k])) / sum(gains.values())


def ndcg_at(ranked: Sequence[uuid.UUID], gains: Gains, k: int) -> float:
    dcg = sum(gains.get(r, 0) / math.log2(i + 2) for i, r in enumerate(ranked[:k]))
    ideal = sum(
        g / math.log2(i + 2) for i, g in enumerate(sorted(gains.values(), reverse=True)[:k])
    )
    return dcg / ideal


def grade(ranked: Sequence[uuid.UUID], gains: Gains) -> Metrics:
    """`recall@k` and `ndcg@k` for each k; `gains` must not be empty."""
    seen = list(dict.fromkeys(ranked))
    return {
        **{f"recall@{k}": recall_at(seen, gains, k) for k in KS},
        **{f"ndcg@{k}": ndcg_at(seen, gains, k) for k in KS},
    }


def mean_interval(values: Sequence[float]) -> tuple[float, float, float]:
    """The mean and a 95% bootstrap interval over the questions (seeded: a report repeats)."""
    rng, n = random.Random(SEED), len(values)
    means = sorted(statistics.fmean(rng.choices(values, k=n)) for _ in range(RESAMPLES))
    return (
        statistics.fmean(values),
        means[int(0.025 * RESAMPLES)],
        means[int(0.975 * RESAMPLES) - 1],
    )


def summarize(rows: Sequence[Mapping[str, Metrics]]) -> dict[str, dict[str, dict[str, float]]]:
    """Per arm and metric: mean, interval and the questions graded. `rows` are one
    `{arm: metrics}` per graded question; an arm that did not run for one is absent."""
    arms = sorted({a for r in rows for a in r})
    out: dict[str, dict[str, dict[str, float]]] = {}
    for arm in arms:
        out[arm] = {}
        for metric in (f"{m}@{k}" for m in ("recall", "ndcg") for k in KS):
            values = [r[arm][metric] for r in rows if arm in r]
            mean, lo, hi = mean_interval(values) if values else (0.0, 0.0, 0.0)
            out[arm][metric] = {"mean": mean, "lo": lo, "hi": hi, "n": float(len(values))}
    return out


def text_summary(
    report: Mapping[str, object], summary: Mapping[str, Mapping[str, Mapping[str, float]]]
) -> str:
    """A few lines: the questions graded and each arm's means."""
    lines = [
        f"questions {report['questions']}, graded {report['graded']}, "
        f"no visible expected record {report['ungraded']}",
    ]
    for arm, metrics in summary.items():
        cells = "  ".join(
            f"{m} {v['mean']:.3f} [{v['lo']:.3f}, {v['hi']:.3f}]" for m, v in metrics.items()
        )
        lines.append(f"{arm:<6} {cells}")
    if report.get("halted"):
        lines.append(f"halted: {report['halted']}")
    return "\n".join(lines)
