"""Tests for AgentSetupView (F5 V2 LayoutView) rendering and disabled-state semantics.

Plan 70-05 migrated the main panel from a classic View + Embed to a LayoutView +
Container (Components V2). Tests here verify the F5 card structure: container text,
thumbnail/header, picker options, lifecycle buttons, and member-gating.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.agent_setup.panel import AgentSetupView, build_panel_container
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


def _make_runtime() -> DiscordRuntime:
    # The View doesn't call runtime methods at construction time — a lightweight
    # MagicMock is fine for view-shape tests.
    return MagicMock(spec=DiscordRuntime)


def _make_runtime_with_settings(*, fallback_pat: object = None) -> DiscordRuntime:
    """Runtime mock for callback paths that read runtime.settings.github.fallback_pat.

    DiscordRuntime is a frozen dataclass, so spec-restricted mocks reject the
    instance-field `.settings`; use an unrestricted mock with `.settings` wired."""
    runtime = MagicMock()
    runtime.settings.github.fallback_pat = fallback_pat
    return runtime


def _admin_interaction(user_id: int = 42) -> MagicMock:
    """Interaction whose caller is a live guild admin (administrator permission).

    ``is_guild_admin`` short-circuits on the permission, so no guild owner_id or
    DB read is involved.
    """
    interaction = MagicMock()
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = user_id
    interaction.user.guild_permissions.administrator = True
    interaction.guild.owner_id = user_id + 1
    interaction.guild_id = 2001
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


def _non_admin_interaction(user_id: int = 42) -> MagicMock:
    """Interaction whose caller is a live non-admin: no administrator, no
    manage_guild, and not the guild owner — the demoted-admin shape."""
    interaction = MagicMock()
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = user_id
    interaction.user.guild_permissions.administrator = False
    interaction.user.guild_permissions.manage_guild = False
    interaction.guild.owner_id = user_id + 1
    interaction.guild_id = 2001
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


def _container_text(container: discord.ui.Container[discord.ui.LayoutView]) -> str:
    """Collect all TextDisplay content from a V2 Container (depth-first)."""
    parts: list[str] = []
    for child in container.children:
        if isinstance(child, discord.ui.TextDisplay):
            parts.append(child.content)
        elif isinstance(child, discord.ui.Section):
            for section_child in child.children:
                if isinstance(section_child, discord.ui.TextDisplay):
                    parts.append(section_child.content)
    return "\n".join(parts)


def _walk_buttons(
    view: discord.ui.LayoutView,
) -> list[discord.ui.Button[AgentSetupView]]:
    """Collect all Button children from a LayoutView, walking Container→ActionRow."""
    buttons: list[discord.ui.Button[AgentSetupView]] = []
    for child in view.children:
        if isinstance(child, discord.ui.Button):
            buttons.append(child)  # type: ignore[arg-type]
        elif isinstance(child, discord.ui.Container):
            for grandchild in child.children:
                if isinstance(grandchild, discord.ui.Button):
                    buttons.append(grandchild)  # type: ignore[arg-type]
                elif isinstance(grandchild, discord.ui.ActionRow):
                    for item in grandchild.children:
                        if isinstance(item, discord.ui.Button):
                            buttons.append(item)  # type: ignore[arg-type]
    return buttons


def _walk_selects(
    view: discord.ui.LayoutView,
) -> list[discord.ui.Select[AgentSetupView]]:
    """Collect all Select children from a LayoutView, walking Container→ActionRow."""
    selects: list[discord.ui.Select[AgentSetupView]] = []
    for child in view.children:
        if isinstance(child, discord.ui.Select):
            selects.append(child)  # type: ignore[arg-type]
        elif isinstance(child, discord.ui.Container):
            for grandchild in child.children:
                if isinstance(grandchild, discord.ui.Select):
                    selects.append(grandchild)  # type: ignore[arg-type]
                elif isinstance(grandchild, discord.ui.ActionRow):
                    for item in grandchild.children:
                        if isinstance(item, discord.ui.Select):
                            selects.append(item)  # type: ignore[arg-type]
    return selects


def _find_button(view: discord.ui.LayoutView, label: str) -> discord.ui.Button[AgentSetupView]:
    for btn in _walk_buttons(view):
        if btn.label == label:
            return btn
    raise AssertionError(f"No button labeled {label!r}")


def _find_select(view: discord.ui.LayoutView) -> discord.ui.Select[AgentSetupView]:
    selects = _walk_selects(view)
    if selects:
        return selects[0]
    raise AssertionError("No Select component on view")


# ---------------------------------------------------------------------------
# F5 container: header and thumbnail
# ---------------------------------------------------------------------------


def test_build_panel_container_header_contains_agent_name(account_id: uuid.UUID) -> None:
    """Header section must show the agent name prefixed with 🤖."""
    selected = _entry("research-bot")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    assert "research-bot" in text, "container text must include the agent name"
    assert "🤖" in text, "container header must be prefixed with 🤖"


def test_build_panel_container_with_thumbnail_uses_section(account_id: uuid.UUID) -> None:
    """When thumbnail_url is given, the header is wrapped in a Section with a Thumbnail."""
    selected = _entry("bot")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    container = build_panel_container(state, thumbnail_url="https://example.com/avatar.png")
    sections = [c for c in container.children if isinstance(c, discord.ui.Section)]
    assert len(sections) == 1, "thumbnail_url present → exactly one Section in container"
    assert isinstance(sections[0].accessory, discord.ui.Thumbnail), (
        "Section accessory must be a Thumbnail when thumbnail_url is given"
    )


def test_build_panel_container_without_thumbnail_uses_text_display(account_id: uuid.UUID) -> None:
    """When thumbnail_url is None, the header is a bare TextDisplay (no Section needed)."""
    selected = _entry("bot")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    container = build_panel_container(state, thumbnail_url=None)
    sections = [c for c in container.children if isinstance(c, discord.ui.Section)]
    assert len(sections) == 0, "thumbnail_url=None → no Section in container (bare TextDisplay)"
    text_displays = [c for c in container.children if isinstance(c, discord.ui.TextDisplay)]
    assert len(text_displays) >= 1, "header must be a TextDisplay when no thumbnail_url"
    assert "🤖 bot" in text_displays[0].content, "first TextDisplay must contain the agent name"


def test_build_panel_container_vitals_contains_model(account_id: uuid.UUID) -> None:
    """The header subtext (vitals line) must contain the agent's model."""
    selected = _entry("bot", model="claude-opus-4-8")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    assert "claude-opus-4-8" in text, "vitals subtext must include the agent model"


# ---------------------------------------------------------------------------
# F5 container: body text — resources
# ---------------------------------------------------------------------------


def _mcp(name: str, url: str) -> Any:
    from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
        BetaManagedAgentsURLMCPServerParams,
    )

    return BetaManagedAgentsURLMCPServerParams(name=name, type="url", url=url)


def _entry_with(
    name: str, *, skills: list[Any] | None = None, mcps: list[Any] | None = None
) -> RosterEntry:
    from anthropic.types.beta.agent_create_params import Tool
    from anthropic.types.beta.beta_managed_agents_mcp_toolset_params import (
        BetaManagedAgentsMCPToolsetParams,
    )

    mcp_list = mcps or []
    # AgentSpec requires each mcp_server to have a matching mcp_toolset tool entry.
    tools: list[Tool] = [
        BetaManagedAgentsMCPToolsetParams(
            type="mcp_toolset",
            mcp_server_name=m.get("name", ""),
            default_config={"permission_policy": {"type": "always_allow"}},
        )
        for m in mcp_list
    ]
    spec = AgentSpec(
        name=name,
        model="claude-sonnet-4-6",
        skills=skills or [],
        mcp_servers=mcp_list if mcp_list else None,
        tools=tools,
    )
    return RosterEntry(name=name, model="claude-sonnet-4-6", spec=spec)


