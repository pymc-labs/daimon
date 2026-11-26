"""The feature registry: every per-place switch Daimon offers, declared once.

Pure. Chat `/settings`, the agent's settings tools, the web app and the generated
`docs/features.md` all read these specs; `tests/features/` checks that every key read
in code is registered and every registered key is read.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal, get_args

from daimon.core.admin_actions import DelegatedAction
from daimon.core.stores.domain import CHAT_PLATFORMS
from mux.profiles import INLINE_REUSE, MANAGED_AGENTS, PERSISTENT_WORKSPACE

Tier = Literal["deployment", "workspace", "agent", "channel", "thread", "routine"]
TIER_ORDER: Final[tuple[Tier, ...]] = get_args(Tier)
"""Every tier, broadest first; a routine's own tier is the most specific of its runs."""
TIERS: Final[tuple[Tier, ...]] = TIER_ORDER[:-1]
"""The tiers of a chat place, every feature's unless narrowed."""
LOCKABLE: Final[frozenset[Tier]] = frozenset({"deployment", "workspace", "agent", "channel"})
Effect = Literal["model", "display", "capability", "guardrail", "limit"]
Combine = Literal["most_specific", "ceiling", "stricter", "minimum"]
Binding = Literal["turn", "session_mutable", "session_identity", "provision", "restart"]
"""When a change lands. ``session_mutable``: the live session's tools and servers are updated
in place on the next message. ``session_identity``: held by the live session until ``/new`` or a
replacement (system prompt, skills, memory). ``provision``: on the agent's next save or sync.
``restart``: read once when the process starts."""
Category = Literal["model_input", "chat", "capabilities", "limits", "guardrails"]
Requirement = Literal[
    "platform_adapter",
    "anthropic_key",
    "compute_setup",
    "notebook_host",
    "report_host",
    "gemini_key",
    "microsoft_accounts",
    "repo_memory_setup",
    "teams_public_url",
    "support_channel",
    "support_credits",
]
"""``platform_adapter``: the chat platform's adapter is set up in this deployment.
``support_channel``: the platform has a request channel, the deployment's or the workspace's own.
``support_credits``: people have credits to ask (Everyone above 0, or any limit).
The rest name one integration's setup: its operator switch or host address, credentials and keys."""
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
_OFF_ON: Final[Mapping[str, str]] = {"off": "Off", "on": "On"}
"""A guardrail's on/off: On is the stricter value, so it comes last."""
EVERY_PLATFORM: Final[frozenset[str]] = frozenset({"*"})
"""`platforms` for a feature that applies wherever a turn runs, chat or not (web, CLI, MCP)."""
_PROVIDER_ONLY: Final[Mapping[str, tuple[Tier, ...]]] = {
    _GEMINI: ("deployment", "workspace", "agent"),
    _OPENAI: ("deployment", "workspace", "agent"),
}
"""Gemini and OpenAI compile one revision per agent, so a model feature stops at the agent."""
_MANAGED_AGENTS_ONLY: Final[Mapping[str, tuple[Tier, ...]]] = {_GEMINI: (), _OPENAI: ()}
"""Runs only on Claude agent turns; Gemini and OpenAI agents never get it."""
BACKEND_NAMES: Final[Mapping[str, str]] = {
    _ANTHROPIC: "Claude",
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
    `native` (approvals) asks for a live native platform admin at every tier. With
    `action=None` only a server admin may set it, and `agent_owner=False` keeps an
    agent's owner from setting the agent tier too (money and background work)."""

    action: DelegatedAction | None = None
    thread_anyone: bool = False
    native: bool = False
    agent_owner: bool = True


_MARKUP = frozenset("<>@#*_~|`[]\\:")
"""Mention and markdown marks on any platform, and the `:` the tier text and choice use."""
_PLATFORM_RESERVED = ("discord", "clyde")
"""Words Discord refuses in a webhook name."""


def is_feature_text(value: str, max_length: int) -> bool:
    """A free-text value: one trimmed printable line of plain words, never `default`."""
    folded = value.casefold()
    return (
        0 < len(value) <= max_length
        and value == value.strip()
        and value.isprintable()
        and not _MARKUP.intersection(value)
        and not any(word in folded for word in _PLATFORM_RESERVED)
        and value != DEFAULT
    )


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
    """The `Settings` field (dotted for a nested block) of a deprecated per-workspace map
    (workspace id to on/off) that stands for the workspace tier; a stored workspace row always
    beats it."""
    minimum: float = 0
    """The smallest number a limit's deployment tier takes. 0 where no limit is a real setting;
    above 0 where 0 would be an unusable limit, not an off switch."""
    pinned: bool = False
    """An On at the agent or routine tier holds only for the agent and environment in the
    `pin` kept with it; a write without one is refused, and no other tier takes On."""
    max_length: int = 0
    """Above 0, the feature also takes any one-line text up to this many characters (a name),
    besides its listed values."""
    connect_only: bool = False
    """An On needs proof only a sign-in flow has (the GitHub connect page): chat, tools, the web
    app, the settings panel and an agent's spec refuse it, and only the operator CLI may set it.
    Off and clearing stay open to anyone who may change it."""
    zero_unlimited: bool = False
    """A limit whose deployment 0 means no limit, as its legacy variable did: when settings
    combine, 0 is the loosest value, never the smallest."""

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

    def accepts(self, value: str, tier: Tier) -> bool:
        """Whether `value` may be stored at `tier`. A limit's deployment tier also takes any
        number from its `minimum`, so an operator's variable keeps its exact value; lower tiers
        choose from the listed steps."""
        if value in self.values:
            return True
        if self.max_length:
            return is_feature_text(value, self.max_length)
        if self.effect == "limit" and tier == "deployment":
            try:
                number = float(value)
            except ValueError:
                return False
            return math.isfinite(number) and number >= self.minimum
        return False

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


def limit_int(value: str) -> int:
    """A whole-number limit as text (`12`, or `12.0` as a deployment variable may say it).
    Raises ValueError for text `FeatureSpec.accepts` would refuse as not finite."""
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("a limit must be a finite number")
    return int(number)


_BUILTIN_WORDS: Final[Mapping[str, tuple[str, str]]] = {
    "bash": ("Run commands", "run commands"),
    "read": ("Read files", "read files"),
    "edit": ("Edit files", "edit files"),
    "write": ("Write files", "write new files"),
    "grep": ("Search file contents", "search inside files"),
    "glob": ("Find files", "find files by name"),
    "web_fetch": ("Open web pages", "open web pages"),
    "web_search": ("Search the web", "search the web"),
}
"""Each built-in tool's name and what it lets the agent do."""


_SEARCH_BILLING: Final = (
    " Each search is billed at the provider's list price, past any free allowance."
)


def _builtin(tool: str) -> FeatureSpec:
    name, does = _BUILTIN_WORDS[tool]
    return FeatureSpec(
        key=f"tool.{tool}",
        name=name,
        category="model_input",
        effect="model",
        values=_ON_OFF,
        default="on",
        tiers=("deployment", "workspace", "agent", "channel", "thread"),
        binds="session_mutable",
        doc=(
            f"The agent can {does}. Off removes it here; On never gives more than the "
            "agent's own tool settings allow. Applies from the next message."
            + (_SEARCH_BILLING if tool == "web_search" else "")
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
            "Instructions at the top of the agent's prompt on where its keys, repo, memory and "
            "files are and what it may share. Off leaves the agent's own prompt and the safety "
            "rules. Applies when the agent is next saved."
        ),
        platforms=EVERY_PLATFORM,
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
    *(_builtin(tool) for tool in BUILTIN_TOOLS),
    FeatureSpec(
        key="web_awareness",
        name="Web app",
        category="model_input",
        effect="model",
        values=_ON_OFF,
        default="on",
        tiers=("deployment", "workspace", "agent", "channel", "thread"),
        binds="turn",
        doc=(
            "The agent knows the web app: messages, routine runs and tool results carry its "
            "address and pages to link, its invite and access tools are offered, and cards show "
            "buttons into it. Off leaves it out."
        ),
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
            "see the message, and their answers go in front of it as quoted text. Claude agents "
            "only."
        ),
        backend_tiers=_MANAGED_AGENTS_ONLY,
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
        legacy_env=("DAIMON_MCP_CONTEXT__ENABLED",),
    ),
    FeatureSpec(
        key="origin_fragments",
        name="Notes on non-chat messages",
        category="model_input",
        effect="model",
        values=_ON_OFF,
        default="on",
        tiers=TIERS,
        binds="turn",
        doc=(
            "A short note in front of a message that did not come from chat, such as a routine, a"
            " handoff or another agent's request. Off sends the message alone."
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
        tiers=TIERS,
        binds="session_mutable",
        doc=(
            "The agent's saved keys as a .env file in its working files. Off leaves the file "
            "out; the keys stay saved. Applies from the next message. "
            "Claude agents only."
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
        tiers=TIERS,
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
        tiers=TIERS,
        binds="session_identity",
        doc=(
            "The skills Daimon adds for the chat app, such as its formatting. They need the read "
            "tool, so they are off wherever it is. A conversation keeps what it started with "
            "until /new. Claude agents only."
        ),
        backend_tiers=_MANAGED_AGENTS_ONLY,
        platforms=EVERY_PLATFORM,
        depends_on=("tool.read",),
        authority=Authority(DelegatedAction.SKILLS),
    ),
    FeatureSpec(
        key="agent_skills",
        name="Agent skills",
        category="model_input",
        effect="model",
        values=_ON_OFF,
        default="on",
        tiers=TIERS,
        binds="session_identity",
        doc=(
            "The skills listed on the agent. Off leaves them out of new conversations; "
            "the agent keeps its list. Skills need the read tool, so they are off wherever it "
            f"is. {_KEPT} Always on for Gemini and OpenAI agents."
        ),
        backend_fixed=_FIXED_ON,
        platforms=EVERY_PLATFORM,
        depends_on=("tool.read",),
        authority=Authority(DelegatedAction.SKILLS),
    ),
    FeatureSpec(
        key="channel_skills",
        name="Channel skills",
        category="model_input",
        effect="model",
        values=_ON_OFF,
        default="on",
        tiers=TIERS,
        binds="session_identity",
        doc=(
            "The skills added to a channel. They need the read tool, so they are off wherever it "
            "is. A conversation keeps what it started with until /new."
        ),
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
        tiers=TIERS,
        binds="session_identity",
        doc=(
            "Each person's own memory with the agent, in conversations only they can read. "
            f"On never adds it anywhere else. {_KEPT}"
        ),
        platforms=EVERY_PLATFORM,
        requires=("anthropic_key",),
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
    FeatureSpec(
        key="memory_shared",
        name="Shared memory",
        category="capabilities",
        effect="capability",
        values=_ON_OFF,
        default="on",
        tiers=TIERS,
        binds="session_identity",
        doc=(
            "The agent's memory shared by everyone who talks to it. "
            f"{_KEPT} Always on for Gemini and OpenAI agents."
        ),
        backend_fixed=_FIXED_ON,
        platforms=EVERY_PLATFORM,
        requires=("anthropic_key",),
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
    FeatureSpec(
        key="memory_channel",
        name="Channel memory",
        category="capabilities",
        effect="capability",
        values=_ON_OFF,
        default="on",
        tiers=TIERS,
        binds="session_identity",
        doc=(
            "The memory a restricted channel keeps for itself in place of the shared one. Off "
            f"leaves such a channel with no memory at all. {_KEPT}"
        ),
        platforms=EVERY_PLATFORM,
        requires=("anthropic_key",),
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
)

_DM_PLATFORMS: Final = frozenset({"discord", "slack"})
_DM_MEMORY_PLATFORMS: Final = frozenset({"discord", "slack", "teams", "web"})
_DISCORD: Final = frozenset({"discord"})
_CALL_GATE_PLATFORMS: Final = frozenset({"discord", "slack", "teams", "web"})
_SETTINGS: Final = Authority(DelegatedAction.AGENT_SETTINGS)
# Long jobs spend money in the background: server admins only, no delegation, no owners.
_SERVER_ADMIN: Final = Authority(None, agent_owner=False)
_TIME_LIMITS: Final[Mapping[str, str]] = {
    "45": "45 minutes",
    "30": "30 minutes",
    "20": "20 minutes",
    "15": "15 minutes",
}
"""Choices of both time limits, loosest first: the one set keeps them in step."""
_CHANNELS: Final[tuple[Tier, ...]] = ("deployment", "workspace", "channel")
_AGENT_CHANNELS: Final[tuple[Tier, ...]] = ("deployment", "workspace", "agent", "channel")
"""A Discord display choice an agent may differ in from the others in its server."""
_ADMIN: Final = Authority(DelegatedAction.CHANNEL_PERMISSIONS)
# Monitoring keeps what people say: workspace admins switch it on, an agent's owner may not.
_ADMIN_NOT_OWNER: Final = Authority(DelegatedAction.CHANNEL_PERMISSIONS, agent_owner=False)
WEB_MODULES: Final[tuple[str, ...]] = (
    "chat", "agents", "channels", "routines", "credits", "integrations",
)  # fmt: skip


def _shown(
    key: str,
    name: str,
    doc: str,
    *,
    tiers: tuple[Tier, ...] = TIERS,
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
    FeatureSpec(
        key="display_name",
        name="Display name",
        category="chat",
        effect="display",
        values={"id": "The agent's id"},
        default="id",
        tiers=("agent",),
        binds="turn",
        doc="The name people see for an agent: up to 40 plain characters, never another agent's.",
        max_length=40,
    ),
    FeatureSpec(
        key="thread_title_style",
        name="Thread title style",
        category="chat",
        effect="display",
        values={"sentence": "Sentence case", "title": "Title Case"},
        default="sentence",
        tiers=_AGENT_CHANNELS,
        binds="turn",
        doc="Sentence case up to 50 characters, or Title Case up to 100, for a thread opened "
        "to answer a mention.",
        platforms=_DISCORD,
        authority=_ADMIN,
    ),
    FeatureSpec(
        key="thread_auto_archive",
        name="Thread archive time",
        category="chat",
        effect="display",
        values={"10080": "1 week", "1440": "1 day"},
        default="10080",
        tiers=_AGENT_CHANNELS,
        binds="turn",
        doc="How long an idle thread opened to answer a mention stays open.",
        platforms=_DISCORD,
        authority=_ADMIN,
    ),
    _shown(
        "status_reactions",
        "Status reactions",
        "⏳ while the agent works, then ✅ first however it ended, and 📨 instead of a notice on a "
        "queued message. Off shows 👀, and ✅ only with Completion pings.",
        tiers=_AGENT_CHANNELS,
        platforms=_DISCORD,
        authority=_ADMIN,
    ),
    _shown(
        "protected_notice_reaction",
        "Lock reaction",
        "A 🔒 on a mention the agent may not answer, besides the direct message saying why.",
        tiers=_AGENT_CHANNELS,
        default="on",
        platforms=_DISCORD,
        authority=_ADMIN,
    ),
    _shown(
        "pre_tool_messages",
        "Text before tool calls",
        "Text written before a tool call is posted as its own message. Off keeps it on the "
        "working card.",
        default="on",
        platforms=_DISCORD,
    ),
    _shown(
        "card_timer",
        "Working card timer",
        "The card shows Working or Thinking with the time, and the model in its footer.",
        platforms=_DISCORD,
    ),
    _shown(
        "answer_bots",
        "Answer bots",
        "Answer a bot that mentions the agent, under people's access rules; never itself.",
        tiers=("deployment", "workspace", "agent"),
        platforms=_DISCORD,
        authority=_ADMIN,
    ),
    _shown(
        "plain_text_mentions",
        "Typed mentions",
        'A message with "@" and the agent\'s name typed as plain text is answered like a real '
        "mention. Direct messages and bots are unchanged.",
        tiers=_CHANNELS,
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
        "dm_completion_pings",
        "Completion pings in direct messages",
        "Direct messages follow Completion pings.",
        tiers=("deployment", "workspace"),
        platforms=_DISCORD,
        authority=_ADMIN,
    ),
    _shown(
        "model_reactions",
        "Model reactions",
        "The first message in a thread, and the first after the model or effort changes, gets "
        "an emoji for the model and one for the effort it runs on.",
        tiers=("workspace", "agent"),
        platforms=frozenset({"discord"}),
    ),
    _shown(
        "routine_result_header",
        "Routine result header",
        'Off drops the "Result from" line above a routine\'s result.',
        tiers=("deployment", "workspace", "agent", "channel", "routine"),
        default="on",
    ),
    _shown(
        "tool_server_names",
        "Tool server names",
        "Tool lines on the working card name the connected server each tool belongs to.",
        tiers=("workspace", "agent"),
        default="on",
    ),
    _shown(
        "table_rendering",
        "Table images",
        "Tables in an answer are drawn as an image or a native table instead of text.",
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
        tiers=_CHANNELS,
        default="on",
        legacy_env=("DAIMON_BUDGET_NOTICES",),
        env_map="budget_notices",
    ),
    _shown(
        "message_feedback",
        "Vote buttons",
        "Finished answers get thumbs up and down for feedback.",
        default="on",
    ),
    FeatureSpec(
        key="private_channel_messages",
        name="Private channel messages",
        category="capabilities",
        effect="capability",
        values=_ON_OFF,
        default="on",
        tiers=("deployment",),
        binds="restart",
        doc=(
            "Every message in private and shared channels is read through Microsoft Graph, not "
            "only the ones that mention the agent. Needs the bot's public address. Whoever runs "
            "Daimon sets it, and it applies after a restart."
        ),
        platforms=frozenset({"teams"}),
        requires=("teams_public_url",),
        authority=_ADMIN,
        legacy_env=("DAIMON_PRIVATE_CHANNEL_MESSAGES__ENABLED",),
    ),
    FeatureSpec(
        key="shared_connections",
        name="Shared connections",
        category="model_input",
        effect="capability",
        values=_ON_OFF,
        default="on",
        tiers=("deployment", "workspace"),
        binds="turn",
        doc="Tool servers an admin shares, each tool approved first.",
        platforms=EVERY_PLATFORM,
        authority=_ADMIN,
    ),
    FeatureSpec(
        key="direct_messages",
        name="Direct messages",
        category="capabilities",
        effect="capability",
        values=_ON_OFF,
        default="off",
        tiers=("deployment", "workspace", "agent"),
        binds="turn",
        doc=(
            "People may message the agent directly; on for the team, every agent takes part "
            "unless it opts out. Who may message which agent is set apart from this."
        ),
        # Teams personal chats always reached an agent, so they stay on unless turned off.
        platforms=_DM_PLATFORMS | {"teams"},
        platform_defaults={"teams": "on"},
        authority=_ADMIN,
    ),
    FeatureSpec(
        key="dm_debounce_seconds",
        name="Direct message wait",
        category="chat",
        effect="display",
        values={
            "0": "Off",
            "1": "1 second",
            "3": "3 seconds",
            "5": "5 seconds",
        },
        default="0",
        tiers=("deployment", "workspace"),
        binds="turn",
        doc=(
            "Holds a direct message for more, so a burst gets one reply; at most five seconds, "
            "and never an approval reply."
        ),
        platforms=_DM_PLATFORMS | {"teams"},
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
        doc="A reply in a direct message can read the agent's memory but not change it.",
        platforms=_DM_PLATFORMS | {"teams"},
        authority=_ADMIN,
    ),
    FeatureSpec(
        key="dm_approvals",
        name="Approvals in direct messages",
        category="guardrails",
        effect="capability",
        values=_ON_OFF,
        default="off",
        tiers=("deployment", "workspace"),
        binds="turn",
        doc="Direct messages follow the agent's Approvals.",
        platforms=_DM_PLATFORMS | {"teams"},
        authority=Authority(native=True),
    ),
    FeatureSpec(
        key="thread_new",
        name="/new in threads",
        category="chat",
        effect="capability",
        values=_ON_OFF,
        default="off",
        tiers=("deployment", "workspace"),
        binds="turn",
        doc="/new works in threads Daimon started.",
        platforms=_DISCORD,
        authority=_ADMIN,
    ),
    FeatureSpec(
        key="audience_hold",
        name="Rooms with outside people",
        category="guardrails",
        effect="guardrail",
        values={"off": "Off", "shadow": "Report only", "on": "On"},
        default="off",
        tiers=("deployment", "workspace"),
        binds="turn",
        doc="Report only changes nothing.",
        platforms=frozenset({"discord"}),
        authority=_SERVER_ADMIN,
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
        doc=f"The {module} pages of the web app. Off hides them for everyone on the team.",
        platforms=EVERY_PLATFORM,
        authority=_ADMIN,
    )
    for module in WEB_MODULES
)

_GUARDRAILS: Final[tuple[FeatureSpec, ...]] = (
    FeatureSpec(
        key="tool_safety",
        name="Tool safety",
        category="guardrails",
        effect="guardrail",
        values=_OFF_ON,
        default="on",
        tiers=("deployment",),
        binds="turn",
        doc=(
            "A change through a connected tool waits for the person to press Approve. In a "
            "routine or other run nobody watches, it is refused unless whoever runs Daimon "
            "allowed that tool. Reads always run."
        ),
        platforms=EVERY_PLATFORM,
        authority=_ADMIN,
        legacy_env=("DAIMON_TOOL_SAFETY__ENABLED",),
    ),
    FeatureSpec(
        key="host_call_gate",
        name="Approve cards for OpenAI and Gemini",
        category="guardrails",
        effect="guardrail",
        values=_OFF_ON,
        default="on",
        tiers=("deployment",),
        binds="turn",
        doc=(
            "On an OpenAI or Gemini agent, a tool that needs approval posts an Approve card "
            "and runs once the person presses it. Off refuses those tools there."
        ),
        platforms=_CALL_GATE_PLATFORMS,
        authority=_ADMIN,
    ),
    FeatureSpec(
        key="mcp_connector_relay",
        name="Connectors on OpenAI and Gemini",
        category="guardrails",
        effect="guardrail",
        values=_OFF_ON,
        default="off",
        tiers=("deployment",),
        binds="turn",
        doc=(
            "An agent's own connectors work on OpenAI and Gemini agents with the person's "
            "sign-in, and that provider receives what they return. Off leaves them out."
        ),
        platforms=_CALL_GATE_PLATFORMS,
        authority=_ADMIN,
    ),
    FeatureSpec(
        key="reach_budget",
        name="Reach check",
        category="guardrails",
        effect="guardrail",
        values=_OFF_ON,
        default="on",
        tiers=TIERS,
        binds="turn",
        doc=(
            "Before one reply posts in more places, opens more threads or tags more people "
            "than the limits allow, the person approves it on a card. Where no card can be "
            "shown, the post is refused."
        ),
        platforms=EVERY_PLATFORM,
        authority=_ADMIN,
        legacy_env=("DAIMON_OUTBOUND__REACH_BUDGET_ENABLED",),
    ),
    FeatureSpec(
        key="unattended_capabilities",
        name="Built-in tools in routines",
        category="guardrails",
        effect="guardrail",
        values={"on": "Allowed", "off": "Off"},
        default="on",
        tiers=("deployment", "workspace", "agent"),
        binds="turn",
        doc=(
            "Allowed lets an admin turn on built-in tools for an agent's or one routine's runs. "
            "Off keeps them off in every routine, even one an admin turned them on for."
        ),
        platforms=EVERY_PLATFORM,
        authority=_ADMIN,
    ),
    FeatureSpec(
        key="routine_sandbox_tools",
        name="Sandbox tools in routines",
        category="guardrails",
        effect="capability",
        values={"on": "On", "off": "Off"},
        default="off",
        tiers=("workspace", "agent", "routine"),
        binds="turn",
        doc=(
            "On gives an agent's or one routine's runs its built-in tools, for the agent and "
            "environment it was set with. A routine's value beats its agent's; Built-in tools "
            "in routines set to Off still wins. Only a server admin can change it."
        ),
        platforms=EVERY_PLATFORM,
        authority=_SERVER_ADMIN,
        # Off is the default and the safe value: a ceiling would let a plain Off above stop a
        # routine's own On. The nearer tier decides; a Lock above still wins.
        combine_override="most_specific",
        pinned=True,
    ),
    FeatureSpec(
        key="shared_write_approval",
        name="Approval for shared connection changes",
        category="guardrails",
        effect="guardrail",
        values={"skip": "Skip the card", "ask": "Ask"},
        default="ask",
        tiers=("deployment", "workspace", "agent"),
        binds="turn",
        doc=(
            "Skip runs shared connections' writes with no card in chat. After data reads"
            " each call asks; routines need the operator."
        ),
        platforms=EVERY_PLATFORM,
        authority=_ADMIN_NOT_OWNER,
        # Ask is the default and the strict value: the nearer tier decides, as for watches.
        combine_override="most_specific",
    ),
    FeatureSpec(
        key="watch_approval",
        name="Approval for watches",
        category="guardrails",
        effect="guardrail",
        values={"skip": "Skip the card", "ask": "Ask"},
        default="ask",
        tiers=("workspace", "agent"),
        binds="turn",
        doc=(
            "Ask shows a card with the destination and duration before a watch is made or resumed;"
            " Skip makes it in the same thread for the default duration. Editing a watch, long "
            "jobs and key rotation keep asking. Only a server admin can change it."
        ),
        platforms=EVERY_PLATFORM,
        authority=_SERVER_ADMIN,
        # Ask is the default and the strict value, so a stricter-wins rule could never
        # be loosened. Only a server admin sets either tier; the nearer one decides.
        combine_override="most_specific",
    ),
    FeatureSpec(
        key="compute_approval",
        name="Approval for cloud computers",
        category="guardrails",
        effect="guardrail",
        values={"skip": "Skip the card", "ask": "Ask"},
        default="ask",
        tiers=("workspace", "agent"),
        binds="turn",
        doc=(
            "Ask shows a card to start or extend a cloud computer. Skip: no card, default time "
            "only; OpenAI and Gemini still ask. Only a server admin sets it."
        ),
        platforms=EVERY_PLATFORM,
        authority=_SERVER_ADMIN,
        # As `watch_approval`: Ask is the strict default, so the nearer tier decides.
        combine_override="most_specific",
    ),
    FeatureSpec(
        key="compute_wake_runs",
        name="Cloud computers continue after a job",
        category="guardrails",
        effect="guardrail",
        values=_ON_OFF,
        default="off",
        tiers=("workspace", "agent"),
        binds="turn",
        doc=(
            "After an unwatched command ends, the agent may run the next, up to a set number "
            "in a row; then it waits for the owner to run a command or write a file (not a "
            "message). Off: it only reports. Only a server admin sets it."
        ),
        platforms=EVERY_PLATFORM,
        depends_on=("compute",),
        authority=_SERVER_ADMIN,
        # Off is the default and the strict value, so a stricter-wins rule could never
        # turn it on. Only a server admin sets either tier; the nearer one decides.
        combine_override="most_specific",
    ),
    FeatureSpec(
        key="compute_agent_chat",
        name="Cloud computers from agent chat",
        category="guardrails",
        effect="guardrail",
        values=_ON_OFF,
        default="off",
        tiers=("workspace", "agent"),
        binds="turn",
        doc=(
            "Agent chat may start cloud computers without a card and use them; it lists and "
            "stops all the person's. So may their keys driving it. Only a server admin sets it."
        ),
        platforms=EVERY_PLATFORM,
        depends_on=("compute",),
        authority=_SERVER_ADMIN,
        # As `compute_wake_runs`: Off is the strict default, so the nearer tier decides.
        combine_override="most_specific",
    ),
    FeatureSpec(
        key="require_routine_destination",
        name="Routines must say where they post",
        category="guardrails",
        effect="guardrail",
        values={"off": "Optional", "on": "Required"},
        default="off",
        tiers=("deployment", "workspace"),
        binds="turn",
        doc=(
            "A routine the agent makes must name where its results go, or it is not made. "
            "Existing routines are not changed."
        ),
        platforms=EVERY_PLATFORM,
        authority=_ADMIN,
        legacy_env=("DAIMON_MCP__REQUIRE_ROUTINE_DESTINATION",),
    ),
    *(
        FeatureSpec(
            key=key,
            name=name,
            category="guardrails",
            effect="capability",
            values=_ON_OFF,
            default="off",
            tiers=("deployment", "workspace", "agent"),
            # Read on every recorded turn, so a conversation begun before it is turned on
            # is recorded from then on, and nothing from before.
            binds="turn",
            doc=doc,
            platforms=EVERY_PLATFORM,
            authority=_ADMIN_NOT_OWNER,
        )
        for key, name, doc in (
            (
                "monitor_direct_messages",
                "Monitor direct messages",
                "Keeps what people ask this agent in direct messages and web chat, and what "
                "it answers, for admins to read. People are told in the conversation. "
                "Kept from when it is turned on, for the retention period.",
            ),
            (
                "monitor_mcp",
                "Monitor coding tools",
                "Keeps what people ask this agent from their coding tools and its answers, "
                "plus the name, time and outcome of their other tool calls, never what they "
                "sent or returned. People are told in the conversation or their first "
                "tool result. Kept from when it is turned on, for the retention period.",
            ),
        )
    ),
    FeatureSpec(
        key="github_follow_org",
        name="Follow the GitHub org",
        category="guardrails",
        effect="capability",
        values=_ON_OFF,
        default="off",
        tiers=("agent",),
        # Read when GitHub's repo list for the org is refreshed, at most an hour apart.
        binds="provision",
        doc=("Follows the whole org."),
        platforms=EVERY_PLATFORM,
        authority=_ADMIN_NOT_OWNER,
        connect_only=True,
    ),
)


def _features(*specs: FeatureSpec) -> Mapping[str, FeatureSpec]:
    return {spec.key: spec for spec in specs}


def _capability(
    key: str,
    name: str,
    doc: str,
    *,
    tiers: tuple[Tier, ...] = TIERS,
    requires: tuple[Requirement, ...] = (),
    legacy_env: tuple[str, ...] = (),
    binds: Binding = "session_mutable",
    default: Literal["on", "off"] = "on",
    authority: Authority = _SETTINGS,
) -> FeatureSpec:
    """A capability that is On unless a tier turns it Off (Off until one turns it On, for
    `default="off"`); off, the session is not offered its tools (`CAPABILITY_TOOLS`) and what
    it does in the background stops."""
    return FeatureSpec(
        key=key,
        name=name,
        category="capabilities",
        effect="capability",
        values=_ON_OFF,
        default=default,
        tiers=tiers,
        binds=binds,
        doc=doc,
        platforms=EVERY_PLATFORM,
        requires=requires,
        authority=authority,
        legacy_env=legacy_env,
    )


def _limit(
    key: str,
    name: str,
    doc: str,
    steps: Mapping[str, str],
    *,
    tiers: tuple[Tier, ...],
    default: str | None = None,
    legacy_env: tuple[str, ...] = (),
    authority: Authority = _SETTINGS,
    minimum: float = 0,
    zero_unlimited: bool = False,
    depends_on: tuple[str, ...] = (),
) -> FeatureSpec:
    """A limit: the smallest tier setting wins. `steps` run from the loosest down to the
    strictest; the default is the first unless `default` names another step."""
    return FeatureSpec(
        key=key,
        name=name,
        category="limits",
        effect="limit",
        values=steps,
        default=default or next(iter(steps)),
        tiers=tiers,
        binds="turn",
        doc=doc,
        platforms=EVERY_PLATFORM,
        authority=authority,
        legacy_env=legacy_env,
        minimum=minimum,
        zero_unlimited=zero_unlimited,
        depends_on=depends_on,
    )


_CAPABILITIES: Final[tuple[FeatureSpec, ...]] = (
    _capability(
        "watches",
        "Watches",
        (
            "The agent can watch a page or feed and start a conversation when it changes. Off "
            "removes the watch tools here and stops the watches made here. Watches only run once "
            "whoever runs Daimon allows them."
        ),
        legacy_env=("DAIMON_WATCHES__ENABLED",),
    ),
    _capability(
        "routines",
        "Routines",
        "The agent can set up and run routines: tasks that run on a schedule. Off removes "
        "the routine tools here.",
    ),
    _capability(
        "long_jobs",
        "Long jobs",
        (
            "The agent can start a long job that keeps working after the chat moves on. Off "
            "until a server admin turns it on; off removes the long-job tools here."
        ),
        default="off",
        authority=_SERVER_ADMIN,
    ),
    FeatureSpec(
        key="secret_sharing",
        name="Secret sharing",
        category="capabilities",
        effect="capability",
        values=_ON_OFF,
        default="off",
        tiers=("deployment", "workspace", "agent"),
        binds="session_mutable",
        doc=(
            "/share-secret sends a secret to one person's DMs; the agent sends its keys to an "
            "admin, its owner or its setter. They reveal it once, within a day. A server admin "
            "turns it on."
        ),
        platforms=_DISCORD,
        authority=_SERVER_ADMIN,
    ),
    FeatureSpec(
        key="bot_access_wait",
        name="Wait for bot access",
        category="capabilities",
        effect="capability",
        values=_ON_OFF,
        default="off",
        tiers=("workspace",),
        binds="turn",
        doc=(
            "Work for a channel Daimon cannot reach waits, without failing, and runs once it can."
        ),
        platforms=_DISCORD,
        authority=_SERVER_ADMIN,
    ),
    _capability(
        "compute",
        "Cloud computers",
        (
            "A short-lived cloud computer to run code. Only a server admin turns it "
            "on; Off removes its tools."
        ),
        requires=("compute_setup",),
        default="off",
        authority=_SERVER_ADMIN,
    ),
    _capability(
        "notebooks",
        "Notebooks",
        "The agent can publish and manage notebooks. Off removes the notebook tools here.",
        requires=("notebook_host",),
    ),
    _capability(
        "report_publish",
        "Report publishing",
        "The agent can publish a report as a web page and delete it later. Off removes "
        "the report tools here.",
        requires=("report_host",),
    ),
    _capability(
        "media",
        "Video transcripts",
        "The agent can fetch the transcript of a video. Off removes that tool here.",
        requires=("gemini_key",),
    ),
    _capability(
        "microsoft_365",
        "Microsoft 365",
        "People can connect a Microsoft account and the agent can search, read, write and "
        "send with it. Off removes the Microsoft tools here.",
        requires=("microsoft_accounts",),
    ),
    FeatureSpec(
        key="thread_providers",
        name="Other providers per conversation",
        category="chat",
        effect="capability",
        values=_ON_OFF,
        default="off",
        tiers=("deployment", "workspace", "agent"),
        binds="turn",
        doc=(
            "Anyone who can change a conversation's model may pick an OpenAI or Gemini model "
            "for it. Off, only the agent's owner or an admin can."
        ),
        platforms=EVERY_PLATFORM,
        authority=_SETTINGS,
    ),
    FeatureSpec(
        key="support_escalation",
        name="Ask a person",
        category="capabilities",
        effect="capability",
        values=_ON_OFF,
        default="on",
        tiers=("deployment", "workspace", "agent", "channel"),
        binds="turn",
        doc=(
            "Answers offer a way to ask a person for help, posted in the "
            "support channel. Off removes the button and the form. The operator sets the "
            "default channel and credits; an admin sets limits, a menu and their own channel."
        ),
        requires=("support_channel", "support_credits"),
        authority=_SETTINGS,
    ),
    FeatureSpec(
        key="support_feedback",
        name="Feedback to support",
        category="capabilities",
        effect="capability",
        values=_ON_OFF,
        default="off",
        tiers=("deployment", "workspace", "agent", "channel"),
        binds="turn",
        doc=(
            "A submitted thumbs-down form (reasons, text, person, agent and a link to the answer) "
            "also goes where Ask a person requests go, and the form says so. It spends no credits."
            " Off keeps the form in the database only."
        ),
        requires=("support_channel",),
        authority=_SETTINGS,
        legacy_env=("DAIMON_SUPPORT__FEEDBACK_TO_SUPPORT",),
        env_map="support.feedback_to_support",
    ),
    FeatureSpec(
        key="support_own_channel",
        name="Own support channel",
        category="capabilities",
        effect="capability",
        values=_ON_OFF,
        default="on",
        tiers=("deployment",),
        binds="turn",
        doc=(
            "An admin may send Ask a person requests to a channel of their own instead of the "
            "default one; whoever runs Daimon then no longer sees them. Off sends every request "
            "to the default channel."
        ),
        platforms=EVERY_PLATFORM,
        authority=_ADMIN,
    ),
    _capability(
        "github",
        "GitHub access",
        "The agent can ask for access to GitHub repositories, connect an account and pick "
        "its working repository. Off removes those tools here.",
    ),
    _capability(
        "lead_intake",
        "Lead intake",
        "The agent can take in sales leads and look them up. Off removes the lead tools here.",
    ),
    FeatureSpec(
        key="data_reads",
        name="Data source reads",
        category="model_input",
        effect="capability",
        values=_ON_OFF,
        default="off",
        tiers=("workspace", "agent", "channel"),
        binds="session_mutable",
        doc=(
            "The agent can search the team's data sources, showing only what everyone "
            "who will see the answer may read. Other agents cannot ask it, writes out ask "
            "first, and its memories show only to people who could read that data."
        ),
        platforms=EVERY_PLATFORM,
        # Writes after a data read must wait for a card: tool safety is what cards them.
        depends_on=("data_sources", "tool_safety"),
        authority=_ADMIN_NOT_OWNER,
    ),
    _limit(
        "long_jobs_max_usd",
        "Long job dollar cap",
        "The most one long job may spend. The smallest setting wins.",
        {"20": "$20", "10": "$10", "5": "$5", "2": "$2", "1": "$1"},
        default="5",
        tiers=("deployment", "workspace", "agent"),
        legacy_env=("DAIMON_LONG_JOBS__MAX_USD",),
        authority=_SERVER_ADMIN,
        minimum=0.01,
    ),
    _limit(
        "long_jobs_max_deadline",
        "Long job time limit",
        "The longest one long job may run. The smallest setting wins.",
        {"70": "70 minutes", "60": "60 minutes", "30": "30 minutes", "15": "15 minutes"},
        tiers=("deployment", "workspace", "agent"),
        legacy_env=("DAIMON_LONG_JOBS__MAX_DEADLINE_MINUTES",),
        authority=_SERVER_ADMIN,
        minimum=1,
    ),
    _limit(
        "callback_watch_lifetime",
        "Callback watch lifetime",
        "How long a callback watch lasts, new or moved in, before it must be renewed. "
        "The smallest setting wins.",
        {"0": "Permanent", "90": "90 days", "30": "30 days", "7": "7 days"},
        default="30",
        tiers=("deployment", "workspace"),
        authority=_SERVER_ADMIN,
        minimum=1,
        zero_unlimited=True,
    ),
    _limit(
        "turn_time_limit",
        "Answer time limit",
        (
            "The longest an answer may take before it is stopped when someone is talking to "
            "the agent: the standard 45 minutes, or shorter. The smallest setting wins. "
            "Routines and watches use the unattended run limit."
        ),
        _TIME_LIMITS,
        default="45",
        tiers=("workspace", "agent"),
        authority=_SERVER_ADMIN,
        minimum=1,
    ),
    _limit(
        "unattended_time_limit",
        "Unattended run time limit",
        (
            "The longest a run nobody watches may take: a routine, a watch wake-up, an "
            "agent-to-agent chat or a command-line run. The standard 45 minutes, or shorter; the "
            "smallest setting wins. Chats use the answer time limit."
        ),
        _TIME_LIMITS,
        default="45",
        tiers=("workspace", "agent"),
        authority=_SERVER_ADMIN,
        minimum=1,
    ),
    _capability(
        "outbound_reach",
        "Posting to others",
        "The agent can post where other people are: other channels, new threads and "
        "direct messages. Off keeps it to the conversation it is in.",
        binds="turn",
    ),
    _limit(
        "outbound_threads",
        "New threads per reply",
        "How many new threads or forum posts outside its own channel, or tagging someone "
        "else, one reply may open without approval. The smallest setting wins.",
        {"2": "2 threads", "1": "1 thread", "0": "None"},
        tiers=TIERS,
        depends_on=("reach_budget",),
        legacy_env=("DAIMON_OUTBOUND__MAX_THREADS",),
    ),
    _limit(
        "outbound_places",
        "Places per reply",
        "How many channels, threads and direct-message recipients outside its own "
        "conversation one reply may post into without approval. The smallest setting wins.",
        {"2": "2 places", "1": "1 place", "0": "None"},
        tiers=TIERS,
        depends_on=("reach_budget",),
        legacy_env=("DAIMON_OUTBOUND__MAX_PLACES",),
    ),
    _limit(
        "outbound_people",
        "People tagged per reply",
        "How many different people one reply may tag without approval. The smallest setting wins.",
        {"5": "5 people", "3": "3 people", "1": "1 person", "0": "None"},
        tiers=TIERS,
        depends_on=("reach_budget",),
        legacy_env=("DAIMON_OUTBOUND__MAX_PEOPLE",),
    ),
    _limit(
        "outbound_length",
        "Post length elsewhere",
        "The longest post, in characters, a reply may send outside its own conversation. "
        "The smallest setting wins.",
        {
            "1500": "1500 characters",
            "1000": "1000 characters",
            "500": "500 characters",
            "250": "250 characters",
        },
        tiers=TIERS,
        zero_unlimited=True,
        legacy_env=("DAIMON_OUTBOUND__MAX_LENGTH",),
    ),
    _limit(
        "daily_model_cap",
        "Daily spend per model",
        (
            "The most the team may spend on one OpenAI or Gemini model in a day, at list "
            "price. The smallest setting wins, and the provider cap set by whoever runs "
            "Daimon still applies."
        ),
        {"20": "$20", "10": "$10", "5": "$5", "2": "$2", "1": "$1"},
        tiers=("deployment", "workspace", "agent"),
    ),
)


FEATURES: Final[Mapping[str, FeatureSpec]] = _features(
    FeatureSpec(
        key="approvals",
        name="Approvals",
        category="guardrails",
        effect="guardrail",
        values={"on": "Ask", "off": "Allow writes"},
        default="on",
        tiers=("workspace", "agent", "channel", "thread"),
        binds="turn",
        doc=(
            "Ask before an agent writes to an outside service in a chat reply. Allow writes "
            "covers chat replies only, not routines or watches. A coordinator's value decides "
            "its callees' writes. Only a chat admin or the operator can change it."
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
        tiers=("deployment", "workspace", "channel", "thread"),
        binds="turn",
        doc=(
            "The agent joins a thread without being mentioned when it can help. A channel or "
            "thread can turn it on where it is off above, unless that is locked."
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
        default="on",
        tiers=("deployment", "workspace", "agent", "channel", "thread"),
        binds="turn",
        doc=(
            "Each message starts with the notes in the agent's repository memory, and the agent "
            "gets the memory tools. Off hides both here. Whoever runs Daimon must set it up "
            "first."
        ),
        platforms=EVERY_PLATFORM,
        requires=("repo_memory_setup",),
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
    FeatureSpec(
        key="data_sources",
        name="Data sources",
        category="model_input",
        effect="capability",
        values=_ON_OFF,
        default="on",
        tiers=("deployment", "workspace"),
        binds="session_mutable",
        doc=(
            "The data store, with its sources and search, idle until an admin adds a source. "
            "The operator or a team can turn it off."
        ),
        platforms=EVERY_PLATFORM,
        authority=_ADMIN,
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
            "A context provider set up for this channel or agent adds what it knows to each "
            "message. Off skips it here. Claude agents only."
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
        tiers=TIERS,
        binds="turn",
        doc=(
            "How many earlier chat messages a new conversation reads, up to 100 from the "
            "thread and from the channel around it. The smallest setting wins."
        ),
        platforms=frozenset({"discord", "slack", "teams"}),
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
    FeatureSpec(
        key="dm_history_depth",
        name="Direct message memory",
        category="model_input",
        effect="limit",
        values={
            "12": "12 messages",
            "25": "25 messages",
            "50": "50 messages",
            "100": "100 messages",
        },
        default="12",
        # Teams personal chats replayed up to 100 messages before this setting existed.
        platform_defaults={"teams": "100"},
        tiers=("deployment", "workspace", "agent"),
        binds="turn",
        doc=("Earlier direct messages each reply carries; more costs more. The smallest wins."),
        platforms=_DM_MEMORY_PLATFORMS,
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
    FeatureSpec(
        key="channel_history",
        name="Channel background",
        category="model_input",
        effect="display",
        values={
            "100": "100 messages",
            "50": "50 messages",
            "25": "25 messages",
            "10": "10 messages",
            "0": "None",
        },
        default="25",
        tiers=TIERS,
        binds="turn",
        doc=(
            "How many recent channel messages a new thread reads for background when a message "
            "in the channel starts it. Chat history still caps it."
        ),
        platforms=frozenset({"discord", "slack", "teams"}),
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
    FeatureSpec(
        key="answer_messages",
        name="Answer messages",
        category="chat",
        effect="display",
        values={
            "1000": "1000 messages",
            "100": "100 messages",
            "25": "25 messages",
            "10": "10 messages",
            "5": "5 messages",
            "3": "3 messages",
            "1": "1 message",
        },
        default="5",
        tiers=TIERS,
        binds="turn",
        doc=(
            "The most messages one answer may post in a thread. A longer answer is attached "
            "as a single file instead."
        ),
        platforms=frozenset({"discord"}),
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
    *_MODEL_INPUT,
    *_SESSION_IDENTITY,
    *_CHAT_BEHAVIOUR,
    *_WORKSPACE,
    *_CAPABILITIES,
    *_GUARDRAILS,
)
FEATURE_KEYS: Final[tuple[str, ...]] = tuple(FEATURES)


def feature(key: str) -> FeatureSpec | None:
    return FEATURES.get(key)


def resolution_order(keys: Collection[str] | None = None) -> list[FeatureSpec]:
    """`keys` (every feature by default) plus what they depend on, each after its
    dependencies and otherwise in registry order, so a reader that resolves one by one
    always has a feature's `depends_on` answered before it."""
    ordered: dict[str, FeatureSpec] = {}

    def visit(key: str, path: tuple[str, ...]) -> None:
        if key in ordered:
            return
        if key in path:
            raise ValueError(f"feature dependency cycle: {' -> '.join((*path, key))}")
        for dep in FEATURES[key].depends_on:
            visit(dep, (*path, key))
        ordered[key] = FEATURES[key]

    for key in FEATURES:
        if keys is None or key in keys:
            visit(key, ())
    return list(ordered.values())


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
    if not spec.accepts(value, "deployment") or flag not in ("", "locked"):
        takes = ", ".join(spec.values) + (
            f" or a number of at least {spec.minimum:g}" if spec.effect == "limit" else ""
        )
        raise ValueError(f"feature {key!r} takes {takes}, optionally ':locked'")
    return value, flag == "locked"
