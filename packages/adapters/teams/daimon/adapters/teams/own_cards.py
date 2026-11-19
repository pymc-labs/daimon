"""Presses on the cards Daimon answers itself: a host call's Approve/Deny, a switch's Confirm.

Posted from the MCP process (`tools._own_card`); the press re-verifies the clicker, the
message and the tenant in core, the same guards as on Discord and Slack.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from daimon.adapters.teams.card_actions import card_actor, replace_card, submitted_fields, toast
from daimon.core import host_calls, switch_cards
from daimon.core.features.snapshot import deployment_on
from daimon.core.posted_controls import PostedCard
from daimon.core.posted_controls.teams_card import CONTROL_VERB, build_adaptive_card
from microsoft_teams.cards import AdaptiveCard

if TYPE_CHECKING:
    from daimon.adapters.teams.card_actions import Actor
    from daimon.adapters.teams.runtime import TeamsRuntime
    from microsoft_teams.api import AdaptiveCardInvokeActivity, AdaptiveCardInvokeResponse
    from microsoft_teams.apps import ActivityContext

log = structlog.get_logger(__name__)

VERB = CONTROL_VERB
NOT_YOURS = "Only the person who asked can answer this."
UNAVAILABLE = "This card is unavailable. Ask again to redo it."


class TeamsOwnCards:
    """Answers presses on call and switch cards."""

    def __init__(self, runtime: TeamsRuntime) -> None:
        self._runtime = runtime

    async def on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        activity = ctx.activity
        custom_id = str(submitted_fields(activity.value.action.data).get("custom_id") or "")
        actor = await card_actor(self._runtime, activity)
        message_id = activity.reply_to_id
        if actor is None or not message_id:
            return toast(UNAVAILABLE)
        try:
            answered = await self._press(custom_id, actor, message_id)
        except Exception:
            # Boundary: a failed press must answer the presser, not leave the click hanging.
            log.exception("own_card.press_failed", custom_id=custom_id)
            return toast(UNAVAILABLE)
        if isinstance(answered, str):
            return toast(answered)
        return replace_card(AdaptiveCard.model_validate(build_adaptive_card(answered)))

    async def _press(self, custom_id: str, actor: Actor, message_id: str) -> PostedCard | str:
        """The answered card, or the line to toast when the press closes nothing."""
        runtime = self._runtime

        async def current_admin(clicker: str) -> bool:
            # `card_actor` read the clicker's role live for this press.
            return clicker == actor.user_id and actor.is_admin

        now = datetime.now(UTC)
        if (call := host_calls.parse_custom_id(custom_id)) is not None:
            press = await host_calls.press_host_call(
                sessionmaker=runtime.sessionmaker,
                # Off is a rollback: presses on cards already posted are refused too.
                enabled=deployment_on(runtime.settings, "host_call_gate", "teams"),
                tenant_id=actor.tenant_id,
                card_id=call[0],
                action=call[1],
                clicker=actor.user_id,
                message_id=message_id,
                current_admin=current_admin,
                now=now,
            )
            if press is None or not press.closed:
                return NOT_YOURS if press is None else press.text
            return host_calls.answered_call_card(press.text, approved=press.approved)
        if (switch := switch_cards.parse_custom_id(custom_id)) is not None:
            pressed = await switch_cards.press_switch_card(
                deps=runtime.turn_deps,
                sessionmaker=runtime.sessionmaker,
                anthropic=runtime.anthropic,
                turn_settings=runtime.settings.turn,
                deployment_default=runtime.deployment_default,
                tenant_id=actor.tenant_id,
                card_id=switch[0],
                action=switch[1],
                clicker=actor.user_id,
                current_admin=current_admin,
                place_word="team",
                now=now,
                message_id=message_id,
            )
            if pressed is None or not pressed.closed:
                return NOT_YOURS if pressed is None else pressed.text
            return switch_cards.answered_switch_card(pressed)
        return UNAVAILABLE
