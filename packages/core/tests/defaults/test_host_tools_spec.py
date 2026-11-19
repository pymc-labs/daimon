"""A Daimon record runs its provider host's template toolset, whatever it lists itself."""

from __future__ import annotations

from daimon.core.defaults.owned import host_tools_spec
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef
from mux.contracts.resources import AgentSpec, MCPConnection, ToolSpec
from mux.drivers.anthropic import AGENT_TOOLS_NAMESPACE

URL = "https://daimon.example/mcp"
DAIMON = MCPConnection(name="daimon-mcp", url=URL)
PLAIN = AgentSpec(name="default", model=ModelRef(provider="openai", id="gpt-6-luna"))


def test_a_record_never_widens_its_host_toolset() -> None:
    """Its own tools, servers, a re-pointed Daimon server and toolset extension are ignored."""
    widened = PLAIN.model_copy(
        update={
            "tools": (ToolSpec(name="web_fetch", kind="builtin"),),
            "mcp_servers": (
                MCPConnection(name="daimon-mcp", url="https://evil.example/mcp"),
                MCPConnection(name="github", url="https://github.example/mcp"),
            ),
            "extensions": (
                ExtensionConfig(namespace=AGENT_TOOLS_NAMESPACE, version=1, value={"tools": []}),
            ),
        }
    )
    listed = PLAIN.model_copy(update={"mcp_servers": (DAIMON,)})
    assert host_tools_spec(widened, URL) == host_tools_spec(listed, URL)
    assert host_tools_spec(listed, URL).mcp_servers == (DAIMON,)
    bare = host_tools_spec(PLAIN, URL)
    assert bare.mcp_servers == () and bare.tools is None, "no Daimon server listed: no MCP"
    (toolset,) = bare.extensions or ()
    assert [t["type"] for t in toolset.value["tools"]] == ["agent_toolset_20260401"]  # pyright: ignore[reportIndexIssue,reportArgumentType,reportOptionalSubscript,reportCallIssue]
