"""Discord message bodies built by the adapter's own builders.

Each payload is the REST body for `POST /channels/{id}/messages`, serialized the way
discord.py sends it (`view.to_components()`, `embed.to_dict()`). Components V2 layouts
carry the `IS_COMPONENTS_V2` flag and no `content` or `embeds`.

Not covered: modals, which Discord accepts only as an interaction response and so cannot be
posted to a channel (the repo's `scripts/lint_discord_modals.py` covers them statically), and
the wizard, routines panel and agent-setup detail/routing views, which need a runtime and
database to construct.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

from .loaders import DISCORD_TESTS, SLACK_TESTS, call_in_event_loop, load_test_module
from .registry import entry

IS_COMPONENTS_V2 = 1 << 15


def _layout_body(view: Any) -> dict[str, Any]:
    return {"flags": IS_COMPONENTS_V2, "components": view.to_components()}


def _classic_body(
    *, view: Any = None, embed: Any = None, content: str | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if content:
        body["content"] = content
    if embed is not None:
        body["embeds"] = [embed.to_dict()]
    if view is not None:
        body["components"] = view.to_components()
    return body


def _stop_view() -> Any:
    from daimon.adapters.discord.views import CancelView

    return CancelView(allowed_user_id=1, cancel=asyncio.Event(), turn_id=uuid.uuid4())


def _turn_embed(phase: str, **fields: Any) -> Any:
    from daimon.adapters.discord.embed import to_embed_data
    from daimon.adapters.discord.lifecycle import build_discord_embed
    from daimon.core.turn.card_state import CardState, TurnPhase

    state = CardState(
        phase=getattr(TurnPhase, phase), agent_name="Atlas", started_at=100.0, **fields
    )
    return build_discord_embed(to_embed_data(state, now=142.0))


@entry("stop_button_view", "discord", "message")
def stop_button_view() -> Any:
    return call_in_event_loop(lambda: _classic_body(view=_stop_view()))


@entry("turn_card_working_with_stop", "discord", "message")
def turn_card_working_with_stop() -> Any:
    embed = _turn_embed(
        "TOOL_RUNNING",
        plan_lines=("☑️ Load the data", "⏳ Fit the model", "\u2007\u2007\u2007Write it up"),
        tool_lines=("+3 earlier", "☑️ Read a file", "⏳ Running a command"),
        text_preview="Preview text.",
        cost_str="$0.04",
    )
    return call_in_event_loop(lambda: _classic_body(embed=embed, view=_stop_view()))


@entry("turn_card_done", "discord", "message")
def turn_card_done() -> Any:
    return _classic_body(embed=_turn_embed("DONE", cost_str="$0.04"))


@entry("turn_card_error", "discord", "message")
def turn_card_error() -> Any:
    return _classic_body(embed=_turn_embed("ERROR", error_reason="rate limited"))


@entry("help_view_v2", "discord", "message")
def help_view_v2() -> Any:
    from daimon.adapters.discord.commands.help import build_help_view

    return call_in_event_loop(lambda: _layout_body(build_help_view("research-bot")))


@entry("privacy_panel_v2", "discord", "message")
def privacy_panel_v2() -> Any:
    from daimon.adapters.discord.privacy_panel.panel import PrivacyPanelView

    runtime = MagicMock()
    runtime.settings.privacy_policy_url = "https://example.com/privacy"
    runtime.settings.privacy.delete_enabled = True
    runtime.settings.discord.bot_display_name = "research-bot"

    def build() -> dict[str, Any]:
        view = PrivacyPanelView(
            runtime=runtime, account_id=uuid.uuid4(), allowed_user_id=1, user_name="alice"
        )
        return _layout_body(view)

    return call_in_event_loop(build)


@entry("expired_panel_v2", "discord", "message")
def expired_panel_v2() -> Any:
    from daimon.adapters.discord.agent_setup.expiry import build_expired_view

    return call_in_event_loop(lambda: _layout_body(build_expired_view()))


@entry("billing_panel_v2", "discord", "message")
def billing_panel_v2() -> Any:
    from daimon.adapters.discord import layout
    from daimon.adapters.discord.billing_panel.panel import build_billing_container
    from daimon.adapters.discord.billing_panel.state import BillingPanelState, MemberRow

    state = BillingPanelState(
        is_admin=True,
        caller_user_id="1",
        caller_spend=1.5,
        caller_turns=3,
        caller_cap=None,
        guild_balance_usd=Decimal("50.00"),
        guild_spend=10.0,
        guild_turns=20,
        guild_distinct_members=2,
        member_rows=(
            MemberRow(
                platform_user_id="2",
                display_name="Sam",
                cost_usd=8.0,
                turn_count=15,
                is_caller=False,
            ),
        ),
        over_cap_count=0,
    )
    container = build_billing_container(
        state, now=datetime(2025, 1, 15, tzinfo=UTC), since=datetime(2025, 1, 1, tzinfo=UTC)
    )
    return call_in_event_loop(lambda: _layout_body(layout.static_view(container)))


@entry("posted_card_requested_v2", "discord", "message")
def posted_card_requested_v2() -> Any:
    from daimon.adapters.discord.posted_controls.view import build_card_view

    tests = load_test_module(DISCORD_TESTS / "test_posted_controls_view.py")
    return call_in_event_loop(lambda: _layout_body(build_card_view(tests._requested_card())))


@entry("tool_confirmation_pending_v2", "discord", "message")
def tool_confirmation_pending_v2() -> Any:
    from daimon.adapters.discord.tool_confirmation import build_confirmation_view
    from daimon.core.confirmation import ConfirmationPrompt
    from daimon.core.posted_controls.confirmation import build_confirmation_card

    prompt = ConfirmationPrompt(
        title="Confirm this action?",
        requester_platform_user_id="100000000000000001",
        expires_at=datetime(2026, 12, 10, tzinfo=UTC),
    )
    card = build_confirmation_card(prompt, state="pending", token="pending-card")
    return call_in_event_loop(lambda: _layout_body(build_confirmation_view(card, prompt)))


def _feedback_prompt(*, shared: bool) -> dict[str, Any]:
    """The DM the bot sends after a feedback reaction: `feedback_prompt_embed` plus the button view."""
    import discord
    from daimon.adapters.discord.feedback_button import FeedbackButton
    from daimon.adapters.discord.feedback_reactions import feedback_prompt_embed

    link = "https://discord.com/channels/1/2/3"

    def build() -> dict[str, Any]:
        view = discord.ui.View(timeout=None)
        view.add_item(FeedbackButton(feedback_id=str(uuid.uuid4())))
        return _classic_body(view=view, embed=feedback_prompt_embed(link, shared=shared))

    return call_in_event_loop(build)


entry("feedback_prompt", "discord", "message")(lambda: _feedback_prompt(shared=False))
entry("feedback_prompt_shared", "discord", "message")(lambda: _feedback_prompt(shared=True))


@entry("here_embed", "discord", "message")
def here_embed() -> Any:
    from daimon.adapters.discord.commands.here import build_here_embed

    here_tests = load_test_module(SLACK_TESTS / "test_here.py")
    card = here_tests._card("channel").model_copy(update={"platform": "discord"})
    return _classic_body(embed=build_here_embed(card))
