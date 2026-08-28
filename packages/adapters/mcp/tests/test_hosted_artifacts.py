from __future__ import annotations

import asyncio
import base64
import datetime as dt
import io
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta.file_metadata import FileMetadata
from daimon.adapters.mcp.hosted_artifacts import (
    HostedChartDelivery,
    _ChartOutput,
    deliver_hosted_charts,
)
from daimon.core.artifacts import StoredArtifact
from daimon.core.config import ArtifactsSettings
from daimon.testing.ma import build_fake_anthropic
from PIL import Image

pytestmark = pytest.mark.asyncio

_NOW = dt.datetime(2026, 8, 23, 12, 0, tzinfo=dt.UTC)


def _png(*, width: int = 40, height: int = 20) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color=(0, 100, 180)).save(buffer, format="PNG")
    return buffer.getvalue()


_PNG = _png()


def _settings(*, ttl: int = 600) -> ArtifactsSettings:
    return ArtifactsSettings(
        endpoint_url="https://bucket.example.test",
        bucket="private-artifacts",
        access_key_id="access-key",
        secret_access_key="secret-key",
        region="auto",
        url_ttl_seconds=ttl,
    )


class FakeStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def upload_and_presign(self, **kwargs: Any) -> StoredArtifact:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("bucket unavailable")
        key = str(kwargs["key"])
        ttl = int(kwargs["ttl_seconds"])
        return StoredArtifact(
            key=key,
            url=f"https://bucket.example.test/{key}?signed=yes",
            expires_at=_NOW + dt.timedelta(seconds=ttl),
        )


class _FilesTransport:
    def __init__(self, *, content: bytes, files: tuple[dict[str, Any], ...]) -> None:
        self.content = content
        self.files = files
        self.downloaded_ids: list[str] = []
        self.beta_headers: list[str] = []
        self.list_params: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/files":
            self.beta_headers.append(request.headers.get("anthropic-beta", ""))
            self.list_params.append(dict(request.url.params))
            return httpx.Response(200, json={"data": list(self.files), "has_more": False})
        prefix = "/v1/files/"
        suffix = "/content"
        if (
            request.method == "GET"
            and request.url.path.startswith(prefix)
            and request.url.path.endswith(suffix)
        ):
            file_id = request.url.path[len(prefix) : -len(suffix)]
            self.downloaded_ids.append(file_id)
            self.beta_headers.append(request.headers.get("anthropic-beta", ""))
            return httpx.Response(200, content=self.content)
        raise AssertionError(f"unexpected Files API request: {request.method} {request.url.path}")


def _client(
    content: bytes = _PNG,
    *,
    files: tuple[dict[str, Any], ...] = (),
) -> tuple[AsyncAnthropic, _FilesTransport]:
    transport = _FilesTransport(content=content, files=files)
    return build_fake_anthropic(transport), transport


def _file(
    file_id: str,
    filename: str,
    *,
    created_at: dt.datetime,
    size_bytes: int = len(_PNG),
) -> dict[str, Any]:
    return FileMetadata(
        id=file_id,
        type="file",
        filename=filename,
        mime_type="image/png",
        created_at=created_at,
        size_bytes=size_bytes,
    ).model_dump(mode="json")


async def test_embed_only_delivery_needs_no_artifact_settings() -> None:
    client, transport = _client(
        files=(_file("current", "current.png", created_at=_NOW),),
    )

    result = await deliver_hosted_charts(
        client,
        settings=None,
        tenant_id="tenant-1",
        account_id="account-1",
        session_id="session-1",
        turn_started_at=_NOW,
        message="Original answer",
    )

    assert result.message == "Original answer"
    assert result.chart_urls == ()
    assert len(result.image_blocks) == 1
    assert transport.downloaded_ids == ["current"]
    assert len(transport.beta_headers) == 2
    assert all("managed-agents-2026-04-01" in value for value in transport.beta_headers)


async def test_configured_store_adds_presigned_link_and_structured_url() -> None:
    client, _transport = _client()
    store = FakeStore()
    output = _ChartOutput(file_id="file-1", filename="margin.png", size_bytes=len(_PNG))

    with patch(
        "daimon.adapters.mcp.hosted_artifacts._discover_chart_outputs",
        new=AsyncMock(return_value=(output,)),
    ):
        result = await deliver_hosted_charts(
            client,
            settings=_settings(ttl=725),
            tenant_id="tenant-1",
            account_id="account-1",
            session_id="session-1",
            turn_started_at=_NOW,
            message="Analytical answer",
            store=store,
        )

    key = "tenant/tenant-1/account/account-1/session/session-1/file-1/margin.png"
    assert result.message == (
        "Analytical answer\n\n"
        f"Chart: [margin.png](https://bucket.example.test/{key}?signed=yes) — "
        "expires in ~13 min"
    )
    assert len(result.chart_urls) == 1
    assert result.chart_urls[0].filename == "margin.png"
    assert result.chart_urls[0].expires_at == _NOW + dt.timedelta(seconds=725)
    assert len(result.image_blocks) == 1
    assert store.calls == [
        {
            "key": key,
            "content": _PNG,
            "content_type": "image/png",
            "ttl_seconds": 725,
        }
    ]


