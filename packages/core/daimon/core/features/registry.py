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
from mux.profiles import INLINE_REUSE, MANAGED_AGENTS, PERSISTENT_WORKSPACE

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
ENV_IMPORTED: Final = "env_imported"
"""A workspace-tier row (not a feature) that marks the workspace's deprecated env maps as
copied into rows by `daimon features import-env`."""
DEFAULT: Final = "default"
"""The value a control writes to drop its own setting and inherit."""

_ANTHROPIC, _GEMINI, _OPENAI = (
    MANAGED_AGENTS.provider,
    INLINE_REUSE.provider,
    PERSISTENT_WORKSPACE.provider,
)

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
    _GEMINI: ("deployment", "workspace", "agent"),
    _OPENAI: ("deployment", "workspace", "agent"),
}
"""Gemini and OpenAI compile one revision per agent, so a model feature stops at the agent."""
_MANAGED_AGENTS_ONLY: Final[Mapping[str, tuple[Tier, ...]]] = {_GEMINI: (), _OPENAI: ()}
"""Runs only on Managed Agents turns; Gemini and OpenAI agents never get it."""
BACKEND_NAMES: Final[Mapping[str, str]] = {
    _ANTHROPIC: "Managed Agents",
    _GEMINI: "Gemini",
    _OPENAI: "OpenAI",
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
    platform_defaults: Mapping[str, str] = field(default_factory=dict[str, str])
    """A platform whose default differs from `default`, kept as it was before the registry."""
    env_map: str | None = None
    """The `Settings` field of a deprecated per-workspace map (workspace id to on/off) that
    stands for the workspace tier until `daimon features import-env` copies it into rows."""

    def default_for(self, platform: str) -> str:
        return self.platform_defaults.get(platform, self.default)

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
            "On keeps at least the conversation ids the tools need. An isolated agent "
            "never has them. Gemini and OpenAI agents always have "
            "them, so it can't be changed there. Applies from the next message in a "
            "conversation."
        ),
        backend_fixed={_GEMINI: "on", _OPENAI: "on"},
        platforms=EVERY_PLATFORM,
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
    *(_builtin(tool) for tool in BUILTIN_TOOLS),
    FeatureSpec(
        key="turn_controls",
        name="Message controls",
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
    FeatureSpec(
        key="mcp_briefing",
        name="Connected server briefing",
        category="model_input",
        effect="model",
        values=_ON_OFF,
        default="on",
        tiers=("deployment", "workspace", "agent", "channel", "thread"),
        binds="turn",
        doc=(
            "Before each chat message, the agent's connected servers that offer a context tool "
            "are sent the message, and their answers go in front of it as quoted data. "
            "Managed Agents only."
        ),
        backend_tiers=_MANAGED_AGENTS_ONLY,
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
        legacy_env=("DAIMON_MCP_CONTEXT__ENABLED",),
    ),
    FeatureSpec(
        key="origin_fragments",
        name="Run instructions",
        category="model_input",
        effect="model",
        values=_ON_OFF,
        default="on",
        tiers=("agent",),
        binds="turn",
        doc=(
            "A short note in front of a message that did not come from chat: a routine, a "
            "relayed draft, a handoff or another agent's request, with any wording the "
            "agent's spec adds. Off sends the message alone."
        ),
        platforms=EVERY_PLATFORM,
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
    FeatureSpec(
        key="agent_env_file",
        name="Keys file",
        category="model_input",
        effect="model",
        values=_ON_OFF,
        default="on",
        tiers=("agent",),
        binds="session_mutable",
        doc=(
            "The agent's saved keys as a .env file in its working files. Off leaves the file "
            "out; the keys stay saved. Applies from the next message in a conversation. "
            "Managed Agents only."
        ),
        backend_tiers=_MANAGED_AGENTS_ONLY,
        platforms=EVERY_PLATFORM,
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
    FeatureSpec(
        key="checkpoint_prompt",
        name="Carry working files",
        category="model_input",
        effect="model",
        values=_ON_OFF,
        default="on",
        tiers=("deployment", "workspace", "agent"),
        binds="turn",
        doc=(
            "When a long conversation starts over, the agent is first asked to save its "
            "working files to carry across. Off carries only the conversation and skips "
            "that extra paid step. Managed Agents only."
        ),
        backend_tiers=_MANAGED_AGENTS_ONLY,
        platforms=EVERY_PLATFORM,
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
)


_KEPT = "A conversation keeps what it started with until /new."
_FIXED_ON: Final[Mapping[str, str]] = {_GEMINI: "on", _OPENAI: "on"}
_SESSION_IDENTITY: Final[tuple[FeatureSpec, ...]] = (
    FeatureSpec(
        key="platform_prompt",
        name="Platform prompt",
        category="model_input",
        effect="model",
        values=_ON_OFF,
        default="on",
        tiers=("deployment", "workspace", "agent"),
        binds="session_identity",
        doc=(
            "The agent's prompt is adjusted to the chat platform's words and formatting. "
            f"Off sends the agent's own prompt as written. {_KEPT} OpenAI agents always "
            "get their own prompt."
        ),
        backend_fixed={_OPENAI: "off"},
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
    FeatureSpec(
        key="platform_skills",
        name="Platform skills",
        category="model_input",
        effect="model",
        values=_ON_OFF,
        default="on",
        tiers=("deployment", "workspace", "agent", "channel"),
        binds="session_identity",
        doc=(
            "The skills Daimon adds for the chat platform, such as its formatting. "
            f"{_KEPT} Managed Agents only."
        ),
        backend_tiers=_MANAGED_AGENTS_ONLY,
        platforms=EVERY_PLATFORM,
        authority=Authority(DelegatedAction.SKILLS),
    ),
    FeatureSpec(
        key="agent_skills",
        name="Agent skills",
        category="model_input",
        effect="model",
        values=_ON_OFF,
        default="on",
        tiers=("deployment", "workspace", "agent", "channel"),
        binds="session_identity",
        doc=(
            "The skills listed on the agent. Off leaves them out of new conversations; "
            f"the agent keeps its list. {_KEPT} Always on for Gemini and OpenAI agents."
        ),
        backend_fixed=_FIXED_ON,
        platforms=EVERY_PLATFORM,
        authority=Authority(DelegatedAction.SKILLS),
    ),
    FeatureSpec(
        key="channel_skills",
        name="Channel skills",
        category="model_input",
        effect="model",
        values=_ON_OFF,
        default="on",
        tiers=("workspace", "agent", "channel"),
        binds="session_identity",
        doc=(
            "The skills added to a channel. They need the read tool, so they are off "
            f"wherever it is. {_KEPT} Managed Agents only."
        ),
        backend_tiers=_MANAGED_AGENTS_ONLY,
        platforms=EVERY_PLATFORM,
        depends_on=("tool.read",),
        authority=Authority(DelegatedAction.SKILLS),
    ),
    FeatureSpec(
        key="memory_personal",
        name="Personal memory",
        category="capabilities",
        effect="capability",
        values=_ON_OFF,
        default="on",
        tiers=("deployment", "workspace", "agent", "channel"),
        binds="session_identity",
        doc=(
            "Each person's own memory with the agent, in conversations only they can read. "
            f"On never adds it anywhere else. {_KEPT} Off for Gemini and OpenAI agents."
        ),
        backend_fixed={_GEMINI: "off", _OPENAI: "off"},
        platforms=EVERY_PLATFORM,
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
    FeatureSpec(
        key="memory_shared",
        name="Shared memory",
        category="capabilities",
        effect="capability",
        values=_ON_OFF,
        default="on",
        tiers=("deployment", "workspace", "agent", "channel"),
        binds="session_identity",
        doc=(
            "The agent's memory shared by everyone who talks to it. "
            f"{_KEPT} Always on for Gemini and OpenAI agents."
        ),
        backend_fixed=_FIXED_ON,
        platforms=EVERY_PLATFORM,
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
    FeatureSpec(
        key="memory_channel",
        name="Channel memory",
        category="capabilities",
        effect="capability",
        values=_ON_OFF,
        default="on",
        tiers=("workspace", "agent", "channel"),
        binds="session_identity",
        doc=(
            "The memory a restricted channel keeps for itself in place of the shared one. "
            f"{_KEPT} Managed Agents only."
        ),
        backend_tiers=_MANAGED_AGENTS_ONLY,
        platforms=EVERY_PLATFORM,
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
)

_DM_PLATFORMS: Final = frozenset({"discord", "slack"})
_DISCORD: Final = frozenset({"discord"})
_SETTINGS: Final = Authority(DelegatedAction.AGENT_SETTINGS)
_CHANNELS: Final[tuple[Tier, ...]] = ("deployment", "workspace", "channel")
_ADMIN: Final = Authority(DelegatedAction.CHANNEL_PERMISSIONS)
WEB_MODULES: Final[tuple[str, ...]] = (
    "chat", "agents", "channels", "routines", "credits", "integrations",
)  # fmt: skip


def _shown(
    key: str,
    name: str,
    doc: str,
    *,
    tiers: tuple[Tier, ...] = _CHANNELS,
    default: str = "off",
    platforms: frozenset[str] = frozenset(CHAT_PLATFORMS),
    authority: Authority = _SETTINGS,
    **more: object,
) -> FeatureSpec:
    """A display feature: what a chat shows, most specific tier first."""
    return FeatureSpec(
        key=key,
        name=name,
        category="chat",
        effect="display",
        values=_ON_OFF,
        default=default,
        tiers=tiers,
        binds="turn",
        doc=doc,
        platforms=platforms,
        authority=authority,
        **more,  # pyright: ignore[reportArgumentType]
    )


_CHAT_BEHAVIOUR: Final[tuple[FeatureSpec, ...]] = (
    FeatureSpec(
        key="thread_naming",
        name="Thread titles",
        category="chat",
        effect="capability",
        values=_ON_OFF,
        default="on",
        tiers=_CHANNELS,
        binds="turn",
        doc="A thread Daimon starts is titled from its opening message by a short model call.",
        platforms=_DISCORD,
        authority=_ADMIN,
        legacy_env=("DAIMON_THREAD_NAMING__ENABLED",),
    ),
    _shown(
        "plain_text_mentions",
        "Typed mentions",
        'A message with "@" and the agent\'s name typed as plain text is answered like a real '
        "mention. Direct messages and bots are unchanged.",
        authority=_ADMIN,
    ),
    _shown(
        "completion_pings",
        "Completion pings",
        "The person who asked is pinged when a long answer is finished.",
        legacy_env=("DAIMON_COMPLETION_PINGS",),
        env_map="completion_pings",
    ),
    _shown(
        "table_rendering",
        "Table images",
        "Tables in an answer are drawn as an image (Discord) or a native table (Slack) "
        "instead of text.",
        platforms=_DM_PLATFORMS,
        default="on",
        platform_defaults={"slack": "off"},
        legacy_env=("DAIMON_TABLE_RENDERING",),
        env_map="table_rendering",
    ),
    _shown(
        "budget_notices",
        "Budget notices",
        "Admins are told in a direct message when a channel's budget is nearly used or spent.",
        tiers=("deployment", "workspace"),
        default="on",
        legacy_env=("DAIMON_BUDGET_NOTICES",),
        env_map="budget_notices",
    ),
    _shown(
        "native_footer_cost",
        "Cost on hosted-model footers",
        "A finished answer from an OpenAI or Gemini agent shows what it cost, or that the "
        "cost is unknown or partial. Managed Agents footers are unchanged.",
        tiers=("deployment", "workspace"),
        legacy_env=("DAIMON_TURN__NATIVE_FOOTER_COST",),
    ),
    _shown(
        "message_feedback",
        "Vote buttons",
        "Finished answers get thumbs up and down for feedback.",
        default="on",
        platforms=_DISCORD,
    ),
    _shown(
        "unmentioned_reply_hint",
        "Reply hint",
        "A short reminder that a reply needs a mention, where an unmentioned message is skipped.",
        platforms=_DISCORD,
        tiers=_CHANNELS,
        legacy_env=("DAIMON_DISCORD__UNMENTIONED_REPLY_HINT",),
    ),
    _shown(
        "writers_none_notice",
        "No-writers notice",
        "Says so when a channel's write access is set to no one and a message is skipped.",
        platforms=_DISCORD,
        legacy_env=("DAIMON_DISCORD__WRITERS_NONE_NOTICE",),
    ),
    _shown(
        "waiting_card",
        "Waiting card",
        "A card shows in a channel whose turn is waiting on the workspace's turn cap.",
        platforms=_DISCORD,
        tiers=("deployment", "workspace"),
        legacy_env=("DAIMON_DISCORD__TENANT_CAP_WAITING_CARD",),
    ),
    FeatureSpec(
        key="private_channel_messages",
        name="Private channel messages",
        category="capabilities",
        effect="capability",
        values=_ON_OFF,
        default="off",
        tiers=_CHANNELS,
        binds="turn",
        doc=(
            "Every message in Teams private and shared channels is read through Microsoft "
            "Graph, not only the ones that mention the agent."
        ),
        platforms=frozenset({"teams"}),
        authority=_ADMIN,
        legacy_env=("DAIMON_PRIVATE_CHANNEL_MESSAGES__ENABLED",),
    ),
    FeatureSpec(
        key="direct_messages",
        name="Direct messages",
        category="capabilities",
        effect="capability",
        values=_ON_OFF,
        default="off",
        tiers=("deployment", "workspace"),
        binds="turn",
        doc=(
            "Conversations may move to a direct message with the agent. The agent that answers "
            "there is the workspace's DM agent, which is chosen apart from this."
        ),
        platforms=_DM_PLATFORMS,
        authority=_ADMIN,
    ),
    FeatureSpec(
        key="dm_memory_read_only",
        name="Read-only memory in direct messages",
        category="guardrails",
        effect="guardrail",
        values={"off": "Writable", "on": "Read-only"},
        default="off",
        tiers=("deployment", "workspace"),
        binds="turn",
        doc="A turn started from a direct message can read the agent's memory but not change it.",
        platforms=_DM_PLATFORMS,
        authority=_ADMIN,
    ),
)

_WORKSPACE: Final[tuple[FeatureSpec, ...]] = tuple(
    FeatureSpec(
        key=f"web.{module}",
        name=f"Web app: {module}",
        category="capabilities",
        effect="capability",
        values=_ON_OFF,
        default="on",
        tiers=("deployment", "workspace"),
        binds="turn",
        doc=f"The {module} pages of the web app. Off hides them for everyone in the workspace.",
        platforms=EVERY_PLATFORM,
        authority=_ADMIN,
    )
    for module in WEB_MODULES
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
            "Ask before an agent writes to a third-party service. Decided most specific first: "
            "only a native platform admin, checked live, may change it, and it applies "
            "only once the operator enables approval overrides."
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
    FeatureSpec(
        key="repo_memory",
        name="Repository memory",
        category="capabilities",
        effect="capability",
        values=_ON_OFF,
        default="off",
        tiers=("deployment", "workspace", "agent", "channel", "thread"),
        binds="turn",
        doc=(
            "Each message starts with the notes the agent keeps in its repository memory. "
            "The operator turns it on, with the memory tools; below that it can only be "
            "turned off, which also switches the memory tools off."
        ),
        platforms=EVERY_PLATFORM,
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
        legacy_env=("DAIMON_REPO_MEMORY__ENABLED",),
    ),
    FeatureSpec(
        key="context_provider",
        name="Context provider",
        category="capabilities",
        effect="capability",
        values=_ON_OFF,
        default="on",
        tiers=("deployment", "workspace", "agent", "channel", "thread"),
        binds="turn",
        doc=(
            "A context provider set up for this channel or agent adds what it knows to "
            "each message. Off skips it here. Managed Agents only."
        ),
        backend_tiers=_MANAGED_AGENTS_ONLY,
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
    FeatureSpec(
        key="history_depth",
        name="Chat history",
        category="model_input",
        effect="limit",
        values={
            "100": "100 messages",
            "50": "50 messages",
            "25": "25 messages",
            "10": "10 messages",
            "0": "None",
        },
        default="100",
        tiers=("deployment", "workspace", "agent", "channel"),
        binds="turn",
        doc=(
            "How many earlier chat messages a new conversation reads, up to 100 from the "
            "thread and 25 from the channel around it. The smallest setting wins."
        ),
        platforms=frozenset({"discord", "slack", "teams"}),
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
    *_MODEL_INPUT,
    *_SESSION_IDENTITY,
    *_CHAT_BEHAVIOUR,
    *_WORKSPACE,
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
