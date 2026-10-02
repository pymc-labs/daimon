"""The `support` command: ask a human, spending one of the person's support credits.

Discord asks with a reaction on an answer; here it is a command whose
argument is the note, answered in the 1:1 chat like every command. As on
Discord, the row is committed before the post and stamped delivered only once
the post lands, so a failed post loses nothing. Requests go to
`DAIMON_SUPPORT__ESCALATION_CHANNEL_ID`: a Teams channel (`19:…`) gets a post
from this bot, any other id is a Discord channel posted to with the Discord
bot token. `enabled` decides whether the command is registered at all.
"""

from __future__ import annotations

from urllib.parse import quote

import structlog
from daimon.adapters.teams.commands import CommandContext
from daimon.adapters.teams.direct_chats import DirectChats
from daimon.adapters.teams.identity import TeamsInbound
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
from daimon.core.support_escalation import remaining_credits
from microsoft_teams.api import MessageActivityInput

__all__ = ["SupportCommand", "enabled"]

log = structlog.get_logger(__name__)

_DISCORD_API = "https://discord.com/api/v10"
MAX_NOTE_CHARS = 4000  # Discord's support modal cap.
USAGE = "Tell me what you need help with: `support <your question>`. A person will follow up."
OUT_OF_CREDITS = (
    "You've used all your human-support requests. "
    "Contact us if you'd like more added to your account."
)
RECEIVED = (
    "Thanks, your request has been recorded and someone will follow up. You have {remaining} left."
)
RECORDED_UNDELIVERED = "Thanks, your request has been recorded and someone will follow up."


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
        return "(sent in the 1:1 chat)"
    channel, _, root = asked.conversation_id.partition(";messageid=")
    query = f"tenantId={asked.entra_tenant_id}" + (f"&parentMessageId={root}" if root else "")
    return f"https://teams.microsoft.com/l/message/{quote(channel)}/{asked.activity_id}?{query}"


class SupportCommand:
    """`support <note>`; Teams-channel requests are posted through `direct`."""

    def __init__(self, direct: DirectChats | None) -> None:
        self._direct = direct

    async def command(self, context: CommandContext) -> None:
        note = context.args.strip()[:MAX_NOTE_CHARS]
        if not note:
            await _say(context, USAGE)
            return
        runtime, asked = context.runtime, context.asked_in or context.inbound
        allowance = runtime.settings.support.credits_per_user
        async with runtime.sessionmaker.begin() as session:
            thread = await get_latest_thread_session(
                session, tenant_id=context.tenant_id, platform="teams", thread_id=asked.thread_id
            )
            principal = await find_platform_principal(
                session, tenant_id=context.tenant_id, platform="teams", external_id=asked.user_id
            )
            row = await record_escalation(
                session,
                tenant_id=context.tenant_id,
                account_id=principal.account_id if principal is not None else None,
                platform="teams",
                platform_user_id=asked.user_id,
                channel_id=asked.conversation_id,
                message_id=asked.activity_id,
                ma_session_id=thread.ma_session_id if thread is not None else None,
                note=note,
                allowance=allowance,
            )
        if row is None:
            log.info("support.out_of_credits", tenant_id=str(context.tenant_id))
            await _say(context, OUT_OF_CREDITS)
            return
        who = f"{asked.user_name or 'Someone'} (Teams user {asked.user_id})"
        delivered = await self._post(
            runtime, f"**Human support requested** by {who}\n{_link(asked)}\n\n{note}"
        )
        if delivered:
            async with runtime.sessionmaker.begin() as session:
                await mark_delivered(session, escalation_id=row.id)
        log.info("support.escalation_recorded", escalation_id=str(row.id), delivered=delivered)
        if not delivered:
            await _say(context, RECORDED_UNDELIVERED)
            return
        async with runtime.sessionmaker() as session:
            used = await count_escalations_for_user(
                session, tenant_id=context.tenant_id, platform_user_id=asked.user_id
            )
        await _say(
            context, RECEIVED.format(remaining=remaining_credits(allowance=allowance, used=used))
        )

    async def _post(self, runtime: TeamsRuntime, body: str) -> bool:
        """Post `body` to the escalation channel; False when it did not land."""
        settings = runtime.settings
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


async def _say(context: CommandContext, text: str) -> None:
    await context.send(MessageActivityInput(text=text))
