"""Tests for PUT /bundles — auth, streamed cap, gzip check, rate limit, happy path."""

from __future__ import annotations

import gzip
import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import FileMetadata
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core import bundle_handle
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    McpSettings,
    Settings,
)
from daimon.core.stores.pending_file_deletes import list_due_pending_file_deletes
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette

pytestmark = pytest.mark.asyncio

_TENANT_ID: uuid.UUID = uuid.uuid4()
_AGENT_ID: uuid.UUID = uuid.uuid4()
_VALID_TOKEN = "bearer-test-token"
_SECRET = "a" * 32


class _FilesUploadCapture:
    """Records POST /v1/files calls and serves a real FileMetadata.

    Transport-level fake per guideline:testing T3 — no method-level mock on
    the SDK's upload call. Counts calls so cap/gzip tests can assert zero
    uploads happened.
    """

    def __init__(self, *, file_id: str = "file_bundle_test") -> None:
        self.file_id = file_id
        self.upload_count = 0
        self.uploaded_bodies: list[bytes] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/files" and request.method == "POST":
            self.upload_count += 1
            self.uploaded_bodies.append(request.content)
            metadata = FileMetadata(
                id=self.file_id,
                created_at=datetime.now(UTC),
                filename="bundle.tar.gz",
                mime_type="application/gzip",
                size_bytes=len(request.content),
                type="file",
            )
            return httpx.Response(200, json=metadata.model_dump(mode="json"))
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")


def _client_for(capture: _FilesUploadCapture) -> AsyncAnthropic:
    transport = httpx.MockTransport(capture.handler)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://api.anthropic.com")
    return AsyncAnthropic(api_key="test", http_client=http_client)


def _build_app(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    anthropic: AsyncAnthropic,
    tokens: dict[str, dict[str, object]] | None = None,
    bundle_max_bytes: int = 25 * 1024 * 1024,
    bundle_uploads_per_hour: int = 20,
) -> Starlette:
    effective_tokens: dict[str, dict[str, object]] = tokens or {
        _VALID_TOKEN: {
            "client_id": "test",
            "tenant_id": str(_TENANT_ID),
            "agent_id": str(_AGENT_ID),
            "jti": "jti-fixed",
        }
    }
    return create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(
                jwt_secret=SecretStr(_SECRET),
                public_url=HttpUrl("https://x/mcp"),
                bundle_max_bytes=bundle_max_bytes,
                bundle_uploads_per_hour=bundle_uploads_per_hour,
            ),
        ),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens=effective_tokens),
        anthropic=anthropic,
    )


