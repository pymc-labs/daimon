"""Verified identity for inbound Teams messages.

The only place inbound activity fields are trusted. `parse_inbound` checks an
authenticated activity is a channel @mention (or a quote of one of the bot's
messages), an unmentioned human reply in a channel thread (a participation
candidate, `unprompted`), or a personal-chat message from the configured Entra
tenant, and reduces it to `TeamsInbound`. `parse_graph_message` does the
same for a message Graph reported from a subscribed private or shared channel,
where Teams sends the bot only @mentions. `live_tenant_id`
then maps it to the organisation's live daimon tenant; a 1:1 chat names its
organisation, so it needs no DM workspace choice. Group chats are refused for now.

A channel must be the configured tenant's (`conversation.tenantId`, the host
tenant), but its sender may be from another one: a shared channel's external
participant (B2B direct connect), who stays in their home tenant. Any foreign
tenant the activity names for its sender (`foreign_tenant`) marks them
`is_external`; `externals.ExternalParticipants` asks the member lists when
the activity names none, and finds guests. A 1:1 chat naming a foreign tenant
is refused, as before.
"""

from __future__ import annotations

import html
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal

from daimon.adapters.teams.attachments import (
    InboundFile,
    parse_attachments,
    parse_graph_attachments,
)
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.name_mentions import mentions_by_name, strip_name_mentions
from daimon.core.stores.domain import TeamsChannelSubscriptionRow
from daimon.core.stores.tenants import get_tenant
from daimon.core.teams_graph import (
    GraphMessage,
    GraphSender,
    html_to_text,
    message_references,
    message_text,
)
from microsoft_teams.api import Account, MessageActivity
from microsoft_teams.api.activities.utils import StripMentionsTextOptions, strip_mentions_text
from microsoft_teams.api.models.channel_data import ChannelData
from microsoft_teams.api.models.entity.quoted_reply_entity import QuotedReplyData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

DENIED = "This agent is not available for this account."
GROUP_CHAT_UNSUPPORTED = "I answer in channels and in 1:1 chats, not in group chats yet."
INPUT_TOO_LONG = "That message is too long for this agent. Please shorten it."
TEXT_ONLY = "Send your question as text, an image or a file."
MAX_INBOUND_MESSAGE_BYTES = 16 * 1024
# The channel whose id is the team's: Teams sends no name for it.
GENERAL_CHANNEL = "General"
# Where a quote sits in the text; its sender and preview ride in a `quotedReply` entity.
_QUOTED = re.compile(r'<quoted\s+messageId="([^"]*)"\s*/>')
# `[^<>]`, not `[^>]`: many unclosed "<blockquote" tags would otherwise scan quadratically.
_BLOCKQUOTE_TAG = re.compile(r"<(/?)blockquote\b([^<>]*)>", re.IGNORECASE)
_ENTITY = re.compile(r"&(?:#[0-9]+|#x[0-9a-f]+|[a-z][a-z0-9]*);", re.IGNORECASE)
_REPLY_ITEMTYPE = re.compile(r'itemtype\s*=\s*"[^"]*schema\.skype\.com/reply"', re.IGNORECASE)


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
    `by_name` marks a channel post or thread reply by a person that typed
    "@<bot name>" without a real mention: the handler reads the tenant's
    plain-text switch and treats it as a mention when on, as `unprompted`
    says otherwise (a thread reply) or ignores it (a root post).
    `composed_ids` are the earlier queued messages folded into this one.
    `timestamp` is when it was sent (ISO 8601); the channel and team names
    come from the activity, General's filled in. `is_external` marks a sender
    answered as from another organisation, `is_external_known` that it rests on
    positive evidence (`externals.Membership`), `home_tenant_id` their tenant.
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
    by_name: bool = False
    user_name: str | None = None
    composed_ids: tuple[str, ...] = ()
    timestamp: str | None = None
    channel_name: str | None = None
    channel_type: str | None = None
    team_name: str | None = None
    is_external: bool = False
    is_external_known: bool = False
    home_tenant_id: str | None = None

    @property
    def thread_id(self) -> str:
        return self.setup_thread_id or self.conversation_id

    @property
    def message_ids(self) -> tuple[str, ...]:
        """Every message this turn answers, oldest first."""
        return (*self.composed_ids, self.activity_id)


