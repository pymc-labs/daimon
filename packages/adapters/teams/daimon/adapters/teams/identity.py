"""Verified identity for inbound Teams messages.

The only place inbound activity fields are trusted. `parse_inbound` checks an
authenticated activity is a channel @mention (or a quote of one of the bot's
messages), an unmentioned human reply in a channel thread (a participation
candidate, `unprompted`), or a personal-chat message from the configured Entra
tenant, and reduces it to `TeamsInbound`. `live_tenant_id`
then maps it to the organisation's live daimon tenant; a 1:1 chat names its
organisation, so it needs no DM workspace choice. Group chats are refused for now.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from daimon.adapters.teams.attachments import InboundFile, parse_attachments
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.tenants import get_tenant
from microsoft_teams.api import MessageActivity
from microsoft_teams.api.activities.utils import StripMentionsTextOptions, strip_mentions_text
from microsoft_teams.api.models.entity.quoted_reply_entity import QuotedReplyData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

DENIED = "This agent is not available for this account."
GROUP_CHAT_UNSUPPORTED = "I answer in channels and in 1:1 chats, not in group chats yet."
INPUT_TOO_LONG = "That message is too long for this agent. Please shorten it."
TEXT_ONLY = "Send your question as text, an image or a file."
MAX_INBOUND_MESSAGE_BYTES = 16 * 1024
# Where a quote sits in the text; its sender and preview ride in a `quotedReply` entity.
_QUOTED = re.compile(r'<quoted\s+messageId="([^"]*)"\s*/>')


def canonical_uuid(value: object) -> str | None:
    """Lowercase UUID form, or None for anything that is not a UUID string."""
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


@dataclass(frozen=True)
class TeamsInbound:
    """Verified facts about one message. Content is never persisted by the adapter.

    `conversation_id` is where replies go: a channel thread's id
    (`19:…;messageid=<root>`) or a personal chat's. `thread_id` is the session
    and routing key: the conversation, or the live setup conversation a
    personal chat was switched into. `channel_id` is the config-cascade
    channel: the channel for a thread, the chat itself for a DM. `user_id` is
    the sender's Entra object id. `team_id` is a channel's Bot Framework team
    id; `team_group_id` its Entra group id, when the activity carries it.
    `unprompted` marks a thread reply nobody addressed to the bot: it may only
    enter organic thread participation, never the mention path.
    """

    kind: Literal["dm", "channel"]
    entra_tenant_id: str
    user_id: str
    conversation_id: str
    channel_id: str
    activity_id: str
    text: str
    service_url: str | None
    bot_name: str | None = None
    files: tuple[InboundFile, ...] = ()
    setup_thread_id: str | None = None
    team_id: str | None = None
    team_group_id: str | None = None
    unprompted: bool = False
    user_name: str | None = None

    @property
    def thread_id(self) -> str:
        return self.setup_thread_id or self.conversation_id


@dataclass(frozen=True)
class Refusal:
    """Tell the sender why no turn runs. `None` text means drop silently."""

    text: str | None


def _thread_id(conversation_id: str, activity_id: str) -> str:
    # Teams puts the thread root in the id; a root post without it is its own root.
    if ";messageid=" in conversation_id:
        return conversation_id
    return f"{conversation_id};messageid={activity_id}"


def _bare_bot_id(value: str) -> str:
    # A bot's Bot Framework id is `28:<app id>`; accept a sender id in either form.
    return value.lower().removeprefix("28:")


def quotes_bot(activity: MessageActivity) -> bool:
    """Whether the message quotes one the bot sent: Teams' reply-to-the-bot, as on Discord."""
    bot = _bare_bot_id(activity.recipient.id)
    return any(
        _bare_bot_id(entity.quoted_reply.sender_id or "") == bot
        for entity in activity.get_quoted_messages()
    )


def render_quotes(text: str, quotes: Mapping[str, QuotedReplyData]) -> str:
    """Each `<quoted messageId=…/>` placeholder, in place, as the quote it stands for."""

    def describe(match: re.Match[str]) -> str:
        quote = quotes.get(match.group(1))
        if quote is None or quote.is_reply_deleted:
            return "[quoted message unavailable]\n"
        return f'[quoting {quote.sender_name or "unknown"}: "{quote.preview or ""}"]\n'

    return _QUOTED.sub(describe, text)


