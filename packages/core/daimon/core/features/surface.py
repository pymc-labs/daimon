"""How the surfaces say a feature: its words, its controls and its schema.

Chat `/settings`, the web API and the CLI describe features from here, so a new registry
key reaches every surface with no surface code. Pure.

A control's choice is one string, ``key:tier:value`` with ``:lock`` after an admin's
Lock, and ``@<agent>`` after the agent tier's choice (the agent it applies to).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, cast

from daimon.core.features.registry import (
    BACKEND_NAMES,
    DEFAULT,
    FEATURES,
    LOCKABLE,
    TIERS,
    Category,
    FeatureSpec,
    Tier,
)
from daimon.core.features.resolver import ResolvedFeature
from daimon.core.features.service import (
    FeatureChange,
    FeatureView,
    TierSetting,
    where,
)

CATEGORIES: Final[Mapping[Category, str]] = {
    "model_input": "What the agent sees",
    "chat": "Chat",
    "capabilities": "Capabilities",
    "limits": "Limits",
    "guardrails": "Safety",
}
_UNAVAILABLE: Final[Mapping[str, str]] = {
    "platform": "Not available here",
    "backend": "Not available for this agent's model provider",
    "requires": "Not set up",
}
LOCK_FLAG: Final = "lock"


def value_text(spec: FeatureSpec, value: str, locked: bool = False) -> str:
    text = spec.label(value)
    return f"{text}, locked" if locked and value != DEFAULT else text


def unavailable_text(spec: FeatureSpec, reason: str) -> str:
    if reason == "depends_on":
        needs = ", ".join(FEATURES[key].name for key in spec.depends_on if key in FEATURES)
        return f"Needs {needs}" if needs else "Not available here"
    return _UNAVAILABLE.get(reason, "Not available here")


def source_text(spec: FeatureSpec, resolved: ResolvedFeature, platform: str | None) -> str:
    """What is in effect and where it comes from, as one phrase."""
    if resolved.unavailable is not None:
        return unavailable_text(spec, resolved.unavailable)
    text = spec.label(resolved.value)
    if resolved.tier == "default":
        return f"{text}, the default"
    if resolved.tier == "deployment":
        return f"{text} everywhere" + (", locked" if resolved.locked else "")
    where_ = where(resolved.tier, platform)
    return f"{text}, locked by {where_}" if resolved.locked else f"{text}, set for {where_}"


def own_text(spec: FeatureSpec, own: TierSetting) -> str:
    return value_text(spec, own.value, own.locked)


OVERRIDES_OFF: Final = "Saved, but it has no effect yet. Ask an admin to allow approval changes."


def _ignored(spec: FeatureSpec, overrides_active: bool) -> str:
    return f" {OVERRIDES_OFF}" if spec.key == "approvals" and not overrides_active else ""


def change_note(
    change: FeatureChange, platform: str | None, *, overrides_active: bool = True
) -> str:
    """What a change saved, and what is in effect when another tier still decides."""
    spec, after, resolved = change.view.spec, change.after, change.view.resolved
    where_ = where(change.tier, platform)
    if after.value == DEFAULT:
        note = f"{spec.name} is back to the default for {where_}."
    else:
        note = f"{spec.name} is now {value_text(spec, after.value, after.locked)} for {where_}."
    if resolved.tier != change.tier:
        note += f" In effect: {source_text(spec, resolved, platform)}."
    return note + _ignored(spec, overrides_active)


def choice(
    key: str, tier: Tier, value: str, *, locked: bool = False, agent: str | None = None
) -> str:
    raw = f"{key}:{tier}:{value}" + (f":{LOCK_FLAG}" if locked else "")
    return f"{raw}@{agent}" if agent else raw


@dataclass(frozen=True)
class Choice:
    key: str
    tier: Tier
    value: str
    locked: bool = False
    agent: str | None = None


def parse_choice(raw: object) -> Choice | None:
    """A pressed choice, or None when it names no registered key, tier or value."""
    if not isinstance(raw, str):
        return None
    body, _, agent = raw.partition("@")
    parts = body.split(":")
    if len(parts) not in (3, 4) or (agent == "" and "@" in raw):
        return None
    key, tier, value, *flag = parts
    spec = FEATURES.get(key)
    if spec is None or tier not in TIERS or tier == "deployment":
        return None
    if value != DEFAULT and value not in spec.values:
        return None
    if flag and flag != [LOCK_FLAG]:
        return None
    if agent and tier != "agent":
        return None
    return Choice(key, cast(Tier, tier), value, bool(flag), agent or None)


def tier_options(
    spec: FeatureSpec, tier: Tier, own: TierSetting, *, can_lock: bool, agent: str | None = None
) -> tuple[tuple[str, str], ...]:
    """``(choice, label)`` for each other value this tier can take: the values, Default, and
    an admin's Lock of each. The agent's own built-in tool is never locked."""
    builtin = tier == "agent" and spec.key.startswith("tool.")
    options: list[tuple[str, str]] = []
    for value in (*spec.values, DEFAULT):
        if (TierSetting(tier, value)).text != own.text:
            options.append((choice(spec.key, tier, value, agent=agent), spec.label(value)))
    if can_lock and not builtin and tier in LOCKABLE and not spec.authority.native:
        options += [
            (choice(spec.key, tier, value, locked=True, agent=agent), value_text(spec, value, True))
            for value in spec.values
            if TierSetting(tier, value, True).text != own.text
        ]
    return tuple(options)


def feature_schema() -> list[dict[str, Any]]:
    """Every registered feature, for a surface that draws its own controls from it."""
    return [
        {
            "key": spec.key,
            "name": spec.name,
            "category": spec.category,
            "categoryName": CATEGORIES[spec.category],
            "effect": spec.effect,
            "combine": spec.combine,
            "values": [{"value": v, "label": label} for v, label in spec.values.items()],
            "default": spec.default,
            "tiers": list(spec.tiers),
            "lockable": [t for t in spec.tiers if t in LOCKABLE and not spec.authority.native],
            "backendTiers": {b: list(t) for b, t in spec.backend_tiers.items()},
            "backendFixed": dict(spec.backend_fixed),
            "backendNames": dict(BACKEND_NAMES),
            "platforms": sorted(spec.platforms),
            "requires": list(spec.requires),
            "dependsOn": list(spec.depends_on),
            "threadAnyone": spec.authority.thread_anyone,
            "native": spec.authority.native,
            "binds": spec.binds,
            "doc": spec.doc,
        }
        for spec in FEATURES.values()
    ]


def view_json(
    view: FeatureView, platform: str | None, *, overrides_active: bool = True
) -> dict[str, Any]:
    """One feature at a place for a surface that draws its own controls: each tier's own
    value (with its `expected` text), what is in effect, where from, and why it cannot apply."""
    resolved = view.resolved
    return {
        "key": view.spec.key,
        "name": view.spec.name,
        "tiers": [
            {"tier": t.tier, "value": t.value, "locked": t.locked, "text": t.text}
            for t in view.tiers
        ],
        "value": resolved.value,
        "source": resolved.tier,
        "locked": resolved.locked,
        "unavailable": resolved.unavailable,
        "summary": source_text(view.spec, resolved, platform)
        + _ignored(view.spec, overrides_active),
    }
