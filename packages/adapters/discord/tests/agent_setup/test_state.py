"""Pure-reducer tests for PanelState — no I/O, no DB, no MA."""

from __future__ import annotations

import uuid

from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)
from daimon.adapters.discord.agent_setup.state import PanelState, RosterEntry
from daimon.core.specs import AgentSpec


def _state_with_agent(name: str = "a") -> PanelState:
    entry = RosterEntry(
        name=name,
        model="claude-sonnet-4-6",
        spec=AgentSpec(name=name, model="claude-sonnet-4-6", mcp_servers=None, tools=None),
    )
    return PanelState(roster=[entry], selected=entry, account_id=uuid.uuid4())


def test_apply_mcp_modal_adds_matching_toolset_entry() -> None:
    """mcp_servers append MUST also add a matching mcp_toolset entry to tools."""
    state = _state_with_agent()
    server = BetaManagedAgentsURLMCPServerParams(
        name="ga4-mcp", type="url", url="https://ga4.example.com/mcp"
    )
    state.apply_mcp_modal(server_entry=server, token_last4="abcd")
    assert state.selected is not None
    tools = state.selected.spec.tools or []
    referenced = [
        t for t in tools if t.get("type") == "mcp_toolset" and t.get("mcp_server_name") == "ga4-mcp"
    ]
    assert len(referenced) == 1, (
        "apply_mcp_modal must add exactly one mcp_toolset entry referencing the new server"
    )
    default_config = referenced[0].get("default_config") or {}
    assert default_config.get("permission_policy") == {"type": "always_allow"}, (
        "default permission policy must be always_allow"
    )


def test_apply_mcp_modal_is_idempotent_for_duplicate_name() -> None:
    """Re-submitting the same server name does NOT duplicate the toolset entry."""
    state = _state_with_agent()
    server = BetaManagedAgentsURLMCPServerParams(
        name="dup-mcp", type="url", url="https://example.com/mcp"
    )
    state.apply_mcp_modal(server_entry=server, token_last4="abcd")
    state.apply_mcp_modal(server_entry=server, token_last4="abcd")
    assert state.selected is not None
    tools = state.selected.spec.tools or []
    toolset_for_dup = [t for t in tools if t.get("mcp_server_name") == "dup-mcp"]
    assert len(toolset_for_dup) == 1, "duplicate apply must not duplicate the toolset entry"


def test_remove_mcp_at_removes_matching_toolset_entry() -> None:
    """remove_mcp_at must remove BOTH the mcp_servers entry AND the corresponding mcp_toolset."""
    state = _state_with_agent()
    state.apply_mcp_modal(
        server_entry=BetaManagedAgentsURLMCPServerParams(
            name="to-remove", type="url", url="https://example.com/mcp"
        ),
        token_last4="abcd",
    )
    state.remove_mcp_at(0)
    assert state.selected is not None
    assert not (state.selected.spec.mcp_servers or []), "mcp_servers must be empty after remove"
    tools = state.selected.spec.tools or []
    assert not any(
        t.get("type") == "mcp_toolset" and t.get("mcp_server_name") == "to-remove" for t in tools
    ), "removing an MCP must also remove its toolset reference (no orphan tools)"
