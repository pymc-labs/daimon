"""parse_inbound and live_tenant_id: fail-closed identity for channels and 1:1 chats."""

from __future__ import annotations

import time
import uuid
from typing import Any, cast

import pytest
from daimon.adapters.teams.attachments import InboundFile
from daimon.adapters.teams.identity import (
    DENIED,
    INPUT_TOO_LONG,
    MAX_INBOUND_MESSAGE_BYTES,
    TEXT_ONLY,
    Refusal,
    TeamsInbound,
    foreign_tenant,
    kind_of,
    live_tenant_id,
    parse_inbound,
    strip_typed_name,
)
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.tenants import set_provision_status
from microsoft_teams.api import Account, MessageActivity
from microsoft_teams.api.models.channel_data import ChannelData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    BOT_ACCOUNT_ID,
    BOT_CLIENT_ID,
    CHANNEL_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    GROUP_CHAT_ID,
    SERVICE_URL,
    THREAD_ID,
    make_channel_activity,
    make_group_activity,
    make_message_activity,
)

TENANT_UUID = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)


def _parse(payload: dict[str, object]) -> TeamsInbound | Refusal:
    activity = MessageActivity.model_validate(payload)
    return parse_inbound(activity, configured_tenant=ENTRA_TENANT_ID, service_url=SERVICE_URL)


def test_personal_message_is_a_dm_keyed_on_the_chat() -> None:
    inbound = _parse(make_message_activity(text="  hello there  "))
    assert inbound == TeamsInbound(
        kind="dm",
        entra_tenant_id=ENTRA_TENANT_ID,
        user_id=AAD_OBJECT_ID,
        conversation_id=CONVERSATION_ID,
        channel_id=CONVERSATION_ID,
        activity_id="activity-1",
        text="hello there",
        service_url=SERVICE_URL,
        sender_id=f"29:{AAD_OBJECT_ID}",
    )


def test_channel_mention_keys_on_the_thread_and_strips_the_bot_mention() -> None:
    inbound = _parse(make_channel_activity(text="fit a model"))
    assert isinstance(inbound, TeamsInbound)
    assert inbound.kind == "channel"
    assert inbound.conversation_id == THREAD_ID
    assert inbound.channel_id == CHANNEL_ID
    assert inbound.text == "fit a model"


def test_a_channel_message_carries_its_time_and_names_general_by_name() -> None:
    payload = make_channel_activity(conversation_id="19:team@thread.tacv2")
    payload["timestamp"] = "2026-10-02T09:00:00Z"
    payload["channelData"]["team"]["name"] = "Labs"  # type: ignore[index]
    inbound = _parse(payload)
    assert isinstance(inbound, TeamsInbound)
    assert inbound.timestamp == "2026-10-02T09:00:00+00:00"
    assert (inbound.channel_name, inbound.team_name) == ("General", "Labs")


def test_channel_root_post_becomes_its_own_thread() -> None:
    inbound = _parse(make_channel_activity(conversation_id=CHANNEL_ID, activity_id="1700000000009"))
    assert isinstance(inbound, TeamsInbound)
    assert inbound.conversation_id == f"{CHANNEL_ID};messageid=1700000000009"
    assert inbound.channel_id == CHANNEL_ID


def test_a_targeted_thread_reply_is_addressed_and_marked() -> None:
    """A slash command reaches the bot alone, with no mention: it is still asked of it."""
    inbound = _parse(make_channel_activity(mention_bot=False, text="/memory", targeted=True))
    assert isinstance(inbound, TeamsInbound), "a targeted message is addressed to the bot"
    assert inbound.is_targeted and not inbound.unprompted, "never a participation candidate"
    assert (inbound.conversation_id, inbound.sender_id) == (THREAD_ID, f"29:{AAD_OBJECT_ID}")


