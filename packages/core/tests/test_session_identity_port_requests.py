"""Responder identity accepts SDK null/partial metadata without binding projection."""

import contextlib
import uuid
from datetime import UTC, datetime
from typing import Self, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from daimon.core.stores.domain import ThreadSessionRow
from daimon.core.turn import session_identity
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import ResourceRef, Scope
from mux.drivers.anthropic.sessions_lifecycle import AnthropicSessions
from mux.errors import ScopeViolation
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TENANT, ACCOUNT = uuid.UUID(int=801), uuid.UUID(int=802)


class SessionFactory:
    def __call__(self) -> contextlib.nullcontext[Self]:
        return contextlib.nullcontext(self)

    def begin(self) -> contextlib.nullcontext[Self]:
        return contextlib.nullcontext(self)


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


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"metadata": None},
        {"metadata": {"daimon_channel": None}},
        {"metadata": {"daimon_thread": None}},
        {"metadata": {"daimon_channel": None, "daimon_thread": None}},
    ],
    ids=["absent", "null-metadata", "null-channel", "null-thread", "null-channel-and-thread"],
)
async def test_identity_native_read_preserves_minimal_sdk_reply_and_authorized_scope(
    monkeypatch: pytest.MonkeyPatch, extra: dict[str, JsonValue]
) -> None:
    row = mapping()
    factory = cast(async_sessionmaker[AsyncSession], SessionFactory())
    backfill = AsyncMock()
    monkeypatch.setattr(session_identity, "update_agent_identity", backfill)
    observed_scopes: list[tuple[Scope, ResourceRef]] = []
    original_read = AnthropicSessions.read_native

    async def read_native(
        self: AnthropicSessions, scope: Scope, ref: ResourceRef
    ) -> dict[str, JsonValue]:
        observed_scopes.append((scope, ref))
        return await original_read(self, scope, ref)

    monkeypatch.setattr(AnthropicSessions, "read_native", read_native)
    response: dict[str, JsonValue] = {
        "id": "sess_identity",
        "agent": {"id": "agent_original"},
        "status": "future_status",
        "future_field": {"nested": [None, False, 0]},
        **extra,
    }
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        transport.queue(
            ScriptedReply("GET", "/v1/sessions/sess_identity", httpx.Response(200, json=response))
        )
    async with old.client() as legacy, new.client() as client:
        original = await legacy.beta.sessions.retrieve("sess_identity")
        result = await session_identity.check_session_agent(
            client, factory, mapping=row, responder_ma_agent_id="agent_original"
        )
    old.assert_consumed()
    new.assert_consumed()
    assert result.session_exists and result.observed is not None
    assert result.observed.model_dump(mode="json", exclude_unset=True) == original.model_dump(
        mode="json", exclude_unset=True
    )
    backfill.assert_awaited_once_with(factory, id=row.id, ma_agent_id="agent_original")
    assert old.requests == new.requests and len(new.requests) == 1
    assert len(observed_scopes) == 1
    scope, ref = observed_scopes[0]
    assert scope.tenant_id == str(TENANT) and scope.account_id == str(ACCOUNT)
    assert not scope.is_platform and not scope.is_legacy_host_authorized
    assert ref.id == row.ma_session_id and ref.kind == "session"
    assert ref.tenant_id == scope.tenant_id and ref.account_id == scope.account_id
    assert row.watermark_message_id == "progress" and row.ma_agent_id is None


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