async def test_image_block_has_exact_shape_mime_and_bounds() -> None:
    source = _png(width=1600, height=800)
    client, _transport = _client(source)
    output = _ChartOutput(file_id="file-1", filename="wide.png", size_bytes=len(source))

    with patch(
        "daimon.adapters.mcp.hosted_artifacts._discover_chart_outputs",
        new=AsyncMock(return_value=(output,)),
    ):
        result = await deliver_hosted_charts(
            client,
            settings=None,
            tenant_id="tenant-1",
            account_id="account-1",
            session_id="session-1",
            turn_started_at=_NOW,
            message="Answer",
        )

    assert len(result.image_blocks) == 1
    block = result.image_blocks[0]
    assert block.model_dump(by_alias=True, exclude_none=True) == {
        "type": "image",
        "data": block.data,
        "mimeType": "image/png",
    }
    assert len(block.data.encode("ascii")) <= 400 * 1024
    with Image.open(io.BytesIO(base64.b64decode(block.data))) as embedded:
        assert max(embedded.size) <= 1000


async def test_discovery_filters_cutoff_and_suffix() -> None:
    client, transport = _client(
        files=(
            _file("old", "old.png", created_at=_NOW - dt.timedelta(seconds=31)),
            _file("text", "notes.txt", created_at=_NOW),
            _file("current", "current.png", created_at=_NOW),
        ),
    )

    result = await deliver_hosted_charts(
        client,
        settings=None,
        tenant_id="tenant-1",
        account_id="account-1",
        session_id="session-1",
        turn_started_at=_NOW,
        message="Answer",
    )

    assert len(result.image_blocks) == 1
    assert transport.downloaded_ids == ["current"]


async def test_discovery_sorts_newest_first_before_chart_cap() -> None:
    files = tuple(
        _file(
            f"file-{index}",
            f"chart-{index}.png",
            created_at=_NOW + dt.timedelta(seconds=index),
        )
        for index in range(6)
    )
    client, transport = _client(files=files)

    result = await deliver_hosted_charts(
        client,
        settings=None,
        tenant_id="tenant-1",
        account_id="account-1",
        session_id="session-1",
        turn_started_at=_NOW,
        message="Answer",
    )

    assert len(result.image_blocks) == 3
    assert transport.downloaded_ids == [
        "file-5",
        "file-4",
        "file-3",
        "file-2",
        "file-1",
    ]


async def test_unbounded_embed_logs_the_byte_cap_drop() -> None:
    client, _transport = _client()
    output = _ChartOutput(file_id="file-1", filename="dense.png", size_bytes=len(_PNG))

    with (
        patch(
            "daimon.adapters.mcp.hosted_artifacts._discover_chart_outputs",
            new=AsyncMock(return_value=(output,)),
        ),
        patch(
            "daimon.adapters.mcp.hosted_artifacts._bounded_image_block",
            return_value=None,
        ),
        patch("daimon.adapters.mcp.hosted_artifacts._log.warning") as warning,
    ):
        result = await deliver_hosted_charts(
            client,
            settings=None,
            tenant_id="tenant-1",
            account_id="account-1",
            session_id="session-1",
            turn_started_at=_NOW,
            message="Answer survives",
        )

    assert result == HostedChartDelivery(message="Answer survives")
    warning.assert_called_once_with(
        "mcp.hosted_artifact.image_too_large",
        filename="dense.png",
        encoded_byte_cap=400 * 1024,
    )


async def test_invalid_outputs_fail_open_to_unchanged_text() -> None:
    client, transport = _client(
        b"not-an-image",
        files=(
            _file("unsafe", "../escape.png", created_at=_NOW),
            _file("mismatch", "mismatch.png", created_at=_NOW),
        ),
    )

    with patch("daimon.adapters.mcp.hosted_artifacts._log.warning") as warning:
        result = await deliver_hosted_charts(
            client,
            settings=None,
            tenant_id="tenant-1",
            account_id="account-1",
            session_id="session-1",
            turn_started_at=_NOW,
            message="Answer survives",
        )

    assert result == HostedChartDelivery(message="Answer survives")
    assert transport.downloaded_ids == ["mismatch"]
    warning.assert_called_once()
    assert warning.call_args.kwargs["failure_count"] == 2


