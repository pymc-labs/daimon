"""An agent request never sends Gemini an empty tool list.

For the antigravity agent an empty (or omitted) `tools` means the defaults:
code_execution, google_search and url_context (measured 2026-10-23, billable
search among them). A tool-free agent therefore allows only the inert
`daimon_no_tools` sentinel; capped model requests and agents with tools of their
own are unchanged.
"""

from __future__ import annotations

import pytest
from mux.contracts.ids import ModelRef, Scope
from mux.contracts.resources import AgentSpec, MCPConnection, ToolSpec
from mux.drivers.gemini.catalog import native_definition
from mux.drivers.gemini.core import compile_agent
from mux.drivers.gemini.mcp_auth import validate_intent
from mux.drivers.gemini.storage import Records
from mux.drivers.gemini.transport import NO_TOOLS_FUNCTION, NO_TOOLS_TOOL
from mux.errors import UnsupportedCapability

FLASH = ModelRef(provider="gemini", id="gemini-3.8-flash")
SERVER = MCPConnection(name="daimon-mcp", url="https://qa-mcp.invalid/mcp")
SCOPE = Scope(tenant_id="tenant", account_id="account", principal_id="p", authorization_id="a")


def test_a_tool_free_agent_allows_only_the_inert_sentinel() -> None:
    (tool,) = compile_agent(AgentSpec(name="bare", model=FLASH))["tools"]
    assert tool == NO_TOOLS_TOOL and tool["name"] == NO_TOOLS_FUNCTION
    assert tool["type"] == "function" and "Never call this" in tool["description"]


def test_an_agent_with_its_own_tools_is_unchanged() -> None:
    mcp = compile_agent(
        AgentSpec(
            name="daimon",
            model=FLASH,
            tools=(ToolSpec(name="daimon-mcp", kind="mcp_toolset"),),
            mcp_servers=(SERVER,),
        )
    )["tools"]
    assert NO_TOOLS_TOOL not in mcp and [t["type"] for t in mcp] == ["mcp_server"]
    builtin = compile_agent(
        AgentSpec(
            name="search", model=FLASH, tools=(ToolSpec(name="google_search", kind="builtin"),)
        )
    )["tools"]
    assert builtin == [{"type": "google_search"}]


def test_a_capped_model_request_keeps_its_empty_list() -> None:
    # Model requests have no built-in defaults (measured): nothing to close.
    request = compile_agent(AgentSpec(name="worker", model=FLASH), max_output_tokens=512)
    assert request["tools"] == [] and "agent" not in request


def test_a_named_agent_definition_stays_tool_free_while_its_interactions_carry_it() -> None:
    # The Agents API allows no function tool in a definition, and a definition's
    # tools don't restrict a turn: each interaction's own list does (measured).
    spec = AgentSpec(name="named", model=FLASH)
    definition = native_definition(Records(), SCOPE, "project", spec, "daimon-named")
    assert definition["tools"] == []
    assert compile_agent(spec)["tools"] == [NO_TOOLS_TOOL]


def test_a_session_saved_before_the_sentinel_upgrades_and_nothing_else_does() -> None:
    expected = compile_agent(AgentSpec(name="bare", model=FLASH))
    saved = {**expected, "tools": []}
    validate_intent(saved, expected, (), enabled=False)  # same intent, built-ins now closed
    with pytest.raises(UnsupportedCapability, match="changed_mcp_request_intent"):
        validate_intent({**saved, "system_instruction": "changed"}, expected, (), enabled=False)
    with pytest.raises(UnsupportedCapability, match="changed_mcp_request_intent"):
        validate_intent(
            {**saved, "tools": [{"type": "google_search"}]}, expected, (), enabled=False
        )


def test_no_agent_may_declare_a_tool_named_like_the_sentinel() -> None:
    # The host answers a call to that name itself; a real tool can never take it.
    with pytest.raises(UnsupportedCapability, match="reserved_tool_name"):
        compile_agent(
            AgentSpec(
                name="clash",
                model=FLASH,
                tools=(
                    ToolSpec(
                        name=NO_TOOLS_FUNCTION,
                        kind="custom",
                        description="mine",
                        input_schema={"type": "object", "properties": {}},
                    ),
                ),
            )
        )
