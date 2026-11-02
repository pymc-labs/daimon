"""Store tests for staged chat-attachment uploads."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.stores import web_chat as chat
from daimon.core.stores import web_people
from daimon.core.stores.domain import WebAttachment, WebMessageBlocks
from daimon.core.stores.file_uploads import (
    MAX_UPLOAD_BYTES,
    UPLOAD_TTL,
    UploadTooLargeError,
    create_upload,
    delete_unreferenced_web_uploads,
    get_person_upload,
    get_upload,
    person_upload_bytes,
    store_upload_content,
)
from daimon.testing.factories import make_account, make_tenant, make_web_person
from sqlalchemy.ext.asyncio import AsyncSession

_NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)


async def test_create_upload_stages_a_row_with_no_content_yet(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)

    row, token = await create_upload(
        db_session,
        tenant_id=tenant.id,
        title="chart",
        display_filename="chart.png",
        content_type="image/png",
        now=_NOW,
    )

    assert row.content is None, "a freshly minted upload holds no bytes until the sandbox PUTs"
    assert token, "mint must return a usable upload token"


async def test_stored_bytes_come_back_byte_identical(db_session: AsyncSession) -> None:
    """The whole point of the two-step upload: what goes in is what comes out."""
    tenant = await make_tenant(db_session)
    payload = bytes(range(256)) * 40  # 10240 bytes, every byte value, no encoding step
    row, token = await create_upload(
        db_session,
        tenant_id=tenant.id,
        title="chart",
        display_filename="chart.png",
        content_type="image/png",
        now=_NOW,
    )

    await store_upload_content(db_session, upload_token=token, data=payload, now=_NOW)
    fetched = await get_upload(db_session, tenant_id=tenant.id, handle_id=row.id)

    assert fetched is not None, "stored upload should be readable by handle"
    assert fetched.content == payload, "bytes must survive the round trip exactly"


async def test_upload_token_is_single_use(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    _, token = await create_upload(
        db_session,
        tenant_id=tenant.id,
        title="chart",
        display_filename="chart.png",
        content_type="image/png",
        now=_NOW,
    )

    first = await store_upload_content(db_session, upload_token=token, data=b"one", now=_NOW)
    second = await store_upload_content(db_session, upload_token=token, data=b"two", now=_NOW)

    assert first is not None, "the first PUT against a fresh token should land"
    assert second is None, "a burnt token must not accept a second PUT"


async def test_expired_token_is_refused(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    _, token = await create_upload(
        db_session,
        tenant_id=tenant.id,
        title="chart",
        display_filename="chart.png",
        content_type="image/png",
        now=_NOW,
    )

    too_late = _NOW + UPLOAD_TTL + timedelta(seconds=1)
    result = await store_upload_content(db_session, upload_token=token, data=b"x", now=too_late)

    assert result is None, "a token past its TTL must not accept a PUT"


async def test_oversize_upload_is_refused_before_storing(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    _, token = await create_upload(
        db_session,
        tenant_id=tenant.id,
        title="big",
        display_filename="big.bin",
        content_type="application/octet-stream",
        now=_NOW,
    )

    with pytest.raises(UploadTooLargeError):
        await store_upload_content(
            db_session,
            upload_token=token,
            data=b"x" * (MAX_UPLOAD_BYTES + 1),
            now=_NOW,
        )


async def test_upload_is_not_readable_from_another_tenant(db_session: AsyncSession) -> None:
    """A handle id is short; tenant scoping is what stops it being a bearer token."""
    owner = await make_tenant(db_session, workspace_id="guild-owner")
    other = await make_tenant(db_session, workspace_id="guild-other")
    row, token = await create_upload(
        db_session,
        tenant_id=owner.id,
        title="chart",
        display_filename="chart.png",
        content_type="image/png",
        now=_NOW,
    )
    await store_upload_content(db_session, upload_token=token, data=b"secret", now=_NOW)

    leaked = await get_upload(db_session, tenant_id=other.id, handle_id=row.id)

    assert leaked is None, "another tenant must not read an upload by handle id"


async def _web_upload(
    db_session: AsyncSession, tenant_id: object, person_id: object, now: datetime
):
    row, token = await create_upload(
        db_session,
        tenant_id=tenant_id,  # pyright: ignore[reportArgumentType]
        person_id=person_id,  # pyright: ignore[reportArgumentType]
        title="f",
        display_filename="f.txt",
        content_type="text/plain",
        now=now,
    )
    await store_upload_content(db_session, upload_token=token, data=b"12345", now=now)
    return row


async def test_a_web_upload_is_found_and_counted_for_its_person_only(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    ada = await make_web_person(db_session, tenant=tenant)
    bob = await make_web_person(
        db_session, tenant=tenant, account=await make_account(db_session, tenant=tenant)
    )
    row = await _web_upload(db_session, tenant.id, ada.id, _NOW)

    mine = await get_person_upload(
        db_session, tenant_id=tenant.id, person_id=ada.id, handle_id=row.id
    )
    theirs = await get_person_upload(
        db_session, tenant_id=tenant.id, person_id=bob.id, handle_id=row.id
    )

    assert mine is not None and mine.content == b"12345", "the owner reads it"
    assert theirs is None, "another person's handle is not theirs to attach"
    assert await person_upload_bytes(db_session, person_id=ada.id) == 5, "bytes count to the owner"
    assert await person_upload_bytes(db_session, person_id=bob.id) == 0, "and to nobody else"


async def test_only_old_files_no_message_carries_are_swept(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    person = await make_web_person(db_session, tenant=tenant)
    old_loose = await _web_upload(db_session, tenant.id, person.id, _NOW)
    old_sent = await _web_upload(db_session, tenant.id, person.id, _NOW)
    fresh = await _web_upload(db_session, tenant.id, person.id, _NOW + timedelta(days=2))
    thread = await chat.create_thread(
        db_session,
        tenant_id=tenant.id,
        person_id=person.id,
        agent_id="ag",
        agent_name="daimon",
        title="t",
    )
    await chat.insert_user_message(
        db_session,
        tenant_id=tenant.id,
        thread_id=thread.id,
        message_id=uuid.uuid4(),
        content="see",
        blocks=WebMessageBlocks(attachments=(WebAttachment(id=old_sent.id, name="f", size=5),)),
    )

    removed = await delete_unreferenced_web_uploads(
        db_session, created_before=_NOW + timedelta(days=1)
    )

    async def kept(handle: str) -> bool:
        return await get_upload(db_session, tenant_id=tenant.id, handle_id=handle) is not None

    assert removed == 1, "one file was old and unreferenced"
    assert not await kept(old_loose.id), "an old file never sent is swept"
    assert await kept(old_sent.id), "a file a message carries stays however old"
    assert await kept(fresh.id), "a recent file stays"


async def test_erasing_a_person_erases_their_files(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    person = await make_web_person(db_session, tenant=tenant)
    row = await _web_upload(db_session, tenant.id, person.id, _NOW)

    await web_people.delete_for_account(db_session, account_id=person.account_id)

    assert await get_upload(db_session, tenant_id=tenant.id, handle_id=row.id) is None, (
        "the person's files go with them"
    )
