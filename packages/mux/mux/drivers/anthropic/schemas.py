"""Closed schemas for Anthropic-only agent configuration, version 1.

Optional properties serialize with exclude_unset=True: omission, explicit
null, and explicit empty lists preserve the provider's distinct meanings.
"""

from collections.abc import Mapping
from typing import Annotated, Literal, Self, get_args

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from mux.contracts.extensions import ExtensionConfig
from mux.contracts.resources import NetworkPolicy, ToolSpec

AGENT_TOOLS_NAMESPACE = "anthropic.agent_tools"


class NativeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AllowPolicy(NativeConfig):
    type: Literal["always_allow"]


class AskPolicy(NativeConfig):
    type: Literal["always_ask"]


type PermissionPolicy = Annotated[AllowPolicy | AskPolicy, Field(discriminator="type")]


class ToolDefaults(NativeConfig):
    enabled: bool | None = None
    permission_policy: PermissionPolicy | None = None


class ToolConfig(ToolDefaults):
    name: str


type _BuiltinToolName = Literal[
    "bash", "edit", "read", "write", "glob", "grep", "web_fetch", "web_search"
]


_URL_TOOLS: frozenset[str] = frozenset({"web_fetch", "web_search"})


class BuiltinToolConfig(ToolDefaults):
    name: _BuiltinToolName
    type: _BuiltinToolName | None = None
    # A URL tool's sources, as Managed Agents echoes them on readback (SMOKE d75 J4).
    # Kept exactly as given and sent back as given; never set by Daimon.
    url_sources: JsonValue | None = None

    @model_validator(mode="after")
    def matching_type(self) -> Self:
        if "type" in self.model_fields_set and self.type != self.name:
            raise ValueError("builtin tool config type must match its name")
        if "url_sources" in self.model_fields_set and self.name not in _URL_TOOLS:
            raise ValueError("only a URL tool's config carries url_sources")
        return self


class AgentToolset(NativeConfig):
    type: Literal["agent_toolset_20260401"]
    configs: list[BuiltinToolConfig] | None = None
    default_config: ToolDefaults | None = None


class MCPToolset(NativeConfig):
    type: Literal["mcp_toolset"]
    mcp_server_name: str
    configs: list[ToolConfig] | None = None
    default_config: ToolDefaults | None = None


class CustomTool(NativeConfig):
    type: Literal["custom"]
    name: str
    description: str
    input_schema: dict[str, JsonValue]


type AgentTool = Annotated[AgentToolset | MCPToolset | CustomTool, Field(discriminator="type")]


def mcp_permissions(
    tool: MCPToolset, catalogue: frozenset[str]
) -> dict[str, Literal["auto", "ask", "disabled"]]:
    """Resolve every verified MCP tool without discarding sibling capabilities.

    An unresolved or ambiguous policy is never permission to execute a tool.
    Existing neutral toolset projection remains exact and fail-closed.
    """
    configs = tool.configs or []
    if not catalogue or len({item.name for item in configs}) != len(configs):
        raise ValueError("ambiguous MCP tool configuration")
    if any(item.name not in catalogue for item in configs):
        raise ValueError("MCP configuration outside verified catalogue")
    result: dict[str, Literal["auto", "ask", "disabled"]] = {}
    for name in sorted(catalogue):
        config = next((item for item in configs if item.name == name), None)
        defaults = tool.default_config
        enabled = (
            config.enabled
            if config and config.enabled is not None
            else (defaults.enabled if defaults else None)
        )
        policy = (
            config.permission_policy
            if config and config.permission_policy is not None
            else (defaults.permission_policy if defaults else None)
        )
        if enabled is False:
            result[name] = "disabled"
        elif enabled is not True or policy is None:
            raise ValueError("unresolved MCP tool permission")
        else:
            result[name] = "ask" if isinstance(policy, AskPolicy) else "auto"
    return result


