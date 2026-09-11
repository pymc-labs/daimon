"""Tests for EditView V2 rendering and behavior."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.agent_setup.edit_view import (
    EditView,
    _McpRemoveSelect,  # pyright: ignore[reportPrivateUsage]
    _SkillRemoveSelect,  # pyright: ignore[reportPrivateUsage]
    build_edit_container,
)
from daimon.adapters.discord.agent_setup.state import PanelState, RosterEntry
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.specs import AgentSpec
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


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
    runtime.settings.github.app_slug = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    assert isinstance(view, discord.ui.LayoutView), "EditView must be a LayoutView"


def test_edit_view_header_and_subtext(account_id: uuid.UUID) -> None:
    selected = _entry("my-agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    text = _container_text(view)
    assert "## ✏️ Editing " in text, "header must start with ## ✏️ Editing"
    assert "my-agent" in text, "agent name must appear in header text"
    assert "-# changes apply immediately" in text, "subtext must be present"


def test_edit_view_has_only_the_two_remove_selects(account_id: uuid.UUID) -> None:
    from daimon.core.specs import SkillRef

    selected = _entry_with_mcps(
        "agent",
        [_mcp("user-mcp", "https://user.example.com/mcp")],
        skills=[SkillRef(type="custom", skill_id="skill-a")],
    )
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    selects = _walk_selects(view)
    assert len(selects) == 2, f"exactly two remove selects; got {len(selects)}"
    placeholders = [s.placeholder for s in selects]
    assert "✕ Remove a skill…" in placeholders, "skill remove select present"
    assert "✕ Remove an MCP…" in placeholders, "MCP remove select present"
    for select in selects:
        values = [o.value for o in select.options]
        assert "agent" not in values, "no select offers an agent option value"
        assert "repo" not in values, "no select offers a repo option value"


def test_edit_view_button_labels_in_row_order(account_id: uuid.UUID) -> None:
    selected = _entry("agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    btn_labels = [b.label for b in _walk_buttons(view)]
    assert btn_labels == [
        "Prompt & model",
        "Working repo",
        "Keys",
        "+ Add skill",
        "+ Add MCP server",
        "← Back",
    ], f"button labels must be exactly the flat row order; got {btn_labels}"


# --- Install App… link button -------------------------------------------


def test_edit_view_renders_install_app_button_when_slug_configured(
    account_id: uuid.UUID,
) -> None:
    from daimon.core.github_app_auth import build_app_install_url

    selected = _entry("agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = "acme-daimon"

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    first_row_buttons = _walk_buttons(view)[:4]
    assert len(first_row_buttons) == 4, "first row must hold four buttons when a slug is configured"
    install_btn = first_row_buttons[2]
    assert install_btn.style == discord.ButtonStyle.link, (
        "install-app button must use the link style"
    )
    assert install_btn.url == build_app_install_url("acme-daimon"), (
        "install-app button url must be the built install URL for the configured slug"
    )
    assert install_btn.custom_id is None, (
        "a link button must carry no custom_id — Discord rejects url+custom_id together"
    )


def test_edit_view_omits_install_app_button_when_slug_unset(account_id: uuid.UUID) -> None:
    selected = _entry("agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    btn_labels = [b.label for b in _walk_buttons(view)]
    assert btn_labels == [
        "Prompt & model",
        "Working repo",
        "Keys",
        "+ Add skill",
        "+ Add MCP server",
        "← Back",
    ], "no link button must render when app_slug is unset"
    first_row_buttons = _walk_buttons(view)[:3]
    assert len(first_row_buttons) == 3, "first row must hold exactly the three pre-existing buttons"


def test_edit_view_install_app_button_enabled_when_spec_controls_disabled(
    account_id: uuid.UUID,
) -> None:
    """The install-app button is not a spec control: it must render enabled
    even when the reachability gate disables the spec-touching controls."""
    selected = _entry("bot")
    state = _reachable_state(selected, account_id, is_admin=False)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = "acme-daimon"

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    install_btn = next(b for b in _walk_buttons(view) if b.style == discord.ButtonStyle.link)
    assert install_btn.disabled is False, (
        "install-app button must stay enabled even when spec controls are disabled"
    )


def test_edit_view_pat_absent_from_main_view(account_id: uuid.UUID) -> None:
    """PAT must not appear as its own control on EditView."""
    selected = _entry("agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    btn_labels = [b.label for b in _walk_buttons(view) if b.label is not None]
    assert not any("PAT" in label for label in btn_labels), "PAT must not be on EditView directly"


def test_edit_view_timeout(account_id: uuid.UUID) -> None:
    selected = _entry("agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    assert view.timeout == 300, "EditView must use timeout=300"


# --- Prompt & model/Working repo buttons --------------------------------------------------


@pytest.mark.asyncio
async def test_edit_view_agent_button_opens_agent_section_modal(
    account_id: uuid.UUID, mock_interaction: MagicMock
) -> None:
    from daimon.adapters.discord.agent_setup.modals import AgentSectionModal

    selected = _entry("agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    agent_btn = _find_button(view, "Prompt & model")
    assert agent_btn.callback is not None
    await agent_btn.callback(mock_interaction)

    mock_interaction.response.send_modal.assert_called_once()
    modal = mock_interaction.response.send_modal.call_args.args[0]
    assert isinstance(modal, AgentSectionModal), "Prompt & model must open AgentSectionModal"


@pytest.mark.asyncio
async def test_edit_view_github_button_opens_repo_auth_modal(
    account_id: uuid.UUID, mock_interaction: MagicMock
) -> None:
    from daimon.adapters.discord.agent_setup.modals import RepoAuthModal

    selected = _entry("agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    github_btn = _find_button(view, "Working repo")
    assert github_btn.callback is not None
    await github_btn.callback(mock_interaction)

    mock_interaction.response.send_modal.assert_called_once()
    modal = mock_interaction.response.send_modal.call_args.args[0]
    assert isinstance(modal, RepoAuthModal), "Working repo must open RepoAuthModal directly"
    mock_interaction.response.send_message.assert_not_called()


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
    runtime.settings.github.app_slug = None

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
    runtime.settings.github.app_slug = None

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
    runtime.settings.github.app_slug = None

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
    runtime.settings.github.app_slug = None

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
    runtime.settings.github.app_slug = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    mcp_select = next(s for s in _walk_selects(view) if isinstance(s, _McpRemoveSelect))  # pyright: ignore[reportPrivateUsage]
    assert mcp_select.disabled is True, "empty mcp select must be disabled"
    assert len(mcp_select.options) == 1, "empty select must carry one dummy option"
    assert mcp_select.options[0].value == "__none__", "dummy option value must be __none__"
    assert mcp_select.placeholder == "(no MCP servers — use + Add MCP server)", (
        f"empty-mcp placeholder mismatch; got {mcp_select.placeholder!r}"
    )


def test_edit_view_add_skill_button_disabled_at_cap(account_id: uuid.UUID) -> None:
    from daimon.core.specs import SkillRef

    skills = [SkillRef(type="custom", skill_id=f"s{i:02d}") for i in range(20)]
    selected = _entry_with("agent", skills=skills)
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    add_skill = _find_button(view, "+ Add skill")
    assert add_skill.disabled is True, "Add skill must be disabled at 20-skill cap"


def test_edit_view_add_mcp_button_disabled_at_cap(account_id: uuid.UUID) -> None:
    mcps = [_mcp(f"m{i:02d}", f"https://m{i:02d}.example.com") for i in range(20)]
    selected = _entry_with_mcps("agent", mcps)
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    add_mcp = _find_button(view, "+ Add MCP server")
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
    runtime.settings.github.app_slug = None

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
    runtime.settings.github.app_slug = None

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
    runtime.settings.github.app_slug = None

    async def fake_reconcile(rt: object, st: object, *, tenant_id: object) -> object:
        return MagicMock()

    monkeypatch.setattr(edit_view_mod, "replace_agent_resources_for_panel", fake_reconcile)

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    skill_select = next(s for s in _walk_selects(view) if isinstance(s, _SkillRemoveSelect))  # pyright: ignore[reportPrivateUsage]
    skill_select._values = ["0"]  # pyright: ignore[reportPrivateUsage]
    await skill_select.callback(mock_interaction)

    mock_interaction.response.edit_message.assert_not_called()
    mock_interaction.edit_original_response.assert_called_once()


def test_env_vars_button_label_has_no_plus(account_id: uuid.UUID) -> None:
    """The Keys button opens a sub-view (not an add action), so its label is
    'Keys', not '+ Keys'."""
    selected = _entry("agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    labels = [b.label for b in _walk_buttons(view) if b.label is not None]
    assert "Keys" in labels, "Keys button must be labeled 'Keys'"
    assert "+ Keys" not in labels, "the '+' prefix must be dropped from the Keys button"


def test_edit_view_env_vars_button_always_enabled(account_id: uuid.UUID) -> None:
    """Keys are per-agent daimon state, never part of the agent spec, so the
    button is always enabled — for the seeded/system agent exactly like any other."""
    selected = RosterEntry(
        name="sys",
        model="claude-sonnet-4-6",
        spec=AgentSpec(name="sys", model="claude-sonnet-4-6", system=None),
        is_system=True,
    )
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    assert _find_button(view, "Keys").disabled is False, (
        "the seeded/system agent's Keys button must be enabled"
    )

    user_selected = _entry("bot")
    user_state = PanelState(roster=[user_selected], selected=user_selected, account_id=account_id)
    user_view = EditView(user_state, runtime=runtime, allowed_user_id=42)
    assert _find_button(user_view, "Keys").disabled is False, "user agents can open Keys"


# --- Spec-control reachability gate ---------------------------


def _reachable_state(entry: RosterEntry, account_id: uuid.UUID, *, is_admin: bool) -> PanelState:
    """A state whose selected agent IS reachable via the deployment default."""
    from daimon.core.scope import DeploymentDefault

    return PanelState(
        roster=[entry],
        selected=entry,
        account_id=account_id,
        is_admin=is_admin,
        cascade_view=(None, []),
        deployment_default=DeploymentDefault(agent_name=entry.name),
    )


def test_edit_view_non_admin_reachable_non_system_disables_spec_controls(
    account_id: uuid.UUID,
) -> None:
    """A non-admin editing a reachable, non-system agent gets the five
    spec-touching controls disabled; Working repo and Keys stay enabled.

    Enabled is not unguarded: the attachment gate refuses those two on click,
    so the caller reads why they cannot bind a repo instead of staring at a
    greyed-out button."""
    from daimon.core.specs import SkillRef

    selected = _entry_with_mcps(
        "bot",
        [_mcp("user-mcp", "https://user.example.com/mcp")],
        skills=[SkillRef(type="custom", skill_id="skill-a")],
    )
    state = _reachable_state(selected, account_id, is_admin=False)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    assert _find_button(view, "Prompt & model").disabled is True, "Prompt & model must be disabled"
    assert _find_button(view, "+ Add skill").disabled is True, "+ Add skill must be disabled"
    assert _find_button(view, "+ Add MCP server").disabled is True, (
        "+ Add MCP server must be disabled"
    )
    skill_select = next(s for s in _walk_selects(view) if isinstance(s, _SkillRemoveSelect))  # pyright: ignore[reportPrivateUsage]
    mcp_select = next(s for s in _walk_selects(view) if isinstance(s, _McpRemoveSelect))  # pyright: ignore[reportPrivateUsage]
    assert skill_select.disabled is True, "skill remove select must be disabled"
    assert mcp_select.disabled is True, "mcp remove select must be disabled"
    assert _find_button(view, "Working repo").disabled is False, "Working repo must stay enabled"
    assert _find_button(view, "Keys").disabled is False, "Keys must stay enabled"


def test_edit_view_non_admin_unreachable_non_system_enables_spec_controls(
    account_id: uuid.UUID,
) -> None:
    """A non-admin editing an agent nobody has scoped gets every spec control
    enabled — it is their scratchpad."""
    from daimon.core.specs import SkillRef

    selected = _entry_with_mcps(
        "bot",
        [_mcp("user-mcp", "https://user.example.com/mcp")],
        skills=[SkillRef(type="custom", skill_id="skill-a")],
    )
    state = PanelState(roster=[selected], selected=selected, account_id=account_id, is_admin=False)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    assert _find_button(view, "Prompt & model").disabled is False, "Prompt & model must be enabled"
    assert _find_button(view, "+ Add skill").disabled is False, "+ Add skill must be enabled"
    assert _find_button(view, "+ Add MCP server").disabled is False, (
        "+ Add MCP server must be enabled"
    )
    skill_select = next(s for s in _walk_selects(view) if isinstance(s, _SkillRemoveSelect))  # pyright: ignore[reportPrivateUsage]
    mcp_select = next(s for s in _walk_selects(view) if isinstance(s, _McpRemoveSelect))  # pyright: ignore[reportPrivateUsage]
    assert skill_select.disabled is False, "skill remove select must be enabled"
    assert mcp_select.disabled is False, "mcp remove select must be enabled"


def test_edit_view_admin_reachable_non_system_enables_spec_controls(account_id: uuid.UUID) -> None:
    """An admin editing a reachable, non-system agent still gets the spec
    controls enabled — the reachability gate is admin-exempt."""
    selected = _entry("bot")
    state = _reachable_state(selected, account_id, is_admin=True)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    assert _find_button(view, "Prompt & model").disabled is False, (
        "Prompt & model must be enabled for an admin"
    )


def test_edit_view_reachable_system_agent_disables_spec_controls_for_admin_too(
    account_id: uuid.UUID,
) -> None:
    """is_system stays absolute: even an admin gets the spec controls disabled
    on the seeded agent."""
    selected = RosterEntry(
        name="daimon",
        model="claude-sonnet-4-6",
        spec=AgentSpec(name="daimon", model="claude-sonnet-4-6", system=None),
        is_system=True,
    )
    state = _reachable_state(selected, account_id, is_admin=True)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    assert _find_button(view, "Prompt & model").disabled is True, (
        "Prompt & model must stay disabled on a system agent even for an admin"
    )
    assert _find_button(view, "Working repo").disabled is False, "Working repo is not spec-gated"
    assert _find_button(view, "Keys").disabled is False, "Keys is not spec-gated"


# --- Back / open_edit_view edit-in-place -----------------------------------


@pytest.mark.asyncio
async def test_edit_view_back_edits_to_agent_setup_view(
    account_id: uuid.UUID, mock_interaction: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EditView's ← Back edits back to AgentSetupView in place; it never deletes."""
    import daimon.adapters.discord.agent_setup.panel as panel_mod
    from daimon.adapters.discord.agent_setup.panel import AgentSetupView

    monkeypatch.setattr(panel_mod, "load_secret_count", AsyncMock(return_value=0))
    monkeypatch.setattr(panel_mod, "load_selected_github_login", AsyncMock(return_value=None))
    monkeypatch.setattr(panel_mod, "load_repo_binding", AsyncMock(return_value=None))

    selected = _entry("agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None
    runtime.settings.github.fallback_pat = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    back_btn = _find_button(view, "← Back")
    await back_btn.callback(mock_interaction)

    mock_interaction.edit_original_response.assert_called_once()
    view_kwarg = mock_interaction.edit_original_response.call_args.kwargs["view"]
    assert isinstance(view_kwarg, AgentSetupView), "Back must edit back to AgentSetupView"
    mock_interaction.delete_original_response.assert_not_called()


@pytest.mark.asyncio
async def test_open_edit_view_swaps_onto_the_panel_message(
    account_id: uuid.UUID, mock_interaction: MagicMock
) -> None:
    """open_edit_view swaps EditView onto the existing message; it never sends a new one."""
    from daimon.adapters.discord.agent_setup.edit_view import open_edit_view

    selected = _entry("agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None

    await open_edit_view(mock_interaction, state, runtime=runtime, allowed_user_id=42)

    mock_interaction.response.edit_message.assert_called_once()
    mock_interaction.response.send_message.assert_not_called()
    view_kwarg = mock_interaction.response.edit_message.call_args.kwargs["view"]
    assert isinstance(view_kwarg, EditView), "open_edit_view must swap in an EditView"


# ---------------------------------------------------------------------------
# Shared on_timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_view_timeout_replaces_the_edit_view(
    account_id: uuid.UUID, mock_interaction: MagicMock
) -> None:
    selected = _entry("agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    view.bind_render_interaction(mock_interaction, panel=state)

    await view.on_timeout()

    mock_interaction.edit_original_response.assert_called_once()
    call_kwargs = mock_interaction.edit_original_response.call_args.kwargs
    assert "content" not in call_kwargs, "the timeout edit must not override content"
    expired_view = call_kwargs["view"]
    walked = list(expired_view.walk_children())
    assert not any(isinstance(c, discord.ui.Button) for c in walked), (
        "the expired replacement must carry no interactive children"
    )
    assert not any(isinstance(c, discord.ui.Select) for c in walked), (
        "the expired replacement must carry no interactive children"
    )
    assert view.timeout == 300, "the shared on_timeout mixin leaves timeout values unchanged"


@pytest.mark.asyncio
async def test_open_edit_view_binds_the_render_interaction(
    account_id: uuid.UUID, mock_interaction: MagicMock
) -> None:
    """open_edit_view's swapped-in EditView must bind the interaction that rendered it."""
    from daimon.adapters.discord.agent_setup.edit_view import open_edit_view

    selected = _entry("agent")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None

    await open_edit_view(mock_interaction, state, runtime=runtime, allowed_user_id=42)

    view_kwarg = mock_interaction.response.edit_message.call_args.kwargs["view"]
    await view_kwarg.on_timeout()

    mock_interaction.edit_original_response.assert_called_once()


# ---------------------------------------------------------------------------
# Click-time authz gate on the remove selects
# ---------------------------------------------------------------------------


def _non_admin_interaction(*, guild_id: int) -> MagicMock:
    interaction = MagicMock()
    interaction.guild_id = guild_id
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 42
    interaction.user.guild_permissions.administrator = False
    interaction.user.guild_permissions.manage_guild = False
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


def _admin_interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 42
    interaction.user.guild_permissions.administrator = True
    interaction.user.guild_permissions.manage_guild = False
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


@pytest.mark.asyncio
async def test_skill_remove_select_refuses_write_when_target_is_reachable_for_non_admin(
    account_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    import daimon.adapters.discord.agent_setup.edit_view as edit_view_mod
    from daimon.core.scope import DeploymentDefault
    from daimon.core.specs import SkillRef
    from daimon.testing.factories import make_tenant

    async def unexpected_reconcile(rt: object, st: object, *, tenant_id: object) -> object:
        raise AssertionError("reconcile must not run when the write is refused")

    monkeypatch.setattr(edit_view_mod, "replace_agent_resources_for_panel", unexpected_reconcile)

    guild_id = 910101
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, platform="discord", workspace_id=str(guild_id))

    skills = [SkillRef(type="custom", skill_id="skill-a")]
    selected = _entry_with("bot", skills=skills)
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None
    runtime.sessionmaker = db_session_factory
    runtime.deployment_default = DeploymentDefault(agent_name="bot")

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    skill_select = next(s for s in _walk_selects(view) if isinstance(s, _SkillRemoveSelect))  # pyright: ignore[reportPrivateUsage]
    skill_select._values = ["0"]  # pyright: ignore[reportPrivateUsage]

    interaction = _non_admin_interaction(guild_id=guild_id)
    await skill_select.callback(interaction)

    assert state.selected is not None
    remaining = {s.skill_id for s in state.selected.spec.skills}
    assert "skill-a" in remaining, "skill must not be removed when the write is refused"
    interaction.response.send_message.assert_called_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_skill_remove_select_refuses_write_on_system_agent_even_for_admin(
    account_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    import daimon.adapters.discord.agent_setup.edit_view as edit_view_mod
    from daimon.core.specs import SkillRef

    async def unexpected_reconcile(rt: object, st: object, *, tenant_id: object) -> object:
        raise AssertionError("reconcile must not run when the write is refused")

    monkeypatch.setattr(edit_view_mod, "replace_agent_resources_for_panel", unexpected_reconcile)

    skills = [SkillRef(type="custom", skill_id="skill-a")]
    selected = RosterEntry(
        name="daimon",
        model="claude-sonnet-4-6",
        spec=AgentSpec(name="daimon", model="claude-sonnet-4-6", skills=skills),
        is_system=True,
    )
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    skill_select = next(s for s in _walk_selects(view) if isinstance(s, _SkillRemoveSelect))  # pyright: ignore[reportPrivateUsage]
    skill_select._values = ["0"]  # pyright: ignore[reportPrivateUsage]

    interaction = _admin_interaction()
    await skill_select.callback(interaction)

    assert state.selected is not None
    remaining = {s.skill_id for s in state.selected.spec.skills}
    assert "skill-a" in remaining, "a system agent's skills must never be removed from the panel"
    interaction.response.send_message.assert_called_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_mcp_remove_select_refuses_write_when_target_is_reachable_for_non_admin(
    account_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    import daimon.adapters.discord.agent_setup.edit_view as edit_view_mod
    from daimon.core.scope import DeploymentDefault
    from daimon.testing.factories import make_tenant

    async def unexpected_reconcile(rt: object, st: object, *, tenant_id: object) -> object:
        raise AssertionError("reconcile must not run when the write is refused")

    monkeypatch.setattr(edit_view_mod, "replace_agent_resources_for_panel", unexpected_reconcile)

    guild_id = 910102
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, platform="discord", workspace_id=str(guild_id))

    selected = _entry_with_mcps("bot", [_mcp("user-mcp", "https://user.example.com/mcp")])
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None
    runtime.sessionmaker = db_session_factory
    runtime.deployment_default = DeploymentDefault(agent_name="bot")

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    mcp_select = next(s for s in _walk_selects(view) if isinstance(s, _McpRemoveSelect))  # pyright: ignore[reportPrivateUsage]
    mcp_select._values = ["0"]  # pyright: ignore[reportPrivateUsage]

    interaction = _non_admin_interaction(guild_id=guild_id)
    await mcp_select.callback(interaction)

    assert state.selected is not None
    remaining = state.selected.spec.mcp_servers or []
    assert len(remaining) == 1, "an MCP must not be removed when the write is refused"
    interaction.response.send_message.assert_called_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_mcp_remove_select_refuses_write_on_system_agent_even_for_admin(
    account_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    import daimon.adapters.discord.agent_setup.edit_view as edit_view_mod
    from anthropic.types.beta.agent_create_params import Tool
    from anthropic.types.beta.beta_managed_agents_mcp_toolset_params import (
        BetaManagedAgentsMCPToolsetParams,
    )

    async def unexpected_reconcile(rt: object, st: object, *, tenant_id: object) -> object:
        raise AssertionError("reconcile must not run when the write is refused")

    monkeypatch.setattr(edit_view_mod, "replace_agent_resources_for_panel", unexpected_reconcile)

    mcps = [_mcp("user-mcp", "https://user.example.com/mcp")]
    tools: list[Tool] = [
        BetaManagedAgentsMCPToolsetParams(
            type="mcp_toolset",
            mcp_server_name="user-mcp",
            default_config={"permission_policy": {"type": "always_allow"}},
        )
    ]
    selected = RosterEntry(
        name="daimon",
        model="claude-sonnet-4-6",
        spec=AgentSpec(name="daimon", model="claude-sonnet-4-6", mcp_servers=mcps, tools=tools),
        is_system=True,
    )
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    mcp_select = next(s for s in _walk_selects(view) if isinstance(s, _McpRemoveSelect))  # pyright: ignore[reportPrivateUsage]
    mcp_select._values = ["0"]  # pyright: ignore[reportPrivateUsage]

    interaction = _admin_interaction()
    await mcp_select.callback(interaction)

    assert state.selected is not None
    remaining = state.selected.spec.mcp_servers or []
    assert len(remaining) == 1, "a system agent's mcp_servers must never be removed from the panel"
    interaction.response.send_message.assert_called_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_edit_view_github_button_refuses_for_non_admin_on_reachable_agent(
    account_id: uuid.UUID,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The Working repo button stays clickable for everyone, but a non-admin
    clicking it on a currently-reachable agent gets the refusal instead of the
    modal — so no GitHub token is ever typed into a form that would be
    rejected on submit."""
    from daimon.core.scope import DeploymentDefault
    from daimon.testing.factories import make_tenant

    guild_id = 910103
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, platform="discord", workspace_id=str(guild_id))

    selected = _entry("bot")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None
    runtime.sessionmaker = db_session_factory
    runtime.deployment_default = DeploymentDefault(agent_name="bot")

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    github_btn = _find_button(view, "Working repo")
    assert github_btn.disabled is False, "Working repo must stay clickable for a non-admin"
    assert github_btn.callback is not None

    interaction = _non_admin_interaction(guild_id=guild_id)
    interaction.response.send_modal = AsyncMock()
    await github_btn.callback(interaction)

    interaction.response.send_modal.assert_not_called()
    interaction.response.send_message.assert_called_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_edit_view_github_button_opens_modal_for_admin_on_system_agent(
    account_id: uuid.UUID,
) -> None:
    """An admin may bind a repo to the deployment's built-in agent — that is
    the first-run step — so the button opens the modal even though the target
    is both a system agent and the workspace's default."""
    from daimon.adapters.discord.agent_setup.modals import RepoAuthModal
    from daimon.core.scope import DeploymentDefault

    selected = RosterEntry(
        name="daimon",
        model="claude-sonnet-4-6",
        spec=AgentSpec(name="daimon", model="claude-sonnet-4-6"),
        is_system=True,
    )
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        deployment_default=DeploymentDefault(agent_name="daimon"),
    )
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    runtime.settings.github.app_slug = None

    view = EditView(state, runtime=runtime, allowed_user_id=42)
    github_btn = _find_button(view, "Working repo")
    assert github_btn.callback is not None

    interaction = _admin_interaction()
    interaction.response.send_modal = AsyncMock()
    await github_btn.callback(interaction)

    interaction.response.send_modal.assert_called_once()
    modal = interaction.response.send_modal.call_args.args[0]
    assert isinstance(modal, RepoAuthModal), "an admin must still reach the repo-bind modal"
    interaction.response.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_env_vars_paste_modal_refuses_on_reachable_agent_for_non_admin(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`put_agent_file` is an upsert and the files are mounted read-write on
    every session of the agent, so a non-admin pasting over the workspace's
    current default agent would overwrite secrets everyone in the install
    depends on. The Keys button still opens for them; the write does not."""
    from daimon.adapters.discord.agent_setup.credentials import PasteSecretModal
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.ma_resolver import new_resolver_cache
    from daimon.core.notebooks._rate_limit import RateLimiter
    from daimon.core.scope import DeploymentDefault
    from daimon.core.stores.agent_files import get_agent_file
    from daimon.testing.factories import make_tenant

    guild_id = 910301
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(guild_id))
    agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_env_test")

    settings = MagicMock()
    settings.mcp.public_url = None
    settings.github.app_slug = None
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=MagicMock(),
        sessionmaker=db_session_factory,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        # The target agent is the workspace's current default, so it is shared.
        deployment_default=DeploymentDefault(agent_name="daimon"),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # never runs a turn
    )

    async def _noop(_interaction: discord.Interaction) -> None:
        return None

    modal = PasteSecretModal(
        runtime=runtime,
        tenant_id=tenant.id,
        agent_id=agent_id,
        entry=_entry("daimon"),
        on_added=_noop,
    )
    modal.content_input._value = "XERO_API_KEY=abc123"  # pyright: ignore[reportPrivateUsage]

    interaction = _non_admin_interaction(guild_id=guild_id)

    await modal.on_submit(interaction)

    async with db_session_factory() as session:
        row = await get_agent_file(
            session, tenant_id=tenant.id, agent_id=agent_id, key="XERO_API_KEY"
        )
    assert row is None, "a non-admin must not write keys onto the workspace's default agent"
    interaction.response.defer.assert_not_called()
    interaction.response.send_message.assert_called_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True
