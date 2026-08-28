"""Hosted-client chart delivery with embedded images and optional private URLs."""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import io
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta.file_metadata import FileMetadata
from daimon.core.artifacts import ArtifactStore
from daimon.core.config import ArtifactsSettings
from PIL import Image
from pydantic import BaseModel

from mcp.types import ImageContent

_log = structlog.get_logger(__name__)

_ALLOWED_SUFFIXES = {".jpeg", ".jpg", ".png", ".svg"}
_MAX_BYTES = 10 * 1024 * 1024
_MAX_CHARTS = 5
_MAX_IMAGE_BLOCKS = 3
_MAX_IMAGE_EDGE = 1000
_MAX_IMAGE_BLOCK_ENCODED_BYTES = 400 * 1024
# Pillow's decompression-bomb *error* fires only above ~179M pixels; below
# that a compact PNG can still decode to gigabytes of RGBA, so the byte cap
# alone does not bound decode memory.
_MAX_IMAGE_PIXELS = 25_000_000
_MAX_SCANNED = 200
_CLOCK_SLACK = dt.timedelta(seconds=30)
_HOSTED_DELIVERY_TIMEOUT_SECONDS = 60.0
# put_object runs in a thread asyncio.timeout cannot cancel; an upload that
# starts near the deadline completes after the caller gave up and strands an
# object nobody has a URL for. Don't start one that can't finish in time.
_UPLOAD_DEADLINE_MARGIN_SECONDS = 10.0


class ChartUrl(BaseModel):
    """One time-bounded chart URL returned in a hosted tool result."""

    model_config = {"frozen": True}

    filename: str
    url: str
    expires_at: dt.datetime


@dataclass(frozen=True, slots=True)
class HostedChartDelivery:
    """Text additions and structured chart content for one completed turn."""

    message: str
    chart_urls: tuple[ChartUrl, ...] = ()
    image_blocks: tuple[ImageContent, ...] = ()


@dataclass(frozen=True, slots=True)
class _ChartOutput:
    file_id: str
    filename: str
    size_bytes: int


def _parse_created(value: object) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    return None


def _safe_filename(filename: str) -> str:
    """Accept one bounded basename; model-authored paths never become object keys."""
    if (
        not filename
        or len(filename) > 255
        or filename != PurePosixPath(filename).name
        or "\\" in filename
        or filename in {".", ".."}
    ):
        raise ValueError("unsafe artifact filename")
    return filename


def _image_content_type(filename: str, content: bytes) -> str:
    """Validate extension against a small magic-byte/media allowlist."""
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix == ".png" and content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if suffix in {".jpg", ".jpeg"} and content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if suffix == ".svg":
        prefix = content[:2048].lstrip().lower()
        if prefix.startswith(b"<svg") or (prefix.startswith(b"<?xml") and b"<svg" in prefix):
            return "image/svg+xml"
    raise ValueError("artifact content does not match an allowed image type")


def _markdown_label(filename: str) -> str:
    bounded = filename.replace("\r", " ").replace("\n", " ")[:255]
    return bounded.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


_RASTER_CONTENT_TYPES = frozenset({"image/png", "image/jpeg"})


