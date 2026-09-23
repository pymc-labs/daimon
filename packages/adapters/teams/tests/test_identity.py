"""VerifiedTeamsTurnResolver: fail-closed identity mapping for personal chat.

Every case drives the REAL resolver over a real ``MessageActivity`` model —
only ``ctx.send`` is recorded, and the tenant row comes from the test DB.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from daimon.adapters.teams import identity
from daimon.adapters.teams.identity import (
    DENIED_MESSAGE,
    INPUT_TOO_LONG_MESSAGE,
    MAX_INBOUND_MESSAGE_BYTES,
    PERSONAL_CHAT_ONLY_MESSAGE,
    VerifiedTeamsTurnResolver,
)
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.tenants import set_provision_status
from microsoft_teams.api import MessageActivity  # pyright: ignore[reportMissingTypeStubs]
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import AAD_OBJECT_ID, ENTRA_TENANT_ID, make_message_activity


def _ctx(payload: dict[str, object]) -> tuple[Any, list[str]]:
    sent: list[str] = []

    async def _send(text: str) -> None:
        sent.append(text)

    ctx = SimpleNamespace(activity=MessageActivity.model_validate(payload), send=_send)
    return ctx, sent


def _resolver(
    db_factory: async_sessionmaker[AsyncSession], *, tenant_id: str = ENTRA_TENANT_ID
) -> VerifiedTeamsTurnResolver:
    return VerifiedTeamsTurnResolver(sessionmaker=db_factory, entra_tenant_id=tenant_id)


@pytest.fixture
async def provisioned(db_session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    return derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)


@pytest.mark.asyncio
async def test_valid_personal_activity_maps_to_authorized_turn(
    db_session_factory: async_sessionmaker[AsyncSession],
    provisioned: uuid.UUID,
) -> None:
    ctx, sent = _ctx(make_message_activity(text="  hello there  "))
    authorized = await _resolver(db_session_factory)(ctx)
    assert authorized is not None
    assert sent == []
    assert authorized.tenant_id == provisioned
    assert authorized.external_user_id == AAD_OBJECT_ID
    assert authorized.conversation_id
    assert authorized.activity_id == "activity-1"
    assert authorized.message == "hello there"


@pytest.mark.asyncio
async def test_group_conversation_gets_personal_only_notice(
    db_session_factory: async_sessionmaker[AsyncSession],
    provisioned: uuid.UUID,
) -> None:
    ctx, sent = _ctx(make_message_activity(conversation_type="groupChat"))
    assert await _resolver(db_session_factory)(ctx) is None
    assert sent == [PERSONAL_CHAT_ONLY_MESSAGE]


@pytest.mark.asyncio
async def test_contradictory_group_flag_fails_closed(
    db_session_factory: async_sessionmaker[AsyncSession],
    provisioned: uuid.UUID,
) -> None:
    ctx, sent = _ctx(make_message_activity(is_group=True))
    assert await _resolver(db_session_factory)(ctx) is None
    assert sent == [DENIED_MESSAGE]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload_kwargs",
    [
        {"tenant_id": str(uuid.UUID(int=3))},
        {"channel_tenant_id": str(uuid.UUID(int=3))},
        {"aad_object_id": None},
        {"aad_object_id": "not-a-uuid"},
        {"channel_id": "webchat"},
    ],
    ids=[
        "wrong_conversation_tenant",
        "wrong_channel_data_tenant",
        "missing_aad_object_id",
        "malformed_aad_object_id",
        "wrong_channel",
    ],
)
async def test_unverifiable_fields_deny(
    db_session_factory: async_sessionmaker[AsyncSession],
    provisioned: uuid.UUID,
    payload_kwargs: dict[str, object],
) -> None:
    ctx, sent = _ctx(make_message_activity(**payload_kwargs))
    assert await _resolver(db_session_factory)(ctx) is None
    assert sent == [DENIED_MESSAGE]


def _without_text(text: str | None) -> dict[str, object]:
    payload = make_message_activity()
    if text is None:
        payload.pop("text")  # attachment-only / card-submit activities carry no text
    else:
        payload["text"] = text
    return payload


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [None, "", "   "], ids=["absent", "empty", "blank"])
async def test_verified_message_without_text_gets_text_only_notice(
    db_session_factory: async_sessionmaker[AsyncSession],
    provisioned: uuid.UUID,
    text: str | None,
) -> None:
    """A verified user who sends only an image, a file or a card submit is
    told the agent reads text — not that their account has no access."""
    ctx, sent = _ctx(_without_text(text))
    assert await _resolver(db_session_factory)(ctx) is None
    assert sent != [DENIED_MESSAGE], "a verified user was told their account has no access"
    assert sent == [identity.TEXT_ONLY_MESSAGE]


@pytest.mark.asyncio
async def test_message_without_text_from_unprovisioned_tenant_still_denies(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx, sent = _ctx(_without_text(None))
    assert await _resolver(db_session_factory)(ctx) is None
    assert sent == [DENIED_MESSAGE]


@pytest.mark.asyncio
async def test_oversized_text_gets_length_notice(
    db_session_factory: async_sessionmaker[AsyncSession],
    provisioned: uuid.UUID,
) -> None:
    ctx, sent = _ctx(make_message_activity(text="x" * (MAX_INBOUND_MESSAGE_BYTES + 1)))
    assert await _resolver(db_session_factory)(ctx) is None
    assert sent == [INPUT_TOO_LONG_MESSAGE]


@pytest.mark.asyncio
async def test_tenant_provisioned_from_uppercase_portal_guid_is_admitted(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The documented provisioning call takes the tenant id as pasted from the
    Azure portal, while TeamsSettings canonicalizes it to lowercase — both
    must land on the same tenant row, or every message is silently denied."""
    pasted = "7F3E2A10-BC4D-4E5F-9A6B-C0D1E2F3A4B5"
    canonical = pasted.lower()  # what TeamsSettings stores and Teams activities carry
    result = await provision_tenant(db_session_factory, platform="teams", workspace_id=pasted)
    ctx, sent = _ctx(make_message_activity(tenant_id=canonical, channel_tenant_id=canonical))
    authorized = await _resolver(db_session_factory, tenant_id=canonical)(ctx)
    assert sent == [], "every message from the provisioned tenant is denied"
    assert authorized is not None
    assert authorized.tenant_id == result.tenant_id
    assert result.external_id == canonical


