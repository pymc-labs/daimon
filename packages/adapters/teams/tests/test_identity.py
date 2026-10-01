"""parse_inbound and live_tenant_id: fail-closed identity for channels and 1:1 chats."""

from __future__ import annotations

import uuid
from typing import Any

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


def test_channel_root_post_becomes_its_own_thread() -> None:
    inbound = _parse(make_channel_activity(conversation_id=CHANNEL_ID, activity_id="1700000000009"))
    assert isinstance(inbound, TeamsInbound)
    assert inbound.conversation_id == f"{CHANNEL_ID};messageid=1700000000009"
    assert inbound.channel_id == CHANNEL_ID


def test_channel_message_without_a_bot_mention_is_ignored_silently() -> None:
    assert _parse(make_channel_activity(mention_bot=False)) == Refusal(None)


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
