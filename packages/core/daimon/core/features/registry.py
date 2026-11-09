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
"""When a change lands. ``session_mutable``: the live session's tools and servers are updated
in place on the next message. ``session_identity``: held by the live session until ``/new`` or a
replacement (system prompt, skills, memory). ``provision``: on the agent's next save or sync."""
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
EVERY_PLATFORM: Final[frozenset[str]] = frozenset({"*"})
"""`platforms` for a feature that applies wherever a turn runs, chat or not (web, CLI, MCP)."""
_PROVIDER_ONLY: Final[Mapping[str, tuple[Tier, ...]]] = {
    "gemini": ("deployment", "workspace", "agent"),
    "openai": ("deployment", "workspace", "agent"),
}
"""Gemini and OpenAI compile one revision per agent, so a model feature stops at the agent."""
BACKEND_NAMES: Final[Mapping[str, str]] = {
    "anthropic": "Managed Agents",
    "gemini": "Gemini",
    "openai": "OpenAI",
}
BUILTIN_TOOLS: Final[tuple[str, ...]] = (
    "bash", "read", "edit", "write", "grep", "glob", "web_fetch", "web_search",
)  # fmt: skip


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
    backend_fixed: Mapping[str, str] = field(default_factory=dict[str, str])
    """The value a backend always runs with: no tier can change it there."""

    @property
    def combine(self) -> Combine:
        return self.combine_override or _BY_EFFECT[self.effect]

    @property
    def off(self) -> str | None:
        """The value that turns the feature off, for on/off features; None for choices."""
        return "off" if "off" in self.values else None

    def on_platform(self, platform: str) -> bool:
        return self.platforms == EVERY_PLATFORM or platform in self.platforms

    def label(self, value: str) -> str:
        return self.values.get(value, "Default" if value == DEFAULT else value)

    def tiers_on(self, backend: str | None) -> tuple[Tier, ...]:
        """The tiers that apply on `backend`; every declared tier when it is unknown."""
        if backend in self.backend_fixed:
            return ()
        if backend is None or backend not in self.backend_tiers:
            return self.tiers
        allowed = self.backend_tiers[backend]
        return tuple(tier for tier in self.tiers if tier in allowed)


def _builtin(tool: str) -> FeatureSpec:
    words = tool.replace("_", " ")
    return FeatureSpec(
        key=f"tool.{tool}",
        name=f"Built-in tool: {words}",
        category="model_input",
        effect="model",
        values=_ON_OFF,
        default="on",
        tiers=("deployment", "workspace", "agent", "channel", "thread"),
        binds="session_mutable",
        doc=(
            f"The agent's built-in {words} tool. The agent level is the agent's own tool "
            "settings. On never widens what a self-hosted harness allows. Applies from the "
            "next message in a conversation."
        ),
        backend_tiers=_PROVIDER_ONLY,
        platforms=EVERY_PLATFORM,
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    )


_MODEL_INPUT: Final[tuple[FeatureSpec, ...]] = (
    FeatureSpec(
        key="guidance",
        name="Setup guidance",
        category="model_input",
        effect="model",
        values=_ON_OFF,
        default="on",
        tiers=("deployment", "workspace", "agent"),
        binds="provision",
        doc=(
            "Instructions at the top of the agent's prompt on where its keys, repo, memory "
            "and files are and what it may share. Off leaves the agent's own prompt and the "
            "safety rules. Applies when the agent is next saved or synced."
        ),
        platforms=EVERY_PLATFORM,
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
    FeatureSpec(
        key="daimon_tools",
        name="Daimon tools",
        category="model_input",
        effect="model",
        values=_ON_OFF,
        default="on",
        tiers=("deployment", "workspace", "agent", "channel", "thread"),
        binds="session_mutable",
        doc=(
            "Daimon's own tools: chat, settings, routines, memory and the rest. "
            "On keeps at least the conversation ids in the turn controls, which the tools "
            "need. An isolated agent never has them. Gemini and OpenAI agents always have "
            "them, so it can't be changed there. Applies from the next message in a "
            "conversation."
        ),
        backend_fixed={"gemini": "on", "openai": "on"},
        platforms=EVERY_PLATFORM,
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
    *(_builtin(tool) for tool in BUILTIN_TOOLS),
    FeatureSpec(
        key="turn_controls",
        name="Turn controls",
        category="model_input",
        effect="model",
        values={"full": "Full", "origin": "Origin only", "minimal": "Minimal"},
        default="full",
        tiers=("deployment", "workspace", "agent"),
        binds="turn",
        doc=(
            "Facts sent with each message: where it was sent, who is answering, and how "
            "to use them. Origin only keeps the ids and the honesty facts; Minimal keeps "
            "only the facts about the model and the conversation. Daimon tools keep it at "
            "Origin only or more."
        ),
        platforms=EVERY_PLATFORM,
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
    FeatureSpec(
        key="web_awareness",
        name="Web app links",
        category="model_input",
        effect="model",
        values=_ON_OFF,
        default="on",
        tiers=("deployment", "workspace", "agent", "channel", "thread"),
        binds="turn",
        doc="Each message tells the agent the web app's address and the pages it may link to.",
        platforms=EVERY_PLATFORM,
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
)


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
    *_MODEL_INPUT,
)
FEATURE_KEYS: Final[tuple[str, ...]] = tuple(FEATURES)


def feature(key: str) -> FeatureSpec | None:
    return FEATURES.get(key)


def deployment_key(name: str) -> str:
    """A `DAIMON_FEATURES__<NAME>` suffix as its feature key (`TOOL_BASH` is `tool.bash`)."""
    name = name.lower()
    return next((key for key in FEATURES if key.replace(".", "_") == name), name)


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
