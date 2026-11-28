"""Versioned routine outbox envelopes holding file references, never bytes.

A Managed Agents fire's files are referenced by their Files API ids. Another
provider's files live in the fire's own session, which delivery cannot reach, so
they are read while that runtime is live and staged as `file_uploads` rows.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import anthropic
import structlog
from anthropic import AsyncAnthropic
from daimon.core.channel_backend import backend_refusal_causes
from daimon.core.checkpoint_prompt import HANDOFF_FILENAME_PREFIX
from daimon.core.output_delivery import (
    MAX_BYTES_CEILING,
    DeliverableFile,
    SkippedFile,
    download_output_file,
    poll_output_files,
    render_oversize_notice,
)
from daimon.core.output_runtime import OutputRuntime
from daimon.core.stores.file_uploads import (
    MAX_UPLOAD_BYTES,
    ROUTINE_OUTPUT_TITLE,
    UploadTooLargeError,
    delete_uploads,
    get_upload,
    is_staged_output,
    stage_upload,
)
from mux.errors import MuxError, UnsupportedCapability
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.core.fire.delivery import DeliveryFacts

log = structlog.get_logger(__name__)

PREFIX = "\x1eroutine-files-v1\n"
MAX_FILES = 10
OUTBOX_FILES = "generated_files"


class RoutineFile(BaseModel):
    file_id: str
    filename: str
    mime_type: str
    size_bytes: int = Field(gt=0, le=MAX_BYTES_CEILING)
    # A `file_uploads` handle (`stage_routine_files`), not a Files API id.
    staged: bool = False


class RoutinePayload(BaseModel):
    text: str
    files: list[RoutineFile] = Field(
        default_factory=lambda: list[RoutineFile](), max_length=MAX_FILES
    )


def decode_payload(payload: str | None) -> RoutinePayload:
    if payload is not None and payload.startswith(PREFIX):
        return RoutinePayload.model_validate_json(payload[len(PREFIX) :])
    return RoutinePayload(text=payload or "")


def plain_payload(text: str | None) -> str | None:
    """Escape an untrusted reply that happens to start with the envelope marker."""
    if text is not None and text.startswith(PREFIX):
        return PREFIX + RoutinePayload(text=text).model_dump_json()
    return text


def payload_for(row: DeliveryFacts) -> str | None:
    """Only host-tagged envelopes may supply file ids; legacy replies stay text."""
    if row.delivery_note == OUTBOX_FILES:
        return row.delivery_payload
    return plain_payload(row.delivery_payload)


async def collect_routine_files(
    client: AsyncAnthropic,
    *,
    session_id: str,
    text: str,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str:
    """Reuse interactive listing settlement; keep references until the outbox settles."""
    try:
        listed = await poll_output_files(client, session_id=session_id, sleep=sleep)
    except anthropic.APIError as error:
        # File collection must not discard a routine's completed reply.
        log.exception(
            "routine.files_collection_failed",
            session_id=session_id,
            causes=backend_refusal_causes(error),
        )
        return (
            plain_payload(
                "\n\n".join(filter(None, [text, "Generated files could not be attached."]))
            )
            or ""
        )
    files: list[RoutineFile] = []
    notices: list[str] = []
    for meta in listed.values():
        if meta.filename.startswith(HANDOFF_FILENAME_PREFIX) or meta.size_bytes == 0:
            continue
        if meta.size_bytes > MAX_BYTES_CEILING:
            notices.append(
                render_oversize_notice(
                    SkippedFile(
                        file_id=meta.id, filename=meta.filename, size_bytes=meta.size_bytes
                    ),
                    max_bytes=MAX_BYTES_CEILING,
                )
            )
        elif len(files) == MAX_FILES:
            notices.append(
                "More generated files remain with the agent: "
                "this delivery can attach at most 10 files."
            )
            break
        else:
            files.append(
                RoutineFile(
                    file_id=meta.id,
                    filename=meta.filename,
                    mime_type=meta.mime_type,
                    size_bytes=meta.size_bytes,
                )
            )
    return _envelope(text, files, notices)


def _envelope(text: str, files: Sequence[RoutineFile], notices: Sequence[str]) -> str:
    if not files and not notices:
        return plain_payload(text) or ""
    return (
        PREFIX
        + RoutinePayload(text="\n\n".join([text, *notices]), files=list(files)).model_dump_json()
    )


async def stage_routine_files(
    sessions: async_sessionmaker[AsyncSession],
    outputs: OutputRuntime | None,
    *,
    tenant_id: uuid.UUID,
    text: str,
) -> str:
    """A provider fire's reply with its staged files (`stage_files`), in the same envelope
    as `collect_routine_files`."""
    files, notices = await stage_files(sessions, outputs, tenant_id=tenant_id)
    return _envelope(text, files, notices)


async def stage_files(
    sessions: async_sessionmaker[AsyncSession],
    outputs: OutputRuntime | None,
    *,
    tenant_id: uuid.UUID,
    title: str = ROUTINE_OUTPUT_TITLE,
) -> tuple[list[RoutineFile], list[str]]:
    """Read a provider turn's files while its runtime is live and stage them for delivery: a
    routine fire's, or a workflow step's `steps.<id>.files` (`title` is then the run's).

    At most ten files; a file over what a staged row holds stays in the workspace and is
    named in a notice.
    """
    files: list[RoutineFile] = []
    notices: list[str] = []
    if outputs is None:
        return files, notices
    try:
        listed = await outputs.list_outputs()
    except UnsupportedCapability:  # a backend with no workspace listing has nothing to send
        return files, notices
    except MuxError as error:
        listed = ()
        log.exception(
            "routine.files_staging_failed",
            session_id=outputs.session_id,
            causes=backend_refusal_causes(error),
        )
        notices.append("Generated files could not be attached.")
    try:
        for meta in listed:
            if meta.filename.startswith(HANDOFF_FILENAME_PREFIX) or meta.size_bytes == 0:
                continue
            limit = min(MAX_UPLOAD_BYTES, meta.max_read_bytes or MAX_UPLOAD_BYTES)
            if meta.size_bytes > limit:
                skipped = SkippedFile(
                    file_id=meta.id, filename=meta.filename, size_bytes=meta.size_bytes
                )
                notices.append(render_oversize_notice(skipped, max_bytes=limit))
                continue
            if len(files) == MAX_FILES:
                notices.append(
                    "More generated files remain with the agent: "
                    "this delivery can attach at most 10 files."
                )
                break
            content = await outputs.read_output(meta.id)
            if not content:
                continue
            async with sessions.begin() as db:
                handle = await stage_upload(
                    db,
                    tenant_id=tenant_id,
                    title=title,
                    display_filename=meta.filename,
                    content_type=meta.mime_type,
                    content=content,
                    now=datetime.now(UTC),
                )
            files.append(
                RoutineFile(
                    file_id=handle,
                    filename=meta.filename,
                    mime_type=meta.mime_type,
                    size_bytes=len(content),
                    staged=True,
                )
            )
    except (MuxError, UploadTooLargeError) as error:
        # File collection must not discard a routine's completed reply.
        log.exception(
            "routine.files_staging_failed",
            session_id=outputs.session_id,
            causes=backend_refusal_causes(error),
        )
        notices.append("Generated files could not be attached.")
    return files, notices


async def deliver_routine_files(
    client: AsyncAnthropic | None,
    payload: str | None,
    *,
    max_bytes: int,
    post: Callable[[DeliverableFile], Awaitable[None]],
    notice: Callable[[str], Awaitable[None]],
    may_post: Callable[[], Awaitable[bool]],
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    keep_staged: bool = False,
) -> None:
    """Fail on the first uncertain upload; the outbox owns retries and settlement.

    Delivery is never retried, so a routine's staged files are deleted once it ends, either
    way. A workflow's stay: a later post, an approval or the run page reads them again.
    """
    refs = decode_payload(payload).files
    try:
        await _deliver(client, refs, max_bytes, post, notice, may_post, sessions, tenant_id)
    finally:
        if not keep_staged and (staged := [ref.file_id for ref in refs if ref.staged]):
            async with sessions.begin() as db:
                await delete_uploads(
                    db, title=ROUTINE_OUTPUT_TITLE, tenant_id=tenant_id, handle_ids=staged
                )


class FileGone(RuntimeError):
    """A staged file's bytes are no longer stored."""


async def read_file(
    client: AsyncAnthropic | None,
    ref: RoutineFile,
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> bytes:
    """A generated file's bytes: from the staging table when staged, else the provider's files."""
    if not ref.staged:
        if client is None:
            raise RuntimeError("Generated file delivery is unavailable; it will not be retried.")
        return await download_output_file(client, ref.file_id)
    async with sessions() as db:
        upload = await get_upload(db, tenant_id=tenant_id, handle_id=ref.file_id)
    if upload is None or upload.content is None or not is_staged_output(upload.title):
        raise FileGone("A generated file is gone; delivery stopped.")
    return upload.content


async def _deliver(
    client: AsyncAnthropic | None,
    refs: Sequence[RoutineFile],
    max_bytes: int,
    post: Callable[[DeliverableFile], Awaitable[None]],
    notice: Callable[[str], Awaitable[None]],
    may_post: Callable[[], Awaitable[bool]],
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> None:
    for ref in refs:
        if not await may_post():
            raise RuntimeError("The routine destination or owner changed; delivery stopped.")
        if ref.size_bytes > max_bytes:
            await notice(
                render_oversize_notice(
                    SkippedFile(
                        file_id=ref.file_id, filename=ref.filename, size_bytes=ref.size_bytes
                    ),
                    max_bytes=max_bytes,
                )
            )
            continue
        content = await read_file(client, ref, sessions, tenant_id)
        if len(content) != ref.size_bytes:
            raise ValueError("Generated file size changed; delivery stopped.")
        if not await may_post():
            raise RuntimeError("The routine destination or owner changed; delivery stopped.")
        await post(DeliverableFile(**ref.model_dump(exclude={"staged"}), content=content))


def unsupported_files_note(payload: str | None) -> str:
    return (
        "\n\nGenerated files are not supported here; they remain with the agent."
        if decode_payload(payload).files
        else ""
    )
