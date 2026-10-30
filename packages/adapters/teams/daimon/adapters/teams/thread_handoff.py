"""The Hand over button on a Teams conversation whose channel now answers with another agent.

Teams' side of `daimon.adapters.slack.thread_handoff`: the responder-changed
notice carries a button whose data names the agent the channel answers with
now. A click hands the conversation it sits in to that agent for the verified
clicker, decided by `daimon.core.thread_handoff` with their admin status and
the grant-named teams they own, confirmed live. The agent id is only a
request.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

import structlog
from daimon.adapters.teams.admin_caller import admin_caller
from daimon.adapters.teams.card_actions import (
    FAILED,
    button,
    card_actor,
    guarded,
    replace_card,
    submitted_fields,
    text_lines,
    toast,
)
from daimon.adapters.teams.identity import DENIED, channel_thread_id
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.thread_handoff import switch_thread_on_request
from microsoft_teams.api import AdaptiveCardInvokeActivity, AdaptiveCardInvokeResponse
from microsoft_teams.apps import ActivityContext
from microsoft_teams.cards import AdaptiveCard, ExecuteAction

__all__ = ["VERB", "TeamsThreadHandoff", "hand_over_button"]

log = structlog.get_logger()

VERB: Final = "thread_hand_over"


def hand_over_button(*, agent_id: str, agent_name: str, offer: str) -> ExecuteAction:
    """One Hand over button carrying the agent's MA id and its card's text, `offer`,
    so a click can redraw the card without the button."""
    return button(
        VERB,
        f"Switch to {agent_name}",
        "hand_over",
        style="positive",
        agent=agent_id,
        offer=offer,
    )


class TeamsThreadHandoff:
    """Hand over clicks."""

    def __init__(self, runtime: TeamsRuntime) -> None:
        self._runtime = runtime

    async def on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        return await guarded(self._on_action(ctx), toast(FAILED), "teams.thread_handoff.failed")

    async def _on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        """Switch the clicked conversation to the button's agent, or say privately why not."""
        runtime, activity = self._runtime, ctx.activity
        actor = await card_actor(runtime, activity)
        if actor is None:
            return toast(DENIED)
        agent_id = str(submitted_fields(activity.value.action.data).get("agent") or "")
        conversation, notice = activity.conversation.id, activity.reply_to_id
        # The notice is a reply in the thread it is about; in a 1:1 or group chat, the chat.
        personal = activity.conversation.conversation_type in ("personal", "groupChat")
        if personal:
            conversation = thread_id = conversation.split(";", 1)[0]
        elif notice:
            thread_id = channel_thread_id(conversation, notice)
        else:
            return toast(FAILED)
        if not agent_id:
            return toast(FAILED)
        outcome = await switch_thread_on_request(
            runtime.anthropic,
            runtime.sessionmaker,
            tenant_id=actor.tenant_id,
            platform="teams",
            parent_channel_id=conversation if personal else conversation.split(";", 1)[0],
            thread_id=thread_id,
            ma_agent_id=agent_id,
            caller=await admin_caller(
                runtime, tenant_id=actor.tenant_id, user_id=actor.user_id, is_admin=actor.is_admin
            ),
            default=runtime.deployment_default,
            channel="this chat" if personal else "this channel",
            now=datetime.now(UTC),
        )
        log.info(
            "thread_handoff.clicked", platform="teams", switched=outcome.switched, agent_id=agent_id
        )
        if not outcome.switched:
            return toast(outcome.text)
        # The agent now answering is told (`switch_thread_on_request`); the offer
        # keeps its words and drops its button. A card from before `offer` gets a pop-up.
        offer = str(submitted_fields(activity.value.action.data).get("offer") or "")
        if not offer:
            return toast(outcome.text)
        return replace_card(AdaptiveCard(body=text_lines(offer), fallback_text=offer))