async def test_embedding_can_be_disabled_without_disabling_urls() -> None:
    client, _transport = _client()
    store = FakeStore()
    output = _ChartOutput(file_id="file-1", filename="chart.png", size_bytes=len(_PNG))
    settings = _settings().model_copy(update={"embed_images": False})

    with patch(
        "daimon.adapters.mcp.hosted_artifacts._discover_chart_outputs",
        new=AsyncMock(return_value=(output,)),
    ):
        result = await deliver_hosted_charts(
            client,
            settings=settings,
            tenant_id="tenant-1",
            account_id="account-1",
            session_id="session-1",
            turn_started_at=_NOW,
            message="Answer",
            store=store,
        )

    assert len(result.chart_urls) == 1
    assert result.image_blocks == ()


async def test_storage_failure_keeps_embed_only_result() -> None:
    client, _transport = _client()
    store = FakeStore(fail=True)
    output = _ChartOutput(file_id="file-1", filename="chart.png", size_bytes=len(_PNG))

    with (
        patch(
            "daimon.adapters.mcp.hosted_artifacts._discover_chart_outputs",
            new=AsyncMock(return_value=(output,)),
        ),
        patch("daimon.adapters.mcp.hosted_artifacts._log.warning") as warning,
    ):
        result = await deliver_hosted_charts(
            client,
            settings=_settings(),
            tenant_id="tenant-1",
            account_id="account-1",
            session_id="session-1",
            turn_started_at=_NOW,
            message="Answer still returned",
            store=store,
        )

    assert result.message == "Answer still returned"
    assert result.chart_urls == ()
    assert len(result.image_blocks) == 1
    warning.assert_called_once()
    assert "https://" not in repr(warning.call_args)
    assert warning.call_args.kwargs["failure_count"] == 1


async def test_delivery_timeout_returns_text_unchanged() -> None:
    client, _transport = _client()

    async def hang(*args: Any, **kwargs: Any) -> tuple[_ChartOutput, ...]:
        del args, kwargs
        await asyncio.Event().wait()
        return ()

    with (
        patch("daimon.adapters.mcp.hosted_artifacts._discover_chart_outputs", new=hang),
        patch("daimon.adapters.mcp.hosted_artifacts._HOSTED_DELIVERY_TIMEOUT_SECONDS", 0.001),
        patch("daimon.adapters.mcp.hosted_artifacts._log.warning") as warning,
    ):
        result = await deliver_hosted_charts(
            client,
            settings=None,
            tenant_id="tenant-1",
            account_id="account-1",
            session_id="session-1",
            turn_started_at=_NOW,
            message="Analysis survives",
        )

    assert result == HostedChartDelivery(message="Analysis survives")
    warning.assert_called_once()
    assert warning.call_args.kwargs["error_type"] == "TimeoutError"
    assert warning.call_args.kwargs["failure_count"] == 1


async def test_delivery_caps_each_turn_and_embeds_at_most_three_images() -> None:
    client, transport = _client()
    store = FakeStore()
    outputs = tuple(
        _ChartOutput(
            file_id=f"file-{index}",
            filename=f"chart-{index}.png",
            size_bytes=(10 * 1024 * 1024 + 1 if index == 0 else len(_PNG)),
        )
        for index in range(6)
    )

    with patch(
        "daimon.adapters.mcp.hosted_artifacts._discover_chart_outputs",
        new=AsyncMock(return_value=outputs),
    ):
        result = await deliver_hosted_charts(
            client,
            settings=_settings(),
            tenant_id="tenant-1",
            account_id="account-1",
            session_id="session-1",
            turn_started_at=_NOW,
            message="Answer",
            store=store,
        )

    assert "chart-0.png" not in result.message
    assert "chart-5.png" not in result.message
    assert len(result.chart_urls) == 4
    assert len(result.image_blocks) == 3
    assert len(store.calls) == 4
    assert len(transport.downloaded_ids) == 4


