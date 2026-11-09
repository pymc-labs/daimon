"""Pure answers of the model-input features: guidance, the session toolset, turn controls."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal, cast

from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.agent_create_params import Tool
from daimon.core.capability_tools import CAPABILITY_KEYS, CAPABILITY_TOOLS
from daimon.core.features.registry import BUILTIN_TOOLS
from daimon.core.features.snapshot import FeatureSnapshot
from daimon.core.repo_memory_tools import REPO_MEMORY_TOOLS

TOOL_KEYS: Final[tuple[str, ...]] = tuple(f"tool.{tool}" for tool in BUILTIN_TOOLS)
SESSION_KEYS: Final[tuple[str, ...]] = (
    "daimon_tools",
    *TOOL_KEYS,
    "agent_env_file",
    *CAPABILITY_KEYS,
)
TOOLSET_KEYS: Final[tuple[str, ...]] = (*SESSION_KEYS, "repo_memory")
"""What `SessionToolset.of` reads: `repo_memory` also hides its tools, while it binds per turn."""
TURN_KEYS: Final[tuple[str, ...]] = ("daimon_tools", "turn_controls", "web_awareness")
PER_TURN_KEYS: Final[tuple[str, ...]] = (
    "mcp_briefing", "repo_memory", "context_provider", "origin_fragments", "checkpoint_prompt",
)  # fmt: skip
"""Turn-bound switches each turn checks with `Admission.feature_on`."""
MODEL_INPUT_KEYS: Final[tuple[str, ...]] = ("guidance", *SESSION_KEYS, *TURN_KEYS[1:])

HISTORY_KEYS: Final[tuple[str, ...]] = ("history_depth",)
CHAT_KEYS: Final[tuple[str, ...]] = (
    "completion_pings",
    "table_rendering",
    "message_feedback",
    "native_footer_cost",
    "support_escalation",
    "status_log",
    "outbound_people",
    "reach_budget",
    "daily_model_cap",
)
"""Chat display settings the adapters read from the turn's admission."""
CHANNEL_BACKFILL: Final[int] = 25
"""The most channel messages around a new thread any chat adapter reads."""

TurnControls = Literal["full", "origin", "minimal"]
TOOLSET_TYPE: Final[str] = "agent_toolset_20260401"


def guidance_applies(*, isolated: bool, features: FeatureSnapshot) -> bool:
    """The one predicate every guidance writer asks; an isolated agent never gets guidance."""
    return not isolated and features.on("guidance")


def turn_controls(features: FeatureSnapshot) -> TurnControls:
    """The turn-control level; Daimon tools need the origin ids, so they lift Minimal."""
    level = features.value("turn_controls")
    if level == "minimal" and features.on("daimon_tools"):
        return "origin"
    return "full" if level == "full" else "origin" if level == "origin" else "minimal"


@dataclass(frozen=True)
class HistoryDepth:
    """How many earlier messages a chat adapter reads: the thread's, and the channel's."""

    replay: int = 100

    @property
    def backfill(self) -> int:
        return min(CHANNEL_BACKFILL, self.replay)

    @classmethod
    def of(cls, features: FeatureSnapshot | None) -> HistoryDepth:
        """From a snapshot; none, or one without the key (a hand-built admission), reads 100."""
        found = features.features.get("history_depth") if features is not None else None
        return cls() if found is None else cls(int(found.value))


FULL_HISTORY: Final[HistoryDepth] = HistoryDepth()


