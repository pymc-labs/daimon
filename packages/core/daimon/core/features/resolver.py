"""Resolve one feature for one place from the values set at each tier. Pure.

The walk goes broadest first (deployment, workspace, agent, channel, thread). A
locked tier is final for every tier below it; otherwise the spec's `combine` rule
decides: most specific wins, a set Off is a ceiling, the stricter value wins, or the
minimum. A legacy `disabled` is Off + Locked.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from daimon.core.features.registry import TIERS, FeatureSpec, Tier

Decider = Tier | Literal["default"]
Unavailable = Literal["platform", "backend", "requires", "depends_on"]


@dataclass(frozen=True)
class TierValue:
    """What one tier set for one feature at this place."""

    tier: Tier
    value: str
    locked: bool = False


@dataclass(frozen=True)
class ResolvedFeature:
    """`value` is what applies; `tier` decided it ("default" when nothing was set).
    `unavailable` names why the feature cannot apply here, and then `value` is Off."""

    key: str
    value: str
    tier: Decider
    locked: bool = False
    unavailable: Unavailable | None = None

    @property
    def on(self) -> bool:
        return self.value not in ("off", "hidden")


def stricter_than(spec: FeatureSpec, candidate: str, current: str) -> int:
    """How much stricter `candidate` is than `current` (negative: looser)."""
    order = list(spec.values)
    return order.index(candidate) - order.index(current)


def _extent(spec: FeatureSpec, value: str) -> float:
    """A limit's size for comparing; 0 is no limit where the spec says so."""
    number = float(value)
    return math.inf if spec.zero_unlimited and number == 0 else number


def _wins(spec: FeatureSpec, current: ResolvedFeature, candidate: str, tier: Tier) -> bool:
    if spec.combine == "stricter" and tier != "deployment":
        # The default is the deployment's floor: below it a guardrail only tightens.
        stricter = stricter_than(spec, candidate, current.value)
        return stricter > 0 or (stricter == 0 and current.tier == "default")
    if current.tier == "default":
        return True
    match spec.combine:
        case "most_specific":
            return True
        case "ceiling":
            return current.value != spec.off
        case "stricter":
            return stricter_than(spec, candidate, current.value) > 0
        case "minimum":
            return _extent(spec, candidate) < _extent(spec, current.value)


def resolve_feature(
    spec: FeatureSpec,
    values: Iterable[TierValue],
    *,
    unavailable: Unavailable | None = None,
    fixed: str | None = None,
    default: str | None = None,
) -> ResolvedFeature:
    """Walk `values` broadest tier first; tiers the spec does not offer are ignored.
    An unavailable feature is Off, or `fixed` when its backend always runs that value.
    `default` is the platform's own default where it differs from the spec's."""
    by_tier = {value.tier: value for value in values if value.tier in spec.tiers}
    effective = ResolvedFeature(spec.key, default or spec.default, "default")
    for tier in TIERS:
        if effective.locked:
            break
        setting = by_tier.get(tier)
        if setting is None or not spec.accepts(setting.value, tier):
            continue
        if _wins(spec, effective, setting.value, tier):
            effective = ResolvedFeature(spec.key, setting.value, tier, setting.locked)
        elif setting.locked:
            effective = ResolvedFeature(spec.key, effective.value, effective.tier, True)
    if unavailable is not None:
        off = fixed or spec.off or spec.default
        return ResolvedFeature(spec.key, off, effective.tier, effective.locked, unavailable)
    return effective


def resolve_above(spec: FeatureSpec, values: Iterable[TierValue], tier: Tier) -> ResolvedFeature:
    """Resolve only the tiers broader than `tier`: what a write there lives under."""
    cut = TIERS.index(tier)
    return resolve_feature(spec, (v for v in values if TIERS.index(v.tier) < cut))