def test_a_targeted_root_post_is_answered_in_the_channel_itself() -> None:
    """A targeted message takes no replies, so its answer cannot thread under it."""
    payload = make_channel_activity(
        mention_bot=False, conversation_id=CHANNEL_ID, activity_id="1700000000009", targeted=True
    )
    inbound = _parse(payload)
    assert isinstance(inbound, TeamsInbound) and inbound.is_targeted
    assert (inbound.conversation_id, inbound.channel_id) == (CHANNEL_ID, CHANNEL_ID)


def test_a_targeted_message_in_a_personal_chat_is_an_ordinary_one() -> None:
    """Everything in a 1:1 chat is private already."""
    inbound = _parse(make_message_activity(text="hello", targeted=True))
    assert isinstance(inbound, TeamsInbound) and not inbound.is_targeted


def _quoting(sender_id: str | None, *, text: str = "is this right?") -> dict[str, object]:
    """A thread reply quoting message 1700000000002, shaped as the SDK's quotedReply entity."""
    payload = make_channel_activity(
        mention_bot=False, text=f'<quoted messageId="1700000000002"/>{text}'
    )
    quote: dict[str, object] = {"messageId": "1700000000002", "preview": "Ship it Thursday."}
    if sender_id is not None:
        quote |= {"senderId": sender_id, "senderName": "daimon"}
    payload["entities"] = [{"type": "quotedReply", "quotedReply": quote}]
    return payload


def test_an_unmentioned_root_post_is_ignored_silently() -> None:
    root = make_channel_activity(mention_bot=False, conversation_id=CHANNEL_ID)
    assert _parse(root) == Refusal(None), "a new post stays mention-only"
    explicit_root = make_channel_activity(
        mention_bot=False, conversation_id=THREAD_ID, activity_id="1700000000001"
    )
    assert _parse(explicit_root) == Refusal(None), "the root names itself in the conversation id"


def test_an_unmentioned_thread_reply_is_an_unprompted_candidate() -> None:
    inbound = _parse(make_channel_activity(mention_bot=False, text="any update?"))
    assert isinstance(inbound, TeamsInbound)
    assert inbound.unprompted, "nobody addressed the bot: participation screens it"
    assert (inbound.conversation_id, inbound.channel_id) == (THREAD_ID, CHANNEL_ID), (
        "cascade keys: the thread's conversation id and its channel"
    )
    assert inbound.text == "any update?"


def test_a_mentioned_thread_reply_is_not_unprompted() -> None:
    inbound = _parse(make_channel_activity(text="any update?"))
    assert isinstance(inbound, TeamsInbound) and not inbound.unprompted


def _to_named_bot(payload: dict[str, object]) -> dict[str, object]:
    """Teams names the bot on `recipient`; the typed-name match reads it there."""
    return payload | {"recipient": {"id": BOT_ACCOUNT_ID, "name": "Daimon"}}


def test_a_root_post_typing_the_bot_name_is_marked_by_name() -> None:
    """A typed "@daimon" on a new post is no longer silent: the handler reads the switch."""
    root = make_channel_activity(
        mention_bot=False, conversation_id=CHANNEL_ID, text="@daimon summarise the week"
    )
    inbound = _parse(_to_named_bot(root))
    assert isinstance(inbound, TeamsInbound), "a root post naming the bot must be parsed"
    assert inbound.by_name and not inbound.unprompted, (
        "by name only: a root post is never a participation candidate"
    )
    assert inbound.text == "@daimon summarise the week", (
        "the name stays until the switch is known, so 'off' changes nothing"
    )


def test_a_thread_reply_typing_the_bot_name_is_by_name_and_unprompted() -> None:
    """Off, the reply is screened as before; on, it is a mention."""
    reply = make_channel_activity(mention_bot=False, text="thanks @DAIMON, and now?")
    inbound = _parse(_to_named_bot(reply))
    assert isinstance(inbound, TeamsInbound)
    assert inbound.by_name and inbound.unprompted, "both outcomes stay open for the handler"