def _bounded_image_block(content: bytes, content_type: str) -> ImageContent | None:
    """Downscale one raster and return an explicitly typed, bounded MCP block."""
    if content_type not in _RASTER_CONTENT_TYPES:
        return None

    with Image.open(io.BytesIO(content)) as source:
        if source.width * source.height > _MAX_IMAGE_PIXELS:
            raise ValueError("artifact exceeds the per-chart pixel limit")
        source.load()
        image = source.copy()

    image.thumbnail((_MAX_IMAGE_EDGE, _MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
    output_format = "PNG" if content_type == "image/png" else "JPEG"
    if output_format == "JPEG" and image.mode not in {"L", "RGB"}:
        image = image.convert("RGB")

    for _ in range(6):
        buffer = io.BytesIO()
        save_kwargs: dict[str, object] = {"optimize": True}
        if output_format == "JPEG":
            save_kwargs["quality"] = 82
        image.save(buffer, format=output_format, **save_kwargs)
        encoded = base64.b64encode(buffer.getvalue())
        if len(encoded) <= _MAX_IMAGE_BLOCK_ENCODED_BYTES:
            return ImageContent(
                type="image",
                data=encoded.decode("ascii"),
                mimeType=content_type,
            )
        width, height = image.size
        if max(width, height) <= 128:
            break
        image.thumbnail(
            (max(1, int(width * 0.75)), max(1, int(height * 0.75))),
            Image.Resampling.LANCZOS,
        )
    return None


async def _discover_chart_outputs(
    anthropic: AsyncAnthropic,
    *,
    session_id: str,
    turn_started_at: dt.datetime,
) -> tuple[_ChartOutput, ...]:
    """Enumerate recent image outputs from the session's Files API scope."""
    if turn_started_at.tzinfo is None:
        turn_started_at = turn_started_at.replace(tzinfo=dt.UTC)
    cutoff = turn_started_at - _CLOCK_SLACK
    candidates: list[FileMetadata] = []
    async for item in anthropic.beta.files.list(
        scope_id=session_id,
        limit=min(_MAX_SCANNED, 1000),
        betas=["managed-agents-2026-04-01"],
    ):
        candidates.append(item)
        if len(candidates) >= _MAX_SCANNED:
            # The Files API documents no ordering, so the sort below only sees
            # what fit under the cap; a >_MAX_SCANNED session may lose charts.
            _log.warning("mcp.hosted_artifact.scan_truncated", scanned=_MAX_SCANNED)
            break

    candidates.sort(
        key=lambda item: _parse_created(item.created_at) or dt.datetime.min.replace(tzinfo=dt.UTC),
        reverse=True,
    )
    outputs: list[_ChartOutput] = []
    for item in candidates:
        filename = item.filename or item.id
        if PurePosixPath(filename).suffix.lower() not in _ALLOWED_SUFFIXES:
            continue
        created = _parse_created(item.created_at)
        if created is None or created < cutoff:
            continue
        outputs.append(_ChartOutput(file_id=item.id, filename=filename, size_bytes=item.size_bytes))
        if len(outputs) >= _MAX_CHARTS:
            break
    return tuple(outputs)


async def _deliver_hosted_charts_impl(
    anthropic: AsyncAnthropic,
    *,
    settings: ArtifactsSettings | None,
    tenant_id: str,
    account_id: str,
    session_id: str,
    turn_started_at: dt.datetime,
    message: str,
    store: ArtifactStore | None = None,
    record_failure: Callable[..., None],
    upload_deadline: float,
) -> HostedChartDelivery:
    """Embed charts by default and add URLs only when storage is configured."""
    if settings is not None and store is None:
        _log.warning("mcp.hosted_artifact.store_unconfigured")
    try:
        outputs = await _discover_chart_outputs(
            anthropic,
            session_id=session_id,
            turn_started_at=turn_started_at,
        )
    except Exception as exc:  # noqa: BLE001 — degrade-not-block boundary
        record_failure(stage="discovery", key=None, exc=exc)
        return HostedChartDelivery(message=message)

    if not outputs:
        return HostedChartDelivery(message=message)

    lines: list[str] = []
    chart_urls: list[ChartUrl] = []
    image_blocks: list[ImageContent] = []
    embed_images = settings is None or settings.embed_images
    ttl_minutes = max(1, (settings.url_ttl_seconds + 59) // 60) if settings is not None else None

    for output in outputs[:_MAX_CHARTS]:
        key: str | None = None
        try:
            filename = _safe_filename(output.filename)
            if output.size_bytes > _MAX_BYTES:
                raise ValueError("artifact exceeds the per-chart byte limit")
            response = await anthropic.beta.files.download(
                output.file_id,
                betas=["managed-agents-2026-04-01"],
            )
            content = await response.read()
            if len(content) > _MAX_BYTES:
                raise ValueError("artifact exceeds the per-chart byte limit")
            content_type = _image_content_type(filename, content)
        except Exception as exc:  # noqa: BLE001 — one chart cannot block the answer
            record_failure(stage="download", key=None, exc=exc)
            continue

        if embed_images and len(image_blocks) < _MAX_IMAGE_BLOCKS:
            try:
                image_block = await asyncio.to_thread(_bounded_image_block, content, content_type)
            except Exception as exc:  # noqa: BLE001 — optional URLs can still succeed
                record_failure(stage="embed", key=None, exc=exc)
            else:
                if image_block is not None:
                    image_blocks.append(image_block)
                elif content_type in _RASTER_CONTENT_TYPES:
                    _log.warning(
                        "mcp.hosted_artifact.image_too_large",
                        filename=filename,
                        encoded_byte_cap=_MAX_IMAGE_BLOCK_ENCODED_BYTES,
                    )
                else:
                    _log.debug(
                        "mcp.hosted_artifact.image_not_embeddable",
                        filename=filename,
                        content_type=content_type,
                    )

        if settings is None or store is None:
            continue

        if asyncio.get_running_loop().time() > upload_deadline - _UPLOAD_DEADLINE_MARGIN_SECONDS:
            record_failure(
                stage="storage",
                key=None,
                exc=TimeoutError("upload skipped near the delivery deadline"),
            )
            continue

        try:
            # file_id keeps the key unique per upload: a turn that rewrites
            # chart.png must not repoint an earlier turn's still-live URL.
            key = (
                f"tenant/{tenant_id}/account/{account_id}/session/{session_id}/"
                f"{_safe_filename(output.file_id)}/{filename}"
            )
            stored = await store.upload_and_presign(
                key=key,
                content=content,
                content_type=content_type,
                ttl_seconds=settings.url_ttl_seconds,
            )
            chart_urls.append(
                ChartUrl(
                    filename=filename,
                    url=stored.url,
                    expires_at=stored.expires_at,
                )
            )
            lines.append(
                f"Chart: [{_markdown_label(filename)}]({stored.url}) — "
                f"expires in ~{ttl_minutes} min"
            )
            _log.info(
                "mcp.hosted_artifact.available",
                key=key,
                ttl_seconds=settings.url_ttl_seconds,
            )
        except Exception as exc:  # noqa: BLE001 — embedding remains available
            record_failure(stage="storage", key=key, exc=exc)

    delivered_message = f"{message}\n\n" + "\n".join(lines) if lines else message
    return HostedChartDelivery(
        message=delivered_message,
        chart_urls=tuple(chart_urls),
        image_blocks=tuple(image_blocks),
    )


async def deliver_hosted_charts(
    anthropic: AsyncAnthropic,
    *,
    settings: ArtifactsSettings | None,
    tenant_id: str,
    account_id: str,
    session_id: str,
    turn_started_at: dt.datetime,
    message: str,
    store: ArtifactStore | None = None,
) -> HostedChartDelivery:
    """Deliver bounded chart images and optional URLs without blocking text."""
    failure_count = 0
    first_failure: tuple[str, str | None, str] | None = None

    def record_failure(*, stage: str, key: str | None, exc: Exception) -> None:
        nonlocal failure_count, first_failure
        failure_count += 1
        if first_failure is None:
            first_failure = (stage, key, type(exc).__name__)

    def log_failure_summary() -> None:
        if first_failure is None:
            return
        stage, key, error_type = first_failure
        _log.warning(
            "mcp.hosted_artifact.unavailable",
            stage=stage,
            key=key,
            error_type=error_type,
            failure_count=failure_count,
        )

    try:
        upload_deadline = asyncio.get_running_loop().time() + _HOSTED_DELIVERY_TIMEOUT_SECONDS
        async with asyncio.timeout(_HOSTED_DELIVERY_TIMEOUT_SECONDS):
            result = await _deliver_hosted_charts_impl(
                anthropic,
                settings=settings,
                tenant_id=tenant_id,
                account_id=account_id,
                session_id=session_id,
                turn_started_at=turn_started_at,
                message=message,
                store=store,
                record_failure=record_failure,
                upload_deadline=upload_deadline,
            )
    except TimeoutError as exc:
        record_failure(stage="timeout", key=None, exc=exc)
        result = HostedChartDelivery(message=message)
    log_failure_summary()
    return result
