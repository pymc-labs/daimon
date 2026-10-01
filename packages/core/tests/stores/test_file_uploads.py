"""Store tests for staged chat-attachment uploads."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.stores.file_uploads import (
    MAX_UPLOAD_BYTES,
    UPLOAD_TTL,
    UploadTooLargeError,
    create_upload,
    get_upload,
    store_upload_content,
)
from daimon.testing.factories import make_tenant
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