def _toolset_intent(
    tool: AgentTool, mcp_catalogues: Mapping[str, frozenset[str]] | None = None
) -> tuple[ToolSpec, ...] | None:
    """Project one closed tool shape to its neutral form, per toolset.

    Returns `None` for a well-formed toolset with no neutral projection (an
    `MCPToolset` without an explicitly verified catalogue, or an `AgentToolset`
    without complete coverage and resolved
    effective enablement and permission controls).
    `None` is never a schema failure: the caller only sees it for toolsets
    that already passed `AgentToolsConfig` validation. Treat it as a known,
    droppable loss — exclude it from a worker ceiling (stricter, never
    looser) or flag it as a declarable "tools" obligation, but never raise.
    """
    if isinstance(tool, CustomTool):
        return (
            ToolSpec(
                name=tool.name,
                kind="custom",
                description=tool.description,
                input_schema=tool.input_schema,
            ),
        )
    if isinstance(tool, MCPToolset):
        catalogue = (mcp_catalogues or {}).get(tool.mcp_server_name)
        if not catalogue:
            return None
        configs = tool.configs or []
        if len({item.name for item in configs}) != len(configs):
            return None
        if any(item.name not in catalogue for item in configs):
            return None
        defaults = tool.default_config
        for name in catalogue:
            config = next((item for item in configs if item.name == name), None)
            enabled = (
                config.enabled
                if config and config.enabled is not None
                else (defaults.enabled if defaults else None)
            )
            policy = (
                config.permission_policy
                if config and config.permission_policy
                else (defaults.permission_policy if defaults else None)
            )
            if enabled is not True or not isinstance(policy, AllowPolicy):
                return None
        return (ToolSpec(name=tool.mcp_server_name, kind="mcp_toolset", permission="auto"),)
    default_enabled = tool.default_config.enabled if tool.default_config else None
    default_policy = tool.default_config.permission_policy if tool.default_config else None
    configs = tool.configs or []
    missing = set(get_args(_BuiltinToolName.__value__)) - {c.name for c in configs}
    if missing and default_enabled is not False:
        if default_enabled is None or default_policy is None:
            return None
        # A resolved readback default governs every builtin absent from configs.
        configs = [*configs, *(BuiltinToolConfig(name=name) for name in sorted(missing))]
    result: dict[str, ToolSpec] = {}
    disabled: set[str] = set()
    for config in configs:
        enabled = config.enabled if config.enabled is not None else default_enabled
        if enabled is False:
            disabled.add(config.name)
            continue
        policy = config.permission_policy or default_policy
        if enabled is None or policy is None:
            return None
        permission = "ask" if isinstance(policy, AskPolicy) else "auto"
        previous = result.get(config.name)
        if previous is not None and previous.permission == "ask":
            permission = "ask"
        result[config.name] = ToolSpec(name=config.name, kind="builtin", permission=permission)
    return tuple(tool for name, tool in result.items() if name not in disabled)


class AgentToolsConfig(NativeConfig):
    """Payload for anthropic.agent_tools@1."""

    tools: list[AgentTool]

    def intent(
        self,
        *,
        exact: bool = False,
        mcp_catalogues: Mapping[str, frozenset[str]] | None = None,
    ) -> tuple[ToolSpec, ...]:
        """Project every toolset with a complete neutral representation.

        A well-formed toolset with no neutral form (see `_toolset_intent`)
        is silently excluded, never raised on — call `has_unrepresented_toolset`
        to detect the drop for obligation/declared-drop bookkeeping.
        """
        result: list[ToolSpec] = []
        for tool in self.tools:
            if exact and not isinstance(tool, CustomTool) and tool.configs is not None:
                names = [config.name for config in tool.configs]
                if len(names) != len(set(names)):
                    raise ValueError("duplicate native tool configs cannot prove execution")
            projected = _toolset_intent(tool, mcp_catalogues)
            if exact and projected is None:
                raise ValueError("native tools have no complete neutral representation")
            if projected is not None:
                result.extend(projected)
        return tuple(result)

    def excluded_mcp_servers(self) -> frozenset[str]:
        """Servers paired with native-only toolsets cannot enter a worker ceiling."""
        return frozenset(
            tool.mcp_server_name
            for tool in self.tools
            if isinstance(tool, MCPToolset) and _toolset_intent(tool) is None
        )

    def has_unrepresented_toolset(
        self, *, mcp_catalogues: Mapping[str, frozenset[str]] | None = None
    ) -> bool:
        """True iff some well-formed toolset here has no neutral projection."""
        return any(_toolset_intent(tool, mcp_catalogues) is None for tool in self.tools)


