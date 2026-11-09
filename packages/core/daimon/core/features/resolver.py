"""Resolve one feature for one place from the values set at each tier. Pure.

The walk goes broadest first (deployment, workspace, agent, channel, thread). A
locked tier is final for every tier below it; otherwise the spec's `combine` rule
decides: most specific wins, a set Off is a ceiling, the stricter value wins, or the
minimum. Generalises `scope_cascade.resolve_cascade`, which is the most-specific
rule with today's `disabled` read as Off + Locked.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from daimon.core.features.registry import TIERS, FeatureSpec, Tier
from daimon.core.scope_cascade import CascadeMode, ResolvedCascade

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


def _wins(spec: FeatureSpec, current: ResolvedFeature, candidate: str) -> bool:
    if current.tier == "default":
        return True
    match spec.combine:
        case "most_specific":
            return True
        case "ceiling":
            return current.value != spec.off
        case "stricter":
            order = list(spec.values)
            return order.index(candidate) > order.index(current.value)
        case "minimum":
            return float(candidate) < float(current.value)


def resolve_feature(
    spec: FeatureSpec,
    values: Iterable[TierValue],
    *,
    unavailable: Unavailable | None = None,
    fixed: str | None = None,
) -> ResolvedFeature:
    """Walk `values` broadest tier first; tiers the spec does not offer are ignored.
    An unavailable feature is Off, or `fixed` when its backend always runs that value."""
    by_tier = {value.tier: value for value in values if value.tier in spec.tiers}
    effective = ResolvedFeature(spec.key, spec.default, "default")
    for tier in TIERS:
        if effective.locked:
            break
        setting = by_tier.get(tier)
        if setting is None or setting.value not in spec.values:
            continue
        if _wins(spec, effective, setting.value):
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


def as_cascade(resolved: ResolvedFeature) -> ResolvedCascade:
    """The on/off/disabled view the participation gate and the chat notes read."""
    tier = "deployment" if resolved.tier == "default" else resolved.tier
    if resolved.unavailable is not None or (resolved.locked and not resolved.on):
        return ResolvedCascade(CascadeMode.DISABLED, tier)
    return ResolvedCascade(CascadeMode.ON if resolved.on else CascadeMode.OFF, tier)


def from_mode(mode: CascadeMode) -> tuple[str, bool]:
    """A legacy cascade mode as (value, locked): `disabled` is Off + Locked."""
    return ("off", True) if mode is CascadeMode.DISABLED else (mode.value, False)