def test_body_text_shows_skills_group_when_skills_present(account_id: uuid.UUID) -> None:
    """When the agent has skills, body shows the 🧩 Skills group with skill IDs."""
    from daimon.core.specs import SkillRef

    selected = _entry_with("bot", skills=[SkillRef(type="custom", skill_id="mySkill")])
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    assert "🧩" in text, "body must contain 🧩 Skills group when skills present"
    assert "mySkill" in text, "skill_id must appear in body text"


def test_body_text_shows_mcp_group_when_user_mcps_present(account_id: uuid.UUID) -> None:
    """When the agent has non-default MCP servers, body shows the 🔌 MCPs group."""
    selected = _entry_with("bot", mcps=[_mcp("test-mcp", "https://example.com/mcp")])
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    assert "🔌" in text, "body must contain 🔌 MCPs group when user MCPs present"
    assert "test-mcp" in text, "MCP name must appear in body text"
    assert "https://example.com/mcp" in text, "MCP URL must appear in body text"


def test_body_text_filters_default_mcp_from_mcp_group(account_id: uuid.UUID) -> None:
    """Default-MCP entries (matching default_mcp_url) must be filtered from the body."""
    selected = _entry_with(
        "bot",
        mcps=[
            _mcp("daimon-mcp", "https://default.example/mcp"),
            _mcp("test-mcp", "https://example.com/mcp"),
        ],
    )
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        default_mcp_url="https://default.example/mcp",
    )
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    assert "daimon-mcp" not in text, "default MCP must be filtered from body text"
    assert "test-mcp" in text, "user-added MCP must remain in body text"


def test_body_text_shows_repo_auth_group_when_repo_set(account_id: uuid.UUID) -> None:
    """When a repo is bound, body shows the 📦 Repo group with URL."""
    selected = _entry_with("bot")
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        bound_repo_url="https://github.com/me/repo",
        bound_branch="main",
    )
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    assert "📦 **Repo**" in text, "body must contain 📦 Repo group when repo is set"
    assert "github.com/me/repo" in text, "repo URL must appear in body text"
    assert "🔑 **Env**" not in text, "no secrets configured — 🔑 Env group must be absent"


def test_body_text_shows_pat_last4_masked(account_id: uuid.UUID) -> None:
    """A transient pat_last4 must appear masked (••••XXXX) in the body text."""
    selected = _entry_with("bot")
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        pat_last4="7890",
        fallback_pat_configured=True,
    )
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    assert "••••7890" in text, "pat_last4 must render as ••••7890 in body text"
    assert "shared service account" not in text, (
        "a masked PAT must suppress the shared-service-account line"
    )


def test_body_text_shows_github_login_when_hydrated(account_id: uuid.UUID) -> None:
    """A hydrated GitHub login must appear in the body text."""
    selected = _entry_with("bot")
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        github_login="octocat",
        fallback_pat_configured=True,
    )
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    assert "@octocat" in text, "hydrated GitHub login must render as @handle in body"
    assert "shared service account" not in text, (
        "a linked GitHub login must suppress the shared-service-account line"
    )


def test_body_text_labels_inline_pat_as_pat_not_handle(account_id: uuid.UUID) -> None:
    """The sentinel login '(inline-pat)' must render as 'PAT', not '@(inline-pat)'."""
    selected = _entry_with("bot")
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        github_login="(inline-pat)",
    )
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    assert "PAT" in text, "inline-pat credential must render as 'PAT'"
    assert "inline-pat" not in text, "the raw sentinel login must not leak into body"


def test_body_text_shows_secret_count_when_secrets_present(account_id: uuid.UUID) -> None:
    """When secret_count > 0, body shows a separate 🔑 Env group with the count —
    it must never fold back into the 📦 Repo line, even when both groups render."""
    selected = _entry_with("bot")
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        bound_repo_url="https://github.com/me/repo",
        bound_branch="main",
        secret_count=3,
    )
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    assert "🔑 **Env**" in text, "body must show a separate 🔑 Env group when secrets are present"
    assert "3 variables" in text, "body must show the secret count as N variables"
    repo_line = text.split("📦 **Repo**")[1].split("🔑 **Env**")[0]
    assert "3" not in repo_line, "the secret count must never appear inside the 📦 Repo line"


def test_body_text_hint_line_when_all_empty(account_id: uuid.UUID) -> None:
    """When no resources are configured, body shows a single '-# ＋ ...' hint line."""
    selected = _entry_with("bot")
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        secret_count=0,
    )
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    # Empty panel → hint replaces all groups (no group headers)
    assert "🧩" not in text, "empty agent must NOT show 🧩 Skills header"
    assert "🔌" not in text, "empty agent must NOT show 🔌 MCPs header"
    assert "📦" not in text, "empty agent must NOT show 📦 Repo header"
    assert "Edit" in text, "hint line must mention **Edit**"


def test_body_text_shows_env_group_without_repo_group(account_id: uuid.UUID) -> None:
    """Secrets present but nothing repo/auth-related bound: 🔑 Env renders on its
    own, with no 📦 Repo group and no fallback-hint replacing it."""
    selected = _entry_with("bot")
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        secret_count=2,
    )
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    assert "🔑 **Env**" in text, "🔑 Env group must render when secrets are present"
    assert "2 variables" in text, "body must show the secret count as N variables"
    assert "📦" not in text, "no repo/auth bound — 📦 Repo group must be absent"


def test_body_text_missing_hint_names_env_vars(account_id: uuid.UUID) -> None:
    """The missing-resource hint names 'keys', not 'secrets'."""
    selected = _entry_with("bot")
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        bound_repo_url="https://github.com/me/repo",
        bound_branch="main",
    )
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    assert "＋ keys" in text, "missing-resource hint must say ＋ keys"
    assert "＋ secrets" not in text, "the hint must not still say ＋ secrets"


def test_body_text_no_none_literal_in_empty_fields(account_id: uuid.UUID) -> None:
    """Body must never render the string '(none)' for empty resources."""
    selected = _entry_with("bot")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    assert "(none)" not in text, "empty resources must use hint line, never '(none)'"


def test_body_text_last_sync_error_appears_in_repo_group(account_id: uuid.UUID) -> None:
    """When last_sync_error is set, a warning line appears in the 📦 Repo group."""
    selected = _entry("my-agent")
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        bound_repo_url="https://github.com/owner/repo",
        bound_branch="main",
        last_sync_error="rate limit exceeded",
    )
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    assert "last sync failed" in text, (
        "body must contain a warning line when last_sync_error is set"
    )
    assert "rate limit exceeded" in text, "body must include the actual error message"


def test_body_text_last_sync_error_absent_when_none(account_id: uuid.UUID) -> None:
    """When last_sync_error is None, no warning line appears."""
    selected = _entry("my-agent")
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        bound_repo_url="https://github.com/owner/repo",
        bound_branch="main",
        last_sync_error=None,
    )
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    assert "last sync failed" not in text, (
        "body must NOT contain a warning line when last_sync_error is None"
    )
    assert "⚠️" not in text, "no warning emoji must appear when last_sync_error is None"