@pytest.mark.asyncio
async def test_provisioning_a_non_uuid_teams_tenant_is_rejected(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(ValueError):
        await provision_tenant(db_session_factory, platform="teams", workspace_id="contoso")


@pytest.mark.asyncio
async def test_unprovisioned_tenant_denies(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx, sent = _ctx(make_message_activity())
    assert await _resolver(db_session_factory)(ctx) is None
    assert sent == [DENIED_MESSAGE]


@pytest.mark.asyncio
async def test_pending_tenant_denies(
    db_session_factory: async_sessionmaker[AsyncSession],
    provisioned: uuid.UUID,
) -> None:
    await set_provision_status(db_session_factory, tenant_id=provisioned, status="pending")
    ctx, sent = _ctx(make_message_activity())
    assert await _resolver(db_session_factory)(ctx) is None
    assert sent == [DENIED_MESSAGE]


@pytest.mark.asyncio
async def test_archived_tenant_denies(
    db_session_factory: async_sessionmaker[AsyncSession],
    provisioned: uuid.UUID,
) -> None:
    await set_provision_status(db_session_factory, tenant_id=provisioned, archive=True)
    ctx, sent = _ctx(make_message_activity())
    assert await _resolver(db_session_factory)(ctx) is None
    assert sent == [DENIED_MESSAGE]
