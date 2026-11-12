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

Tier = Literal["deployment", "workspace", "agent", "channel", "thread"]
TIERS: Final[tuple[Tier, ...]] = get_args(Tier)
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
    "compute_setup",
    "notebook_host",
    "report_host",
    "gemini_key",
    "microsoft_accounts",
    "repo_memory_setup",
    "teams_public_url",
    "support_channel",
]
"""``platform_adapter``: the chat platform's adapter is set up in this deployment.
``support_channel``: the platform has an escalation channel and people have credits to ask.
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
    stands for the workspace tier; a stored workspace row always beats it."""
    minimum: float = 0
    """The smallest number a limit's deployment tier takes. 0 where no limit is a real setting;
    above 0 where 0 would be an unusable limit, not an off switch."""
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
            "agent's own tool settings allow. Applies from the next message in a conversation."
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
            "rules. Applies the next time the agent is saved."
        ),
        platforms=EVERY_PLATFORM,
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
    *(_builtin(tool) for tool in BUILTIN_TOOLS),
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
            "out; the keys stay saved. Applies from the next message in a conversation. "
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
            "is. A conversation keeps what it started with until /new. Claude agents only."
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
        tiers=TIERS,
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
        tiers=TIERS,
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
        tiers=TIERS,
        binds="session_identity",
        doc=(
            "The memory a restricted channel keeps for itself in place of the shared one. Off "
            f"leaves such a channel with no memory at all. {_KEPT} Claude agents only."
        ),
        backend_tiers=_MANAGED_AGENTS_ONLY,
        platforms=EVERY_PLATFORM,
        authority=Authority(DelegatedAction.AGENT_SETTINGS),
    ),
)

_DM_PLATFORMS: Final = frozenset({"discord", "slack"})
_DISCORD: Final = frozenset({"discord"})
_SETTINGS: Final = Authority(DelegatedAction.AGENT_SETTINGS)
# Long jobs spend money in the background: server admins only, no delegation, no owners.
_SERVER_ADMIN: Final = Authority(None, agent_owner=False)
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
            "only the ones that mention the agent. Needs the public address the bot is reached "
            "at. Whoever runs Daimon sets it, and it applies after a restart."
        ),
        platforms=frozenset({"teams"}),
        requires=("teams_public_url",),
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
            "there is the team's DM agent, which is chosen apart from this."
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
        doc="A reply in a direct message can read the agent's memory but not change it.",
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
            "Allowed lets an admin turn on built-in tools for one routine. Off keeps them off in "
            "every routine, even one an admin turned them on for."
        ),
        platforms=EVERY_PLATFORM,
        authority=_ADMIN,
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
            "The agent can watch a page or feed and start a conversation when it changes. Off"
            " removes the watch tools here and stops the watches made here. Watches only run "
            "once whoever runs Daimon allows them, whatever this says."
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
    _capability(
        "compute",
        "Cloud computers",
        "The agent can start a short-lived cloud computer to run code and keep files. Off "
        "removes the computer tools here.",
        requires=("compute_setup",),
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
        key="support_escalation",
        name="Ask a person",
        category="capabilities",
        effect="capability",
        values=_ON_OFF,
        default="on",
        tiers=("deployment", "workspace"),
        binds="turn",
        doc=(
            "Answers offer a way to ask the Daimon team for help, which posts the request in the "
            "support channel. Off removes the button and the form. Whoever runs Daimon sets the "
            "channel and each person's credits."
        ),
        requires=("support_channel",),
        authority=_SETTINGS,
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
        "conversation_retention",
        "Conversation history kept",
        "How long Daimon keeps its copy of a conversation after its last message. An idle "
        "thread past this starts a fresh session on its next message. The smallest setting "
        "wins; 0 at the deployment keeps conversations until they are deleted.",
        {"365": "1 year", "180": "180 days", "90": "90 days", "30": "30 days"},
        tiers=("deployment", "workspace"),
        authority=_SERVER_ADMIN,
        zero_unlimited=True,
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
        tiers=("workspace", "channel", "thread"),
        binds="turn",
        doc=(
            "Ask before an agent writes to an outside service in a chat reply. Allow writes "
            "covers chat replies only: routines and watches follow the unattended rule whoever "
            "runs Daimon set. Only an admin of the chat app can change it."
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
            "thread and 25 from the channel around it. The smallest setting wins."
        ),
        platforms=frozenset({"discord", "slack", "teams"}),
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