def test_a_real_mention_with_the_typed_name_is_an_ordinary_mention() -> None:
    """A message with both forms is addressed already; the typed name adds nothing."""
    inbound = _parse(_to_named_bot(make_channel_activity(text="@daimon please look")))
    assert isinstance(inbound, TeamsInbound)
    assert not inbound.by_name and not inbound.unprompted, "one ordinary mention"


def test_a_personal_chat_typing_the_bot_name_is_unchanged() -> None:
    """1:1 chats need no mention, so the typed name marks nothing."""
    inbound = _parse(_to_named_bot(make_message_activity(text="@daimon hi")))
    assert isinstance(inbound, TeamsInbound) and not inbound.by_name, "DMs are unchanged"


@pytest.mark.parametrize(
    "sender",
    [
        {"id": BOT_ACCOUNT_ID},
        {"id": "28:another-bot", "aadObjectId": AAD_OBJECT_ID},
        {"id": "29:someone", "aadObjectId": AAD_OBJECT_ID, "role": "bot"},
    ],
)
def test_bots_typing_the_bot_name_are_ignored(sender: dict[str, object]) -> None:
    """The bot's own answers and other bots never start a turn by name."""
    payload = _to_named_bot(
        make_channel_activity(mention_bot=False, conversation_id=CHANNEL_ID, text="@daimon loop")
    )
    payload["from"] = sender
    assert _parse(payload) == Refusal(None), "a bot naming the bot must stay silent"


@pytest.mark.parametrize(
    "sender",
    [
        {"id": BOT_ACCOUNT_ID},  # the bot itself
        {"id": "28:another-bot", "aadObjectId": AAD_OBJECT_ID},
        {"id": "29:someone", "aadObjectId": AAD_OBJECT_ID, "role": "bot"},
    ],
)
def test_bots_never_become_participation_candidates(sender: dict[str, object]) -> None:
    payload = make_channel_activity(mention_bot=False)
    payload["from"] = sender
    assert _parse(payload) == Refusal(None), "only people enter participation, silently"


@pytest.mark.parametrize(
    "overrides",
    [{"aad_object_id": None}, {"tenant_id": str(uuid.uuid4())}, {"text": "   "}],
)
def test_an_unprompted_reply_that_fails_a_check_is_dropped_silently(
    overrides: dict[str, Any],
) -> None:
    payload = make_channel_activity(mention_bot=False, **overrides)
    assert _parse(payload) == Refusal(None), "nobody asked, so not even a denial is owed"


def test_an_unprompted_reply_over_the_size_cap_is_dropped_silently() -> None:
    payload = make_channel_activity(mention_bot=False, text="x" * (MAX_INBOUND_MESSAGE_BYTES + 1))
    assert _parse(payload) == Refusal(None)


@pytest.mark.parametrize("sender_id", [BOT_ACCOUNT_ID, BOT_CLIENT_ID, BOT_ACCOUNT_ID.upper()])
def test_quoting_the_bot_counts_as_a_mention(sender_id: str) -> None:
    inbound = _parse(_quoting(sender_id))
    assert isinstance(inbound, TeamsInbound)
    assert not inbound.unprompted, f"a quote of {sender_id} addresses the bot, as a Discord reply"
    assert inbound.text == '[quoting daimon: "Ship it Thursday."]\nis this right?', (
        "the placeholder becomes the quote it stands for, in place"
    )


def test_quoting_the_bot_in_a_root_post_starts_a_thread() -> None:
    payload = _quoting(BOT_ACCOUNT_ID)
    payload["conversation"] = {**cast(dict[str, object], payload["conversation"]), "id": CHANNEL_ID}
    inbound = _parse(payload)
    assert isinstance(inbound, TeamsInbound) and not inbound.unprompted
    assert inbound.conversation_id == f"{CHANNEL_ID};messageid=activity-1"


