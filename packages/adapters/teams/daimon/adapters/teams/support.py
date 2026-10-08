"""The `support` command: ask a human, spending one of the person's support credits.

Discord asks with a reaction on an answer; here `support` posts a form in the
1:1 chat, answered there like every command, and sending it spends the credit.
It is a bare word, so prose such as "support vector machines" stays a turn. As
on Discord, the row is committed before the post and stamped delivered only
once the post lands, so a failed post loses nothing. Requests go to
`DAIMON_SUPPORT__ESCALATION_CHANNEL_ID`: a Teams channel (`19:…`) gets a post
from this bot, any other id is a Discord channel posted to with the Discord
bot token. `enabled` decides whether the command is registered at all.

When it is, every answer also carries an Ask a human button (`card.ASK_HUMAN_DIALOG`),
Slack's: it opens the same form in a dialog only the clicker sees, for the
people who could have asked the agent there (`answer_access`), and the request
links to that answer. The same post path carries a tenant's routed 👎 forms
(`routes_feedback`, `feedback`), which spend no credit.
"""

from __future__ import annotations

import secrets
import uuid
from collections import OrderedDict
from dataclasses import dataclass

import structlog
from daimon.adapters.teams.answer_access import AnswerPlace, check_answer_access, refusal_text
from daimon.adapters.teams.card import ASK_HUMAN_DIALOG
from daimon.adapters.teams.card_actions import (
    FAILED,
    button,
    card_actor,
    dialog,
    dialog_message,
    error_text,
    guarded,
    heading,
    replace_card,
    submitted_fields,
    text_card,
    text_lines,
    toast,
)
from daimon.adapters.teams.commands import CommandContext
from daimon.adapters.teams.direct_chats import DirectChats
from daimon.adapters.teams.identity import DENIED, TeamsInbound
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.config import Settings
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.support_escalation import (
    count_escalations_for_user,
    mark_delivered,
    record_escalation,
)
from daimon.core.stores.thread_sessions import get_latest_thread_session
from daimon.core.support_escalation import (
    OUT_OF_CREDITS,
    RECEIVED,
    RECORDED_UNDELIVERED,
    received_text,
    remaining_credits,
)
from microsoft_teams.api import (
    AdaptiveCardInvokeActivity,
    AdaptiveCardInvokeResponse,
    MessageActivityInput,
    TaskFetchInvokeActivity,
    TaskModuleResponse,
    TaskSubmitInvokeActivity,
)
from microsoft_teams.apps import ActivityContext
from microsoft_teams.cards import (
    ActionSet,
    AdaptiveCard,
    CardElement,
    SubmitAction,
    SubmitData,
    TextInput,
)

__all__ = [
    "OUT_OF_CREDITS",
    "RECEIVED",
    "RECORDED_UNDELIVERED",
    "VERB",
    "SupportCommand",
    "enabled",
    "post_to_support_channel",
    "routes_feedback",
]

log = structlog.get_logger(__name__)

VERB = "support"
NOTE_INPUT = "note"
_DISCORD_API = "https://discord.com/api/v10"
_DM_LINK = "(sent in the 1:1 chat)"
_MAX_PENDING = 256  # forms whose "asked in" is remembered; older ones name the 1:1 chat
MAX_NOTE_CHARS = 4000  # Discord's support modal cap.
TITLE = "🙋 Human support"
FORM_TEXT = (
    "Tell us what you need help with and a person will follow up. "
    "You have {remaining} requests left."
)
USAGE = "Write what you need help with first."
# OUT_OF_CREDITS, RECEIVED and RECORDED_UNDELIVERED are the shared core copy
# (`daimon.core.support_escalation`), re-exported for this module's callers.


def _teams_channel(channel_id: str) -> bool:
    return channel_id.startswith("19:")


def enabled(settings: Settings) -> bool:
    """Whether escalation is on and its channel reachable from this process."""
    channel, credits = settings.support.escalation_channel_id, settings.support.credits_per_user
    if channel is None or credits <= 0:
        return False
    return _teams_channel(channel) or settings.discord is not None


def routes_feedback(settings: Settings, tenant_id: uuid.UUID) -> bool:
    """Whether this tenant's submitted 👎 forms also go to the escalation channel.

    Off unless the tenant turned it on and the channel is reachable from here,
    as for `enabled`; credits play no part, since a form spends none.
    """
    channel = settings.support.escalation_channel_id
    if channel is None or settings.support.routes_feedback(tenant_id) is not True:
        return False
    return _teams_channel(channel) or settings.discord is not None


