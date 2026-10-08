"""The `support` command: ask a human, spending one of the person's support credits.

Discord asks with a reaction on an answer; here `support` posts a form in the
1:1 chat, answered there like every command, and sending it spends the credit.
It is a bare word, so prose such as "support vector machines" stays a turn. As
on Discord, the row is committed before the post and stamped delivered only
once the post lands, so a failed post loses nothing. Requests go to
`DAIMON_SUPPORT__ESCALATION_CHANNEL_ID`: a Teams channel (`19:…`) gets a post
from this bot, any other id is a Discord channel posted to with the Discord
bot token. `enabled` decides whether the command is registered at all.
"""

from __future__ import annotations

import secrets
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import quote

import structlog
from daimon.adapters.teams.card_actions import (
    FAILED,
    button,
    card_actor,
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
)
from microsoft_teams.apps import ActivityContext
from microsoft_teams.cards import ActionSet, AdaptiveCard, TextInput

__all__ = [
    "OUT_OF_CREDITS",
    "RECEIVED",
    "RECORDED_UNDELIVERED",
    "VERB",
    "SupportCommand",
    "enabled",
]

log = structlog.get_logger(__name__)

VERB = "support"
NOTE_INPUT = "note"
_DISCORD_API = "https://discord.com/api/v10"
_DM_LINK = "(sent in the 1:1 chat)"
_MAX_PENDING = 256  # forms whose "asked in" is remembered; older ones name the 1:1 chat
MAX_NOTE_CHARS = 4000  # Discord's support modal cap.
TITLE = "Ask a person"
FORM_TEXT = "What do you need help with?\n{remaining} requests left"
USAGE = "Write a few words first."
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


def _link(asked: TeamsInbound) -> str:
    """A link to the message `support` was typed in; the 1:1 chat has none to share."""
    if asked.kind == "dm":
        return _DM_LINK
    channel, _, root = asked.conversation_id.partition(";messageid=")
    query = f"tenantId={asked.entra_tenant_id}" + (f"&parentMessageId={root}" if root else "")
    return f"https://teams.microsoft.com/l/message/{quote(channel)}/{asked.activity_id}?{query}"


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


def form_card(token: str, remaining: int) -> AdaptiveCard:
    note = TextInput(id=NOTE_INPUT, is_multiline=True, max_length=MAX_NOTE_CHARS)
    return AdaptiveCard(
        body=[
            heading(TITLE),
            *text_lines(FORM_TEXT.format(remaining=remaining)),
            note,
            ActionSet(actions=[button(VERB, "Send", "send", ask=token)]),
        ],
        fallback_text=TITLE,
    )


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
        delivered = await self._post(body)
        if delivered:
            async with runtime.sessionmaker.begin() as session:
                await mark_delivered(session, escalation_id=row.id)
        log.info("support.escalation_recorded", escalation_id=str(row.id), delivered=delivered)
        if not delivered:
            return RECORDED_UNDELIVERED
        return received_text(remaining=await self._remaining(tenant_id, asked.user_id))

    async def _post(self, body: str) -> bool:
        """Post `body` to the escalation channel; False when it did not land."""
        settings = self._runtime.settings
        channel = settings.support.escalation_channel_id
        try:
            if channel is None or (_teams_channel(channel) and self._direct is None):
                return False
            if _teams_channel(channel) and self._direct is not None:
                await self._direct.post(channel, body)
                return True
            if settings.discord is None:
                return False
            token = settings.discord.bot_token.get_secret_value()
            response = await self._runtime.http_client.post(
                f"{_DISCORD_API}/channels/{channel}/messages",
                headers={"Authorization": f"Bot {token}"},
                json={"content": body[:2000], "allowed_mentions": {"parse": []}},
            )
            response.raise_for_status()
            return True
        except TEAMS_SEND_ERRORS as exc:
            log.warning("support.channel_undeliverable", err_type=type(exc).__name__)
            return False
