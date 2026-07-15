"""Tests for credential_env: pure .env assembly + shell upload/mount."""

from __future__ import annotations

import datetime as dt
import uuid

import httpx
import pytest
import structlog.testing
from anthropic import AsyncAnthropic
from anthropic.types.beta import FileMetadata
from daimon.core.credential_env import assemble_env_bytes, upload_env_and_mount
from daimon.core.stores.agent_files import put_agent_file
from daimon.core.stores.domain import AgentFileRow
from daimon.core.stores.pending_file_deletes import list_due_pending_file_deletes
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_NOW = dt.datetime(2026, 5, 29, 12, 0, 0, tzinfo=dt.UTC)


def _row(key: str, content: str) -> AgentFileRow:
    return AgentFileRow(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        key=key,
        content=content,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_assemble_env_bytes_multi_rows_produces_key_value_lines() -> None:
    rows = [_row("A", "1"), _row("B", "2")]
    assert assemble_env_bytes(rows) == b"A=1\nB=2\n", (
        "multi-row assembly should produce KEY=VALUE lines with trailing newline"
    )


def test_assemble_env_bytes_empty_rows_produces_empty_bytes() -> None:
    assert assemble_env_bytes([]) == b"", "empty rows should produce empty bytes"


def test_assemble_env_bytes_single_row_has_trailing_newline() -> None:
    assert assemble_env_bytes([_row("KEY", "VALUE")]) == b"KEY=VALUE\n", (
        "single row should produce KEY=VALUE\\n with trailing newline"
    )


def test_assemble_env_bytes_unicode_roundtrips_byte_exact() -> None:
    value = "café-π-密钥"
    rows = [_row("UNICODE", value)]
    expected = f"UNICODE={value}\n".encode()
    assert assemble_env_bytes(rows) == expected, (
        "non-ASCII values should round-trip byte-exact via utf-8 encode"
    )


class _UploadCapture:
    """Records POST /v1/files calls and serves a real FileMetadata.

    Uploaded multipart body bytes are captured so tests can assert the .env
    content; the handler counts hits so the no-secrets path can assert zero
    uploads.
    """

    def __init__(self, *, file_id: str) -> None:
        self.file_id = file_id
        self.upload_count = 0
        self.uploaded_bodies: list[bytes] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/files" and request.method == "POST":
            self.upload_count += 1
            self.uploaded_bodies.append(request.content)
            metadata = FileMetadata(
                id=self.file_id,
                created_at=_NOW,
                filename=".env",
                mime_type="text/plain",
                size_bytes=len(request.content),
                type="file",
            )
            return httpx.Response(200, json=metadata.model_dump(mode="json"))
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")


def _client_for(capture: _UploadCapture) -> AsyncAnthropic:
    transport = httpx.MockTransport(capture.handler)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://api.anthropic.com")
    return AsyncAnthropic(api_key="test", http_client=http_client)


@pytest.mark.asyncio
async def test_upload_env_and_mount_uploads_env_and_returns_mount_dict(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await put_agent_file(db_session, tenant_id=tenant.id, agent_id=agent_id, key="A", content="1")
    await put_agent_file(db_session, tenant_id=tenant.id, agent_id=agent_id, key="B", content="2")
    await db_session.commit()

    capture = _UploadCapture(file_id="file_test123")
    client = _client_for(capture)

    result = await upload_env_and_mount(
        client, db_session_factory, tenant_id=tenant.id, agent_id=agent_id
    )

    assert result == {
        "type": "file",
        "file_id": "file_test123",
        "mount_path": ".env",
    }, "should return the file resource mount dict with mount_path .env"
    assert capture.upload_count == 1, "should upload exactly once"
    assert b"A=1\nB=2\n" in capture.uploaded_bodies[0], (
        "uploaded multipart body should contain the assembled .env bytes"
    )


@pytest.mark.asyncio
async def test_upload_env_and_mount_returns_none_and_skips_upload_when_no_secrets(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()

    capture = _UploadCapture(file_id="file_unused")
    client = _client_for(capture)

    result = await upload_env_and_mount(
        client, db_session_factory, tenant_id=tenant.id, agent_id=uuid.uuid4()
    )

    assert result is None, "no secrets should return None"
    assert capture.upload_count == 0, "no secrets should perform no upload"


@pytest.mark.asyncio
async def test_upload_env_and_mount_enqueues_ttl_delete(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await put_agent_file(db_session, tenant_id=tenant.id, agent_id=agent_id, key="A", content="1")
    await db_session.commit()

    capture = _UploadCapture(file_id="file_ttl")
    client = _client_for(capture)

    before = dt.datetime.now(dt.UTC)
    await upload_env_and_mount(client, db_session_factory, tenant_id=tenant.id, agent_id=agent_id)
    after = dt.datetime.now(dt.UTC)

    due = await list_due_pending_file_deletes(db_session, now=after + dt.timedelta(hours=2))
    matching = [r for r in due if r.file_id == "file_ttl"]
    assert len(matching) == 1, "exactly one pending-delete row for the uploaded file_id"
    delete_after = matching[0].delete_after
    assert before + dt.timedelta(hours=1) - dt.timedelta(minutes=1) <= delete_after, (
        "delete_after should be ~now + 1h"
    )
    assert delete_after <= after + dt.timedelta(hours=1) + dt.timedelta(minutes=1), (
        "delete_after should be ~now + 1h"
    )


@pytest.mark.asyncio
async def test_upload_env_and_mount_is_tenant_isolated(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_a = await make_tenant(db_session)
    tenant_b = await make_tenant(db_session)
    agent_id = uuid.uuid4()  # same agent_id across tenants
    await put_agent_file(
        db_session, tenant_id=tenant_a.id, agent_id=agent_id, key="A_KEY", content="aval"
    )
    await put_agent_file(
        db_session, tenant_id=tenant_b.id, agent_id=agent_id, key="B_KEY", content="bval"
    )
    await db_session.commit()

    capture = _UploadCapture(file_id="file_tenantA")
    client = _client_for(capture)

    await upload_env_and_mount(client, db_session_factory, tenant_id=tenant_a.id, agent_id=agent_id)

    body = capture.uploaded_bodies[0]
    assert b"A_KEY=aval" in body, "tenant A's secret must be present"
    assert b"B_KEY" not in body, "tenant B's secret must NOT leak into tenant A's .env"
    assert b"bval" not in body, "tenant B's value must NOT leak into tenant A's .env"


@pytest.mark.asyncio
async def test_upload_env_and_mount_never_logs_secret_values(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    secret_value = "super-secret-value-xyz"
    await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        key="SECRET",
        content=secret_value,
    )
    await db_session.commit()

    capture = _UploadCapture(file_id="file_log")
    client = _client_for(capture)

    with structlog.testing.capture_logs() as logs:
        await upload_env_and_mount(
            client, db_session_factory, tenant_id=tenant.id, agent_id=agent_id
        )

    serialized = repr(logs)
    assert secret_value not in serialized, "secret value must never appear in any log event"
    assert any(e.get("event") == "credential_env.mounted" for e in logs), (
        "should emit a mounted log event"
    )
    mounted = [e for e in logs if e.get("event") == "credential_env.mounted"][0]
    assert mounted.get("file_id") == "file_log", "log should carry file_id"
    assert mounted.get("key_count") == 1, "log should carry key_count"