@pytest.mark.parametrize("sender_id", ["29:someone-else", "8:orgid:" + AAD_OBJECT_ID, None])
def test_quoting_someone_else_is_not_a_mention(sender_id: str | None) -> None:
    inbound = _parse(_quoting(sender_id))
    assert isinstance(inbound, TeamsInbound)
    assert inbound.unprompted, "only a quote of the bot's own message addresses it"


def _html_quoting(sender_id: str, *, nested_in: str | None = None) -> dict[str, object]:
    """A thread reply quoting the older way: a reply blockquote in the `text/html` body."""
    quote = (
        '<blockquote itemscope="" itemtype="http://schema.skype.com/Reply" itemid="1700000000002">'
        f'<strong itemprop="mri" itemid="{sender_id}">daimon</strong>'
        '<span itemprop="time" itemid="1700000000002"></span>'
        '<p itemprop="preview">Ship it Thursday.</p></blockquote>'
    )
    if nested_in is not None:
        quote = (
            '<blockquote itemtype="http://schema.skype.com/Reply" itemid="1700000000003">'
            f'<strong itemprop="mri" itemid="{nested_in}">Ada</strong>{quote}</blockquote>'
        )
    payload = make_channel_activity(mention_bot=False, text="is this right?")
    payload["attachments"] = [
        {"contentType": "text/html", "content": f"{quote}<p>is this right?</p>"}
    ]
    return payload


@pytest.mark.parametrize("sender_id", [BOT_ACCOUNT_ID, BOT_CLIENT_ID])
def test_an_html_quote_of_the_bot_counts_as_a_mention(sender_id: str) -> None:
    inbound = _parse(_html_quoting(sender_id))
    assert isinstance(inbound, TeamsInbound)
    assert not inbound.unprompted, "a client without the quotedReply entity still addresses it"
    assert inbound.text == "is this right?", "the text is kept as sent"


def test_an_html_quote_in_the_text_counts_as_a_mention() -> None:
    payload = _html_quoting(BOT_ACCOUNT_ID)
    attachments = cast(list[dict[str, str]], payload.pop("attachments"))
    payload["text"] = attachments[0]["content"]
    inbound = _parse(payload)
    assert isinstance(inbound, TeamsInbound) and not inbound.unprompted, (
        "the blockquote is read wherever it arrives"
    )


@pytest.mark.parametrize(
    ("sender_id", "nested_in"),
    [("8:orgid:" + AAD_OBJECT_ID, None), (BOT_ACCOUNT_ID, "8:orgid:" + AAD_OBJECT_ID)],
)
def test_an_html_quote_of_someone_else_is_not_a_mention(
    sender_id: str, nested_in: str | None
) -> None:
    inbound = _parse(_html_quoting(sender_id, nested_in=nested_in))
    assert isinstance(inbound, TeamsInbound)
    assert inbound.unprompted, "only the outer quote's sender counts, not one it quoted"


def test_a_deleted_or_unknown_quote_is_marked_unavailable() -> None:
    payload = make_message_activity(text='<quoted messageId="gone"/>still there?')
    inbound = _parse(payload)
    assert isinstance(inbound, TeamsInbound)
    assert inbound.text == "[quoted message unavailable]\nstill there?"


def test_a_group_chat_mention_is_one_conversation_keyed_on_the_chat() -> None:
    payload = make_group_activity(text="what did we decide?")
    payload["conversation"]["name"] = "Launch crew"  # type: ignore[index]
    inbound = _parse(payload)
    assert isinstance(inbound, TeamsInbound)
    assert (inbound.kind, inbound.conversation_id, inbound.channel_id) == (
        "group",
        GROUP_CHAT_ID,
        GROUP_CHAT_ID,
    ), "a quote-reply or any later message keys on the same chat"
    assert inbound.thread_id == GROUP_CHAT_ID
    assert (inbound.text, inbound.channel_name, inbound.unprompted) == (
        "what did we decide?",
        "Launch crew",
        False,
    )
    assert inbound.team_id is None