def test_body_text_anon_binding_warns_without_fallback(account_id: uuid.UUID) -> None:
    """An anon: binding with no operator fallback PAT shows 'won't clone — no token'."""
    selected = _entry("my-agent")
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        bound_repo_url="https://github.com/owner/repo",
        bound_branch="main",
        bound_secret_ref="anon:",
        fallback_pat_configured=False,
    )
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    assert "won't clone — no token" in text, (
        "anon: binding with no fallback PAT must warn it won't clone"
    )
    assert "shared service account" not in text, (
        "no fallback PAT configured must NOT show the shared-service-account line"
    )


def test_body_text_anon_binding_no_warning_with_fallback(account_id: uuid.UUID) -> None:
    """An anon: binding clones via the operator fallback PAT — no warning, and the
    shared-service-account line renders instead."""
    selected = _entry("my-agent")
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        bound_repo_url="https://github.com/owner/repo",
        bound_branch="main",
        bound_secret_ref="anon:",
        fallback_pat_configured=True,
    )
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    assert "won't clone" not in text, (
        "anon: binding must NOT warn when the operator fallback PAT is configured"
    )
    assert "shared service account" in text, (
        "an anon: binding backed by the fallback PAT must show the shared-service-account line"
    )


def test_body_text_shows_shared_service_account_line_with_no_repo_bound(
    account_id: uuid.UUID,
) -> None:
    """With the fallback PAT configured but nothing bound yet, the 📦 Repo group
    renders with just the shared-service-account line — not the 'unconfigured' hint."""
    selected = _entry("my-agent")
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        fallback_pat_configured=True,
    )
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    assert "📦 **Repo**" in text, "fallback PAT configured must show the 📦 Repo group even unbound"
    assert "shared service account" in text, (
        "fallback PAT configured with nothing bound must show the shared-service-account line"
    )
    assert "won't clone" not in text, "no repo is bound, so the clone warning must not appear"
    assert "🔑 **Env**" not in text, "no secrets configured — 🔑 Env group must be absent"


def test_body_text_inline_pat_binding_never_warns(account_id: uuid.UUID) -> None:
    """An inline-pat: binding always carries a per-agent PAT — never warns."""
    selected = _entry("my-agent")
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        bound_repo_url="https://github.com/owner/repo",
        bound_branch="main",
        bound_secret_ref="inline-pat:abc",
        fallback_pat_configured=False,
    )
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    assert "won't clone" not in text, "inline-pat: binding must never warn (per-agent PAT present)"


def test_body_text_unbound_agent_has_no_repo_group(account_id: uuid.UUID) -> None:
    """An unbound agent shows no 📦 Repo group and no clone warning."""
    selected = _entry("my-agent")
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        bound_secret_ref=None,
        fallback_pat_configured=False,
    )
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    assert "📦" not in text, "unbound agent must not show the 📦 Repo group"
    assert "won't clone" not in text, "unbound agent must not show a clone warning"


# ---------------------------------------------------------------------------
# F5 container: empty roster
# ---------------------------------------------------------------------------


def test_empty_roster_container_shows_no_agents_copy_for_admin(account_id: uuid.UUID) -> None:
    """Empty roster admin copy: hints at New button."""
    state = PanelState.initial(
        roster=[], account_id=account_id, platform_principal_id=uuid.uuid4(), is_admin=True
    )
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    assert "New" in text, "empty-roster admin copy must mention New"


def test_empty_roster_container_shows_new_copy_for_member_too(account_id: uuid.UUID) -> None:
    """Empty roster member copy: every member can create, so the copy invites
    creating the first agent — no 'View only' variant on an empty roster."""
    state = PanelState.initial(
        roster=[], account_id=account_id, platform_principal_id=uuid.uuid4(), is_admin=False
    )
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    assert "New" in text, "empty-roster member copy must mention New, same as the admin copy"
    assert "View only" not in text, (
        "the empty-roster branch must not show the view-only variant to anyone"
    )


# ---------------------------------------------------------------------------
# Picker select: options, defaults, cap
# ---------------------------------------------------------------------------


def test_picker_has_one_option_per_roster_entry(account_id: uuid.UUID) -> None:
    """Picker must show one option per roster entry (up to 25)."""
    roster = [_entry("a"), _entry("b"), _entry("c")]
    state = PanelState.initial(
        roster=roster, account_id=account_id, platform_principal_id=uuid.uuid4(), is_admin=True
    )
    view = AgentSetupView(state, runtime=_make_runtime(), allowed_user_id=42)
    picker = _find_select(view)
    assert len(picker.options) == 3, "picker must show one option per roster entry"


def test_picker_marks_selected_as_default(account_id: uuid.UUID) -> None:
    """The currently selected agent must be the default-selected option in the picker."""
    roster = [_entry("a"), _entry("b"), _entry("c")]
    state = PanelState.initial(
        roster=roster, account_id=account_id, platform_principal_id=uuid.uuid4(), is_admin=True
    )
    view = AgentSetupView(state, runtime=_make_runtime(), allowed_user_id=42)
    picker = _find_select(view)
    defaulted = [o for o in picker.options if o.default]
    assert len(defaulted) == 1 and defaulted[0].value == "a", (
        "the first roster entry must be the default-selected option"
    )


def test_picker_caps_at_25_options(account_id: uuid.UUID) -> None:
    """Discord StringSelect hard limit is 25 options."""
    roster = [_entry(f"agent-{i:02d}") for i in range(30)]
    state = PanelState.initial(
        roster=roster, account_id=account_id, platform_principal_id=uuid.uuid4()
    )
    view = AgentSetupView(state, runtime=_make_runtime(), allowed_user_id=42)
    picker = _find_select(view)
    assert len(picker.options) <= 25, (
        "Discord StringSelect hard limit is 25 options — picker must truncate"
    )


def test_picker_description_contains_model_and_counts(account_id: uuid.UUID) -> None:
    """Picker option description must show model · N skills · M MCP."""
    from daimon.core.specs import SkillRef

    selected = _entry_with(
        "bot",
        skills=[SkillRef(type="custom", skill_id="mySkill")],
        mcps=[_mcp("test-mcp", "https://example.com/mcp")],
    )
    state = PanelState.initial(
        roster=[selected], account_id=account_id, platform_principal_id=uuid.uuid4()
    )
    view = AgentSetupView(state, runtime=_make_runtime(), allowed_user_id=42)
    picker = _find_select(view)
    option = picker.options[0]
    assert option.description is not None, "picker option must have a description"
    assert "claude-sonnet-4-6" in option.description, "description must include the model"
    assert "skills" in option.description, "description must mention skill count"
    assert "MCP" in option.description, "description must mention MCP count"


def test_picker_system_agent_gets_lock_emoji(account_id: uuid.UUID) -> None:
    """System agents get the 🔒 emoji in the picker."""
    seeded = RosterEntry(
        name="daimon",
        model="claude-sonnet-4-6",
        spec=AgentSpec(name="daimon", model="claude-sonnet-4-6"),
        is_system=True,
    )
    state = PanelState.initial(
        roster=[seeded], account_id=account_id, platform_principal_id=uuid.uuid4()
    )
    view = AgentSetupView(state, runtime=_make_runtime(), allowed_user_id=42)
    picker = _find_select(view)
    option = picker.options[0]
    assert option.emoji is not None and str(option.emoji) == "🔒", (
        "system agent must use 🔒 emoji in picker"
    )


