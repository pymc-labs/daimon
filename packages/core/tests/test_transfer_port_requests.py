"""Thread reads and bundle reuploads preserve their SDK requests and outcomes."""

import contextlib
import io
import uuid
from datetime import UTC, datetime
from typing import Self, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from anthropic import APIError, APIStatusError
from anthropic.types.beta import FileMetadata
from daimon.core import thread_handoff, workspace_transfer
from daimon.core.session_ports_compat import session_scope
from daimon.core.session_seal import session_facts
from daimon.testing.ma_models import ma_session
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOW = datetime(2026, 10, 9, tzinfo=UTC)
TENANT = uuid.UUID(int=1)
ACCOUNT = uuid.UUID(int=2)


class SessionFactory:
    def __call__(self) -> contextlib.nullcontext[Self]:
        return contextlib.nullcontext(self)

    def begin(self) -> contextlib.nullcontext[Self]:
        return contextlib.nullcontext(self)


@pytest.mark.parametrize("status", [200, 404])
async def test_recorded_handoff_sessions_keep_wire_request_and_fail_closed(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    factory = cast(async_sessionmaker[AsyncSession], SessionFactory())
    row = SimpleNamespace(ma_session_id="sess_handoff", account_id=ACCOUNT)
    monkeypatch.setattr(thread_handoff, "list_live_thread_sessions", AsyncMock(return_value=[row]))
    seals = AsyncMock()
    monkeypatch.setattr(thread_handoff, "record_session_seals", seals)
    native = ma_session(id=row.ma_session_id, metadata={"daimon_tenant": str(TENANT)})
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        transport.queue(
            ScriptedReply(
                "GET",
                "/v1/sessions/sess_handoff",
                httpx.Response(
                    status,
                    json=native.model_dump(mode="json")
                    if status == 200
                    else {"error": {"type": "not_found_error", "message": "gone"}},
                ),
            )
        )
    async with old.client() as legacy, new.client() as client:
        with contextlib.suppress(APIError):
            await legacy.beta.sessions.retrieve(row.ma_session_id)
        result = await thread_handoff.recorded_thread_sessions(
            client, factory, tenant_id=TENANT, platform="slack", thread_id="thread"
        )
    old.assert_consumed()
    new.assert_consumed()
    assert old.requests == new.requests
    assert result[0].account_id == ACCOUNT
    if status == 200:
        assert result[0].facts == session_facts(native.metadata, owned=True)
        seals.assert_awaited_once()
    else:
        assert result[0].facts.seal_ids
        assert all(seal.startswith("\x00") for seal in result[0].facts.seal_ids)
        seals.assert_not_awaited()


@pytest.mark.parametrize("status", [200, 400])
async def test_rehost_keeps_default_upload_beta_and_bundle_queue_outcome(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pin multipart boundary entropy so the entire request bytes can be compared.
    def pinned_entropy(count: int) -> bytes:
        return b"\x05" * count

    monkeypatch.setattr("httpx._multipart.os.urandom", pinned_entropy)
    content = b"dummy-gzip-bundle"
    bundle = FileMetadata(
        id="file_output",
        filename="handoff.tar.gz",
        mime_type="application/gzip",
        size_bytes=len(content),
        created_at=NOW,
        type="file",
        downloadable=True,
    )
    uploaded = bundle.model_copy(update={"id": "file_upload"})
    queued = AsyncMock()
    monkeypatch.setattr(workspace_transfer, "enqueue_pending_file_delete", queued)
    factory = cast(async_sessionmaker[AsyncSession], SessionFactory())
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        transport.queue(
            ScriptedReply(
                "GET", "/v1/files/file_output/content", httpx.Response(200, content=content)
            )
        )
        transport.queue(
            ScriptedReply(
                "DELETE",
                "/v1/files/file_output",
                httpx.Response(200, json={"id": "file_output", "type": "file_deleted"}),
            )
        )
        transport.queue(
            ScriptedReply(
                "POST",
                "/v1/files",
                httpx.Response(
                    status,
                    json=uploaded.model_dump(mode="json")
                    if status == 200
                    else {"error": {"type": "invalid_request_error", "message": "failed"}},
                ),
            )
        )
    async with old.client() as legacy, new.client() as client:
        response = await legacy.beta.files.download(bundle.id, betas=["managed-agents-2026-04-01"])
        assert await response.read() == content
        await legacy.beta.files.delete(bundle.id, betas=["managed-agents-2026-04-01"])
        with contextlib.suppress(APIStatusError):
            await legacy.beta.files.upload(
                file=(bundle.filename, io.BytesIO(content), "application/gzip")
            )
        result = await workspace_transfer._rehost_bundle(  # pyright: ignore[reportPrivateUsage]
            client,
            factory,
            bundle=bundle,
            now=lambda: NOW,
            scope=session_scope(tenant_id=TENANT, account_id=None, call_site="test:transfer"),
        )
    old.assert_consumed()
    new.assert_consumed()
    assert old.requests == new.requests
    if status == 200:
        assert result == (uploaded.id, len(content))
        queued.assert_awaited_once_with(
            factory, file_id=uploaded.id, delete_after=NOW + workspace_transfer.BUNDLE_RETENTION
        )
    else:
        assert result == "upload_failed"
        queued.assert_not_awaited()


@pytest.mark.parametrize(
    "upload_reply",
    [
        {"id": "file_upload"},
        {
            "id": "file_upload",
            "filename": "handoff.tar.gz",
            "mime_type": "application/gzip",
            "size_bytes": 3,
        },
    ],
    ids=["id-only", "missing-created-at"],
)
async def test_partial_bundle_upload_preserves_rehost_result_and_cleanup(
    upload_reply: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    def pinned_entropy(count: int) -> bytes:
        return b"\x05" * count

    monkeypatch.setattr("httpx._multipart.os.urandom", pinned_entropy)
    queued = AsyncMock()
    monkeypatch.setattr(workspace_transfer, "enqueue_pending_file_delete", queued)
    factory = cast(async_sessionmaker[AsyncSession], SessionFactory())
    content = b"abc"
    bundle = FileMetadata(
        id="file_output",
        filename="handoff.tar.gz",
        mime_type="application/gzip",
        size_bytes=3,
        created_at=NOW,
        type="file",
        downloadable=True,
    )
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        transport.queue(
            ScriptedReply(
                "GET", "/v1/files/file_output/content", httpx.Response(200, content=content)
            ),
            ScriptedReply(
                "DELETE",
                "/v1/files/file_output",
                httpx.Response(200, json={"id": "file_output", "type": "file_deleted"}),
            ),
            ScriptedReply("POST", "/v1/files", httpx.Response(200, json=upload_reply)),
        )
    async with old.client() as legacy, new.client() as client:
        response = await legacy.beta.files.download(bundle.id, betas=["managed-agents-2026-04-01"])
        legacy_content = await response.read()
        await legacy.beta.files.delete(bundle.id, betas=["managed-agents-2026-04-01"])
        uploaded = await legacy.beta.files.upload(
            file=(bundle.filename, io.BytesIO(legacy_content), "application/gzip")
        )
        expected = (uploaded.id, len(legacy_content))
        result = await workspace_transfer._rehost_bundle(  # pyright: ignore[reportPrivateUsage]
            client,
            factory,
            bundle=bundle,
            now=lambda: NOW,
            scope=session_scope(tenant_id=TENANT, account_id=None, call_site="test:partial-upload"),
        )
    old.assert_consumed()
    new.assert_consumed()
    assert result == expected == ("file_upload", 3)
    assert old.requests == new.requests
    assert len(new.requests) == 3
    queued.assert_awaited_once_with(
        factory, file_id="file_upload", delete_after=NOW + workspace_transfer.BUNDLE_RETENTION
    )
    assert uploaded.model_dump(mode="json", exclude_unset=True) == upload_reply