def test_a_group_chat_quote_of_the_bot_addresses_it() -> None:
    payload = _quoting(BOT_ACCOUNT_ID)
    payload["conversation"] = make_group_activity()["conversation"]
    inbound = _parse(payload)
    assert isinstance(inbound, TeamsInbound) and inbound.kind == "group"
    assert inbound.conversation_id == GROUP_CHAT_ID and not inbound.unprompted


def test_unaddressed_group_chat_messages_start_nothing() -> None:
    assert _parse(make_group_activity(mention_bot=False)) == Refusal(None), "no organic turns"
    quoting_someone = _quoting("29:someone-else")
    quoting_someone["conversation"] = make_group_activity()["conversation"]
    assert _parse(quoting_someone) == Refusal(None)


def test_a_targeted_group_chat_message_is_marked_private() -> None:
    inbound = _parse(make_group_activity(mention_bot=False, text="/help", targeted=True))
    assert isinstance(inbound, TeamsInbound)
    assert inbound.is_targeted and inbound.conversation_id == GROUP_CHAT_ID


def test_a_typed_name_in_a_group_chat_waits_for_the_tenant_switch() -> None:
    inbound = _parse(_to_named_bot(make_group_activity(mention_bot=False, text="@daimon hi")))
    assert isinstance(inbound, TeamsInbound)
    assert inbound.by_name and not inbound.unprompted


@pytest.mark.parametrize(
    "overrides",
    [{"aad_object_id": None}, {"tenant_id": str(uuid.UUID(int=99))}],
)
def test_a_group_chat_without_an_id_or_of_another_tenant_is_denied(
    overrides: dict[str, Any],
) -> None:
    assert _parse(make_group_activity(**overrides)) == Refusal(DENIED)


def test_a_group_chat_sender_from_another_tenant_is_answered() -> None:
    payload = make_group_activity()
    payload["from"]["tenantId"] = str(uuid.UUID(int=99))  # type: ignore[index]
    assert isinstance(_parse(payload), TeamsInbound), "a group chat may hold other organisations"
    named = make_group_activity(channel_tenant_id=str(uuid.UUID(int=99)))
    assert isinstance(_parse(named), TeamsInbound), "whichever field names their tenant"


@pytest.mark.parametrize("chat", ["19:meeting_abc@thread.skype", "a:not-a-group"])
def test_a_group_chat_whose_id_is_not_one_is_denied(chat: str) -> None:
    """A stored turn resumes by its id's kind, so a group chat must look like one."""
    assert _parse(make_group_activity(conversation_id=chat)) == Refusal(DENIED)


def test_a_stored_turn_knows_its_kind_from_its_ids() -> None:
    assert kind_of(GROUP_CHAT_ID, GROUP_CHAT_ID) == "group", "a group chat keys on itself"
    assert kind_of(CONVERSATION_ID, CONVERSATION_ID) == "dm"
    assert kind_of(THREAD_ID, CHANNEL_ID) == "channel"


def test_an_unknown_conversation_type_is_denied() -> None:
    assert _parse(make_message_activity(conversation_type="meeting")) == Refusal(DENIED)


@pytest.mark.parametrize(
    "overrides",
    [
        {"tenant_id": str(uuid.UUID(int=99))},
        {"channel_tenant_id": str(uuid.UUID(int=99))},
        {"channel_tenant_id": None},
        {"aad_object_id": None},
        {"aad_object_id": "not-a-uuid"},
        {"channel_id": "webchat"},
    ],
)
def test_unverified_senders_are_denied(overrides: dict[str, Any]) -> None:
    assert _parse(make_message_activity(**overrides)) == Refusal(DENIED)