def _is_thread_reply_by_person(activity: MessageActivity) -> bool:
    """A human's reply under a channel post: what organic participation may screen.

    Root posts stay mention-only, and bots (the bot itself included) never
    qualify: the mention path is the only automation entry point, as on Discord.
    """
    sender = activity.from_
    if sender.id.startswith("28:") or sender.role == "bot" or sender.id == activity.recipient.id:
        return False
    _, _, root = activity.conversation.id.partition(";messageid=")
    return bool(root) and root != activity.id


def parse_inbound(
    activity: MessageActivity, *, configured_tenant: str, service_url: str | None
) -> TeamsInbound | Refusal:
    """Reduce an authenticated activity to verified facts, or refuse it.

    An unaddressed thread reply comes back `unprompted`, and every refusal of
    one is silent: nobody asked, so nothing is owed.
    """
    conversation = activity.conversation
    channel_data = activity.channel_data
    kind = conversation.conversation_type
    if kind == "groupChat" or conversation.is_group and kind != "channel":
        return Refusal(GROUP_CHAT_UNSUPPORTED)
    if kind not in ("personal", "channel"):
        return Refusal(DENIED)
    addressed = kind != "channel" or activity.is_recipient_mentioned() or quotes_bot(activity)
    if not addressed and not _is_thread_reply_by_person(activity):
        return Refusal(None)  # Channel chatter the bot was not asked about.

    tenant = canonical_uuid(configured_tenant)
    channel_tenant = None
    if channel_data is not None and channel_data.tenant is not None:
        channel_tenant = channel_data.tenant.id
    user_id = canonical_uuid(activity.from_.aad_object_id)
    verified = (
        tenant is not None
        and canonical_uuid(conversation.tenant_id) == tenant
        and canonical_uuid(channel_tenant) == tenant
        and user_id is not None
        and activity.channel_id == "msteams"
        and bool(conversation.id.strip())
        and bool(activity.id.strip())
    )
    if not verified or tenant is None or user_id is None:
        return Refusal(DENIED if addressed else None)

    # Drop the bot's own mention; keep other people's names, minus the tags.
    bot_only = StripMentionsTextOptions(account_id=activity.recipient.id)
    text = strip_mentions_text(activity, bot_only) or ""
    others = activity.model_copy(update={"text": text})
    text = strip_mentions_text(others, StripMentionsTextOptions(tag_only=True)) or ""
    quotes = {e.quoted_reply.message_id: e.quoted_reply for e in activity.get_quoted_messages()}
    text = render_quotes(text, quotes).strip()
    files = parse_attachments(activity.attachments or [], personal=kind == "personal")
    if not addressed and not text and not files:
        return Refusal(None)
    # A bare channel mention asks about the thread, which the turn replays.
    if not text and not files and kind == "personal":
        return Refusal(TEXT_ONLY)
    if len(text.encode("utf-8")) > MAX_INBOUND_MESSAGE_BYTES:
        return Refusal(INPUT_TOO_LONG if addressed else None)

    team = channel_data.team if channel_data is not None else None
    if kind == "personal":
        conversation_id = channel_id = conversation.id
        team = None
    else:
        conversation_id = _thread_id(conversation.id, activity.id)
        channel_id = conversation_id.split(";", 1)[0]
    return TeamsInbound(
        kind="dm" if kind == "personal" else "channel",
        entra_tenant_id=tenant,
        user_id=user_id,
        conversation_id=conversation_id,
        channel_id=channel_id,
        activity_id=activity.id,
        text=text,
        service_url=service_url,
        bot_name=activity.recipient.name,
        files=files,
        team_id=team.id if team is not None else None,
        team_group_id=canonical_uuid(team.aad_group_id) if team is not None else None,
        unprompted=not addressed,
        user_name=activity.from_.name,
    )


async def live_tenant_id(
    sessionmaker: async_sessionmaker[AsyncSession], entra_tenant_id: str
) -> uuid.UUID | None:
    """The organisation's tenant id while it is provisioned and not archived."""
    tenant_id = derive_tenant_uuid(platform="teams", workspace_id=entra_tenant_id)
    async with sessionmaker() as session:
        tenant = await get_tenant(session, tenant_id)
    live = (
        tenant is not None
        and tenant.platform == "teams"
        and tenant.provision_status == "ready"
        and tenant.archived_at is None
    )
    return tenant_id if live else None
