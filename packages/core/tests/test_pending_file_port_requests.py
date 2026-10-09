"""TTL deletion keeps the original Files API headers and queue outcomes."""

from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import httpx
import pytest
from anthropic import InternalServerError, NotFoundError
from daimon.core import pending_file_sweeper
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.parametrize("status", [200, 404, 500])
async def test_ttl_delete_matches_original_request_and_retains_failed_queue_row(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    due = AsyncMock(return_value=[SimpleNamespace(file_id="file_ttl")])
    clear = AsyncMock()
    monkeypatch.setattr(pending_file_sweeper, "list_due_pending_file_deletes", due)
    monkeypatch.setattr(pending_file_sweeper, "delete_pending_file_delete", clear)
    session = SimpleNamespace(begin=nullcontext)

    def session_factory() -> nullcontext[SimpleNamespace]:
        return nullcontext(session)

    # A context-only fake exercises queue outcomes without a database.
    factory = cast(async_sessionmaker[AsyncSession], session_factory)

    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        transport.queue(
            ScriptedReply(
                "DELETE",
                "/v1/files/file_ttl",
                httpx.Response(
                    status,
                    json={"id": "file_ttl", "type": "file_deleted"}
                    if status == 200
                    else {"error": {"type": "api_error", "message": "gone or failed"}},
                ),
            )
        )
    async with old.client() as legacy, new.client() as client:
        if status == 500:
            with pytest.raises(InternalServerError):
                await legacy.beta.files.delete("file_ttl")
            with pytest.raises(InternalServerError):
                await pending_file_sweeper.sweep_pending_file_deletes(
                    client, factory, now=datetime(2026, 10, 9, tzinfo=UTC)
                )
            clear.assert_not_awaited()
        else:
            try:
                await legacy.beta.files.delete("file_ttl")
            except NotFoundError:
                assert status == 404
            assert await pending_file_sweeper.sweep_pending_file_deletes(
                client, factory, now=datetime(2026, 10, 9, tzinfo=UTC)
            ) == ["file_ttl"]
            clear.assert_awaited_once_with(session, file_id="file_ttl")
    old.assert_consumed()
    new.assert_consumed()
    assert old.requests == new.requests