# ---------------------------------------------------------------------------
# Lifecycle buttons: admin gating and disabled states
# ---------------------------------------------------------------------------


def test_initial_render_admin_enables_buttons(account_id: uuid.UUID) -> None:
    """Admin with a selection: Edit, Delete enabled; New always enabled."""
    roster = [_entry("a"), _entry("b"), _entry("c")]
    state = PanelState.initial(
        roster=roster, account_id=account_id, platform_principal_id=uuid.uuid4(), is_admin=True
    )
    view = AgentSetupView(state, runtime=_make_runtime(), allowed_user_id=42)

    edit_btn = _find_button(view, "Edit")
    assert edit_btn.disabled is False, "Edit must be enabled when a user-editable entry is selected"
    delete_btn = _find_button(view, "Delete")
    assert delete_btn.disabled is False, "Delete must be enabled when an agent is selected"
    new_btn = _find_button(view, "New")
    assert new_btn.disabled is False, "New must always be enabled"


def test_empty_roster_disables_edit_and_delete(account_id: uuid.UUID) -> None:
    """Admin with empty roster: Edit and Delete disabled; New still enabled."""
    state = PanelState.initial(
        roster=[], account_id=account_id, platform_principal_id=uuid.uuid4(), is_admin=True
    )
    view = AgentSetupView(state, runtime=_make_runtime(), allowed_user_id=42)

    for label in ("Edit", "Delete"):
        btn = _find_button(view, label)
        assert btn.disabled is True, (
            f"{label} must be disabled on empty roster — only New is enabled"
        )
    new_btn = _find_button(view, "New")
    assert new_btn.disabled is False, "New must remain enabled on empty roster"


def test_seeded_agent_enables_edit_disables_delete(account_id: uuid.UUID) -> None:
    """A system agent's Edit is enabled (it is now the only door to its env
    vars); Delete stays disabled; Fork remains enabled."""
    selected = RosterEntry(
        name="daimon",
        model="claude-sonnet-4-6",
        spec=AgentSpec(name="daimon", model="claude-sonnet-4-6"),
        is_system=True,
    )
    state = PanelState(roster=[selected], selected=selected, account_id=account_id, is_admin=True)
    view = AgentSetupView(state, runtime=_make_runtime(), allowed_user_id=42)

    edit_btn = _find_button(view, "Edit")
    assert edit_btn.disabled is False, "Edit must be enabled on a system agent"
    delete_btn = _find_button(view, "Delete")
    assert delete_btn.disabled is True, "Delete must stay disabled on a system agent"
    fork_btn = _find_button(view, "Fork")
    assert fork_btn.disabled is False, "Fork must remain enabled (escape hatch)"


def test_edit_btn_disabled_when_no_selection(account_id: uuid.UUID) -> None:
    """Edit must be disabled when no agent is selected."""
    state = PanelState(
        roster=[],
        selected=None,
        account_id=account_id,
        platform_principal_id=uuid.uuid4(),
        is_admin=True,
    )
    view = AgentSetupView(state, runtime=_make_runtime(), allowed_user_id=42)
    btn = _find_button(view, "Edit")
    assert btn.disabled is True, "Edit must be disabled when no agent selected"


def test_admin_view_has_set_default_button(account_id: uuid.UUID) -> None:
    """Admin panel has 'Set as default…' button; disabled with no selection, enabled with one."""
    state = PanelState.initial(
        roster=[],
        account_id=account_id,
        platform_principal_id=uuid.uuid4(),
        is_admin=True,
    )
    view = AgentSetupView(state, runtime=_make_runtime(), allowed_user_id=42)
    btn = _find_button(view, "Set as default…")
    assert btn.disabled is True, "'Set as default…' must be disabled when no agent selected"

    state2 = PanelState.initial(
        roster=[_entry("alice")],
        account_id=account_id,
        platform_principal_id=uuid.uuid4(),
        is_admin=True,
    )
    view2 = AgentSetupView(state2, runtime=_make_runtime(), allowed_user_id=42)
    btn2 = _find_button(view2, "Set as default…")
    assert btn2.disabled is False, "'Set as default…' must be enabled when an agent is selected"


# ---------------------------------------------------------------------------
# Member gating
# ---------------------------------------------------------------------------


def test_read_only_view_shows_build_buttons_hides_admin_buttons(account_id: uuid.UUID) -> None:
    """Member (is_admin=False) panel has New/Fork/Edit (build controls) but NO
    Set as default…/Delete/Connect via MCP (reachability + destructive controls);
    picker IS present."""
    from daimon.core.scope import ChannelConfigRow, TenantConfigRow

    ch_row = ChannelConfigRow(agent_name="alice", channel_id="1001", tenant_id=uuid.UUID(int=0))
    state = PanelState.initial(
        roster=[_entry("alice")],
        account_id=account_id,
        platform_principal_id=uuid.uuid4(),
        is_admin=False,
        guild_id=2001,
        channel_id=1001,
        cascade_view=(TenantConfigRow(agent_name="bob", tenant_id=uuid.UUID(int=0)), [ch_row]),
    )
    view = AgentSetupView(state, runtime=_make_runtime(), allowed_user_id=42)

    all_buttons = _walk_buttons(view)
    btn_labels = [b.label for b in all_buttons if b.label is not None]
    for build_label in ("New", "Fork", "Edit"):
        assert build_label in btn_labels, (
            f"member view must contain {build_label!r} button — build/configure is unconditional"
        )
    for admin_label in ("Set as default…", "Delete", "🔌 Connect via MCP"):
        assert admin_label not in btn_labels, (
            f"member view must not contain {admin_label!r} button (admin-only)"
        )
    selects = _walk_selects(view)
    assert len(selects) == 1, "member view must still have the agent picker"


def test_read_only_body_contains_view_only_line(account_id: uuid.UUID) -> None:
    """Member panel body must contain the 'View only — ask an admin' line."""
    state = PanelState.initial(
        roster=[_entry("alice")],
        account_id=account_id,
        platform_principal_id=uuid.uuid4(),
        is_admin=False,
    )
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    assert "View only — ask an admin to change defaults" in text, (
        "member body must contain the view-only line"
    )


def test_read_only_body_shows_cascade_ladder(account_id: uuid.UUID) -> None:
    """Member body shows '← in effect here' cascade marker."""
    from daimon.core.scope import ChannelConfigRow, TenantConfigRow

    channel_id = 1001
    ch_row = ChannelConfigRow(
        agent_name="alice", channel_id=str(channel_id), tenant_id=uuid.UUID(int=0)
    )
    tenant_row = TenantConfigRow(agent_name="bob", tenant_id=uuid.UUID(int=0))

    state = PanelState.initial(
        roster=[_entry("alice")],
        account_id=account_id,
        platform_principal_id=uuid.uuid4(),
        is_admin=False,
        guild_id=2001,
        channel_id=channel_id,
        cascade_view=(tenant_row, [ch_row]),
    )
    container = build_panel_container(state, thumbnail_url=None)
    text = _container_text(container)
    assert "← in effect here" in text, (
        "member body must show the cascade ladder with the winning tier marked"
    )


