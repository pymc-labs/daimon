"""Versioned routine outbox envelopes holding provider references, never bytes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import anthropic
import structlog
from anthropic import AsyncAnthropic
from daimon.core.checkpoint_prompt import HANDOFF_FILENAME_PREFIX
from daimon.core.output_delivery import (
    MAX_BYTES_CEILING,
    DeliverableFile,
    SkippedFile,
    download_output_file,
    poll_output_files,
    render_oversize_notice,
)
from daimon.core.stores.domain import RoutineRow
from pydantic import BaseModel, Field

log = structlog.get_logger(__name__)

PREFIX = "\x1eroutine-files-v1\n"
MAX_FILES = 10
OUTBOX_FILES = "generated_files"


class RoutineFile(BaseModel):
    file_id: str
    filename: str
    mime_type: str
    size_bytes: int = Field(gt=0, le=MAX_BYTES_CEILING)


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


def payload_for(row: RoutineRow) -> str | None:
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
    except anthropic.APIError:
        # File collection must not discard a routine's completed reply.
        log.exception("routine.files_collection_failed", session_id=session_id)
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
    if not files and not notices:
        return plain_payload(text) or ""
    return (
        PREFIX + RoutinePayload(text="\n\n".join([text, *notices]), files=files).model_dump_json()
    )


async def deliver_routine_files(
    client: AsyncAnthropic,
    payload: str | None,
    *,
    max_bytes: int,
    post: Callable[[DeliverableFile], Awaitable[None]],
    notice: Callable[[str], Awaitable[None]],
    may_post: Callable[[], Awaitable[bool]],
) -> None:
    """Fail on the first uncertain upload; the outbox owns retries and settlement."""
    for ref in decode_payload(payload).files:
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
        content = await download_output_file(client, ref.file_id)
        if len(content) != ref.size_bytes:
            raise ValueError("Generated file size changed; delivery stopped.")
        if not await may_post():
            raise RuntimeError("The routine destination or owner changed; delivery stopped.")
        await post(DeliverableFile(**ref.model_dump(), content=content))


def unsupported_files_note(payload: str | None) -> str:
    return (
        "\n\nGenerated files are not supported here; they remain with the agent."
        if decode_payload(payload).files
        else ""
    )
