"""The feature registry: every per-place switch Daimon offers, declared once.

Pure. Chat `/settings`, the agent's settings tools, the web app and the generated
`docs/features.md` all read these specs; `tests/features/` checks that every key read
in code is registered and every registered key is read.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal, get_args

from daimon.core.admin_actions import DelegatedAction
from daimon.core.stores.domain import CHAT_PLATFORMS

Tier = Literal["deployment", "workspace", "agent", "channel", "thread"]
TIERS: Final[tuple[Tier, ...]] = get_args(Tier)
LOCKABLE: Final[frozenset[Tier]] = frozenset({"deployment", "workspace", "agent", "channel"})
Effect = Literal["model", "display", "capability", "guardrail", "limit"]
Combine = Literal["most_specific", "ceiling", "stricter", "minimum"]
Binding = Literal["turn", "session_mutable", "session_identity", "provision"]
Category = Literal["model_input", "chat", "capabilities", "guardrails"]
Requirement = Literal["platform_adapter"]
"""``platform_adapter``: the chat platform's adapter is set up in this deployment."""
DEFAULT: Final = "default"
"""The value a control writes to drop its own setting and inherit."""

_BY_EFFECT: Final[Mapping[Effect, Combine]] = {
    "model": "most_specific",
    "display": "most_specific",
    "capability": "ceiling",
    "guardrail": "stricter",
    "limit": "minimum",
}
_ON_OFF: Final[Mapping[str, str]] = {"on": "On", "off": "Off"}


@dataclass(frozen=True)
class Authority:
    """Who may set a feature: an admin for `action` at the workspace and channel tiers,
    an owner or admin at the agent tier, anyone at the thread tier when `thread_anyone`.
    `native` (approvals) asks for a live native platform admin at every tier."""

    action: DelegatedAction | None = None
    thread_anyone: bool = False
    native: bool = False


@dataclass(frozen=True)
class FeatureSpec:
    """One feature. `values` maps each stored value to its label, the permissive or
    "on" value first (a guardrail's strictest value comes last). `tiers` are where it can
    be set; `backend_tiers` narrows them per model backend. `combine` overrides the
    effect's rule only to keep an existing key's behaviour, with the reason in its doc."""

    key: str
    name: str
    category: Category
    effect: Effect
    values: Mapping[str, str]
    default: str
    tiers: tuple[Tier, ...]
    binds: Binding
    doc: str
    backend_tiers: Mapping[str, tuple[Tier, ...]] = field(
        default_factory=dict[str, tuple[Tier, ...]]
    )
    platforms: frozenset[str] = frozenset(CHAT_PLATFORMS)
    requires: tuple[Requirement, ...] = ()
    depends_on: tuple[str, ...] = ()
    authority: Authority = Authority()
    legacy_env: tuple[str, ...] = ()
    combine_override: Combine | None = None

    @property
    def combine(self) -> Combine:
        return self.combine_override or _BY_EFFECT[self.effect]

    @property
    def off(self) -> str | None:
        """The value that turns the feature off, for on/off features; None for choices."""
        return "off" if "off" in self.values else None

    def label(self, value: str) -> str:
        return self.values.get(value, "Default" if value == DEFAULT else value)

    def tiers_on(self, backend: str | None) -> tuple[Tier, ...]:
        """The tiers that apply on `backend`; every declared tier when it is unknown."""
        if backend is None or backend not in self.backend_tiers:
            return self.tiers
        allowed = self.backend_tiers[backend]
        return tuple(tier for tier in self.tiers if tier in allowed)


def _features(*specs: FeatureSpec) -> Mapping[str, FeatureSpec]:
    return {spec.key: spec for spec in specs}


FEATURES: Final[Mapping[str, FeatureSpec]] = _features(
    FeatureSpec(
        key="approvals",
        name="Approvals",
        category="guardrails",
        effect="guardrail",
        values={"on": "Ask", "off": "Allow writes"},
        default="on",
        tiers=("workspace", "channel", "thread"),
        binds="turn",
        doc=(
            "Ask before an agent writes to a third-party service. Kept in the access policy "
            "and decided most specific first, as before the registry: only a native "
            "platform admin, checked live, may change it, and it applies only once the "
            "operator enables approval overrides."
        ),
        authority=Authority(native=True),
        combine_override="most_specific",
    ),
    FeatureSpec(
        key="participation",
        name="Thread participation",
        category="chat",
        effect="capability",
        values=_ON_OFF,
        default="off",
        tiers=("deployment", "workspace", "agent", "channel", "thread"),
        binds="turn",
        doc=(
            "Reply in a thread without being mentioned when a short classifier call says "
            "the agent can help. A lower tier may turn it on under a higher Off, as before "
            "the registry; a Lock stops that."
        ),
        platforms=frozenset({"discord", "teams"}),
        requires=("platform_adapter",),
        authority=Authority(DelegatedAction.CHANNEL_PERMISSIONS, thread_anyone=True),
        legacy_env=("DAIMON_THREAD_PARTICIPATION__MODE",),
        combine_override="most_specific",
    ),
    FeatureSpec(
        key="progress_plans",
        name="Progress plans",
        category="chat",
        effect="model",
        values=_ON_OFF,
        default="off",
        tiers=("deployment", "workspace", "agent", "channel", "thread"),
        binds="turn",
        doc=(
            "The agent keeps a short plan of a longer task's steps on the working card, "
            "with one-line updates on what it found and does next."
        ),
        authority=Authority(thread_anyone=True),
        legacy_env=("DAIMON_PROGRESS_PLANS__MODE",),
    ),
    FeatureSpec(
        key="cost_footer",
        name="Cost footer",
        category="chat",
        effect="display",
        values={"shown": "Shown", "hidden": "Hidden"},
        default="shown",
        tiers=("workspace", "channel", "thread"),
        binds="turn",
        doc=(
            "Finished answers end with a footer naming the agent, the time taken, what the "
            "answer cost and the money left."
        ),
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
)
FEATURE_KEYS: Final[tuple[str, ...]] = tuple(FEATURES)


def feature(key: str) -> FeatureSpec | None:
    return FEATURES.get(key)


def parse_deployment(key: str, raw: str) -> tuple[str, bool]:
    """A `DAIMON_FEATURES__<KEY>` value as (value, locked): `<value>` or `<value>:locked`,
    and `disabled` for Off + Locked. Raises ValueError naming the key, never the value."""
    spec = FEATURES.get(key)
    if spec is None or "deployment" not in spec.tiers:
        known = ", ".join(k for k, s in FEATURES.items() if "deployment" in s.tiers)
        raise ValueError(f"unknown feature {key!r} (known: {known})")
    value, _, flag = raw.strip().lower().partition(":")
    if value == "disabled" and spec.off is not None and not flag:
        return spec.off, True
    if value not in spec.values or flag not in ("", "locked"):
        raise ValueError(f"feature {key!r} takes {', '.join(spec.values)}, optionally ':locked'")
    return value, flag == "locked"
