"""Tests for live setup-panel state."""

from __future__ import annotations

import uuid

from daimon.adapters.discord.agent_setup.state import PanelState, RosterEntry
from daimon.core.specs import AgentSpec


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