def test_tenant_comparison_ignores_case() -> None:
    inbound = _parse(make_message_activity(tenant_id=ENTRA_TENANT_ID.upper()))
    assert isinstance(inbound, TeamsInbound)


def test_a_bare_channel_mention_is_a_turn_over_the_thread() -> None:
    """A mention with no words asks about the thread, which the turn replays."""
    inbound = _parse(make_channel_activity(text=""))
    assert isinstance(inbound, TeamsInbound), "a bare channel mention runs a turn"
    assert inbound.text == ""
    assert inbound.team_id is not None, "the team id is kept for the Graph lookup"


def test_an_empty_personal_message_asks_for_text() -> None:
    """A 1:1 chat has no thread to replay, so an empty message is refused."""
    assert _parse(make_message_activity(text="")) == Refusal(TEXT_ONLY)


def test_a_file_without_text_is_a_turn() -> None:
    payload = make_message_activity(text="")
    url = f"{SERVICE_URL}/v3/attachments/0-img/views/original"
    payload["attachments"] = [{"contentType": "image/*", "contentUrl": url}]
    inbound = _parse(payload)
    assert isinstance(inbound, TeamsInbound)
    assert inbound.files == (InboundFile("pasted_image", "image", url),)


def test_oversize_input_is_refused() -> None:
    text = "x" * (MAX_INBOUND_MESSAGE_BYTES + 1)
    assert _parse(make_message_activity(text=text)) == Refusal(INPUT_TOO_LONG)


