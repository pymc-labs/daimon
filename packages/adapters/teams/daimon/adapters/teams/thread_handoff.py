"""The Hand over button on a Teams conversation whose channel now answers with another agent.

Teams' side of `daimon.adapters.slack.thread_handoff`: the responder-changed
notice carries a button whose data names the agent the channel answers with
now. A click hands the conversation it sits in to that agent for the verified
clicker, decided by `daimon.core.thread_handoff` with their admin status and
the grant-named teams they own, confirmed live. The agent id is only a
request; a clicker from another organisation is refused.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

import structlog
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
from daimon.adapters.teams.channel_admin_groups import channel_admin_caller
from daimon.adapters.teams.identity import DENIED, channel_thread_id
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.thread_handoff import switch_thread_on_request
from microsoft_teams.api import AdaptiveCardInvokeActivity, AdaptiveCardInvokeResponse
from microsoft_teams.apps import ActivityContext
from microsoft_teams.cards import AdaptiveCard, ExecuteAction

__all__ = ["VERB", "TeamsThreadHandoff", "hand_over_button"]

log = structlog.get_logger()

VERB: Final = "thread_hand_over"


def hand_over_button(*, agent_id: str, agent_name: str) -> ExecuteAction:
    """One Hand over button carrying the agent's MA id."""
    return button(VERB, f"Hand over to {agent_name}", "hand_over", style="positive", agent=agent_id)


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
        personal = activity.conversation.conversation_type == "personal"
        # The notice is a reply in the thread it is about; in a 1:1 chat, the chat.
        if personal:
            thread_id = conversation
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
            caller=await channel_admin_caller(
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
        # Everyone sees who handed it over; the button has done its job.
        who = activity.from_.name or "Someone"
        text = f"{who} handed this conversation over. {outcome.text}"
        return replace_card(AdaptiveCard(body=text_lines(text), fallback_text=text))
