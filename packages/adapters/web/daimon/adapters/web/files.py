"""Files in the web chat: shared uploads the agent fetches by link, and files a turn made.

An upload's link is sealed with the deployment's key and expires, so it is a short-lived
capability like the chat apps' download URLs. Every file is served as a download, never
rendered on the app's own origin.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Literal
from urllib.parse import quote

from anthropic.types.beta.sessions import BetaManagedAgentsImageBlockParam
from cryptography.fernet import InvalidToken, MultiFernet
from daimon.core.media.attachments import AttachedFile, attachment_lines
from daimon.core.media.vision import (
    MAX_VISION_IMAGE_BYTES,
    MAX_VISION_IMAGES,
    build_image_block,
    sniff_image_media_type,
)
from daimon.core.output_delivery import DeliverableFile
from daimon.core.stores import file_uploads
from daimon.core.stores.domain import WebAttachment, WebOutputFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: How long an upload's link works; the agent fetches it at the start of its turn.
ATTACHMENT_TTL_S = 30 * 60
#: What one person's files may add up to; deleting a chat frees its files.
PERSON_UPLOAD_QUOTA_BYTES = 200_000_000
#: A web file no message carries is deleted after this long.
UNREFERENCED_UPLOAD_TTL = timedelta(hours=24)
_PURPOSE = "web-attachment"
_SHEETS = {"xlsx": "xlsx", "xls": "xlsx", "csv": "csv"}
_IMAGES = frozenset({"png", "jpg", "jpeg", "gif", "webp"})


def seal_attachment(fernet: MultiFernet, *, tenant_id: uuid.UUID, handle: str) -> str:
    payload = {"p": _PURPOSE, "t": str(tenant_id), "h": handle}
    return fernet.encrypt(json.dumps(payload).encode()).decode()


def open_attachment(
    fernet: MultiFernet, sealed: str, *, ttl_s: int = ATTACHMENT_TTL_S
) -> tuple[uuid.UUID, str] | None:
    """`(tenant id, upload handle)` of a live link, or None."""
    try:
        payload = json.loads(fernet.decrypt(sealed.encode(), ttl=ttl_s))
        if payload["p"] != _PURPOSE:
            return None
        return uuid.UUID(payload["t"]), str(payload["h"])
    except (InvalidToken, ValueError, TypeError, KeyError):
        return None


def file_kind(name: str) -> Literal["csv", "xlsx", "pdf", "png"]:
    """The icon a file gets: a sheet, an image, or a document."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in _SHEETS:
        return "csv" if _SHEETS[ext] == "csv" else "xlsx"
    return "png" if ext in _IMAGES else "pdf"


async def keep_output(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    person_id: uuid.UUID,
    file: DeliverableFile,
    now: datetime,
) -> WebOutputFile:
    """Store a turn's output file for its reply; it belongs to the person."""
    async with sessionmaker.begin() as session:
        row, token = await file_uploads.create_upload(
            session,
            tenant_id=tenant_id,
            person_id=person_id,
            title=file.filename,
            display_filename=file.filename,
            content_type=file.mime_type or "application/octet-stream",
            now=now,
        )
        await file_uploads.store_upload_content(
            session, upload_token=token, data=file.content, now=now
        )
    return WebOutputFile(
        id=row.id, name=file.filename, size=file.size_bytes, kind=file_kind(file.filename)
    )


def download(name: str, data: bytes) -> Response:
    """The bytes as a download: never sniffed or rendered on this origin."""
    plain = name.encode("ascii", "replace").decode().replace("\\", "_").replace('"', "_")
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{plain}\"; filename*=UTF-8''{quote(name, safe='')}"
            ),
            "Content-Security-Policy": "sandbox",
            "Cache-Control": "private, no-store",
        },
    )


async def attachment_prompt(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    person_id: uuid.UUID,
    files: Sequence[WebAttachment],
    link: Callable[[str], str] | None,
) -> tuple[str, list[BetaManagedAgentsImageBlockParam]]:
    """The lines telling the agent what a message carries, and the images sent inline.

    Images go inline; any other file is a short-lived link the agent fetches. `link` makes
    an upload's link; without one (no key to seal it) a file is only named.
    """
    if not files:
        return "", []
    async with sessionmaker() as session:
        held = await file_uploads.get_person_uploads(
            session, tenant_id=tenant_id, person_id=person_id, handle_ids=[f.id for f in files]
        )
    shared: list[AttachedFile] = []
    blocks: list[BetaManagedAgentsImageBlockParam] = []
    for file in files:
        upload = held.get(file.id)
        data = upload.content if upload is not None else None
        kind = sniff_image_media_type(data) if data else None
        if (
            data
            and kind
            and len(data) <= MAX_VISION_IMAGE_BYTES
            and len(blocks) < MAX_VISION_IMAGES
        ):
            blocks.append(build_image_block(data, kind))
            shared.append(AttachedFile(file.name, inlined=True))
        elif data and link is not None:
            shared.append(AttachedFile(file.name, url=link(file.id)))
        else:
            shared.append(AttachedFile(file.name))
    return f"{attachment_lines(shared)}\n", blocks