def _link(asked: TeamsInbound) -> str:
    """A link to the message `support` was typed in; the 1:1 chat has none to share."""
    if asked.kind == "dm":
        return _DM_LINK
    return AnswerPlace(asked.conversation_id, asked.activity_id, True).link(asked.entra_tenant_id)


@dataclass(frozen=True)
class _Asked:
    """Where a request was made, for its row and its link."""

    user_id: str
    user_name: str | None
    conversation_id: str
    thread_id: str
    message_id: str
    link: str

    @classmethod
    def of(cls, inbound: TeamsInbound) -> _Asked:
        return cls(
            inbound.user_id,
            inbound.user_name,
            inbound.conversation_id,
            inbound.thread_id,
            inbound.activity_id,
            _link(inbound),
        )


def _note(value: str = "") -> TextInput:
    return TextInput(
        id=NOTE_INPUT, is_multiline=True, max_length=MAX_NOTE_CHARS, value=value or None
    )


def form_card(token: str, remaining: int) -> AdaptiveCard:
    return AdaptiveCard(
        body=[
            heading(TITLE),
            *text_lines(FORM_TEXT.format(remaining=remaining)),
            _note(),
            ActionSet(actions=[button(VERB, "Send", "send", ask=token)]),
        ],
        fallback_text=TITLE,
    )


def ask_form(
    message_id: str, remaining: int, *, note: str = "", error: str | None = None
) -> AdaptiveCard:
    """The same form in Ask a human's dialog, which carries the answer it was opened on."""
    body: list[CardElement] = [error_text(error)] if error else []
    body += [*text_lines(FORM_TEXT.format(remaining=remaining)), _note(note)]
    send = SubmitAction(title="Send", data=SubmitData(ASK_HUMAN_DIALOG, {"message": message_id}))
    return AdaptiveCard(body=body, actions=[send], fallback_text=TITLE)


