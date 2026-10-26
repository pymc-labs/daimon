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
    assert "@daily standup" not in joined, "routine IDs stay out of the preview"
    # Zero-count principal_links category MUST NOT appear:
    assert "principal link" not in joined.lower(), (
        "zero-count categories must NOT render a row (D-PREVIEW-FMT-01)"
    )


def test_cascade_container_says_what_delete_removes_and_what_stays() -> None:
    """D-MA-01: the confirm says what Delete reaches at Anthropic and what it leaves.

    `purge_account` deletes the account's Managed Agents sessions, so the
    confirm must not claim the conversations stay; agents and memory do.
    """
    container = build_cascade_preview_container(_make_preview(), bot_display_name="Daimon")
    texts = [
        item.content for item in container.children if isinstance(item, discord.ui.TextDisplay)
    ]
    assert texts[1] == (
        "This deletes Daimon's records about you and tries to delete your conversations "
        "stored at Anthropic."
        "\n\nShared agents and their memory stay, and other people may keep using them."
    ), "the scope sits right under the title, two lines a blank line apart"
    assert "Session transcripts" not in _joined_text(container), "transcripts do not stay"


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
    assert "skills you synced" in joined, "user_skills row must mention synced skill ledger"


def test_cascade_plain_rows_keep_routine_id_out_and_space_the_list() -> None:
    preview = _make_preview(
        routines=PurgePreviewRow(count=2, example="routine-uuid"),
        user_configs=PurgePreviewRow(count=1, example=None),
        mcp_tokens=PurgePreviewRow(count=1, example=None),
    )
    body = _text_displays(build_cascade_preview_container(preview))[-1]
    assert "⚡ **What will happen**\n\n-#" in body
    assert "📋 **What is intentionally kept elsewhere**\n\n-#" in body
    assert "-# ⏰ Cancel **2** routines" in body
    assert "routine-uuid" not in body
    assert "-# ⚙ Remove your saved settings" in body
    assert "Daimon access token" in body


def test_cascade_container_user_skills_row_absent_when_zero() -> None:
    """D-PREVIEW-FMT-01: user_skills row is suppressed when count == 0."""
    preview = _make_preview(user_skills=PurgePreviewRow(count=0, example=None))
    container = build_cascade_preview_container(preview)
    joined = _joined_text(container)
    assert "skills you synced" not in joined, (
        "zero-count user_skills must NOT render a row (D-PREVIEW-FMT-01)"
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


def test_cascade_container_mcp_tokens_row_renders_when_nonzero() -> None:
    """per-agent MCP token row appears when count > 0."""
    preview = _make_preview(mcp_tokens=PurgePreviewRow(count=2, example=None))
    joined = _joined_text(build_cascade_preview_container(preview))
    assert "2" in joined, "mcp_tokens count must appear in the row"
    assert "Daimon access token" in joined, "mcp_tokens row must mention Daimon access token"


def test_cascade_container_mcp_tokens_row_absent_when_zero() -> None:
    """per-agent MCP token row is suppressed when count == 0."""
    preview = _make_preview(mcp_tokens=PurgePreviewRow(count=0, example=None))
    joined = _joined_text(build_cascade_preview_container(preview))
    assert "Daimon access token" not in joined, "zero-count mcp_tokens must NOT render a row"


def test_cascade_container_slack_user_tokens_row_renders_when_nonzero() -> None:
    """slack_user_tokens row appears when count > 0."""
    preview = _make_preview(slack_user_tokens=PurgePreviewRow(count=1, example=None))
    joined = _joined_text(build_cascade_preview_container(preview))
    assert "1" in joined, "slack_user_tokens count must appear in the row"
    assert "saved access token" in joined, "slack_user_tokens row must mention saved access token"


def test_cascade_container_slack_user_tokens_row_absent_when_zero() -> None:
    """slack_user_tokens row is suppressed when count == 0."""
    preview = _make_preview(slack_user_tokens=PurgePreviewRow(count=0, example=None))
    joined = _joined_text(build_cascade_preview_container(preview))
    assert "saved access token" not in joined, "zero-count slack_user_tokens must NOT render a row"


def test_cascade_container_slack_turn_contexts_row_renders_when_nonzero() -> None:
    """slack_turn_contexts row appears when count > 0."""
    preview = _make_preview(slack_turn_contexts=PurgePreviewRow(count=4, example=None))
    joined = _joined_text(build_cascade_preview_container(preview))
    assert "4" in joined, "slack_turn_contexts count must appear in the row"
    assert "temporary request record" in joined, (
        "slack_turn_contexts row must mention temporary request record"
    )


def test_cascade_container_slack_turn_contexts_row_absent_when_zero() -> None:
    """slack_turn_contexts row is suppressed when count == 0."""
    preview = _make_preview(slack_turn_contexts=PurgePreviewRow(count=0, example=None))
    joined = _joined_text(build_cascade_preview_container(preview))
    assert "temporary request record" not in joined, (
        "zero-count slack_turn_contexts must NOT render a row"
    )


def test_cascade_names_the_conversations_it_deletes_and_the_shared_ones_it_keeps() -> None:
    preview = _make_preview(
        mux_slots=PurgePreviewRow(count=2, example=None),
        retained_shared_conversations=PurgePreviewRow(count=3, example=None),
    )
    text = _joined_text(build_cascade_preview_container(preview))
    assert "Delete Daimon's copy of **2** saved conversations, with its messages" in text
    assert (
        "Your messages in **3** shared conversations stay for the people still using them." in text
    )


def test_cascade_is_silent_about_conversations_when_there_are_none() -> None:
    text = _joined_text(build_cascade_preview_container(_make_preview()))
    assert "saved conversation" not in text and "shared conversation" not in text
