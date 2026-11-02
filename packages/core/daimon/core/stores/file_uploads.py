"""Async store for file_uploads — chat attachments staged by the agent.

Two-step by design. `create_upload` mints an empty row plus a single-use
capability token; the agent's sandbox PUTs the bytes against that token, and
`store_upload_content` fills the row and burns the token. The bytes therefore
never pass through the model's token stream, which is what makes byte-exact
delivery possible at all.

No try/except anywhere in this module — exceptions propagate to the adapter
boundary. Callers own the transaction; every write ends with `await
session.flush()`.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Collection, Sequence
from datetime import datetime, timedelta
from typing import Any, cast

from daimon.core._models import FileUpload, WebMessage
from daimon.core.stores.domain import FileUploadRow
from sqlalchemy import (
    CursorResult,
    Select,
    and_,
    delete,
    func,
    literal_column,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

# Matches Discord's per-file upload ceiling — a larger row could never be
# delivered, so it is refused at the door rather than after the transfer.
MAX_UPLOAD_BYTES = 24_000_000

# How long a minted upload stays usable. Long enough for a sandbox to render a
# figure and curl it up; short enough that abandoned mints do not accumulate.
UPLOAD_TTL = timedelta(minutes=30)

_HANDLE_BYTES = 8
_TOKEN_BYTES = 24


class UploadTooLargeError(Exception):
    """Raised when a PUT body exceeds `MAX_UPLOAD_BYTES`."""


async def create_upload(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    title: str,
    display_filename: str,
    content_type: str,
    now: datetime,
    person_id: uuid.UUID | None = None,
) -> tuple[FileUploadRow, str]:
    """Mint an empty upload row and return it with its single-use token.

    A web person's file carries `person_id`: only they may attach it, it counts toward
    their quota, and it goes with them.

    The token is returned to the caller once and never read back out of the
    row by anything except `store_upload_content`, which clears it.
    """
    upload_token = secrets.token_urlsafe(_TOKEN_BYTES)
    row = FileUpload(
        id=secrets.token_urlsafe(_HANDLE_BYTES),
        tenant_id=tenant_id,
        person_id=person_id,
        upload_token=upload_token,
        title=title,
        display_filename=display_filename,
        content_type=content_type,
        content=None,
        created_at=now,
    )
    session.add(row)
    await session.flush()
    return FileUploadRow.model_validate(row), upload_token


async def store_upload_content(
    session: AsyncSession,
    *,
    upload_token: str,
    data: bytes,
    now: datetime,
) -> FileUploadRow | None:
    """Fill the row the token names and burn the token.

    Returns None when the token matches nothing, is already burnt, or has aged
    past `UPLOAD_TTL` — the caller cannot distinguish those, deliberately, since
    they are all "this token is not usable" to an unauthenticated PUT.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadTooLargeError(
            f"upload is {len(data)} bytes, over the {MAX_UPLOAD_BYTES} byte limit"
        )
    row = (
        await session.execute(select(FileUpload).where(FileUpload.upload_token == upload_token))
    ).scalar_one_or_none()
    if row is None or row.created_at < now - UPLOAD_TTL:
        return None
    row.content = data
    row.upload_token = None
    await session.flush()
    return FileUploadRow.model_validate(row)


async def get_upload(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    handle_id: str,
) -> FileUploadRow | None:
    """Read a staged upload by handle, scoped to the tenant that minted it.

    Tenant scoping is the whole authorization story here: a handle id is short
    and guessable enough that it must not be a bearer token across tenants.
    """
    row = (
        await session.execute(
            select(FileUpload).where(
                FileUpload.id == handle_id,
                FileUpload.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    return None if row is None else FileUploadRow.model_validate(row)


async def get_person_upload(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    person_id: uuid.UUID,
    handle_id: str,
) -> FileUploadRow | None:
    """A landed upload that belongs to the web person, or None (another's, or not filled)."""
    row = (
        await session.execute(
            select(FileUpload).where(
                FileUpload.id == handle_id,
                FileUpload.tenant_id == tenant_id,
                FileUpload.person_id == person_id,
                FileUpload.content.is_not(None),
            )
        )
    ).scalar_one_or_none()
    return None if row is None else FileUploadRow.model_validate(row)


async def get_person_uploads(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    person_id: uuid.UUID,
    handle_ids: Sequence[str],
) -> dict[str, FileUploadRow]:
    """`get_person_upload` for several handles in one read; a missing one is absent."""
    if not handle_ids:
        return {}
    rows = await session.scalars(
        select(FileUpload).where(
            FileUpload.id.in_(list(handle_ids)),
            FileUpload.tenant_id == tenant_id,
            FileUpload.person_id == person_id,
            FileUpload.content.is_not(None),
        )
    )
    return {row.id: FileUploadRow.model_validate(row) for row in rows}


async def person_upload_bytes(session: AsyncSession, *, person_id: uuid.UUID) -> int:
    """The bytes of every file the web person holds."""
    total = await session.scalar(
        select(func.coalesce(func.sum(func.octet_length(FileUpload.content)), 0)).where(
            FileUpload.person_id == person_id
        )
    )
    return int(total or 0)


async def move_person_uploads(
    session: AsyncSession, *, from_person_id: uuid.UUID, to_person_id: uuid.UUID
) -> None:
    await session.execute(
        update(FileUpload)
        .where(FileUpload.person_id == from_person_id)
        .values(person_id=to_person_id)
    )


async def lock_person_uploads(session: AsyncSession, *, person_id: uuid.UUID) -> None:
    """Serialize one person's uploads until the transaction ends, so the quota holds."""
    key = func.hashtextextended(str(person_id), 0)
    await session.execute(select(func.pg_advisory_xact_lock(key)))


def _carrying_message() -> Select[tuple[uuid.UUID]]:
    """The messages that carry the file, as an attachment or as a reply's output."""
    handle = func.jsonb_build_object(literal_column("'id'"), FileUpload.id)
    carried = func.jsonb_build_array(handle)
    # Whole-column containment, so the GIN index on `blocks` answers it.
    return select(WebMessage.id).where(
        or_(
            WebMessage.blocks.op("@>")(
                func.jsonb_build_object(literal_column("'attachments'"), carried)
            ),
            WebMessage.blocks.op("@>")(func.jsonb_build_object(literal_column("'files'"), carried)),
        )
    )


async def delete_unreferenced_web_uploads(
    session: AsyncSession,
    *,
    person_id: uuid.UUID | None = None,
    handle_ids: Collection[str] | None = None,
    created_before: datetime | None = None,
) -> int:
    """Delete web files no message carries; returns how many.

    Narrowed to one person, to some handles, and to files made before a time.
    """
    unreferenced = ~_carrying_message().exists()
    conditions = [FileUpload.person_id.is_not(None), unreferenced]
    if person_id is not None:
        conditions.append(FileUpload.person_id == person_id)
    if handle_ids is not None:
        conditions.append(FileUpload.id.in_(list(handle_ids)))
    if created_before is not None:
        conditions.append(FileUpload.created_at < created_before)
    result = await session.execute(delete(FileUpload).where(and_(*conditions)))
    return cast(CursorResult[Any], result).rowcount