async def _chunked_body(total_bytes: int, chunk_size: int = 1024) -> AsyncIterator[bytes]:
    """An async generator content= source — httpx omits Content-Length for it."""
    sent = 0
    chunk = b"\x1f\x8bx" * (chunk_size // 3 + 1)
    while sent < total_bytes:
        piece = chunk[: min(chunk_size, total_bytes - sent)]
        sent += len(piece)
        yield piece


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


async def test_bundles_no_authorization_header_returns_401(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    capture = _FilesUploadCapture()
    app = _build_app(sessionmaker, anthropic=_client_for(capture))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.put("/bundles", content=gzip.compress(b"data"))
    assert r.status_code == 401, "missing Authorization header must be rejected with 401"
    assert capture.upload_count == 0, "no upload should be attempted without auth"


async def test_bundles_rejected_bearer_returns_401(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    capture = _FilesUploadCapture()
    app = _build_app(sessionmaker, anthropic=_client_for(capture))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.put(
            "/bundles",
            content=gzip.compress(b"data"),
            headers={"Authorization": "Bearer forged-invalid-token"},
        )
    assert r.status_code == 401, "a token the verifier rejects must return 401"
    assert capture.upload_count == 0, "no upload should be attempted with a rejected token"


async def test_bundles_missing_tenant_id_claim_returns_403(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    token = "token-no-tenant"
    capture = _FilesUploadCapture()
    app = _build_app(
        sessionmaker,
        anthropic=_client_for(capture),
        tokens={token: {"client_id": "test", "agent_id": str(_AGENT_ID)}},
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.put(
            "/bundles",
            content=gzip.compress(b"data"),
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 403, "a verified token with no tenant_id claim must be rejected"
    assert capture.upload_count == 0


async def test_bundles_missing_agent_id_claim_returns_403(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A plain account token (tenant scope, no agent scope) cannot upload — T-21-04-B."""
    token = "token-no-agent"
    capture = _FilesUploadCapture()
    app = _build_app(
        sessionmaker,
        anthropic=_client_for(capture),
        tokens={token: {"client_id": "test", "tenant_id": str(_TENANT_ID)}},
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.put(
            "/bundles",
            content=gzip.compress(b"data"),
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 403, (
        "a verified token with a tenant_id but no agent_id claim must be rejected"
    )
    assert capture.upload_count == 0, "no upload should be attempted without agent scope"


# ---------------------------------------------------------------------------
# size cap
# ---------------------------------------------------------------------------


async def test_bundles_content_length_over_cap_returns_413(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    capture = _FilesUploadCapture()
    app = _build_app(sessionmaker, anthropic=_client_for(capture), bundle_max_bytes=100)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.put(
            "/bundles",
            content=b"\x1f\x8b" + b"x" * 200,
            headers={"Authorization": f"Bearer {_VALID_TOKEN}"},
        )
    assert r.status_code == 413, "a declared length over the cap must be refused"
    assert capture.upload_count == 0, "an oversize declared length must never reach the SDK"


async def test_bundles_chunked_body_exceeds_cap_mid_stream_returns_413(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """No Content-Length header (chunked) — only the mid-stream running-total
    check can catch this; the header-only precedent would let it through."""
    capture = _FilesUploadCapture()
    app = _build_app(sessionmaker, anthropic=_client_for(capture), bundle_max_bytes=100)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.put(
            "/bundles",
            content=_chunked_body(total_bytes=500),
            headers={"Authorization": f"Bearer {_VALID_TOKEN}"},
        )
    assert r.status_code == 413, "a chunked body exceeding the cap mid-stream must be refused"
    assert capture.upload_count == 0, "an oversize streamed body must never reach the SDK"


# ---------------------------------------------------------------------------
# gzip check
# ---------------------------------------------------------------------------


async def test_bundles_non_gzip_body_returns_415(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    capture = _FilesUploadCapture()
    app = _build_app(sessionmaker, anthropic=_client_for(capture))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.put(
            "/bundles",
            content=b"not gzip at all",
            headers={"Authorization": f"Bearer {_VALID_TOKEN}"},
        )
    assert r.status_code == 415, "a non-gzip body must be refused with 415"
    assert capture.upload_count == 0, "a non-gzip body must never reach the SDK"


# ---------------------------------------------------------------------------
# rate limit
# ---------------------------------------------------------------------------


async def test_bundles_second_upload_within_hour_returns_429(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    capture = _FilesUploadCapture()
    app = _build_app(sessionmaker, anthropic=_client_for(capture), bundle_uploads_per_hour=1)
    transport = httpx.ASGITransport(app=app)
    body = gzip.compress(b"first upload payload")
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        first = await ac.put(
            "/bundles", content=body, headers={"Authorization": f"Bearer {_VALID_TOKEN}"}
        )
        second = await ac.put(
            "/bundles", content=body, headers={"Authorization": f"Bearer {_VALID_TOKEN}"}
        )
    assert first.status_code == 200, f"first upload under the limit should succeed: {first.text}"
    assert second.status_code == 429, "second upload beyond the per-hour cap must be refused"
    assert capture.upload_count == 1, "the rate-limited call must never reach the SDK"


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


async def test_bundles_happy_path_returns_signed_handle_and_enqueues_delete(
    sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    capture = _FilesUploadCapture(file_id="file_happy_path")
    app = _build_app(sessionmaker, anthropic=_client_for(capture))
    transport = httpx.ASGITransport(app=app)
    body = gzip.compress(b"a small real gzip bundle body")

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.put(
            "/bundles", content=body, headers={"Authorization": f"Bearer {_VALID_TOKEN}"}
        )

    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    payload = r.json()
    assert set(payload) == {"bundle", "sha256", "size_bytes", "expires_at"}, (
        "response must carry exactly bundle, sha256, size_bytes, expires_at"
    )
    assert payload["sha256"] == hashlib.sha256(body).hexdigest(), (
        "sha256 must be over the exact uploaded bytes"
    )
    assert payload["size_bytes"] == len(body), "size_bytes must equal the uploaded body length"

    claims = bundle_handle.verify(
        _SECRET,
        payload["bundle"],
        tenant_id=_TENANT_ID,
        agent_id=_AGENT_ID,
        now=datetime.now(UTC),
    )
    assert claims is not None, "the returned handle must verify against the caller's own claims"
    assert claims.file_id == "file_happy_path", (
        "the handle's file_id must be the id the fake Files API returned"
    )

    due = await list_due_pending_file_deletes(
        db_session, now=datetime.now(UTC) + timedelta(days=365)
    )
    matching = [row for row in due if row.file_id == "file_happy_path"]
    assert len(matching) == 1, "exactly one pending-delete row must exist for the uploaded file_id"
