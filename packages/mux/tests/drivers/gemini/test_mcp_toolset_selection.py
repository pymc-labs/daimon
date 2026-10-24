"""Neutral MCP selectors compile only their actually declared Gemini servers."""

import pytest
from mux.contracts.ids import ModelRef
from mux.contracts.resources import AgentSpec, MCPConnection, ToolSpec
from mux.drivers.gemini.core import compile_agent
from mux.errors import UnsupportedCapability

SERVER = MCPConnection(name="daimon-mcp", url="https://qa-mcp.invalid/mcp")


def test_declared_mcp_toolset_compiles_server_once() -> None:
    spec = AgentSpec(
        name="daimon",
        model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
        tools=(
            ToolSpec(name="code_execution", kind="builtin"),
            ToolSpec(name="daimon-mcp", kind="mcp_toolset"),
        ),
        mcp_servers=(SERVER,),
    )
    assert compile_agent(spec)["tools"] == [
        {"type": "code_execution"},
        {"type": "mcp_server", "name": "daimon_mcp", "url": SERVER.url},
    ]


@pytest.mark.parametrize("servers", [(), (SERVER.model_copy(update={"name": "other"}),)])
def test_unbound_mcp_selector_refuses(servers: tuple[MCPConnection, ...]) -> None:
    with pytest.raises(UnsupportedCapability):
        compile_agent(
            AgentSpec(
                name="daimon",
                model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
                tools=(ToolSpec(name="daimon-mcp", kind="mcp_toolset"),),
                mcp_servers=servers,
            )
        )


def test_mcp_selector_retains_confirmation_policy_refusal() -> None:
    with pytest.raises(UnsupportedCapability):
        compile_agent(
            AgentSpec(
                name="daimon",
                model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
                tools=(ToolSpec(name="daimon-mcp", kind="mcp_toolset", permission="ask"),),
                mcp_servers=(SERVER,),
            )
        )