async def test_live_tenant_id_accepts_a_live_tenant_for_dms_and_channels(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    for payload in (make_message_activity(), make_channel_activity()):
        inbound = _parse(payload)
        assert isinstance(inbound, TeamsInbound)
        assert await live_tenant_id(db_session_factory, inbound.entra_tenant_id) == TENANT_UUID


@pytest.mark.parametrize("state", ["missing", "pending", "archived"])
async def test_live_tenant_id_denies_a_tenant_that_is_not_live(
    db_session_factory: async_sessionmaker[AsyncSession], state: str
) -> None:
    if state != "missing":
        await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
        await set_provision_status(
            db_session_factory,
            tenant_id=TENANT_UUID,
            status="pending" if state == "pending" else None,
            archive=state == "archived",
        )
    for payload in (make_message_activity(), make_channel_activity()):
        inbound = _parse(payload)
        assert isinstance(inbound, TeamsInbound)
        assert await live_tenant_id(db_session_factory, inbound.entra_tenant_id) is None


OTHER_TENANT = str(uuid.UUID(int=99))


@pytest.mark.parametrize(
    ("sender", "channel_data"),
    [
        ({"id": "29:x"}, {"tenant": {"id": OTHER_TENANT}}),
        ({"id": "29:x", "tenantId": OTHER_TENANT.upper()}, {"tenant": {"id": ENTRA_TENANT_ID}}),
        ({"id": "29:x", "properties": {"tenantId": OTHER_TENANT}}, None),
    ],
    ids=["channel-data", "from-tenant", "from-properties"],
)
def test_each_place_a_sender_tenant_appears_marks_a_foreign_one(
    sender: dict[str, object], channel_data: dict[str, object] | None
) -> None:
    data = ChannelData.model_validate(channel_data) if channel_data is not None else None
    found = foreign_tenant(Account.model_validate(sender), data, ours=ENTRA_TENANT_ID)
    assert found == OTHER_TENANT, "canonical form of the first foreign tenant"


def test_our_own_tenant_everywhere_is_not_foreign_and_garbage_is() -> None:
    ours = Account.model_validate({"id": "29:x", "tenantId": ENTRA_TENANT_ID.upper()})
    data = ChannelData.model_validate({"tenant": {"id": ENTRA_TENANT_ID}})
    assert foreign_tenant(ours, data, ours=ENTRA_TENANT_ID) is None, "ours, case-blind"
    odd = Account.model_validate({"id": "29:x", "tenantId": " contoso "})
    assert foreign_tenant(odd, None, ours=ENTRA_TENANT_ID) == "contoso", "not ours: foreign"


def test_a_channel_sender_from_another_tenant_is_accepted() -> None:
    inbound = _parse(make_channel_activity(channel_tenant_id=OTHER_TENANT))
    assert isinstance(inbound, TeamsInbound), "a shared channel may hold them"


def test_a_channel_without_a_channel_data_tenant_is_accepted_as_ours() -> None:
    inbound = _parse(make_channel_activity(channel_tenant_id=None))
    assert isinstance(inbound, TeamsInbound), "the conversation tenant is the check"


def test_a_channel_in_another_tenant_is_still_denied() -> None:
    assert _parse(make_channel_activity(tenant_id=OTHER_TENANT)) == Refusal(DENIED)


def test_a_personal_chat_whose_sender_names_another_tenant_is_denied() -> None:
    payload = make_message_activity()
    cast(dict[str, object], payload["from"])["tenantId"] = OTHER_TENANT
    assert _parse(payload) == Refusal(DENIED), "Teams opens no 1:1 chat across organisations"


def test_an_oversize_typed_bot_name_is_refused_once_the_switch_is_known() -> None:
    """Too long, it still carries `by_name`, so the handler can send the notice when on."""
    text = "@daimon " + "x" * MAX_INBOUND_MESSAGE_BYTES
    root = make_channel_activity(mention_bot=False, conversation_id=CHANNEL_ID, text=text)
    assert _parse(_to_named_bot(root)) == Refusal(INPUT_TOO_LONG, by_name=True), (
        "an oversize typed name must get the notice a real mention gets, behind the switch"
    )


def test_a_quoted_bot_name_is_not_a_typed_mention() -> None:
    """A blockquote reply puts the quoted message in `text`; its "@daimon" is not the sender's."""
    quote = (
        '<blockquote itemscope="" itemtype="http://schema.skype.com/Reply" itemid="1">'
        '<strong itemprop="mri" itemid="8:orgid:someone">Ada</strong>'
        '<p itemprop="preview">@daimon ship it</p></blockquote>'
    )
    reply = make_channel_activity(mention_bot=False, text=f"{quote}<p>agreed</p>")
    inbound = _parse(_to_named_bot(reply))
    assert isinstance(inbound, TeamsInbound) and not inbound.by_name, (
        "a name inside someone else's quote must not address the bot"
    )


def test_a_typed_multi_word_name_with_an_html_no_break_space_is_by_name() -> None:
    """Teams may send the space between typed words as `&nbsp;`."""
    reply = make_channel_activity(mention_bot=False, text="<p>@Daimon&nbsp;Bot hi</p>")
    payload = reply | {"recipient": {"id": BOT_ACCOUNT_ID, "name": "Daimon Bot"}}
    inbound = _parse(payload)
    assert isinstance(inbound, TeamsInbound) and inbound.by_name, "the entity must read as a space"
    assert strip_typed_name(inbound.text, inbound.bot_name) == "<p>hi</p>", (
        "the name the match read must also leave the turn text"
    )


def test_strip_typed_name_keeps_markup_entities_encoded() -> None:
    """Decoding `&lt;` would turn escaped text into markup."""
    assert strip_typed_name("@Daimon &lt;b&gt; &amp; x", "Daimon") == "&lt;b&gt; &amp; x", (
        "only non-markup entities may decode"
    )


def test_many_unclosed_blockquote_tags_parse_in_linear_time() -> None:
    """`[^>]*` in the tag pattern made 60 KB of unclosed "<blockquote" take seconds."""
    text = "@daimon " + "<blockquote " * (100_000 // 12)
    started = time.perf_counter()
    _parse(_to_named_bot(make_channel_activity(mention_bot=False, text=text)))
    assert time.perf_counter() - started < 1.0, "the typed-text scan must stay linear"
