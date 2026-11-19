"""Responder identity accepts SDK null/partial metadata without binding projection."""

import uuid
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock

import httpx
import pytest
from daimon.core.stores.domain import ThreadSessionRow
from daimon.core.turn import session_identity
from mux.errors import ScopeViolation
from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .shared_fakes import SessionFactory

TENANT, ACCOUNT = uuid.UUID(int=801), uuid.UUID(int=802)


def mapping() -> ThreadSessionRow:
    return ThreadSessionRow(
        id=uuid.UUID(int=803),
        tenant_id=TENANT,
        account_id=ACCOUNT,
        ma_agent_id=None,
        ma_session_id="sess_identity",
        platform="discord",
        thread_id="thread",
        watermark_message_id="progress",
        status="live",
        created_at=datetime(2026, 10, 10, tzinfo=UTC),
        updated_at=datetime(2026, 10, 10, tzinfo=UTC),
    )


async def test_identity_native_read_rejects_foreign_tenant_before_identity_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backfill = AsyncMock()
    monkeypatch.setattr(session_identity, "update_agent_identity", backfill)
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "GET",
            "/v1/sessions/sess_identity",
            httpx.Response(
                200,
                json={
                    "id": "sess_identity",
                    "agent": {"id": "agent_original"},
                    "metadata": {"daimon_tenant": "other", "daimon_channel": None},
                },
            ),
        )
    )
    async with transport.client() as client:
        with pytest.raises(ScopeViolation):
            await session_identity.check_session_agent(
                client,
                cast(async_sessionmaker[AsyncSession], SessionFactory()),
                mapping=mapping(),
                responder_ma_agent_id="agent_original",
            )
    transport.assert_consumed()
    assert len(transport.requests) == 1
    backfill.assert_not_awaited()