def test_member_roster_matches_admin_and_hides_only_admin_buttons(account_id: uuid.UUID) -> None:
    """SC-4: member view shows same roster as admin; build buttons are shared,
    only the admin-only row (Set as default…/Delete/Connect via MCP) differs."""
    roster = [_entry("alice"), _entry("bob"), _entry("charlie")]

    admin_state = PanelState.initial(
        roster=roster,
        account_id=account_id,
        platform_principal_id=uuid.uuid4(),
        is_admin=True,
    )
    member_state = PanelState.initial(
        roster=roster,
        account_id=account_id,
        platform_principal_id=uuid.uuid4(),
        is_admin=False,
    )

    admin_view = AgentSetupView(admin_state, runtime=_make_runtime(), allowed_user_id=42)
    member_view = AgentSetupView(member_state, runtime=_make_runtime(), allowed_user_id=42)

    admin_picker = _find_select(admin_view)
    member_picker = _find_select(member_view)

    admin_options = [(o.label, o.value) for o in admin_picker.options]
    member_options = [(o.label, o.value) for o in member_picker.options]
    assert admin_options == member_options, (
        "SC-4: member picker must list exactly the same roster entries as admin picker"
    )

    member_buttons = _walk_buttons(member_view)
    member_btn_labels = [b.label for b in member_buttons if b.label is not None]
    for build_label in ("New", "Fork", "Edit"):
        assert build_label in member_btn_labels, (
            f"SC-4: member view must contain {build_label!r} button"
        )
    for admin_label in ("Delete", "Set as default…", "🔌 Connect via MCP"):
        assert admin_label not in member_btn_labels, (
            f"SC-4: member view must not contain {admin_label!r} button"
        )

    member_selects = _walk_selects(member_view)
    assert len(member_selects) == 1, (
        "SC-4: member view must have exactly one component — the agent picker Select"
    )


# ---------------------------------------------------------------------------
# PanelState.is_selected_reachable
# ---------------------------------------------------------------------------


def test_is_selected_reachable_false_when_nothing_selected(account_id: uuid.UUID) -> None:
    """No selection → not reachable (nothing to be reachable)."""
    from daimon.core.scope import DeploymentDefault

    state = PanelState(
        roster=[],
        selected=None,
        account_id=account_id,
        deployment_default=DeploymentDefault(agent_name="daimon"),
    )
    assert state.is_selected_reachable() is False, "no selection must never be reachable"


def test_is_selected_reachable_true_via_deployment_default(account_id: uuid.UUID) -> None:
    """Fresh install, no config rows: the deployment default's agent is reachable."""
    from daimon.core.scope import DeploymentDefault

    selected = _entry("daimon")
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        cascade_view=(None, []),
        deployment_default=DeploymentDefault(agent_name="daimon"),
    )
    assert state.is_selected_reachable() is True, (
        "the deployment default's agent must be reachable on a fresh install"
    )


def test_is_selected_reachable_false_when_deployment_default_names_other_agent(
    account_id: uuid.UUID,
) -> None:
    """Fresh install, no config rows: an agent the deployment default does NOT
    name is not reachable."""
    from daimon.core.scope import DeploymentDefault

    selected = _entry("research-bot")
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        cascade_view=(None, []),
        deployment_default=DeploymentDefault(agent_name="daimon"),
    )
    assert state.is_selected_reachable() is False, (
        "an agent not named by the deployment default must not be reachable"
    )


def test_is_selected_reachable_true_via_channel_row(account_id: uuid.UUID) -> None:
    """A channel row in mode='agent' naming the selected agent makes it reachable."""
    from daimon.core.scope import ChannelConfigRow, DeploymentDefault

    selected = _entry("alice")
    ch_row = ChannelConfigRow(
        tenant_id=uuid.UUID(int=0), channel_id="1001", agent_name="alice", mode="agent"
    )
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        cascade_view=(None, [ch_row]),
        deployment_default=DeploymentDefault(agent_name="daimon"),
    )
    assert state.is_selected_reachable() is True, (
        "a channel row naming the selected agent must make it reachable"
    )


def test_is_selected_reachable_false_when_tenant_row_names_other_agent(
    account_id: uuid.UUID,
) -> None:
    """A tenant row in mode='agent' naming a different agent suppresses the
    deployment-default fall-through for the selected (unnamed) agent."""
    from daimon.core.scope import DeploymentDefault, TenantConfigRow

    selected = _entry("daimon")
    tenant_row = TenantConfigRow(tenant_id=uuid.UUID(int=0), agent_name="bob", mode="agent")
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        cascade_view=(tenant_row, []),
        deployment_default=DeploymentDefault(agent_name="daimon"),
    )
    assert state.is_selected_reachable() is False, (
        "a tenant row naming a different agent must suppress the deployment "
        "default's reachability for the selected agent"
    )


# ---------------------------------------------------------------------------
# interaction_check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interaction_check_rejects_wrong_user(account_id: uuid.UUID) -> None:
    state = PanelState.initial(
        roster=[_entry("a")], account_id=account_id, platform_principal_id=uuid.uuid4()
    )
    view = AgentSetupView(state, runtime=_make_runtime(), allowed_user_id=42)
    interaction = MagicMock()
    interaction.user.id = 999
    interaction.response.send_message = AsyncMock()
    ok = await view.interaction_check(interaction)
    assert ok is False, "non-invoker must be rejected by the view's interaction_check"
    interaction.response.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_interaction_check_allows_invoker(account_id: uuid.UUID) -> None:
    state = PanelState.initial(
        roster=[_entry("a")], account_id=account_id, platform_principal_id=uuid.uuid4()
    )
    view = AgentSetupView(state, runtime=_make_runtime(), allowed_user_id=42)
    interaction = MagicMock()
    interaction.user.id = 42
    interaction.response.send_message = AsyncMock()
    ok = await view.interaction_check(interaction)
    assert ok is True, "invoker must pass the view's interaction_check"


@pytest.mark.asyncio
async def test_admin_view_invoker_match_still_guards(account_id: uuid.UUID) -> None:
    """Non-invoker interaction still fails even on admin panel."""
    state = PanelState.initial(
        roster=[_entry("alice")],
        account_id=account_id,
        platform_principal_id=uuid.uuid4(),
        is_admin=True,
    )
    view = AgentSetupView(state, runtime=_make_runtime(), allowed_user_id=42)
    interaction = MagicMock()
    interaction.user.id = 999
    interaction.response.send_message = AsyncMock()

    ok = await view.interaction_check(interaction)
    assert ok is False, "non-invoker must be rejected even on admin panel"
    interaction.response.send_message.assert_called_once()
    args, _kwargs = interaction.response.send_message.call_args
    assert "Only the command invoker" in args[0], (
        "rejection message must identify the invoker-match guard"
    )


