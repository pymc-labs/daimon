"""Tests for EditView V2 rendering and behavior."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.agent_setup.edit_view import (
    EditView,
    _AuthFollowUpView,  # pyright: ignore[reportPrivateUsage]
    _McpRemoveSelect,  # pyright: ignore[reportPrivateUsage]
    _ScalarFieldSelect,  # pyright: ignore[reportPrivateUsage]
    _SkillRemoveSelect,  # pyright: ignore[reportPrivateUsage]
    build_edit_container,
)
from daimon.adapters.discord.agent_setup.state import PanelState, RosterEntry
from daimon.core.specs import AgentSpec


def _entry(name: str, model: str = "claude-sonnet-4-6") -> RosterEntry:
    return RosterEntry(
        name=name,
        model=model,
        spec=AgentSpec(name=name, model=model, system=None),
    )


def _entry_with(
    name: str, *, skills: list[Any] | None = None, mcps: list[Any] | None = None
) -> RosterEntry:
    spec = AgentSpec(
        name=name,
        model="claude-sonnet-4-6",
        skills=skills or [],
        mcp_servers=mcps,
    )
    return RosterEntry(name=name, model="claude-sonnet-4-6", spec=spec)


def _mcp(name: str, url: str) -> Any:
    from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
        BetaManagedAgentsURLMCPServerParams,
    )

    return BetaManagedAgentsURLMCPServerParams(name=name, type="url", url=url)


def _entry_with_mcps(name: str, mcps: list[Any], *, skills: list[Any] | None = None) -> RosterEntry:
    """RosterEntry whose spec carries `mcps` AND the matching `mcp_toolset`
    tools entries that `AgentSpec`'s validator requires (mirrors what
    `PanelState.apply_mcp_modal` builds in production)."""
    from anthropic.types.beta.agent_create_params import Tool
    from anthropic.types.beta.beta_managed_agents_mcp_toolset_params import (
        BetaManagedAgentsMCPToolsetParams,
    )

    tools: list[Tool] = [
        BetaManagedAgentsMCPToolsetParams(
            type="mcp_toolset",
            mcp_server_name=m.get("name", ""),
            default_config={"permission_policy": {"type": "always_allow"}},
        )
        for m in mcps
    ]
    spec = AgentSpec(
        name=name,
        model="claude-sonnet-4-6",
        mcp_servers=mcps,
        tools=tools,
        skills=skills or [],
    )
    return RosterEntry(name=name, model="claude-sonnet-4-6", spec=spec)


def _walk_buttons(view: discord.ui.LayoutView) -> list[discord.ui.Button[Any]]:
    """Walk the full LayoutView tree and collect all Button items."""
    buttons: list[discord.ui.Button[Any]] = []
    for item in view.walk_children():
        if isinstance(item, discord.ui.Button):
            buttons.append(item)
    return buttons


def _walk_selects(view: discord.ui.LayoutView) -> list[discord.ui.Select[Any]]:
    """Walk the full LayoutView tree and collect all Select items."""
    selects: list[discord.ui.Select[Any]] = []
    for item in view.walk_children():
        if isinstance(item, discord.ui.Select):
            selects.append(item)
    return selects


def _find_button(view: discord.ui.LayoutView, label: str) -> discord.ui.Button[Any]:
    for btn in _walk_buttons(view):
        if btn.label == label:
            return btn
    raise AssertionError(f"No button labeled {label!r}")


def _container_text(view: discord.ui.LayoutView) -> str:
    """Collect all TextDisplay content strings from the view."""
    parts: list[str] = []
    for item in view.walk_children():
        if isinstance(item, discord.ui.TextDisplay):
            parts.append(item.content)
    return "\n".join(parts)


# --- build_edit_container (pure) -------------------------------------------


def test_build_edit_container_header_text() -> None:
    container = build_edit_container(agent_name="my-bot")
    texts = [
        child.content for child in container.children if isinstance(child, discord.ui.TextDisplay)
    ]
    assert len(texts) == 1, "exactly one TextDisplay in the header container"
    assert texts[0].startswith("## ✏️ Editing "), "header starts with ## ✏️ Editing"
    assert "my-bot" in texts[0], "agent name in header"
    assert "-# changes apply immediately" in texts[0], "subtext present"


# --- EditView V2 structure --------------------------------------------------


def test_edit_view_is_layout_view(account_id: uuid.UUID) -> None:
    selected = _entry("agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    assert isinstance(view, discord.ui.LayoutView), "EditView must be a LayoutView"


def test_edit_view_header_and_subtext(account_id: uuid.UUID) -> None:
    selected = _entry("my-agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    text = _container_text(view)
    assert "## ✏️ Editing " in text, "header must start with ## ✏️ Editing"
    assert "my-agent" in text, "agent name must appear in header text"
    assert "-# changes apply immediately" in text, "subtext must be present"


def test_edit_view_has_three_selects_via_walk_children(account_id: uuid.UUID) -> None:
    from daimon.core.specs import SkillRef

    selected = _entry_with_mcps(
        "agent",
        [_mcp("user-mcp", "https://user.example.com/mcp")],
        skills=[SkillRef(type="custom", skill_id="skill-a")],
    )
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    selects = _walk_selects(view)
    placeholders = [s.placeholder for s in selects]
    assert "Edit a field…" in placeholders, "scalar select present"
    assert "✕ Remove a skill…" in placeholders, "skill remove select present"
    assert "✕ Remove an MCP…" in placeholders, "MCP remove select present"


def test_edit_view_has_five_buttons_via_walk_children(account_id: uuid.UUID) -> None:
    selected = _entry("agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    btn_labels = [b.label for b in _walk_buttons(view)]
    assert "+ Add skill" in btn_labels, "+ Add skill button present"
    assert "+ Add MCP" in btn_labels, "+ Add MCP button present"
    assert "Auth…" in btn_labels, "Auth… button present"
    assert "Secrets" in btn_labels, "Secrets button present"
    assert "← Back" in btn_labels, "← Back button present"


def test_edit_view_pat_absent_from_main_view(account_id: uuid.UUID) -> None:
    """PAT must not appear on the EditView itself — it lives in the Auth… follow-up."""
    selected = _entry("agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    btn_labels = [b.label for b in _walk_buttons(view) if b.label is not None]
    assert not any("PAT" in label for label in btn_labels), "PAT must not be on EditView directly"


def test_edit_view_timeout(account_id: uuid.UUID) -> None:
    selected = _entry("agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    assert view.timeout == 300, "EditView must use timeout=300"


# --- Auth… follow-up view --------------------------------------------------


def test_auth_followup_view_has_exactly_one_button(account_id: uuid.UUID) -> None:
    """Connect GitHub was removed; only Paste a PAT… remains."""
    selected = _entry("agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None

    follow_up = _AuthFollowUpView(state, runtime=runtime, allowed_user_id=42)
    buttons = _walk_buttons(follow_up)
    labels = [b.label for b in buttons]
    assert "Paste a PAT…" in labels, "Paste a PAT… option in follow-up"
    assert len(buttons) == 1, f"exactly 1 button in auth follow-up; got {labels}"


@pytest.mark.asyncio
async def test_edit_view_auth_button_sends_followup(
    account_id: uuid.UUID, mock_interaction: MagicMock
) -> None:
    selected = _entry("agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    auth_btn = _find_button(view, "Auth…")
    assert auth_btn.callback is not None
    await auth_btn.callback(mock_interaction)

    mock_interaction.response.send_message.assert_called_once()
    kwargs = mock_interaction.response.send_message.call_args.kwargs
    assert isinstance(kwargs["view"], _AuthFollowUpView), "Auth… sends _AuthFollowUpView"
    assert kwargs.get("ephemeral") is True, "follow-up must be ephemeral"


@pytest.mark.asyncio
async def test_auth_followup_pat_opens_repo_auth_modal(
    account_id: uuid.UUID, mock_interaction: MagicMock
) -> None:
    from daimon.adapters.discord.agent_setup.modals import RepoAuthModal

    selected = _entry("agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None

    follow_up = _AuthFollowUpView(state, runtime=runtime, allowed_user_id=42)
    pat_btn = next(b for b in _walk_buttons(follow_up) if b.label == "Paste a PAT…")
    assert pat_btn.callback is not None
    await pat_btn.callback(mock_interaction)

    mock_interaction.response.send_modal.assert_called_once()
    modal = mock_interaction.response.send_modal.call_args.args[0]
    assert isinstance(modal, RepoAuthModal), "Paste a PAT… opens RepoAuthModal"


# --- Select behavior -------------------------------------------------------


def test_edit_view_scalar_select_options(account_id: uuid.UUID) -> None:
    selected = _entry("agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    scalar = next(s for s in _walk_selects(view) if isinstance(s, _ScalarFieldSelect))  # pyright: ignore[reportPrivateUsage]
    values = [o.value for o in scalar.options]
    assert values == ["agent", "repo"], (
        f"scalar select must offer only agent/repo in order; got {values}"
    )


@pytest.mark.asyncio
async def test_edit_view_scalar_select_dispatches_agent_to_agent_section_modal(
    account_id: uuid.UUID, mock_interaction: MagicMock
) -> None:
    from daimon.adapters.discord.agent_setup.modals import AgentSectionModal

    selected = _entry("agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    scalar = next(s for s in _walk_selects(view) if isinstance(s, _ScalarFieldSelect))  # pyright: ignore[reportPrivateUsage]
    scalar._values = ["agent"]  # pyright: ignore[reportPrivateUsage]
    await scalar.callback(mock_interaction)

    mock_interaction.response.send_modal.assert_called_once()
    modal = mock_interaction.response.send_modal.call_args.args[0]
    assert isinstance(modal, AgentSectionModal), "agent pick must open AgentSectionModal"
    mock_interaction.response.defer.assert_not_called()


@pytest.mark.asyncio
async def test_edit_view_scalar_select_dispatches_repo_to_repo_auth_modal(
    account_id: uuid.UUID, mock_interaction: MagicMock
) -> None:
    from daimon.adapters.discord.agent_setup.modals import RepoAuthModal

    selected = _entry("agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    scalar = next(s for s in _walk_selects(view) if isinstance(s, _ScalarFieldSelect))  # pyright: ignore[reportPrivateUsage]
    scalar._values = ["repo"]  # pyright: ignore[reportPrivateUsage]
    await scalar.callback(mock_interaction)

    mock_interaction.response.send_modal.assert_called_once()
    modal = mock_interaction.response.send_modal.call_args.args[0]
    assert isinstance(modal, RepoAuthModal), "repo pick must open RepoAuthModal"


@pytest.mark.asyncio
async def test_edit_view_skill_remove_select_mutates_and_reconciles_and_rerenders(
    account_id: uuid.UUID, mock_interaction: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import daimon.adapters.discord.agent_setup.edit_view as edit_view_mod
    from daimon.core.specs import SkillRef

    skills = [
        SkillRef(type="custom", skill_id="skill-a"),
        SkillRef(type="custom", skill_id="skill-b"),
    ]
    selected = _entry_with("agent", skills=skills)
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None

    reconcile_calls: list[tuple[object, object]] = []

    async def fake_reconcile(rt: object, st: object, *, tenant_id: object) -> object:
        reconcile_calls.append((rt, st))
        return MagicMock()

    monkeypatch.setattr(edit_view_mod, "replace_agent_resources_for_panel", fake_reconcile)

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    skill_select = next(s for s in _walk_selects(view) if isinstance(s, _SkillRemoveSelect))  # pyright: ignore[reportPrivateUsage]
    skill_select._values = ["0"]  # pyright: ignore[reportPrivateUsage]
    await skill_select.callback(mock_interaction)

    assert state.selected is not None
    remaining = {s.skill_id for s in state.selected.spec.skills}
    assert "skill-a" not in remaining, "first skill must be removed"
    assert "skill-b" in remaining, "second skill must remain"
    assert len(reconcile_calls) == 1, "skill removal must reconcile to MA"
    mock_interaction.edit_original_response.assert_called_once()
    view_kwarg = mock_interaction.edit_original_response.call_args.kwargs["view"]
    assert isinstance(view_kwarg, EditView), "re-render must pass a fresh EditView"


@pytest.mark.asyncio
async def test_edit_view_mcp_remove_select_rollback_on_reconcile_failure(
    account_id: uuid.UUID, mock_interaction: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import daimon.adapters.discord.agent_setup.edit_view as edit_view_mod

    selected = _entry_with_mcps("agent", [_mcp("user-mcp", "https://user.example.com/mcp")])
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None

    async def fake_reconcile(rt: object, st: object, *, tenant_id: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(edit_view_mod, "replace_agent_resources_for_panel", fake_reconcile)

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    mcp_select = next(s for s in _walk_selects(view) if isinstance(s, _McpRemoveSelect))  # pyright: ignore[reportPrivateUsage]
    snapshot = state.selected
    mcp_select._values = ["0"]  # pyright: ignore[reportPrivateUsage]
    await mcp_select.callback(mock_interaction)

    assert state.selected is snapshot, (
        "state.selected must be restored to the pre-mutation snapshot on reconcile failure"
    )
    roster_slot = next(e for e in state.roster if e.name == "agent")
    assert roster_slot is snapshot, "roster slot must be restored to the snapshot"
    mock_interaction.followup.send.assert_called_once()
    msg = mock_interaction.followup.send.call_args.args[0]
    assert "boom" in msg, f"error followup must surface the failure; got {msg!r}"
    assert mock_interaction.followup.send.call_args.kwargs.get("ephemeral") is True, (
        "error followup must be ephemeral"
    )


@pytest.mark.asyncio
async def test_edit_view_mcp_remove_select_success_path(
    account_id: uuid.UUID, mock_interaction: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import daimon.adapters.discord.agent_setup.edit_view as edit_view_mod

    selected = _entry_with_mcps(
        "agent",
        [
            _mcp("user-a", "https://a.example.com/mcp"),
            _mcp("user-b", "https://b.example.com/mcp"),
        ],
    )
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None

    reconcile_calls: list[tuple[object, object]] = []

    async def fake_reconcile(rt: object, st: object, *, tenant_id: object) -> object:
        reconcile_calls.append((rt, st))
        return MagicMock()

    monkeypatch.setattr(edit_view_mod, "replace_agent_resources_for_panel", fake_reconcile)

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    mcp_select = next(s for s in _walk_selects(view) if isinstance(s, _McpRemoveSelect))  # pyright: ignore[reportPrivateUsage]
    mcp_select._values = ["0"]  # pyright: ignore[reportPrivateUsage]
    await mcp_select.callback(mock_interaction)

    assert len(reconcile_calls) == 1, "mcp removal must reconcile to MA"
    assert state.selected is not None
    remaining = state.selected.spec.mcp_servers or []
    assert len(remaining) == 1, "one user MCP must remain after removing one"
    mock_interaction.edit_original_response.assert_called_once()
    view_kwarg = mock_interaction.edit_original_response.call_args.kwargs["view"]
    assert isinstance(view_kwarg, EditView), "re-render must pass a fresh EditView"


def test_edit_view_skills_empty_disables_skill_select(account_id: uuid.UUID) -> None:
    selected = _entry_with("agent", skills=[])
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    skill_select = next(s for s in _walk_selects(view) if isinstance(s, _SkillRemoveSelect))  # pyright: ignore[reportPrivateUsage]
    assert skill_select.disabled is True, "empty skill select must be disabled"
    assert len(skill_select.options) == 1, "empty select must carry one dummy option"
    assert skill_select.options[0].value == "__none__", "dummy option value must be __none__"
    assert skill_select.placeholder == "(no skills — use + Add skill)", (
        f"empty-skill placeholder mismatch; got {skill_select.placeholder!r}"
    )


def test_edit_view_user_mcps_empty_disables_mcp_select(account_id: uuid.UUID) -> None:
    selected = _entry_with("agent", mcps=None)
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    mcp_select = next(s for s in _walk_selects(view) if isinstance(s, _McpRemoveSelect))  # pyright: ignore[reportPrivateUsage]
    assert mcp_select.disabled is True, "empty mcp select must be disabled"
    assert len(mcp_select.options) == 1, "empty select must carry one dummy option"
    assert mcp_select.options[0].value == "__none__", "dummy option value must be __none__"
    assert mcp_select.placeholder == "(no MCPs — use + Add MCP)", (
        f"empty-mcp placeholder mismatch; got {mcp_select.placeholder!r}"
    )


def test_edit_view_add_skill_button_disabled_at_cap(account_id: uuid.UUID) -> None:
    from daimon.core.specs import SkillRef

    skills = [SkillRef(type="custom", skill_id=f"s{i:02d}") for i in range(20)]
    selected = _entry_with("agent", skills=skills)
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    add_skill = _find_button(view, "+ Add skill")
    assert add_skill.disabled is True, "Add skill must be disabled at 20-skill cap"


def test_edit_view_add_mcp_button_disabled_at_cap(account_id: uuid.UUID) -> None:
    mcps = [_mcp(f"m{i:02d}", f"https://m{i:02d}.example.com") for i in range(20)]
    selected = _entry_with_mcps("agent", mcps)
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    add_mcp = _find_button(view, "+ Add MCP")
    assert add_mcp.disabled is True, "Add MCP must be disabled at 20-MCP cap"


def test_edit_view_mcp_select_filters_default_mcp_and_preserves_original_index(
    account_id: uuid.UUID,
) -> None:
    mcps = [
        _mcp("user-a", "https://a.example.com/mcp"),
        _mcp("default", "https://default.example/mcp"),
        _mcp("user-b", "https://b.example.com/mcp"),
    ]
    selected = _entry_with_mcps("agent", mcps)
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = "https://default.example/mcp"

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    mcp_select = next(s for s in _walk_selects(view) if isinstance(s, _McpRemoveSelect))  # pyright: ignore[reportPrivateUsage]
    values = [o.value for o in mcp_select.options]
    assert values == ["0", "2"], (
        f"default MCP (index 1) must be filtered, ORIGINAL indices preserved; got {values}"
    )


@pytest.mark.asyncio
async def test_edit_view_interaction_check_rejects_non_invoker(account_id: uuid.UUID) -> None:
    selected = _entry("agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    interaction = MagicMock()
    interaction.user.id = 999
    interaction.response.send_message = AsyncMock()
    ok = await view.interaction_check(interaction)
    assert ok is False, "non-invoker must be rejected by EditView.interaction_check"
    interaction.response.send_message.assert_called_once()
    args, kwargs = interaction.response.send_message.call_args
    assert args[0] == "Only the command invoker can use these buttons."
    assert kwargs.get("ephemeral") is True, "rejection message must be ephemeral"


@pytest.mark.asyncio
async def test_edit_view_remove_paths_never_mutate_main_panel(
    account_id: uuid.UUID, mock_interaction: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import daimon.adapters.discord.agent_setup.edit_view as edit_view_mod
    from daimon.core.specs import SkillRef

    selected = _entry_with("agent", skills=[SkillRef(type="custom", skill_id="skill-a")])
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None

    async def fake_reconcile(rt: object, st: object, *, tenant_id: object) -> object:
        return MagicMock()

    monkeypatch.setattr(edit_view_mod, "replace_agent_resources_for_panel", fake_reconcile)

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    skill_select = next(s for s in _walk_selects(view) if isinstance(s, _SkillRemoveSelect))  # pyright: ignore[reportPrivateUsage]
    skill_select._values = ["0"]  # pyright: ignore[reportPrivateUsage]
    await skill_select.callback(mock_interaction)

    mock_interaction.response.edit_message.assert_not_called()
    mock_interaction.edit_original_response.assert_called_once()


def test_secrets_button_label_has_no_plus(account_id: uuid.UUID) -> None:
    """The Secrets button opens a sub-view (not an add action), so its label is
    'Secrets', not '+ Secrets'."""
    selected = _entry("agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    labels = [b.label for b in _walk_buttons(view) if b.label is not None]
    assert "Secrets" in labels, "Secrets button must be labeled 'Secrets'"
    assert "+ Secrets" not in labels, "the '+' prefix must be dropped from the Secrets button"


def test_edit_view_secrets_button_disabled_for_system_agent(account_id: uuid.UUID) -> None:
    selected = _entry("sys")
    selected = RosterEntry(
        name="sys",
        model="claude-sonnet-4-6",
        spec=AgentSpec(name="sys", model="claude-sonnet-4-6", system=None),
        is_system=True,
    )
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    assert _find_button(view, "Secrets").disabled is True, (
        "system agents see the Secrets button disabled (defensive)"
    )

    user_selected = _entry("bot")
    user_state = PanelState(roster=[user_selected], selected=user_selected, account_id=account_id)
    user_view = EditView(user_state, runtime=runtime, allowed_user_id=42)
    assert _find_button(user_view, "Secrets").disabled is False, "user agents can open Secrets"
