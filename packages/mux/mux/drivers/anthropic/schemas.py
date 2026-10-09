"""Closed schemas for Anthropic-only agent configuration, version 1.

Optional properties serialize with exclude_unset=True: omission, explicit
null, and explicit empty lists preserve the provider's distinct meanings.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from mux.contracts.extensions import ExtensionConfig


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


class BuiltinToolConfig(ToolDefaults):
    name: Literal["bash", "edit", "read", "write", "glob", "grep", "web_fetch", "web_search"]


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


class AgentToolsConfig(NativeConfig):
    """Payload for anthropic.agent_tools@1."""

    tools: list[AgentTool]


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
    if extension.namespace != "anthropic.agent_tools" or extension.version != 1:
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


class ModelConfig(NativeConfig):
    id: str
    speed: Literal["standard", "fast"] | None = None


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