@dataclass(frozen=True)
class Refusal:
    """Tell the sender why no turn runs. `None` text means drop silently.

    `by_name` refusals answer a typed "@<bot name>": sent only with the
    tenant's plain-text switch on.
    """

    text: str | None
    by_name: bool = False


def foreign_tenant(sender: Account, channel_data: ChannelData | None, *, ours: str) -> str | None:
    """A tenant id the activity names for its sender that is not `ours`, or None.

    Undocumented for a cross-tenant sender, so each place one may appear is
    read: `channelData.tenant.id`, `from.tenantId`, `from.properties.tenantId`.
    A value that is not a UUID counts as foreign: it is not ours.
    """
    tenant = channel_data.tenant if channel_data is not None else None
    properties = sender.properties or {}
    for candidate in (tenant.id if tenant else None, sender.tenant_id, properties.get("tenantId")):
        if isinstance(candidate, str) and candidate.strip() and canonical_uuid(candidate) != ours:
            return canonical_uuid(candidate) or candidate.strip()
    return None


def delivery_key(conversation_id: str, message_id: str) -> tuple[str, str]:
    """What a message is claimed under so it runs once, from Bot Framework or Graph alike.

    Whether a thread id names its root differs by source, so the key is the
    channel (or chat) and the message id, which Teams keeps unique within it.
    """
    return conversation_id.split(";", 1)[0], message_id


def channel_thread_id(conversation_id: str, activity_id: str) -> str:
    # Teams puts the thread root in the id; a root post without it is its own root.
    if ";messageid=" in conversation_id:
        return conversation_id
    return f"{conversation_id};messageid={activity_id}"


def _bare_bot_id(value: str) -> str:
    # A bot's Bot Framework id is `28:<app id>`; accept a sender id in either form.
    return value.lower().removeprefix("28:")


