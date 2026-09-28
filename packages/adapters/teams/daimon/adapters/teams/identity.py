"""Verified identity for inbound Teams messages.

The only place inbound activity fields are trusted. `parse_inbound` checks an
authenticated activity is a channel @mention or a personal-chat message from
the configured Entra tenant and reduces it to `TeamsInbound`. `live_tenant_id`
then maps it to the organisation's live daimon tenant; a 1:1 chat names its
organisation, so it needs no DM workspace choice. Group chats are refused for now.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from daimon.adapters.teams.attachments import InboundFile, parse_attachments
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.tenants import get_tenant
from microsoft_teams.api import MessageActivity
from microsoft_teams.api.activities.utils import StripMentionsTextOptions, strip_mentions_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

DENIED = "This agent is not available for this account."
GROUP_CHAT_UNSUPPORTED = "I answer in channels and in 1:1 chats, not in group chats yet."
INPUT_TOO_LONG = "That message is too long for this agent. Please shorten it."
TEXT_ONLY = "Send your question as text, an image or a file."
MAX_INBOUND_MESSAGE_BYTES = 16 * 1024


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
    the sender's Entra object id.
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


def parse_inbound(
    activity: MessageActivity, *, configured_tenant: str, service_url: str | None
) -> TeamsInbound | Refusal:
    """Reduce an authenticated activity to verified facts, or refuse it."""
    conversation = activity.conversation
    channel_data = activity.channel_data
    kind = conversation.conversation_type
    if kind == "groupChat" or conversation.is_group and kind != "channel":
        return Refusal(GROUP_CHAT_UNSUPPORTED)
    if kind not in ("personal", "channel"):
        return Refusal(DENIED)
    if kind == "channel" and not activity.is_recipient_mentioned():
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
        return Refusal(DENIED)

    # Drop the bot's own mention; keep other people's names, minus the tags.
    bot_only = StripMentionsTextOptions(account_id=activity.recipient.id)
    text = strip_mentions_text(activity, bot_only) or ""
    others = activity.model_copy(update={"text": text})
    text = (strip_mentions_text(others, StripMentionsTextOptions(tag_only=True)) or "").strip()
    files = parse_attachments(activity.attachments or [], personal=kind == "personal")
    if not text and not files:
        return Refusal(TEXT_ONLY)
    if len(text.encode("utf-8")) > MAX_INBOUND_MESSAGE_BYTES:
        return Refusal(INPUT_TOO_LONG)

    if kind == "personal":
        conversation_id = channel_id = conversation.id
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
