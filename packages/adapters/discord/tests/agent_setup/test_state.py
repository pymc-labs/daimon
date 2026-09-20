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


def test_initial_selection_uses_parent_responder_and_preserves_explicit_selection() -> None:
    from daimon.core.scope import ChannelConfigRow, DeploymentDefault, TenantConfigRow

    tenant_id = uuid.uuid4()
    roster = [
        RosterEntry(
            name=name,
            model="claude-sonnet-4-6",
            spec=AgentSpec(name=name, model="claude-sonnet-4-6"),
        )
        for name in ("first", "specialist", "daimon")
    ]
    state = PanelState.initial(
        roster=roster,
        account_id=uuid.uuid4(),
        platform_principal_id=uuid.uuid4(),
        channel_id=222,
        cascade_view=(
            TenantConfigRow(tenant_id=tenant_id, agent_name="daimon"),
            [ChannelConfigRow(tenant_id=tenant_id, channel_id="222", agent_name="specialist")],
        ),
        deployment_default=DeploymentDefault(agent_name="first"),
    )
    assert state.selected is roster[1], (
        "Details initially targets the actual parent-channel responder"
    )
    state.select("first")
    assert state.selected is roster[0], "explicit selection must win after initialization"


def test_initial_selection_without_a_responder_keeps_setup_target_unset() -> None:
    roster = [
        RosterEntry(
            name="specialist",
            model="claude-sonnet-4-6",
            spec=AgentSpec(name="specialist", model="claude-sonnet-4-6"),
        )
    ]
    state = PanelState.initial(
        roster=roster, account_id=uuid.uuid4(), platform_principal_id=uuid.uuid4()
    )
    assert state.selected is None, (
        "setup must ask which agent instead of silently picking the first roster entry"
    )


def test_select_agent_keeps_legacy_selection_in_sync() -> None:
    """The read-only panel's selection must be visible to the editor panel.

    Both panels live in the tree at once; a target chosen on one and read off
    the other would send setup at the wrong agent.
    """
    from daimon.adapters.discord.agent_setup.state import RosterEntry
    from daimon.core.roster import RosterAgent

    entries = [
        RosterEntry(
            name=name,
            model="claude-sonnet-4-6",
            spec=AgentSpec(name=name, model="claude-sonnet-4-6"),
            ma_agent_id=f"ag_{name}",
        )
        for name in ("first", "specialist")
    ]
    state = PanelState(
        roster=entries,
        selected=entries[0],
        account_id=uuid.uuid4(),
        expanded_detail="keys",
    )
    chosen = RosterAgent(
        name="specialist",
        ma_agent_id="ag_specialist",
        model_id="claude-sonnet-4-6",
        is_built_in=False,
    )

    state.select_agent(chosen)

    assert state.selected_agent is chosen, "the new panel points at the chosen agent"
    assert state.selected is entries[1], "and the editor panel's selection follows it by name"
    assert state.expanded_detail is None, "switching agents collapses the previous detail list"


def test_select_agent_without_a_legacy_entry_leaves_the_editor_selection_alone() -> None:
    from daimon.core.roster import RosterAgent

    state = PanelState(roster=[], selected=None, account_id=uuid.uuid4())
    chosen = RosterAgent(
        name="only-on-the-new-panel",
        ma_agent_id="ag_new",
        model_id="claude-sonnet-4-6",
        is_built_in=False,
    )

    state.select_agent(chosen)

    assert state.selected_agent is chosen, "the read-only panel always records its own selection"
    assert state.selected is None, "there is no editor entry to point at, and none is invented"


def test_roster_page_of_clamps_stale_page() -> None:
    """A page number that outlived its rows shows the last page, not an error."""
    from daimon.core.roster import RosterAgent

    agents = tuple(
        RosterAgent(
            name=f"agent-{index}",
            ma_agent_id=f"ag_{index}",
            model_id="claude-sonnet-4-6",
            is_built_in=False,
        )
        for index in range(3)
    )
    state = PanelState(
        roster=[],
        selected=None,
        account_id=uuid.uuid4(),
        roster_agents=agents,
        roster_page=7,
    )

    page = state.roster_page_of(2)

    assert page.page == 1, "a page past the end clamps to the last page that exists"
    assert [agent.name for agent in page.items] == ["agent-2"], (
        "the clamped page carries the rows that are actually there"
    )
    assert page.has_next is False and page.has_previous is True, (
        "the pager must know it is at the end"
    )
