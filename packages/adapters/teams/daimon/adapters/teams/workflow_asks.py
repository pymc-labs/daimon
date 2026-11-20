"""A workflow ask's card, its presses and typed answers on Teams.

The card carries its answer boxes inline (a reject's reason, the answer, a form's fields)
and `wfa` Action.Execute buttons (`daimon.core.workflows.answers`). `claim_inbound` is
what the message dispatch runs before admitting a turn: a reply in a channel thread an
ask opened is checked as its answer. Teams' channel access is the outer boundary; core
checks the person against the ask.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import TYPE_CHECKING, Any, Final

import structlog
from daimon.adapters.teams.card import notice_card
from daimon.adapters.teams.card_actions import card_actor, submitted_fields, toast
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.lifecycle import SEND_TIMEOUT_S, TEAMS_SEND_ERRORS, TeamsSender
from daimon.core.workflows import answers, waits
from microsoft_teams.cards import (
    ActionSet,
    AdaptiveCard,
    CardElement,
    ExecuteAction,
    TextBlock,
    TextInput,
)

if TYPE_CHECKING:
    from daimon.adapters.teams.runtime import TeamsRuntime
    from microsoft_teams.api import AdaptiveCardInvokeActivity, AdaptiveCardInvokeResponse
    from microsoft_teams.apps import ActivityContext

__all__ = ["VERB", "AskActions", "ask_card", "claim_inbound"]

log = structlog.get_logger(__name__)
VERB: Final = answers.ASK_ACTION
_FAILED = "That didn't work. Try again."


def _boxes(ask: dict[str, Any]) -> answers.AskForm | None:
    shape = ask.get("shape")
    if shape == "choice":
        return None
    return answers.form_of(ask, waits.REJECT if shape == "approve" else waits.ANSWER)


def ask_card(text: str, ask: dict[str, Any]) -> AdaptiveCard:
    """The ask's words, its answer boxes and its buttons."""
    form = _boxes(ask)
    body: list[CardElement] = [TextBlock(text=text, wrap=True)]
    for name, title in () if form is None else form.fields:
        body.append(TextInput(id=name, label=title, is_multiline=True, max_length=2000))
    actions = [
        ExecuteAction(
            title=b.label,
            verb=VERB,
            data={"action": VERB, "custom_id": b.custom_id},
            style="positive" if b.primary else None,
        )
        for b in answers.buttons(ask)
    ]
    body.append(ActionSet(actions=list(actions)))
    return AdaptiveCard(body=body, fallback_text=text)


class AskActions:
    """Answers presses on ask cards."""

    def __init__(self, runtime: TeamsRuntime) -> None:
        self._runtime = runtime

    async def on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        try:
            return toast(await self._press(ctx.activity))
        except Exception as err:
            # Boundary: a failed press must answer the presser, not leave the click hanging.
            log.exception("workflow_ask.press_failed", err_type=type(err).__name__)
            return toast(_FAILED)

    async def _press(self, activity: AdaptiveCardInvokeActivity) -> str:
        runtime = self._runtime
        fields = submitted_fields(activity.value.action.data)
        parsed = answers.parse_action(str(fields.get("custom_id") or ""))
        actor = await card_actor(runtime, activity)
        message_id = activity.reply_to_id
        if parsed is None or actor is None or not message_id:
            return "This request is gone."
        route = parsed[1]
        form = await answers.ask_form(
            runtime.sessionmaker, platform="teams", message_id=message_id, route=route
        )
        values = {} if form is None else {n: str(fields.get(n) or "") for n, _ in form.fields}
        if form is not None and form.fields and not any(v.strip() for v in values.values()):
            return "Fill in the box above, then press the button again."
        comment = str(fields.get("comment") or "").strip() or None
        sent, reason = answers.form_answer(route, values) if form else ({}, comment)
        claimed = await answers.press(
            runtime.sessionmaker,
            runtime.settings,
            platform="teams",
            author=actor.user_id,
            message_id=message_id,
            route=route,
            values=sent,
            comment=reason,
        )
        return claimed.text or "Your answer is in."


async def claim_inbound(
    runtime: TeamsRuntime, parsed: TeamsInbound, tenant_id: uuid.UUID, sender: TeamsSender
) -> bool:
    """Answer an ask with a reply in a channel thread it opened. True when no turn follows.

    Personal chats have no threads, so an ask sent by DM is answered on its card."""
    if parsed.kind != "channel" or ";messageid=" not in parsed.conversation_id:
        return False
    claimed = await answers.claim_reply(
        runtime.sessionmaker,
        runtime.settings,
        answers.Reply(
            platform="teams",
            tenant_ids=(tenant_id,),
            author=parsed.user_id,
            message_id=parsed.activity_id,
            text=parsed.text.strip(),
            thread_id=parsed.conversation_id,
        ),
    )
    if claimed is None:
        return False
    if claimed.text is not None:
        card = notice_card(claimed.text, error=claimed.status == "refused")
        with contextlib.suppress(*TEAMS_SEND_ERRORS):
            await asyncio.wait_for(
                sender.send(parsed.conversation_id, card, service_url=parsed.service_url),
                SEND_TIMEOUT_S,
            )
    return True