@dataclass(frozen=True)
class SessionToolset:
    """What a session's features change in the agent's tools. The default changes nothing.

    `builtins` holds only the places' overrides: a builtin's agent tier is the agent's own
    tool config, so a broader tier decides only when the agent left that tool unset."""

    daimon_tools: bool = True
    builtins: Mapping[str, bool] = field(default_factory=dict[str, bool])
    repo_memory: bool = True
    env_file: bool = True
    """`agent_env_file`: the agent's keys are mounted as `.env`."""
    off_capabilities: frozenset[str] = frozenset()
    """Capabilities a tier turned Off or whose setup is missing: their tools are not offered."""

    @classmethod
    def of(
        cls,
        features: FeatureSnapshot,
        agent_builtins: Mapping[str, bool | None],
        *,
        isolated: bool = False,
    ) -> SessionToolset:
        """From a snapshot holding `TOOLSET_KEYS`; an isolated agent's tools only ever shrink."""
        builtins: dict[str, bool] = {}
        for tool in BUILTIN_TOOLS:
            found = features.features.get(f"tool.{tool}")
            if found is None or found.tier in ("default", "agent"):
                continue
            broad = found.tier in ("deployment", "workspace")
            if broad and not found.locked and agent_builtins.get(tool) is not None:
                continue
            if found.on and isolated:
                continue
            builtins[tool] = found.on
        found = features.features.get("daimon_tools")
        memory = features.features.get("repo_memory")
        env = features.features.get("agent_env_file")
        # Off at the deployment registers no memory tools, so there is nothing to hide.
        hides_memory = (
            memory is not None and not memory.on and memory.tier not in ("default", "deployment")
        )
        return cls(
            daimon_tools=not isolated and (found is None or found.on),
            builtins=builtins,
            repo_memory=not hides_memory,
            env_file=env is None or env.on,
            off_capabilities=frozenset(
                key
                for key in CAPABILITY_KEYS
                if (cap := features.features.get(key)) is not None
                and not cap.on
                and (cap.tier != "default" or cap.unavailable == "requires")
            ),
        )

    @property
    def changes(self) -> bool:
        return (
            not self.daimon_tools
            or bool(self.builtins)
            or not self.repo_memory
            or not self.env_file
            or bool(self.off_capabilities)
        )

    @property
    def hidden_daimon_tools(self) -> frozenset[str]:
        """Daimon tools this place turns off on the session's `daimon-mcp` toolset."""
        hidden: frozenset[str] = frozenset() if self.repo_memory else REPO_MEMORY_TOOLS
        return hidden.union(*(CAPABILITY_TOOLS[key] for key in self.off_capabilities))

    def apply_builtins(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """`tools` with the overrides applied; turning off a tool the agent lacks adds nothing."""
        typed = cast(list[Tool], tools)
        for tool, enabled in sorted(self.builtins.items()):
            had = builtin_enabled(typed, tool)
            if had is not enabled and (enabled or had is not None):
                typed = with_builtin(typed, tool, enabled)
        return [_with_policies(cast(dict[str, Any], entry)) for entry in typed]


def _with_policies(entry: dict[str, Any]) -> dict[str, Any]:
    """A toolset whose added configs carry its default permission policy, as MA reports them."""
    if not str(entry.get("type", "")).startswith("agent_toolset"):
        return entry
    default: dict[str, Any] = dict(entry.get("default_config") or {"enabled": False})
    policy = default.setdefault("permission_policy", {"type": "always_allow"})
    configs = [{"permission_policy": policy, **c} for c in entry.get("configs") or ()]
    return {**entry, "default_config": default, "configs": configs}


def agent_builtins(tools: list[Tool]) -> dict[str, bool | None]:
    """The agent tier of every `tool.*` key: the agent's own toolset config."""
    return {tool: builtin_enabled(tools, tool) for tool in BUILTIN_TOOLS}


def agent_toolset(
    features: FeatureSnapshot | None, agent: BetaManagedAgentsAgent
) -> SessionToolset:
    """The toolset a session of `agent` runs with at the place `features` was resolved."""
    # Imported here: `defaults` reaches the session snapshot, which imports this module.
    from daimon.core.defaults.metadata import MA_METADATA_KEY_ISOLATED

    if features is None:
        return DEFAULT_TOOLSET
    tools = cast(list[Tool], [tool.model_dump(mode="json") for tool in agent.tools])
    isolated = (agent.metadata or {}).get(MA_METADATA_KEY_ISOLATED) == "true"
    return SessionToolset.of(features, agent_builtins(tools), isolated=isolated)


DEFAULT_TOOLSET: Final = SessionToolset()


def builtin_enabled(tools: list[Tool], tool: str) -> bool | None:
    """Whether any of the agent's toolsets turns `tool` on; None when it has none."""
    found: bool | None = None
    for entry in tools:
        if not str(entry.get("type", "")).startswith("agent_toolset"):
            continue
        raw = cast(dict[str, Any], entry)
        default = cast(dict[str, Any], raw.get("default_config") or {}).get("enabled")
        on = default is not False
        for config in raw.get("configs") or ():
            if config.get("name") == tool:
                enabled = config.get("enabled")
                on = bool(enabled) if enabled is not None else default is not False
                break
        found = on or bool(found)
    return found


def with_builtin(tools: list[Tool], tool: str, enabled: bool) -> list[Tool]:
    """`tools` with `tool` turned on or off; any permission policy it had is kept.

    An agent with no toolset gains one holding only `tool`, never the defaults.
    """
    if builtin_enabled(tools, tool) is None:
        only: Tool = cast(
            Tool,
            {
                "type": TOOLSET_TYPE,
                "default_config": {"enabled": False},
                "configs": [{"name": tool, "enabled": enabled}],
            },
        )
        return [*tools, only]
    updated: list[Tool] = []
    for entry in tools:
        if not str(entry.get("type", "")).startswith("agent_toolset"):
            updated.append(entry)
            continue
        raw = dict(cast(dict[str, Any], entry))
        configs = [dict(config) for config in raw.get("configs") or ()]
        for config in configs:
            if config.get("name") == tool:
                config["enabled"] = enabled
                break
        else:
            configs.append({"name": tool, "enabled": enabled})
        raw["configs"] = configs
        updated.append(cast(Tool, raw))
    return updated
