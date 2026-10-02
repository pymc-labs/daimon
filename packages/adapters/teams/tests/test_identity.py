"""parse_inbound and live_tenant_id: fail-closed identity for channels and 1:1 chats."""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from daimon.adapters.teams.attachments import InboundFile
from daimon.adapters.teams.identity import (
    DENIED,
    GROUP_CHAT_UNSUPPORTED,
    INPUT_TOO_LONG,
    MAX_INBOUND_MESSAGE_BYTES,
    TEXT_ONLY,
    Refusal,
    TeamsInbound,
    live_tenant_id,
    parse_inbound,
)
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.tenants import set_provision_status
from microsoft_teams.api import MessageActivity
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    BOT_ACCOUNT_ID,
    BOT_CLIENT_ID,
    CHANNEL_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    SERVICE_URL,
    THREAD_ID,
    make_channel_activity,
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


def test_a_deleted_or_unknown_quote_is_marked_unavailable() -> None:
    payload = make_message_activity(text='<quoted messageId="gone"/>still there?')
    inbound = _parse(payload)
    assert isinstance(inbound, TeamsInbound)
    assert inbound.text == "[quoted message unavailable]\nstill there?"


@pytest.mark.parametrize(
    "overrides",
    [
        {"conversation_type": "groupChat", "is_group": False},
        {"conversation_type": "personal", "is_group": True},
    ],
)
def test_group_chats_are_refused(overrides: dict[str, Any]) -> None:
    assert _parse(make_message_activity(**overrides)) == Refusal(GROUP_CHAT_UNSUPPORTED)


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
