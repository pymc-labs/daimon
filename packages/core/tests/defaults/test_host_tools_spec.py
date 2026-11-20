"""A Daimon record runs its provider host's template toolset, narrowed by its own, never widened."""

from __future__ import annotations

from daimon.core.defaults.owned import host_tools_spec
from daimon.core.tool_safety import PUBLISH_TOOLS, ToolSafetyPolicy
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef
from mux.contracts.resources import AgentSpec, MCPConnection, ToolSpec
from mux.drivers.anthropic import AGENT_TOOLS_NAMESPACE, AgentToolsConfig, agent_tools

URL = "https://daimon.example/mcp"
DAIMON = MCPConnection(name="daimon-mcp", url=URL)
LINEAR = MCPConnection(name="linear", url="https://linear.example/mcp")
PLAIN = AgentSpec(name="default", model=ModelRef(provider="openai", id="gpt-6-luna"))
OFF = ToolSafetyPolicy()


def _toolsets(spec: AgentSpec) -> AgentToolsConfig:
    (extension,) = (e for e in spec.extensions or () if e.namespace == AGENT_TOOLS_NAMESPACE)
    return agent_tools(extension)


def _own(*tools: dict) -> ExtensionConfig:
    return ExtensionConfig(namespace=AGENT_TOOLS_NAMESPACE, version=1, value={"tools": list(tools)})


def test_a_record_never_widens_its_host_toolset() -> None:
    """Its neutral tools and a re-pointed Daimon server are ignored; connectors stay, to be
    named or relayed by the switch as for a Managed Agents agent."""
    widened = PLAIN.model_copy(
        update={
            "tools": (ToolSpec(name="web_fetch", kind="builtin"),),
            "mcp_servers": (MCPConnection(name="daimon-mcp", url="https://evil.example/mcp"),),
            "extensions": (_own(),),
        }
    )
    listed = PLAIN.model_copy(update={"mcp_servers": (DAIMON,)})
    assert host_tools_spec(widened, URL, tool_safety=OFF) == host_tools_spec(
        listed, URL, tool_safety=OFF
    ), "an empty toolset is the template's"
    with_linear = PLAIN.model_copy(update={"mcp_servers": (DAIMON, LINEAR)})
    assert host_tools_spec(with_linear, URL, tool_safety=OFF).mcp_servers == (DAIMON, LINEAR), (
        "a connector is never dropped silently"
    )
    bare = host_tools_spec(PLAIN, URL, tool_safety=OFF)
    assert bare.mcp_servers == () and bare.tools is None, "no Daimon server listed: no MCP"
    assert [t.type for t in _toolsets(bare).tools] == ["agent_toolset_20260401"]


def test_a_narrowed_record_stays_narrowed() -> None:
    """Its own toolsets win over the template: off stays off, ask stays ask, and a toolset
    of a server it doesn't list goes. Forbidden built-ins and publish asks still apply."""
    allow = {"type": "always_allow"}
    own = _own(
        {
            "type": "agent_toolset_20260401",
            "default_config": {"enabled": True, "permission_policy": allow},
            "configs": [{"name": "web_search", "enabled": False, "permission_policy": allow}],
        },
        {
            "type": "mcp_toolset",
            "mcp_server_name": "daimon-mcp",
            "default_config": {"enabled": True, "permission_policy": allow},
            "configs": [
                {"name": "memory_write", "enabled": True, "permission_policy": {"type": "always_ask"}}
            ],
        },
        {"type": "mcp_toolset", "mcp_server_name": "unlisted", "default_config": {"enabled": True}},
    )
    record = PLAIN.model_copy(update={"mcp_servers": (DAIMON,), "extensions": (own,)})
    policy = ToolSafetyPolicy(denied=["builtin/bash"])
    hosted = host_tools_spec(record, URL, tool_safety=policy, asks_before_publishing=True)
    toolsets = _toolsets(hosted).tools
    builtins = [t for t in toolsets if t.type == "agent_toolset_20260401"]
    intent = {(t.name, t.permission) for t in AgentToolsConfig(tools=builtins).intent(exact=True)}
    names = {name for name, _ in intent}
    assert "web_search" not in names, "turned off on the record: off on its host"
    assert "bash" not in names, "forbidden by whoever runs Daimon"
    assert ("read", "auto") in intent, "the rest of its own toolset runs"
    (daimon,) = [t for t in toolsets if t.type == "mcp_toolset"]
    asks = {c.name for c in daimon.configs or () if c.permission_policy.type == "always_ask"}
    assert "memory_write" in asks, "its own ask stays"
    assert asks >= PUBLISH_TOOLS, "tool safety: publishing asks first here"
