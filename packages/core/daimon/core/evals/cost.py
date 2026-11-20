"""What a run should cost (design section 16) and the cap the runner keeps to. Pure."""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Final

from daimon.core.evals.format import Arm, Case, Estimate, EstimateLine, JudgeConfig
from daimon.core.pricing import side_call_rates

AGENT_TOKENS: Final = (60_000, 3_000)
"""Input and output tokens of one item's turns when nothing better is known."""
SIMULATOR_TOKENS: Final = (3_000, 800)
JUDGE_TOKENS: Final = (8_000, 600)
UNPRICED_CALL_USD: Final = 1.0
"""What a call on a model with no known price is taken to cost: high, so caps hold."""
ITEM_MINUTES: Final = 5
SIMULATED_CALLS: Final[Mapping[str, float]] = {
    "full": 0.5, "tools": 1.0, "text": 3.0, "authored": 4.0,
}  # fmt: skip
"""Simulated world calls per item by case fidelity: more for less recorded."""
CAP_MARGIN: Final = 1.2
RUNNER_CATEGORIES: Final = ("agent", "simulator", "sandbox")
"""What a run's own cap covers; judging is approved on its own."""


def call_usd(model: str | None, tokens: tuple[int, int]) -> float:
    if model is None:
        return UNPRICED_CALL_USD
    try:
        rates = side_call_rates(model, tokens[0])
    except KeyError:
        return UNPRICED_CALL_USD
    return (tokens[0] * rates.input + tokens[1] * rates.output) / 1_000_000


def _bounds(per_item: Sequence[float], items: int) -> tuple[float, float, float]:
    """Low, expected and high for `items` items: the 10th and 90th percentiles of the prior
    costs, or half and double one guess."""
    if len(per_item) >= 2:
        deciles = statistics.quantiles(per_item, n=10, method="inclusive")
        return deciles[0] * items, statistics.median(per_item) * items, deciles[-1] * items
    one = per_item[0] * items
    return one / 2, one, one * 2


def estimate_run(
    cases: Sequence[Case],
    arms: Sequence[Arm],
    repeats: int,
    judges: Sequence[JudgeConfig],
    *,
    prior: Mapping[str, Sequence[float]],
    simulator_model: str | None,
    sandbox_rate_usd_per_hour: float,
    at: datetime,
) -> Estimate:
    """The estimate of running `cases` on every live arm `repeats` times. `prior` holds, by
    arm id, what earlier items of the same arm config cost on these cases."""
    live = [a for a in arms if a.kind == "live" and a.live is not None]
    per_arm = len(cases) * repeats
    lines: list[tuple[EstimateLine, float, float]] = []

    def add(line: EstimateLine, low: float, high: float) -> None:
        lines.append(
            (line.model_copy(update={"expected_usd": round(line.expected_usd, 6)}), low, high)
        )

    for arm in live:
        assert arm.live is not None
        known = prior.get(arm.id) or []
        basis = "prior_runs" if known else "default"
        low, mid, high = _bounds(known or [call_usd(arm.live.model, AGENT_TOKENS)], per_arm)
        add(EstimateLine(category="agent", arm_id=arm.id, model=arm.live.model, items=per_arm,
                         expected_usd=mid, basis=basis), low, high)  # fmt: skip
    sim_calls = sum(
        SIMULATED_CALLS[c.fidelity.level]
        for c in cases
        if c.world.simulator is None or c.world.simulator.enabled
    )
    if live and sim_calls:
        each = call_usd(simulator_model, SIMULATOR_TOKENS)
        mid = sim_calls * repeats * len(live) * each
        add(EstimateLine(category="simulator", model=simulator_model, items=per_arm * len(live),
                         expected_usd=mid, basis="default"), mid / 2, mid * 2)  # fmt: skip
    pairs = len(live) * (len(live) - 1) // 2
    for judge in judges:
        calls = per_arm * (pairs * (2 if judge.swap_positions else 1)
                           if judge.kind == "pairwise" else len(live))  # fmt: skip
        for model in judge.models:
            mid = calls * call_usd(model.model, JUDGE_TOKENS)
            add(EstimateLine(category="judge", model=model.model, items=calls, expected_usd=mid,
                             basis="default"), mid / 2, mid * 2)  # fmt: skip
    if live and sandbox_rate_usd_per_hour:
        mid = per_arm * len(live) * ITEM_MINUTES / 60 * sandbox_rate_usd_per_hour
        add(EstimateLine(category="sandbox", items=per_arm * len(live), expected_usd=mid,
                         basis="default"), mid / 2, mid * 2)  # fmt: skip
    return Estimate(
        low_usd=round(sum(low for _, low, _ in lines), 6),
        expected_usd=round(sum(line.expected_usd for line, _, _ in lines), 6),
        high_usd=round(sum(high for _, _, high in lines), 6),
        at=at,
        lines=[line for line, _, _ in lines],
    )


def default_cap(estimate: Estimate) -> float:
    """The cap an approver is offered: the high estimate plus 20%."""
    return round(estimate.high_usd * CAP_MARGIN, 2)


def item_usd(estimate: Estimate, arm_id: str) -> float:
    """What one more item of `arm_id` should cost under the run's own cap."""
    total = 0.0
    for line in estimate.lines:
        if line.category in RUNNER_CATEGORIES and line.items and line.arm_id in (None, arm_id):
            total += line.expected_usd / line.items
    return total


def fits(*, spent: float, reserved: float, next_usd: float, cap: float) -> bool:
    """Whether one more item may start: what is spent, held for running items and this one's
    expected cost stay within the cap."""
    return spent + reserved + next_usd <= cap
