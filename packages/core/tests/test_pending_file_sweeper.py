"""Tests for daimon.core.pending_file_sweeper."""

from __future__ import annotations

import datetime as dt

import anthropic
import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import DeletedFile
from daimon.core.pending_file_sweeper import sweep_pending_file_deletes
from daimon.core.stores.pending_file_deletes import (
    enqueue_pending_file_delete,
    list_due_pending_file_deletes,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOW = dt.datetime(2026, 5, 29, 12, 0, 0, tzinfo=dt.UTC)


def _file_delete_id(request: httpx.Request) -> str:
    """Extract the file_id from a DELETE /v1/files/{file_id} request path."""
    return request.url.path.split("/")[-1]


@pytest.mark.asyncio
async def test_sweep_deletes_due_rows_and_leaves_not_due(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two due rows are deleted via the SDK and removed; the not-yet-due row stays."""
    async with db_session_factory() as s, s.begin():
        await enqueue_pending_file_delete(
            s, file_id="file_due_a", delete_after=NOW - dt.timedelta(minutes=5)
        )
        await enqueue_pending_file_delete(
            s, file_id="file_due_b", delete_after=NOW - dt.timedelta(minutes=1)
        )
        await enqueue_pending_file_delete(
            s, file_id="file_future", delete_after=NOW + dt.timedelta(hours=1)
        )

    deleted_via_sdk: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE" and request.url.path.startswith("/v1/files/"):
            file_id = _file_delete_id(request)
            deleted_via_sdk.append(file_id)
            return httpx.Response(
                200,
                json=DeletedFile(id=file_id, type="file_deleted").model_dump(mode="json"),
            )
        raise AssertionError(f"unexpected call: {request.method} {request.url}")

    client = AsyncAnthropic(
        api_key="sk-test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    swept = await sweep_pending_file_deletes(client, db_session_factory, now=NOW)

    assert sorted(swept) == ["file_due_a", "file_due_b"], "both due files reported swept"
    assert sorted(deleted_via_sdk) == ["file_due_a", "file_due_b"], "both due files SDK-deleted"

    async with db_session_factory() as s, s.begin():
        remaining = await list_due_pending_file_deletes(s, now=NOW + dt.timedelta(days=365))
    assert [r.file_id for r in remaining] == ["file_future"], (
        "only the not-yet-due row should remain in the queue"
    )


@pytest.mark.asyncio
async def test_sweep_treats_404_as_success_and_removes_row(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A 404 from the Files-API delete (already gone) still removes the DB row."""
    async with db_session_factory() as s, s.begin():
        await enqueue_pending_file_delete(
            s, file_id="file_gone", delete_after=NOW - dt.timedelta(minutes=5)
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE" and request.url.path.startswith("/v1/files/"):
            return httpx.Response(
                404,
                json={
                    "type": "error",
                    "error": {"type": "not_found_error", "message": "file already gone"},
                },
            )
        raise AssertionError(f"unexpected call: {request.method} {request.url}")

    client = AsyncAnthropic(
        api_key="sk-test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    swept = await sweep_pending_file_deletes(client, db_session_factory, now=NOW)

    assert swept == ["file_gone"], "a 404 is treated as success and the id is reported swept"

    async with db_session_factory() as s, s.begin():
        remaining = await list_due_pending_file_deletes(s, now=NOW + dt.timedelta(days=365))
    assert remaining == [], "the row must be removed even when the object was already gone"


@pytest.mark.asyncio
async def test_sweep_propagates_non_404_and_leaves_row(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A non-404 APIError propagates; the row is NOT removed (retried next sweep)."""
    async with db_session_factory() as s, s.begin():
        await enqueue_pending_file_delete(
            s, file_id="file_err", delete_after=NOW - dt.timedelta(minutes=5)
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE" and request.url.path.startswith("/v1/files/"):
            return httpx.Response(
                500,
                json={
                    "type": "error",
                    "error": {"type": "api_error", "message": "internal error"},
                },
            )
        raise AssertionError(f"unexpected call: {request.method} {request.url}")

    client = AsyncAnthropic(
        api_key="sk-test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(anthropic.APIError):
        await sweep_pending_file_deletes(client, db_session_factory, now=NOW)

    async with db_session_factory() as s, s.begin():
        remaining = await list_due_pending_file_deletes(s, now=NOW + dt.timedelta(days=365))
    assert [r.file_id for r in remaining] == ["file_err"], (
        "a failed delete must leave the row for the next sweep to retry"
    )