class RosterAgent(NativeConfig):
    type: Literal["agent"]
    id: str
    version: int | None = None


class RosterSelf(NativeConfig):
    type: Literal["self"]


class MultiagentConfig(NativeConfig):
    """Payload for anthropic.multiagent@1."""

    type: Literal["coordinator"]
    agents: list[str | Annotated[RosterAgent | RosterSelf, Field(discriminator="type")]]


def agent_tools(extension: ExtensionConfig) -> AgentToolsConfig:
    if extension.namespace != AGENT_TOOLS_NAMESPACE or extension.version != 1:
        raise ValueError("expected anthropic.agent_tools@1")
    return AgentToolsConfig.model_validate(dict(extension.value))


def multiagent(extension: ExtensionConfig) -> MultiagentConfig:
    if extension.namespace != "anthropic.multiagent" or extension.version != 1:
        raise ValueError("expected anthropic.multiagent@1")
    return MultiagentConfig.model_validate(dict(extension.value))


class PackagesConfig(NativeConfig):
    type: Literal["packages"] | None = None
    apt: list[str] | None = None
    cargo: list[str] | None = None
    gem: list[str] | None = None
    go: list[str] | None = None
    npm: list[str] | None = None
    pip: list[str] | None = None


class UnrestrictedNetwork(NativeConfig):
    type: Literal["unrestricted"]


class LimitedNetwork(NativeConfig):
    type: Literal["limited"]
    allowed_hosts: list[str] | None = None
    allow_mcp_servers: bool | None = None
    allow_package_managers: bool | None = None


class CloudConfig(NativeConfig):
    type: Literal["cloud"]
    networking: (
        Annotated[UnrestrictedNetwork | LimitedNetwork, Field(discriminator="type")] | None
    ) = None
    packages: PackagesConfig | None = None


class SelfHostedConfig(NativeConfig):
    type: Literal["self_hosted"]


class EnvironmentConfig(NativeConfig):
    """Payload for the EnvironmentSpec native_config field, version 1."""

    config: Annotated[CloudConfig | SelfHostedConfig, Field(discriminator="type")] | None = None
    create_nulls: list[Literal["description"]] = []

    def intent(self) -> tuple[NetworkPolicy, dict[str, tuple[str, ...]]]:
        """Project explicit cloud policy; implicit defaults retain native obligations."""
        config = self.config
        if not isinstance(config, CloudConfig) or self.create_nulls:
            raise ValueError("environment execution policy is not represented")
        network, packages = config.networking, config.packages
        if network is None or packages is None:
            raise ValueError("environment policy has implicit defaults")
        if isinstance(network, LimitedNetwork) and (
            network.allowed_hosts is None
            or network.allow_mcp_servers is not False
            or network.allow_package_managers is not False
        ):
            raise ValueError("network permissions are not represented")
        if any(getattr(packages, name) is None for name in ("apt", "pip", "npm")) or any(
            getattr(packages, name) != [] for name in ("cargo", "gem", "go")
        ):
            raise ValueError("package policy is not represented")
        return (
            NetworkPolicy(
                mode=network.type,
                allowed_hosts=tuple(network.allowed_hosts or ())
                if isinstance(network, LimitedNetwork)
                else (),
            ),
            {
                name: tuple(getattr(packages, native))
                for name, native in (("system", "apt"), ("python", "pip"), ("npm", "npm"))
            },
        )


EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]


class EffortConfig(NativeConfig):
    type: EffortLevel


class ModelConfig(NativeConfig):
    id: str
    speed: Literal["standard", "fast"] | None = None
    inference_geo: Literal["us", "global"] | None = None
    effort: EffortLevel | EffortConfig | None = None


class AgentModelConfig(NativeConfig):
    """Payload for anthropic.model_config@1; keep string versus object syntax."""

    model: str | ModelConfig


class AgentCreateNulls(NativeConfig):
    """Explicit nulls in a legacy native create, versus neutral spec omission."""

    fields: list[
        Literal["description", "system", "tools", "mcp_servers", "skills", "metadata", "multiagent"]
    ]


class ClearMultiagent(NativeConfig):
    clear: Literal[True]