class _QuoteSenders(HTMLParser):
    """Who each top-level HTML reply quote is from: `<strong itemprop="mri" itemid=…>`."""

    def __init__(self) -> None:
        super().__init__()
        self.senders: list[str] = []
        self._depth = 0  # blockquotes open inside a reply quote, itself included

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        reply = (values.get("itemtype") or "").lower().endswith("schema.skype.com/reply")
        if tag == "blockquote" and (self._depth or reply):
            self._depth += 1
        elif self._depth == 1 and values.get("itemprop") == "mri":
            self.senders.append(values.get("itemid") or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "blockquote" and self._depth:
            self._depth -= 1


def _html_quote_senders(activity: MessageActivity) -> list[str]:
    # Clients that send no quotedReply entity still send the older blockquote, in the
    # text or the `text/html` body. Its sender is unverified, but a forged one only
    # addresses the bot, which any mention does too.
    bodies = [activity.text or ""] + [
        a.content
        for a in activity.attachments or []
        if a.content_type == "text/html" and isinstance(a.content, str)
    ]
    return [sender for body in bodies for sender in _quote_senders(body)]


def _quote_senders(body: str) -> list[str]:
    parser = _QuoteSenders()
    parser.feed(body)
    parser.close()
    return parser.senders


def quotes_bot(activity: MessageActivity) -> bool:
    """Whether the message quotes one the bot sent: Teams' reply-to-the-bot, as on Discord."""
    bot = _bare_bot_id(activity.recipient.id)
    senders = [entity.quoted_reply.sender_id or "" for entity in activity.get_quoted_messages()]
    return any(_bare_bot_id(s) == bot for s in senders + _html_quote_senders(activity))


def describe_quote(sender_name: str | None, preview: str | None) -> str:
    return f'[quoting {sender_name or "unknown"}: "{preview or ""}"]\n'


def render_quotes(text: str, quotes: Mapping[str, QuotedReplyData]) -> str:
    """Each `<quoted messageId=…/>` placeholder, in place, as the quote it stands for."""

    def describe(match: re.Match[str]) -> str:
        quote = quotes.get(match.group(1))
        if quote is None or quote.is_reply_deleted:
            return "[quoted message unavailable]\n"
        return describe_quote(quote.sender_name, quote.preview)

    return _QUOTED.sub(describe, text)


def _typed_text(text: str) -> str:
    """The text the sender typed: reply quotes cut out, HTML entities decoded.

    A blockquote reply carries the quoted message in `text`, so a quote of
    someone's "@Daimon" would otherwise read as this sender naming the bot.
    """
    if "@" not in text and "&" not in text:
        return text  # no "@", typed or as an entity: nothing to match
    return html.unescape(_without_reply_quotes(text))


def _without_reply_quotes(text: str) -> str:
    kept: list[str] = []
    start = depth = 0
    for tag in _BLOCKQUOTE_TAG.finditer(text):
        closing = tag.group(1) == "/"
        if not depth and not closing and _REPLY_ITEMTYPE.search(tag.group(2)):
            kept.append(text[start : tag.start()])
            depth = 1
        elif depth:
            depth += -1 if closing else 1
            if not depth:
                start = tag.end()
    if not depth:
        kept.append(text[start:])
    return "".join(kept)


def strip_typed_name(text: str, name: str | None) -> str:
    """`text` without the typed "@<name>", its entities read as `_typed_text` reads them.

    Markup entities stay encoded; the rest decode, so "@Daimon&nbsp;Bot" goes too.
    """

    def decode(entity: re.Match[str]) -> str:
        char = html.unescape(entity[0])
        return entity[0] if char in ("<", ">", "&") else char

    return strip_name_mentions(_ENTITY.sub(decode, text), (name,))


def _is_from_person(activity: MessageActivity) -> bool:
    sender = activity.from_
    return not (
        sender.id.startswith("28:") or sender.role == "bot" or sender.id == activity.recipient.id
    )


def _is_thread_reply_by_person(activity: MessageActivity) -> bool:
    """A human's reply under a channel post: what organic participation may screen.

    Root posts stay mention-only, and bots (the bot itself included) never
    qualify: the mention path is the only automation entry point, as on Discord.
    """
    if not _is_from_person(activity):
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
    thread_reply = not addressed and _is_thread_reply_by_person(activity)
    # Typed text, not a mention: whether it counts is the tenant's switch, read later.
    by_name = (
        not addressed
        and _is_from_person(activity)
        and mentions_by_name(_typed_text(activity.text or ""), (activity.recipient.name,))
    )
    if not addressed and not thread_reply and not by_name:
        return Refusal(None)  # Channel chatter the bot was not asked about.

    tenant = canonical_uuid(configured_tenant)
    channel_tenant = None
    if channel_data is not None and channel_data.tenant is not None:
        channel_tenant = channel_data.tenant.id
    user_id = canonical_uuid(activity.from_.aad_object_id)
    home = foreign_tenant(activity.from_, channel_data, ours=tenant) if tenant else None
    # The conversation is always the host's. A channel may hold another
    # organisation's people; a 1:1 chat holds only ours, as before.
    sender_verified = kind == "channel" or (
        canonical_uuid(channel_tenant) == tenant and home is None
    )
    verified = (
        tenant is not None
        and canonical_uuid(conversation.tenant_id) == tenant
        and sender_verified
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
    team = channel_data.team if channel_data is not None else None
    channel = channel_data.channel if channel_data is not None else None
    if kind == "personal":
        conversation_id = channel_id = conversation.id
        team = channel = None
    else:
        conversation_id = channel_thread_id(conversation.id, activity.id)
        channel_id = conversation_id.split(";", 1)[0]
    team_id = team.id if team is not None else None
    inbound = TeamsInbound(
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
        team_id=team_id,
        team_group_id=canonical_uuid(team.aad_group_id) if team is not None else None,
        unprompted=thread_reply,
        by_name=by_name,
        user_name=activity.from_.name,
        timestamp=activity.timestamp.isoformat() if activity.timestamp else None,
        channel_name=_channel_name(channel_id, team_id, channel.name if channel else None),
        channel_type=channel.type if channel is not None else None,
        team_name=team.name if team is not None else None,
        is_external=home is not None,
        is_external_known=home is not None,
        home_tenant_id=home,
    )
    return _admitted(inbound, addressed=addressed)


def _channel_name(channel_id: str, team_id: str | None, name: str | None) -> str | None:
    if team_id is not None and channel_id == team_id:
        return name or GENERAL_CHANNEL  # Teams names General nowhere
    return name


def _admitted(inbound: TeamsInbound, *, addressed: bool) -> TeamsInbound | Refusal:
    """The checks every source shares once a message is read: empty, text-only, too long."""
    if not addressed and not inbound.text and not inbound.files:
        return Refusal(None)
    # A bare channel mention asks about the thread, which the turn replays.
    if not inbound.text and not inbound.files and inbound.kind == "dm":
        return Refusal(TEXT_ONLY)
    if len(inbound.text.encode("utf-8")) > MAX_INBOUND_MESSAGE_BYTES:
        by_name = inbound.by_name
        return Refusal(INPUT_TOO_LONG if addressed or by_name else None, by_name=by_name)
    return inbound


def _is_app(identity: GraphSender | None, app_id: str) -> bool:
    application = identity.application if identity is not None else None
    return application is not None and (application.id or "").lower() == app_id


def _display_name(identity: GraphSender | None) -> str | None:
    who = identity and (identity.application or identity.user)
    return who.display_name if who else None


def parse_graph_message(
    message: GraphMessage,
    channel: TeamsChannelSubscriptionRow,
    *,
    configured_tenant: str,
    bot_app_id: str,
) -> TeamsInbound | Refusal:
    """`parse_inbound` for a message Graph reported from a subscribed channel.

    Only a person's new message counts. One that @mentions the bot is refused
    silently too: Bot Framework delivers it. A Reply on one of the bot's
    messages (a `messageReference` naming the bot's app id) is addressed, as a
    quote is there; the rest is screened as an unmentioned channel message.
    """
    sender = message.sender
    user = sender.user if sender is not None else None
    bot = bot_app_id.lower()
    if (
        message.message_type != "message"
        or message.deleted_date_time is not None
        or user is None
        or (sender is not None and sender.application is not None)
        or any(_is_app(mention.mentioned, bot) for mention in message.mentions)
    ):
        return Refusal(None)
    is_html = message.body.content_type == "html"
    body = message.body.content or ""
    references = message_references(message)
    addressed = any(_is_app(ref.message_sender, bot) for ref in references) or (
        is_html and any(_bare_bot_id(s) == bot for s in _quote_senders(body))
    )
    root = message.reply_to_id or message.id
    thread_reply = not addressed and root != message.id
    typed = html_to_text(_without_reply_quotes(body)) if is_html else body
    by_name = not addressed and mentions_by_name(typed, (channel.bot_name,))
    if not addressed and not thread_reply and not by_name:
        return Refusal(None)

    tenant = canonical_uuid(configured_tenant)
    user_id = canonical_uuid(user.id)
    if tenant is None or user_id is None or not message.id.strip():
        return Refusal(DENIED if addressed else None)
    # Graph's sender tenant is not trusted to tell a guest from an external member (a guest
    # may show their home tenant): the roster lookup classifies both, as for Bot Framework.
    quoted = "".join(
        describe_quote(_display_name(ref.message_sender), ref.message_preview) for ref in references
    )
    inbound = TeamsInbound(
        kind="channel",
        entra_tenant_id=tenant,
        user_id=user_id,
        conversation_id=f"{channel.channel_id};messageid={root}",
        channel_id=channel.channel_id,
        activity_id=message.id,
        text=(quoted + message_text(message)).strip(),
        service_url=channel.service_url,
        bot_name=channel.bot_name,
        files=parse_graph_attachments(message),
        team_id=channel.team_id,
        team_group_id=channel.group_id,
        unprompted=thread_reply,
        by_name=by_name,
        user_name=user.display_name,
        timestamp=message.created_date_time,
        channel_name=_channel_name(channel.channel_id, channel.team_id, channel.channel_name),
        channel_type=channel.channel_type,
        team_name=channel.team_name,
    )
    return _admitted(inbound, addressed=addressed)


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
