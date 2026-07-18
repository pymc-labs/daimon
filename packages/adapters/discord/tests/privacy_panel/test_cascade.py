"""CascadePreviewView + build_cascade_preview_container tests."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
from daimon.adapters.discord import theme
from daimon.adapters.discord.privacy_panel.cascade import (
    CascadePreviewView,
    build_cascade_preview_container,
)
from daimon.adapters.discord.privacy_panel.state import PurgePreviewRow

from .conftest import _find_button, _make_preview, _make_runtime


def _text_displays(container: discord.ui.Container[discord.ui.LayoutView]) -> list[str]:
    """Collect content from all TextDisplay children in the container."""
    return [item.content for item in container.children if isinstance(item, discord.ui.TextDisplay)]


def _joined_text(container: discord.ui.Container[discord.ui.LayoutView]) -> str:
    return "\n".join(_text_displays(container))


def _make_view(**overrides: Any) -> CascadePreviewView:
    base: dict[str, Any] = {
        "runtime": _make_runtime(),
        "account_id": uuid.uuid4(),
        "allowed_user_id": 111,
        "user_name": "carlos",
        "preview": _make_preview(),
    }
    base.update(overrides)
    return CascadePreviewView(**base)


def test_cascade_view_has_confirm_and_cancel_only() -> None:
    view = _make_view()
    labels = {c.label for c in view.walk_children() if isinstance(c, discord.ui.Button)}
    assert labels == {"🗑 I understand — confirm", "◀ Cancel"}, (
        f"CascadePreviewView should have exactly 2 buttons (D-LAYOUT-01); got {labels}"
    )


def test_cascade_view_confirm_is_danger_style() -> None:
    view = _make_view()
    confirm = _find_button(view, "🗑 I understand — confirm")
    assert confirm.style == discord.ButtonStyle.danger, (
        "Confirm button must be danger style (destructive action signal)"
    )


def test_cascade_view_cancel_is_secondary_style() -> None:
    view = _make_view()
    cancel = _find_button(view, "◀ Cancel")
    assert cancel.style == discord.ButtonStyle.secondary, (
        "Cancel button must be secondary style (return-to-main, non-destructive)"
    )


async def test_cascade_interaction_check_rejects_non_invoker() -> None:
    """T-29-05: cascade view also enforces invoker gate."""
    view = _make_view(allowed_user_id=111)
    interaction = MagicMock()
    interaction.user.id = 222
    interaction.response.send_message = AsyncMock()
    result = await view.interaction_check(interaction)
    assert result is False, "Cascade view must reject non-invoker"
    interaction.response.send_message.assert_awaited_once()


def test_cascade_container_is_red() -> None:
    """Cascade container accent must be brand red (D-COLOR-01)."""
    container = build_cascade_preview_container(_make_preview())
    assert container.accent_colour == theme.COLOR_RED, (
        f"Cascade container accent must be brand red 0x{theme.COLOR_RED:06X};"
        f" got {container.accent_colour}"
    )


def test_cascade_container_header_starts_with_confirm_delete() -> None:
    """First TextDisplay must start with '## 🗑 Confirm delete'."""
    container = build_cascade_preview_container(_make_preview())
    texts = _text_displays(container)
    assert texts, "Container must have at least one TextDisplay"
    assert texts[0].startswith("## 🗑 Confirm delete"), (
        f"Cascade header must start with '## 🗑 Confirm delete'; got {texts[0]!r}"
    )


def test_cascade_container_header_contains_irreversible() -> None:
    """Header subtext must contain 'irreversible'."""
    container = build_cascade_preview_container(_make_preview())
    texts = _text_displays(container)
    assert texts, "Container must have at least one TextDisplay"
    assert "irreversible" in texts[0], (
        f"Cascade header must mention 'irreversible'; got {texts[0]!r}"
    )


def test_cascade_container_renders_nonzero_categories() -> None:
    """D-PREVIEW-FMT-01: counts-with-example, one row per non-zero category."""
    preview = _make_preview(
        linked_principals=PurgePreviewRow(count=2, example="Discord:1, CLI:os_user=alice"),
        routines=PurgePreviewRow(count=3, example="@daily standup"),
        principal_links=PurgePreviewRow(count=0, example=None),  # excluded
        user_configs=PurgePreviewRow(count=1, example=None),
    )
    container = build_cascade_preview_container(preview)
    joined = _joined_text(container)
    assert "2" in joined, "must mention 2 linked principals"
    assert "Discord:1, CLI:os_user=alice" in joined, (
        "linked_principals.example must appear in the rendered row"
    )
    assert "3" in joined, "must mention 3 routines"
    assert "@daily standup" in joined, "routines.example must appear in the rendered row"
    # Zero-count principal_links category MUST NOT appear:
    assert "principal link" not in joined.lower(), (
        "zero-count categories must NOT render a row (D-PREVIEW-FMT-01)"
    )


def test_cascade_container_includes_what_stays_disclosure() -> None:
    """D-MA-01: the 'What stays in Managed Agents' group is mandatory."""
    container = build_cascade_preview_container(_make_preview())
    joined = _joined_text(container)
    assert "What stays in Managed Agents" in joined, (
        f"Cascade container must include the 'What stays in MA' disclosure; got {joined!r}"
    )
    assert "Anthropic" in joined, "Retention disclosure must mention Anthropic (D-MA-01)"


def test_cascade_container_has_confirm_step_hint() -> None:
    """Container must include the 'type your username to confirm' hint."""
    container = build_cascade_preview_container(_make_preview())
    joined = _joined_text(container)
    assert "type your username to confirm" in joined, (
        f"Cascade container must include the confirm-step hint; got {joined!r}"
    )


def test_cascade_container_user_skills_row_renders_when_nonzero() -> None:
    """user_skills row appears when count > 0."""
    preview = _make_preview(user_skills=PurgePreviewRow(count=2, example="brainstorming"))
    container = build_cascade_preview_container(preview)
    joined = _joined_text(container)
    assert "2" in joined, "user_skills count must appear in the row"
    assert "brainstorming" in joined, "user_skills.example must appear in the row"
    assert "synced skill ledger" in joined, "user_skills row must mention synced skill ledger"


def test_cascade_container_user_skills_row_absent_when_zero() -> None:
    """D-PREVIEW-FMT-01: user_skills row is suppressed when count == 0."""
    preview = _make_preview(user_skills=PurgePreviewRow(count=0, example=None))
    container = build_cascade_preview_container(preview)
    joined = _joined_text(container)
    assert "synced skill ledger" not in joined, (
        "zero-count user_skills must NOT render a row (D-PREVIEW-FMT-01)"
    )


def test_cascade_container_github_credentials_row_renders_when_nonzero() -> None:
    """github_credentials row appears when count > 0."""
    preview = _make_preview(github_credentials=PurgePreviewRow(count=1, example="octocat"))
    container = build_cascade_preview_container(preview)
    joined = _joined_text(container)
    assert "1" in joined, "github_credentials count must appear in the row"
    assert "octocat" in joined, "github_credentials.example (login) must appear in the row"
    assert "GitHub credential" in joined, "github_credentials row must mention GitHub credential"


def test_cascade_container_github_credentials_row_absent_when_zero() -> None:
    """D-PREVIEW-FMT-01: github_credentials row is suppressed when count == 0."""
    preview = _make_preview(github_credentials=PurgePreviewRow(count=0, example=None))
    container = build_cascade_preview_container(preview)
    joined = _joined_text(container)
    assert "GitHub credential" not in joined, (
        "zero-count github_credentials must NOT render a row (D-PREVIEW-FMT-01)"
    )


def test_cascade_container_github_oauth_states_row_renders_when_nonzero() -> None:
    """github_oauth_states row appears when count > 0."""
    preview = _make_preview(github_oauth_states=PurgePreviewRow(count=3, example=None))
    container = build_cascade_preview_container(preview)
    joined = _joined_text(container)
    assert "3" in joined, "github_oauth_states count must appear in the row"
    assert "OAuth handshake" in joined, "github_oauth_states row must mention OAuth handshake"


def test_cascade_container_github_oauth_states_row_absent_when_zero() -> None:
    """D-PREVIEW-FMT-01: github_oauth_states row is suppressed when count == 0."""
    preview = _make_preview(github_oauth_states=PurgePreviewRow(count=0, example=None))
    container = build_cascade_preview_container(preview)
    joined = _joined_text(container)
    assert "OAuth handshake" not in joined, (
        "zero-count github_oauth_states must NOT render a row (D-PREVIEW-FMT-01)"
    )


def test_cascade_container_carveout_usage_records_always_present() -> None:
    """usage-records retention carve-out is always shown regardless of counts."""
    container = build_cascade_preview_container(_make_preview())
    joined = _joined_text(container)
    assert "service integrity" in joined, (
        "Cascade container must disclose usage-records retention carve-out"
    )


def test_cascade_container_carveout_ma_skill_files_always_present() -> None:
    """MA skill files carve-out is always shown."""
    container = build_cascade_preview_container(_make_preview())
    joined = _joined_text(container)
    assert "Managed Agents" in joined, (
        "Cascade container must mention Managed Agents in skill-files carve-out"
    )
    assert "skill files" in joined.lower() or "skill" in joined.lower(), (
        "Cascade container must disclose that uploaded skill files stay in MA"
    )


def test_cascade_container_carveout_github_grant_always_present() -> None:
    """GitHub-side OAuth grant carve-out is always shown."""
    container = build_cascade_preview_container(_make_preview())
    joined = _joined_text(container)
    assert "github.com/settings/applications" in joined, (
        "Cascade container must direct users to revoke their GitHub OAuth grant"
    )


def test_cascade_container_no_github_pats_in_what_stays() -> None:
    """GitHub PATs are stored in our DB (not MA) and are deleted; 'GitHub PATs' must not appear under 'What stays in Managed Agents'."""
    container = build_cascade_preview_container(_make_preview())
    joined = _joined_text(container)
    assert "GitHub PATs" not in joined, (
        "cascade.py must not list 'GitHub PATs' under MA retention — PATs live in our DB and are deleted by this purge"
    )


def test_cascade_container_mcp_tokens_row_renders_when_nonzero() -> None:
    """per-agent MCP token row appears when count > 0."""
    preview = _make_preview(mcp_tokens=PurgePreviewRow(count=2, example=None))
    joined = _joined_text(build_cascade_preview_container(preview))
    assert "2" in joined, "mcp_tokens count must appear in the row"
    assert "MCP token" in joined, "mcp_tokens row must mention MCP token(s)"


def test_cascade_container_mcp_tokens_row_absent_when_zero() -> None:
    """per-agent MCP token row is suppressed when count == 0."""
    preview = _make_preview(mcp_tokens=PurgePreviewRow(count=0, example=None))
    joined = _joined_text(build_cascade_preview_container(preview))
    assert "MCP token" not in joined, "zero-count mcp_tokens must NOT render a row"


def test_cascade_container_agent_github_binding_row_renders_when_nonzero() -> None:
    """per-agent GitHub credential link row appears when count > 0."""
    preview = _make_preview(agent_github_binding=PurgePreviewRow(count=3, example=None))
    joined = _joined_text(build_cascade_preview_container(preview))
    assert "3" in joined, "agent_github_binding count must appear in the row"
    assert "per-agent GitHub credential link" in joined, (
        "agent_github_binding row must mention the per-agent GitHub credential link"
    )


def test_cascade_container_agent_github_binding_row_absent_when_zero() -> None:
    """per-agent GitHub credential link row is suppressed when count == 0."""
    preview = _make_preview(agent_github_binding=PurgePreviewRow(count=0, example=None))
    joined = _joined_text(build_cascade_preview_container(preview))
    assert "per-agent GitHub credential link" not in joined, (
        "zero-count agent_github_binding must NOT render a row"
    )