# ---------------------------------------------------------------------------
# WR-02: failure routing through render_error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_back_button_routes_failure_through_render_error(
    account_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed back-button rerender surfaces a render_error message (with rid), not the raw type."""
    import daimon.adapters.discord.agent_setup.panel as panel_mod
    from daimon.adapters.discord.agent_setup.edit_view import BackButton

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(panel_mod, "rerender_root_panel", _boom)

    selected = _entry("alice")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    button = BackButton(state=state, runtime=_make_runtime(), allowed_user_id=42)
    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.delete_original_response = AsyncMock()
    interaction.followup.send = AsyncMock()

    await button.callback(interaction)

    interaction.followup.send.assert_called_once()
    message = interaction.followup.send.call_args.args[0]
    assert "rid:" in message, "the failure message must carry a request id via render_error"
    assert "RuntimeError" not in message, "the raw exception type must not leak to the user"
    interaction.delete_original_response.assert_not_called()


@pytest.mark.asyncio
async def test_back_button_edits_root_panel_in_place(
    account_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: Back calls rerender_root_panel once and never deletes the message."""
    import daimon.adapters.discord.agent_setup.panel as panel_mod
    from daimon.adapters.discord.agent_setup.edit_view import BackButton

    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def _fake_rerender(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(panel_mod, "rerender_root_panel", _fake_rerender)

    selected = _entry("alice")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    button = BackButton(state=state, runtime=_make_runtime(), allowed_user_id=42)
    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.delete_original_response = AsyncMock()
    interaction.followup.send = AsyncMock()

    await button.callback(interaction)

    assert len(calls) == 1, "rerender_root_panel must be called exactly once"
    interaction.delete_original_response.assert_not_called()


@pytest.mark.asyncio
async def test_rerender_root_panel_hydrates_before_edit(
    account_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rerender_root_panel re-fetches secret_count/github_login before editing."""
    import daimon.adapters.discord.agent_setup.panel as panel_mod
    from daimon.core.stores.domain import AgentRepoBindingRow

    selected = _entry("alice")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    state.secret_count = 0
    state.github_login = None

    async def _fake_secret_count(*_args: Any, **_kwargs: Any) -> int:
        return 7

    async def _fake_github_login(*_args: Any, **_kwargs: Any) -> str | None:
        return "octocat"

    async def _fake_repo_binding(*_args: Any, **_kwargs: Any) -> AgentRepoBindingRow | None:
        return None

    monkeypatch.setattr(panel_mod, "load_secret_count", _fake_secret_count)
    monkeypatch.setattr(panel_mod, "load_selected_github_login", _fake_github_login)
    monkeypatch.setattr(panel_mod, "load_repo_binding", _fake_repo_binding)

    interaction = MagicMock()
    interaction.edit_original_response = AsyncMock()

    await panel_mod.rerender_root_panel(
        interaction, state, runtime=_make_runtime_with_settings(), allowed_user_id=42
    )

    assert state.secret_count == 7, (
        "secret_count must reflect the stubbed loader, not its pre-call value"
    )
    assert state.github_login == "octocat", "github_login must reflect the stubbed loader"
    interaction.edit_original_response.assert_called_once()
    view_kwarg = interaction.edit_original_response.call_args.kwargs["view"]
    assert isinstance(view_kwarg, panel_mod.AgentSetupView), (
        "rerender_root_panel must edit back to the root AgentSetupView"
    )


@pytest.mark.asyncio
async def test_delete_btn_routes_failure_through_render_error(
    account_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed agent delete surfaces a render_error message (with rid), not the raw type."""
    import daimon.adapters.discord.agent_setup.panel as panel_mod

    selected = _entry("alice")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id, is_admin=True)
    view = AgentSetupView(state, runtime=_make_runtime_with_settings(), allowed_user_id=42)

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(panel_mod, "_resolve_tenant", _boom)

    # Live admin: the callback's click-time admin re-check runs before anything
    # else, and this test's subject is the failure path behind it.
    interaction = _admin_interaction()

    await view.delete_btn.callback(interaction)

    interaction.followup.send.assert_called_once()
    message = interaction.followup.send.call_args.args[0]
    assert "rid:" in message, "the failure message must carry a request id via render_error"
    assert "RuntimeError" not in message, "the raw exception type must not leak to the user"


# ---------------------------------------------------------------------------
# EditView cut-over
# ---------------------------------------------------------------------------


def test_edit_view_replaces_old_sub_views_completely() -> None:
    """AT-10: after plan 02, the old sub-view classes are gone from panel.py."""
    from daimon.adapters.discord.agent_setup import panel as panel_mod

    for removed_name in (
        "EditSubView",
        "RepoSubView",
        "SkillsSubView",
        "McpsSubView",
        "_SkillRemoveButton",
        "_McpRemoveButton",
    ):
        assert not hasattr(panel_mod, removed_name), (
            f"{removed_name} should be deleted (its behavior is folded into EditView)"
        )


# ---------------------------------------------------------------------------
# Picker callback: secret count + repo binding refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_picker_switch_refreshes_secret_count(
    account_id: uuid.UUID, mock_interaction: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Switching agents in the picker must re-fetch the selected agent's secret count."""
    from daimon.adapters.discord.agent_setup import panel as panel_mod

    state = PanelState(
        roster=[_entry("alpha"), _entry("bravo")],
        selected=_entry("alpha"),
        account_id=account_id,
        secret_count=0,
    )
    view = AgentSetupView(state, runtime=_make_runtime_with_settings(), allowed_user_id=42)

    seen: list[str] = []

    async def fake_load_secret_count(
        _runtime: object, *, tenant_id: object, agent_name: str
    ) -> int:
        seen.append(agent_name)
        return 7

    monkeypatch.setattr(panel_mod, "load_secret_count", fake_load_secret_count)

    picker = _find_select(view)
    picker._values = ["bravo"]  # pyright: ignore[reportPrivateUsage]
    await picker.callback(mock_interaction)

    assert state.selected is not None and state.selected.name == "bravo", (
        "picker must switch selection"
    )
    assert seen == ["bravo"], "secret count must be re-fetched for the newly selected agent"
    assert state.secret_count == 7, "state.secret_count must update to the switched agent's count"
    mock_interaction.response.edit_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_picker_switch_rehydrates_repo_binding(
    account_id: uuid.UUID, mock_interaction: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Switching agents must re-hydrate the persisted repo binding."""
    from datetime import UTC, datetime

    from daimon.adapters.discord.agent_setup import panel as panel_mod
    from daimon.core.stores.domain import AgentRepoBindingRow

    state = PanelState(
        roster=[_entry("alpha"), _entry("bravo")],
        selected=_entry("alpha"),
        account_id=account_id,
    )
    view = AgentSetupView(state, runtime=_make_runtime_with_settings(), allowed_user_id=42)

    async def fake_load_repo_binding(
        _runtime: object, *, tenant_id: uuid.UUID, entry: RosterEntry | None
    ) -> AgentRepoBindingRow | None:
        if entry is not None and entry.name == "bravo":
            return AgentRepoBindingRow(
                tenant_id=tenant_id,
                agent_id=uuid.uuid4(),
                repo_url="me/bravo",
                default_branch="develop",
                ma_secret_ref="anon:",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        return None

    monkeypatch.setattr(panel_mod, "load_repo_binding", fake_load_repo_binding)
    monkeypatch.setattr(panel_mod, "load_secret_count", AsyncMock(return_value=0))
    monkeypatch.setattr(panel_mod, "load_selected_github_login", AsyncMock(return_value=None))

    picker = _find_select(view)
    picker._values = ["bravo"]  # pyright: ignore[reportPrivateUsage]
    await picker.callback(mock_interaction)

    assert state.bound_repo_url == "https://github.com/me/bravo", (
        "repo binding must be re-hydrated (as a full URL) for the newly selected agent"
    )
    assert state.bound_branch == "develop", "bound branch must follow the hydrated binding"


@pytest.mark.asyncio
async def test_load_repo_binding_reads_persisted_binding(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """load_repo_binding derives the agent uuid from the roster entry's ma_agent_id
    and returns the binding persisted under that key."""
    from daimon.adapters.discord.agent_setup.panel import load_repo_binding
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.stores.agent_repo_binding import set_binding
    from daimon.testing.factories import make_tenant

    _guild = str(uuid.uuid4())
    tenant = await make_tenant(db_session, platform="discord", workspace_id=_guild)

    ma_agent_id = "agent_017abc"
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=ma_agent_id)
    async with db_session_factory.begin() as session:
        await set_binding(
            session,
            tenant_id=tenant.id,
            agent_id=agent_uuid,
            repo_url="https://github.com/me/repo",
            default_branch="develop",
            ma_secret_ref="anon:",
            proof=None,
        )

    runtime = MagicMock(spec=DiscordRuntime)
    runtime.sessionmaker = db_session_factory
    entry = RosterEntry(
        name="bot",
        model="claude-sonnet-4-6",
        spec=AgentSpec(name="bot", model="claude-sonnet-4-6"),
        ma_agent_id=ma_agent_id,
    )

    binding = await load_repo_binding(runtime, tenant_id=tenant.id, entry=entry)

    assert binding is not None, "load_repo_binding must return the persisted binding"
    assert binding.repo_url == "me/repo", "store persists the normalized owner/repo form"
    assert binding.default_branch == "develop", "must read back the bound branch"


# ---------------------------------------------------------------------------
# Cascade ladder (still lives in set_default.py)
# ---------------------------------------------------------------------------


def test_cascade_container_header_is_default_agent(account_id: uuid.UUID) -> None:
    """C9 cascade container header reads '⚙️ Default agent'."""
    from daimon.adapters.discord.agent_setup.set_default import (
        ScopeBlock,
        build_set_default_container,
    )

    blocks = [ScopeBlock(scope_label="#bot-spam", agent_name="alice", audit_line="system default")]
    container = build_set_default_container(blocks)

    text_displays = [
        item for item in container.children if isinstance(item, discord.ui.TextDisplay)
    ]
    header_display = text_displays[0] if text_displays else None
    assert header_display is not None, "container must have a header TextDisplay"
    assert "⚙️ Default agent" in (header_display.content or ""), (
        f"cascade header must contain '⚙️ Default agent'; got {header_display.content!r}"
    )


# ---------------------------------------------------------------------------
# Modal: model validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_modal_rejects_unknown_model(
    monkeypatch: pytest.MonkeyPatch, account_id: uuid.UUID
) -> None:
    """UX-25-03: AgentSectionModal must reject a model not in ALLOWED_MODEL_IDS."""
    from daimon.adapters.discord.agent_setup import modals as modals_mod
    from daimon.adapters.discord.agent_setup.modals import AgentSectionModal

    reconcile_called = False

    async def fake_reconcile(*args: Any, **kwargs: Any) -> Any:
        nonlocal reconcile_called
        reconcile_called = True
        return MagicMock()

    monkeypatch.setattr(modals_mod, "call_reconcile_for_panel", fake_reconcile)

    selected = _entry("a")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None

    modal = AgentSectionModal(state, runtime=runtime, allowed_user_id=42)
    modal.model_in._value = "claude-fake-99"  # pyright: ignore[reportPrivateUsage]
    modal.prompt_in._value = "new prompt"  # pyright: ignore[reportPrivateUsage]

    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.user.id = 42

    await modal.on_submit(interaction)
    interaction.response.send_message.assert_called_once()
    assert not reconcile_called, "reconcile must NOT be called when model is invalid"


# ---------------------------------------------------------------------------
# Sentry capture at broad panel except sites
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_failure_captures_to_sentry_with_tenant_context_and_swallows(
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed delete (after tenant resolution) is captured to Sentry with
    tenant_id + guild_id bound into contextvars, AND the handler still swallows — it
    surfaces the ephemeral render_error and returns rather than re-raising.
    """
    import structlog
    from daimon.adapters.discord.agent_setup import panel as panel_mod

    selected = _entry("alice")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id, is_admin=True)
    view = AgentSetupView(state, runtime=_make_runtime_with_settings(), allowed_user_id=42)

    # _resolve_tenant is stubbed by the autouse fixture to return tenant_id; make the
    # delete itself raise so we exercise the broad except AFTER tenant_id is resolved.
    async def _boom_delete(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("ma delete blew up")

    monkeypatch.setattr(panel_mod, "delete_agent", _boom_delete)

    # Snapshot the contextvars the capture sees, proving tenant attribution at the site.
    seen_scopes: list[dict[str, str | bool | float | int]] = []
    captured: list[BaseException] = []

    def fake_capture(exc: BaseException) -> None:
        seen_scopes.append(dict(structlog.contextvars.get_contextvars()))
        captured.append(exc)

    monkeypatch.setattr(panel_mod, "capture_exception_with_scope", fake_capture)

    # Live admin: the callback's click-time admin re-check runs before the
    # delete, and this test's subject is the capture behind it.
    interaction = _admin_interaction()

    # Must not raise — swallow preserved.
    await view.delete_btn.callback(interaction)

    assert len(captured) == 1, "the broad except site must capture exactly one exception"
    assert isinstance(captured[0], RuntimeError), (
        "captured exception must be the raised RuntimeError"
    )
    assert seen_scopes[0].get("tenant_id") == str(tenant_id), (
        "capture must run with tenant_id bound into contextvars for attribution"
    )
    assert seen_scopes[0].get("guild_id") == "2001", (
        "capture must run with guild_id bound into contextvars for attribution"
    )
    # Swallow preserved: user gets an ephemeral followup, handler returns normally.
    interaction.followup.send.assert_called_once()

    # The binding must not leak past the handler.
    assert "tenant_id" not in structlog.contextvars.get_contextvars(), (
        "tenant_id contextvar must be unbound after the handler returns"
    )
    assert "guild_id" not in structlog.contextvars.get_contextvars(), (
        "guild_id contextvar must be unbound after the handler returns"
    )


def test_daimon_error_fork_site_is_not_captured() -> None:
    """A4: the deliberate `except DaimonError` site (user-facing input error) does NOT
    call capture_exception_with_scope — only the broad except sites do.
    """
    import inspect

    from daimon.adapters.discord.agent_setup import panel as panel_mod

    source = inspect.getsource(panel_mod.ForkAgentModal.on_submit)
    # The DaimonError clause logs at info + sends str(err); no Sentry capture in its body.
    daimon_clause = source.split("except DaimonError")[1].split("except Exception")[0]
    assert "_capture_panel_exception" not in daimon_clause, (
        "the DaimonError site must stay user-facing — never captured to Sentry (A4)"
    )


# ---------------------------------------------------------------------------
# Shared on_timeout (root panel)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_setup_view_timeout_replaces_the_panel(
    account_id: uuid.UUID, mock_interaction: MagicMock
) -> None:
    selected = _entry("alice")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    view = AgentSetupView(state, runtime=_make_runtime(), allowed_user_id=42)
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
    assert view.timeout == 600, "the shared on_timeout mixin leaves timeout values unchanged"


@pytest.mark.asyncio
async def test_superseded_panel_view_does_not_rewrite_the_message(
    account_id: uuid.UUID, mock_interaction: MagicMock
) -> None:
    selected = _entry("alice")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    view_a = AgentSetupView(state, runtime=_make_runtime(), allowed_user_id=42)
    view_a.bind_render_interaction(mock_interaction, panel=state)
    view_b = AgentSetupView(state, runtime=_make_runtime(), allowed_user_id=42)
    view_b.bind_render_interaction(mock_interaction, panel=state)

    await view_a.on_timeout()
    mock_interaction.edit_original_response.assert_not_called()

    await view_b.on_timeout()
    mock_interaction.edit_original_response.assert_called_once()


# ---------------------------------------------------------------------------
# Admin row: click-time admin re-check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_btn_refuses_a_demoted_admin_without_deleting(
    account_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller who was an admin at render time but is not one now deletes nothing."""
    import daimon.adapters.discord.agent_setup.panel as panel_mod

    async def _unexpected_delete(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("delete_agent must not be reached for a non-admin caller")

    monkeypatch.setattr(panel_mod, "delete_agent", _unexpected_delete)

    selected = _entry("alice")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id, is_admin=True)
    view = AgentSetupView(state, runtime=_make_runtime_with_settings(), allowed_user_id=42)
    interaction = _non_admin_interaction()

    await view.delete_btn.callback(interaction)

    interaction.response.send_message.assert_called_once()
    message = interaction.response.send_message.call_args.args[0]
    assert "Manage Server" in message, "the refusal must name the permission the caller lacks"
    # The refusal must own the interaction's first response — no defer precedes it.
    interaction.response.defer.assert_not_called()
    interaction.followup.send.assert_not_called()
    interaction.edit_original_response.assert_not_called()
    assert state.roster == [selected], "a refused delete must leave the roster untouched"


@pytest.mark.asyncio
async def test_delete_btn_refusal_precedes_the_nothing_to_delete_branch(
    account_id: uuid.UUID,
) -> None:
    """With nothing selected AND a non-admin caller, the admin refusal is what is sent.

    Pins the ordering: a non-admin must not learn whether an agent is selected.
    """
    state = PanelState(
        roster=[_entry("alice")], selected=None, account_id=account_id, is_admin=True
    )
    view = AgentSetupView(state, runtime=_make_runtime_with_settings(), allowed_user_id=42)
    interaction = _non_admin_interaction()

    await view.delete_btn.callback(interaction)

    interaction.response.send_message.assert_called_once()
    message = interaction.response.send_message.call_args.args[0]
    assert "Manage Server" in message, "the admin gate must fire before the selection check"
    assert "Nothing to delete." not in message, (
        "a non-admin must not learn whether an agent is selected"
    )


@pytest.mark.asyncio
async def test_delete_btn_still_deletes_for_a_live_admin(
    account_id: uuid.UUID, tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate is not a blanket ban: a live admin's delete still runs end to end."""
    import daimon.adapters.discord.agent_setup.panel as panel_mod
    from daimon.core.stores.domain import AgentRepoBindingRow

    deleted_names: list[str] = []

    async def _spy_delete(*_args: Any, **kwargs: Any) -> None:
        deleted_names.append(str(kwargs["name"]))

    async def _fake_secret_count(*_args: Any, **_kwargs: Any) -> int:
        return 0

    async def _fake_github_login(*_args: Any, **_kwargs: Any) -> str | None:
        return None

    async def _fake_repo_binding(*_args: Any, **_kwargs: Any) -> AgentRepoBindingRow | None:
        return None

    monkeypatch.setattr(panel_mod, "delete_agent", _spy_delete)
    monkeypatch.setattr(panel_mod, "load_secret_count", _fake_secret_count)
    monkeypatch.setattr(panel_mod, "load_selected_github_login", _fake_github_login)
    monkeypatch.setattr(panel_mod, "load_repo_binding", _fake_repo_binding)

    selected = _entry("alice")
    state = PanelState(
        roster=[selected, _entry("bob")],
        selected=selected,
        account_id=account_id,
        is_admin=True,
    )
    view = AgentSetupView(state, runtime=_make_runtime_with_settings(), allowed_user_id=42)
    interaction = _admin_interaction()

    await view.delete_btn.callback(interaction)

    assert deleted_names == ["alice"], "a live admin's delete must still reach delete_agent"
    interaction.response.defer.assert_called_once()
    interaction.followup.send.assert_not_called()
    interaction.edit_original_response.assert_called_once()
    assert "alice" not in {e.name for e in state.roster}, (
        "the deleted agent must be dropped from the roster"
    )
    assert tenant_id is not None, "the autouse tenant stub supplies the id threaded into the delete"


@pytest.mark.asyncio
async def test_connect_via_mcp_refuses_a_demoted_admin_without_minting_a_bearer(
    account_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A demoted admin clicking Connect via MCP is issued no bearer token."""
    import daimon.adapters.discord.agent_setup.mcp_access as mcp_access_mod

    async def _unexpected_connect(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("send_connect_via_mcp must not be reached for a non-admin caller")

    monkeypatch.setattr(mcp_access_mod, "send_connect_via_mcp", _unexpected_connect)

    selected = _entry("alice")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id, is_admin=True)
    view = AgentSetupView(state, runtime=_make_runtime_with_settings(), allowed_user_id=42)
    interaction = _non_admin_interaction()

    await view.connect_mcp_btn.callback(interaction)

    interaction.response.send_message.assert_called_once()
    message = interaction.response.send_message.call_args.args[0]
    assert "Manage Server" in message, "the refusal must name the permission the caller lacks"
    assert "Bearer" not in message, "a refused caller must receive no token material"
    interaction.response.defer.assert_not_called()
    interaction.followup.send.assert_not_called()


@pytest.mark.asyncio
async def test_connect_via_mcp_still_works_for_a_live_admin(
    account_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live admin still reaches the bearer-minting handler unchanged."""
    import daimon.adapters.discord.agent_setup.mcp_access as mcp_access_mod

    connect_calls: list[dict[str, Any]] = []

    async def _spy_connect(*_args: Any, **kwargs: Any) -> None:
        connect_calls.append(kwargs)

    monkeypatch.setattr(mcp_access_mod, "send_connect_via_mcp", _spy_connect)

    selected = _entry("alice")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id, is_admin=True)
    view = AgentSetupView(state, runtime=_make_runtime_with_settings(), allowed_user_id=42)
    interaction = _admin_interaction()

    await view.connect_mcp_btn.callback(interaction)

    assert len(connect_calls) == 1, "a live admin must still reach send_connect_via_mcp"
    assert connect_calls[0]["state"] is state, "the handler must receive the panel's own state"
    interaction.response.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_panel_member_row_is_still_usable_by_a_non_admin(account_id: uuid.UUID) -> None:
    """The admin re-check stays in the two admin-row callbacks, never in interaction_check.

    A view-level admin gate would lock ordinary members out of New / Fork / Edit,
    which the panel deliberately offers to everyone. This test fails if the check
    is ever moved up into the view.
    """
    import daimon.adapters.discord.agent_setup.panel as panel_mod

    state = PanelState(roster=[_entry("alice")], selected=None, account_id=account_id)
    view = AgentSetupView(state, runtime=_make_runtime_with_settings(), allowed_user_id=42)
    interaction = _non_admin_interaction(user_id=42)

    passed = await view.interaction_check(interaction)
    assert passed is True, (
        "the view-level check is invoker-only; an admin gate there would lock members out"
    )

    new_btn = _find_button(view, "New")
    await new_btn.callback(interaction)

    interaction.response.send_modal.assert_called_once()
    modal = interaction.response.send_modal.call_args.args[0]
    assert isinstance(modal, panel_mod.NewAgentModal), (
        "a non-admin must still reach the New agent modal"
    )
    interaction.response.send_message.assert_not_called()