class SupportCommand:
    """`support` posts the form; its Send records and posts the request.

    Where `support` was typed is held here under a token the form carries and
    honoured only for the person who typed it; after a restart, or for anyone
    else, the request names the 1:1 chat it was sent from.
    """

    def __init__(self, runtime: TeamsRuntime, direct: DirectChats | None) -> None:
        self._runtime = runtime
        self._direct = direct
        self._asked: OrderedDict[str, _Asked] = OrderedDict()

    async def command(self, context: CommandContext) -> None:
        asked = context.asked_in or context.inbound
        remaining = await self._remaining(context.tenant_id, asked.user_id)
        if remaining <= 0:
            await context.send(MessageActivityInput(text=OUT_OF_CREDITS))
            return
        token = secrets.token_urlsafe(16)
        self._asked[token] = _Asked.of(asked)
        while len(self._asked) > _MAX_PENDING:
            self._asked.popitem(last=False)
        await context.send_card(form_card(token, remaining))

    async def on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        return await guarded(self._act(ctx.activity), toast(FAILED), "teams.support.failed")

    async def _act(self, activity: AdaptiveCardInvokeActivity) -> AdaptiveCardInvokeResponse:
        actor = await card_actor(self._runtime, activity)
        if actor is None:
            return toast(DENIED)
        data = submitted_fields(activity.value.action.data)
        note = str(data.get(NOTE_INPUT) or "").strip()[:MAX_NOTE_CHARS]
        if not note:
            return toast(USAGE)
        token = str(data.get("ask") or "")
        asked = self._asked.get(token)
        if asked is None or asked.user_id != actor.user_id:
            chat = actor.conversation_id
            asked = _Asked(actor.user_id, activity.from_.name, chat, chat, activity.id, _DM_LINK)
        else:
            del self._asked[token]
        return replace_card(text_card(TITLE, await self._escalate(actor.tenant_id, asked, note)))

    async def on_ask_open(
        self, ctx: ActivityContext[TaskFetchInvokeActivity]
    ) -> TaskModuleResponse:
        """Ask a human on an answer: the form, for someone who could have asked there."""
        return await guarded(
            self._ask_open(ctx.activity), dialog_message(FAILED), "teams.support.failed"
        )

    async def on_ask_submit(
        self, ctx: ActivityContext[TaskSubmitInvokeActivity]
    ) -> TaskModuleResponse:
        return await guarded(
            self._ask_submit(ctx.activity), dialog_message(FAILED), "teams.support.failed"
        )

    async def _ask_open(self, activity: TaskFetchInvokeActivity) -> TaskModuleResponse:
        actor = await card_actor(self._runtime, activity)
        if actor is None or not activity.reply_to_id:
            return dialog_message(DENIED)
        place = AnswerPlace.of(activity, activity.reply_to_id)
        if (access := await check_answer_access(self._runtime, actor, place)) != "allowed":
            return dialog_message(refusal_text(access))
        remaining = await self._remaining(actor.tenant_id, actor.user_id)
        if remaining <= 0:
            return dialog_message(OUT_OF_CREDITS)
        return dialog(TITLE, ask_form(place.message_id, remaining))

    async def _ask_submit(self, activity: TaskSubmitInvokeActivity) -> TaskModuleResponse:
        """Decided again here: the policy may have changed while the dialog was open.

        The answer's id rides in the form; a forged one only points the
        submitter's own request at another message in the same conversation.
        """
        actor = await card_actor(self._runtime, activity)
        data = submitted_fields(activity.value.data)
        message_id = str(data.get("message") or "")
        if actor is None or not message_id:
            return dialog_message(DENIED)
        note = str(data.get(NOTE_INPUT) or "").strip()[:MAX_NOTE_CHARS]
        if not note:
            remaining = await self._remaining(actor.tenant_id, actor.user_id)
            return dialog(TITLE, ask_form(message_id, remaining, error=USAGE))
        place = AnswerPlace.of(activity, message_id)
        if (access := await check_answer_access(self._runtime, actor, place)) != "allowed":
            return dialog_message(refusal_text(access))
        teams = self._runtime.settings.teams
        link = place.link(teams.tenant_id) if teams is not None else place.message_id
        asked = _Asked(
            actor.user_id,
            activity.from_.name,
            place.conversation_id,
            place.conversation_id,
            message_id,
            link,
        )
        return dialog_message(await self._escalate(actor.tenant_id, asked, note))

    async def _remaining(self, tenant_id: uuid.UUID, user_id: str) -> int:
        async with self._runtime.sessionmaker() as session:
            used = await count_escalations_for_user(
                session, tenant_id=tenant_id, platform_user_id=user_id
            )
        allowance = self._runtime.settings.support.credits_per_user
        return remaining_credits(allowance=allowance, used=used)

    async def _escalate(self, tenant_id: uuid.UUID, asked: _Asked, note: str) -> str:
        """Record the request, post it, and say how it went."""
        runtime = self._runtime
        async with runtime.sessionmaker.begin() as session:
            thread = await get_latest_thread_session(
                session, tenant_id=tenant_id, platform="teams", thread_id=asked.thread_id
            )
            principal = await find_platform_principal(
                session, tenant_id=tenant_id, platform="teams", external_id=asked.user_id
            )
            row = await record_escalation(
                session,
                tenant_id=tenant_id,
                account_id=principal.account_id if principal is not None else None,
                platform="teams",
                platform_user_id=asked.user_id,
                channel_id=asked.conversation_id,
                message_id=asked.message_id,
                ma_session_id=thread.ma_session_id if thread is not None else None,
                note=note,
                allowance=runtime.settings.support.credits_per_user,
            )
        if row is None:
            log.info("support.out_of_credits", tenant_id=str(tenant_id))
            return OUT_OF_CREDITS
        who = f"{asked.user_name or 'Someone'} (Teams user {asked.user_id})"
        body = f"**Human support requested** by {who}\n{asked.link}\n\n{note}"
        delivered = await post_to_support_channel(runtime, self._direct, body)
        if delivered:
            async with runtime.sessionmaker.begin() as session:
                await mark_delivered(session, escalation_id=row.id)
        log.info("support.escalation_recorded", escalation_id=str(row.id), delivered=delivered)
        if not delivered:
            return RECORDED_UNDELIVERED
        return received_text(remaining=await self._remaining(tenant_id, asked.user_id))


async def post_to_support_channel(
    runtime: TeamsRuntime, direct: DirectChats | None, body: str
) -> bool:
    """Post `body` to the escalation channel; False when it did not land."""
    settings = runtime.settings
    channel = settings.support.escalation_channel_id
    try:
        if channel is None or (_teams_channel(channel) and direct is None):
            return False
        if _teams_channel(channel) and direct is not None:
            await direct.post(channel, body)
            return True
        if settings.discord is None:
            return False
        token = settings.discord.bot_token.get_secret_value()
        response = await runtime.http_client.post(
            f"{_DISCORD_API}/channels/{channel}/messages",
            headers={"Authorization": f"Bot {token}"},
            json={"content": body[:2000], "allowed_mentions": {"parse": []}},
        )
        response.raise_for_status()
        return True
    except TEAMS_SEND_ERRORS as exc:
        log.warning("support.channel_undeliverable", err_type=type(exc).__name__)
        return False
