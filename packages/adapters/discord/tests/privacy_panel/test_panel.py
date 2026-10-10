"""PrivacyPanelView + build_privacy_main_container tests."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
from daimon.adapters.discord.privacy_panel.panel import (
    PrivacyPanelView,
    build_privacy_main_container,
)

from .conftest import _make_runtime


def _find_button(view: discord.ui.LayoutView, label: str) -> discord.ui.Button[Any]:
    """Walk all children of a LayoutView to find a button by label."""
    for child in view.walk_children():
        if isinstance(child, discord.ui.Button) and child.label == label:
            return child
    raise AssertionError(f"No button labeled {label!r} found in view {type(view).__name__}")


def _text_displays(container: discord.ui.Container[discord.ui.LayoutView]) -> list[str]:
    """Collect content from all TextDisplay children in the container."""
    return [item.content for item in container.children if isinstance(item, discord.ui.TextDisplay)]


def _make_view(**overrides: Any) -> PrivacyPanelView:
    base: dict[str, Any] = {
        "runtime": _make_runtime(),
        "account_id": uuid.uuid4(),
        "allowed_user_id": 999_999_999_111,
        "user_name": "carlos",
    }
    base.update(overrides)
    return PrivacyPanelView(**base)


def test_main_view_has_policy_export_delete_and_done_buttons() -> None:
    view = _make_view()
    labels = {c.label for c in view.walk_children() if isinstance(c, discord.ui.Button)}
    assert labels == {"📄 Policy", "📤 Export", "🗑 Delete…", "✓ Done"}, (
        f"PrivacyPanelView should have exactly 4 buttons (D-LAYOUT-01); got labels {labels}"
    )


def test_main_view_policy_button_is_link_style_pointing_to_policy_url() -> None:
    view = _make_view()
    policy = _find_button(view, "📄 Policy")
    assert policy.style == discord.ButtonStyle.link, (
        "Policy button must be link style (opens browser, no callback)"
    )
    assert policy.url == "https://github.com/pymc-labs/daimon/blob/main/PRIVACY.md", (
        "Policy URL must point to the in-repo daimon privacy policy page"
    )


def test_main_view_policy_button_uses_operator_overridden_url() -> None:
    view = _make_view(runtime=_make_runtime(privacy_policy_url="https://example.com/privacy"))
    policy = _find_button(view, "📄 Policy")
    assert policy.url == "https://example.com/privacy", (
        "Policy button must render the operator-overridden privacy_policy_url from settings"
    )


def test_main_view_export_button_is_secondary_style() -> None:
    view = _make_view()
    export = _find_button(view, "📤 Export")
    assert export.style == discord.ButtonStyle.secondary, (
        "Export button must be secondary style (stubbed day-1, non-destructive)"
    )


def test_main_view_delete_button_is_danger_style() -> None:
    view = _make_view()
    delete = _find_button(view, "🗑 Delete…")
    assert delete.style == discord.ButtonStyle.danger, (
        "Delete button must be danger style (red — destructive action signal)"
    )


def test_main_view_done_button_is_secondary_style() -> None:
    view = _make_view()
    done = _find_button(view, "✓ Done")
    assert done.style == discord.ButtonStyle.secondary, (
        "Done button must be secondary style (close-panel, no payload)"
    )


async def test_interaction_check_rejects_non_invoker() -> None:
    """T-29-05: other users' clicks must be rejected with ephemeral message."""
    view = _make_view(allowed_user_id=111)
    interaction = MagicMock()
    interaction.user.id = 222  # different from allowed_user_id
    interaction.response.send_message = AsyncMock()
    result = await view.interaction_check(interaction)
    assert result is False, "interaction_check must reject non-invoker (return False)"
    interaction.response.send_message.assert_awaited_once()
    sent_text = interaction.response.send_message.call_args.args[0]
    assert "command invoker" in sent_text, (
        f"Rejection message should mention 'command invoker'; got {sent_text!r}"
    )
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True, (
        "Rejection message must be ephemeral"
    )


async def test_interaction_check_accepts_invoker() -> None:
    view = _make_view(allowed_user_id=111)
    interaction = MagicMock()
    interaction.user.id = 111
    interaction.response.send_message = AsyncMock()
    result = await view.interaction_check(interaction)
    assert result is True, "interaction_check must accept the invoker (return True)"
    interaction.response.send_message.assert_not_awaited()


def test_main_container_is_the_title_and_two_lines_on_who_stores_what() -> None:
    """The detail categories live behind Policy; the panel says who stores what."""
    container = build_privacy_main_container(bot_display_name="Daimon")
    assert _text_displays(container) == [
        "## 🔒 Your data",
        "Daimon stores your linked accounts, routines and settings. "
        "Saved GitHub keys are encrypted.\n\n"
        "Anthropic stores your agents and their conversations.",
    ], "a title, then two lines a blank line apart"