async def test_oversized_pixel_raster_is_dropped_not_decoded() -> None:
    source = _png(width=200, height=100)
    client, _transport = _client(source)
    output = _ChartOutput(file_id="file-1", filename="huge.png", size_bytes=len(source))

    with (
        patch(
            "daimon.adapters.mcp.hosted_artifacts._discover_chart_outputs",
            new=AsyncMock(return_value=(output,)),
        ),
        patch("daimon.adapters.mcp.hosted_artifacts._MAX_IMAGE_PIXELS", 10_000),
        patch("daimon.adapters.mcp.hosted_artifacts._log.warning") as warning,
    ):
        result = await deliver_hosted_charts(
            client,
            settings=None,
            tenant_id="tenant-1",
            account_id="account-1",
            session_id="session-1",
            turn_started_at=_NOW,
            message="Answer survives",
        )

    assert result == HostedChartDelivery(message="Answer survives")
    warning.assert_called_once()
    assert warning.call_args.kwargs["stage"] == "embed"
    assert warning.call_args.kwargs["error_type"] == "ValueError"


async def test_same_filename_across_files_gets_distinct_object_keys() -> None:
    client, _transport = _client()
    store = FakeStore()
    outputs = (
        _ChartOutput(file_id="file-a", filename="chart.png", size_bytes=len(_PNG)),
        _ChartOutput(file_id="file-b", filename="chart.png", size_bytes=len(_PNG)),
    )

    with patch(
        "daimon.adapters.mcp.hosted_artifacts._discover_chart_outputs",
        new=AsyncMock(return_value=outputs),
    ):
        await deliver_hosted_charts(
            client,
            settings=_settings(),
            tenant_id="tenant-1",
            account_id="account-1",
            session_id="session-1",
            turn_started_at=_NOW,
            message="Answer",
            store=store,
        )

    keys = [call["key"] for call in store.calls]
    assert keys == [
        "tenant/tenant-1/account/account-1/session/session-1/file-a/chart.png",
        "tenant/tenant-1/account/account-1/session/session-1/file-b/chart.png",
    ]


async def test_upload_is_skipped_when_the_delivery_deadline_is_near() -> None:
    client, _transport = _client()
    store = FakeStore()
    output = _ChartOutput(file_id="file-1", filename="chart.png", size_bytes=len(_PNG))

    with (
        patch(
            "daimon.adapters.mcp.hosted_artifacts._discover_chart_outputs",
            new=AsyncMock(return_value=(output,)),
        ),
        patch("daimon.adapters.mcp.hosted_artifacts._HOSTED_DELIVERY_TIMEOUT_SECONDS", 5.0),
        patch("daimon.adapters.mcp.hosted_artifacts._UPLOAD_DEADLINE_MARGIN_SECONDS", 10.0),
        patch("daimon.adapters.mcp.hosted_artifacts._log.warning") as warning,
    ):
        result = await deliver_hosted_charts(
            client,
            settings=_settings(),
            tenant_id="tenant-1",
            account_id="account-1",
            session_id="session-1",
            turn_started_at=_NOW,
            message="Answer survives",
            store=store,
        )

    assert store.calls == []
    assert result.chart_urls == ()
    assert len(result.image_blocks) == 1
    warning.assert_called_once()
    assert warning.call_args.kwargs["stage"] == "storage"
    assert warning.call_args.kwargs["error_type"] == "TimeoutError"


async def test_discovery_requests_full_pages_and_logs_scan_truncation() -> None:
    files = tuple(
        _file(f"file-{index}", f"chart-{index}.png", created_at=_NOW) for index in range(4)
    )
    client, transport = _client(files=files)

    with (
        patch("daimon.adapters.mcp.hosted_artifacts._MAX_SCANNED", 3),
        patch("daimon.adapters.mcp.hosted_artifacts._log.warning") as warning,
    ):
        await deliver_hosted_charts(
            client,
            settings=None,
            tenant_id="tenant-1",
            account_id="account-1",
            session_id="session-1",
            turn_started_at=_NOW,
            message="Answer",
        )

    assert transport.list_params[0]["limit"] == "3"
    truncation_calls = [
        call
        for call in warning.call_args_list
        if call.args and call.args[0] == "mcp.hosted_artifact.scan_truncated"
    ]
    assert len(truncation_calls) == 1
    assert truncation_calls[0].kwargs["scanned"] == 3


async def test_configured_settings_without_store_logs_once_and_still_embeds() -> None:
    client, _transport = _client(
        files=(_file("current", "current.png", created_at=_NOW),),
    )

    with patch("daimon.adapters.mcp.hosted_artifacts._log.warning") as warning:
        result = await deliver_hosted_charts(
            client,
            settings=_settings(),
            tenant_id="tenant-1",
            account_id="account-1",
            session_id="session-1",
            turn_started_at=_NOW,
            message="Answer",
            store=None,
        )

    assert result.chart_urls == ()
    assert len(result.image_blocks) == 1
    warning.assert_called_once_with("mcp.hosted_artifact.store_unconfigured")
