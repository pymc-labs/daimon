"""Budget-stamp extraction keeps actual SDK requests and partial-record semantics."""

import contextlib
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Self, cast
from unittest.mock import AsyncMock, Mock

import anthropic
import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsSession
from daimon.core import session_preparation
from daimon.core.defaults.metadata import MA_METADATA_KEY_BUDGET_CHANNEL
from daimon.core.stores.domain import ThreadSessionRow
from daimon.core.turn.deps import TurnDeps
from daimon.testing.turn_fakes import deps_for
from mux.contracts.ids import ResourceRef, Scope
from mux.drivers.anthropic.sessions_lifecycle import AnthropicSessions
from mux.errors import ScopeViolation
from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TENANT, ACCOUNT = uuid.UUID(int=901), uuid.UUID(int=902)
SESSION = "sess_budget_stamp"
PATH = f"/v1/sessions/{SESSION}"


class SessionFactory:
    def __call__(self) -> contextlib.nullcontext[Self]:
        return contextlib.nullcontext(self)

    def begin(self) -> contextlib.nullcontext[Self]:
        return contextlib.nullcontext(self)


def mapping(*, stamp: str | None = "") -> ThreadSessionRow:
    return ThreadSessionRow(
        id=uuid.UUID(int=903),
        tenant_id=TENANT,
        account_id=ACCOUNT,
        ma_agent_id=None,
        ma_session_id=SESSION,
        platform="discord",
        thread_id="thread",
        budget_channel_id=stamp,
        watermark_message_id="progress",
        status="live",
        created_at=datetime(2026, 10, 11, tzinfo=UTC),
        updated_at=datetime(2026, 10, 11, tzinfo=UTC),
    )


def host(
    monkeypatch: pytest.MonkeyPatch, client: anthropic.AsyncAnthropic, row: ThreadSessionRow | None
) -> tuple[TurnDeps, AsyncMock, AsyncMock, Mock]:
    factory = cast(async_sessionmaker[AsyncSession], SessionFactory())
    lookup, write, warning = AsyncMock(return_value=row), AsyncMock(), Mock()
    monkeypatch.setattr(session_preparation, "get_live_thread_session", lookup)
    monkeypatch.setattr(session_preparation, "set_budget_channel_stamp", write)
    monkeypatch.setattr(session_preparation.log, "warning", warning)
    deps = replace(deps_for(client, factory), turn_path=None)
    return deps, lookup, write, warning


async def resolve(deps: TurnDeps) -> tuple[uuid.UUID, BetaManagedAgentsSession] | None:
    return await session_preparation._resolve_legacy_budget_stamp(
        deps, tenant_id=TENANT, account_id=ACCOUNT, platform="discord", thread_id="thread"
    )


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {MA_METADATA_KEY_BUDGET_CHANNEL: "channel-original"},
        {MA_METADATA_KEY_BUDGET_CHANNEL: None},
        {"daimon_channel": None},
        {"daimon_thread": None},
        {
            MA_METADATA_KEY_BUDGET_CHANNEL: "channel-original",
            "daimon_channel": None,
            "daimon_thread": None,
        },
        {"daimon_tenant": str(TENANT), "daimon_account": str(ACCOUNT)},
    ],
    ids=["empty", "stamped", "null-stamp", "null-channel", "null-thread", "null-both", "owned"],
)
async def test_budget_native_read_preserves_sdk_request_reply_and_tenant_scope(
    monkeypatch: pytest.MonkeyPatch, metadata: dict[str, JsonValue]
) -> None:
    row = mapping()
    response = {"id": SESSION, "metadata": metadata, "future_field": {"items": [None, False, 0]}}
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        transport.queue(ScriptedReply("GET", PATH, httpx.Response(200, json=response)))
    scopes: list[tuple[Scope, ResourceRef]] = []
    original_read = AnthropicSessions.read_native

    async def read_native(
        self: AnthropicSessions, scope: Scope, ref: ResourceRef
    ) -> dict[str, JsonValue]:
        scopes.append((scope, ref))
        return await original_read(self, scope, ref)

    monkeypatch.setattr(AnthropicSessions, "read_native", read_native)
    async with old.client() as legacy, new.client() as client:
        original = await legacy.beta.sessions.retrieve(SESSION)
        original_stamp = original.metadata.get(MA_METADATA_KEY_BUDGET_CHANNEL)
        deps, lookup, write, warning = host(monkeypatch, client, row)
        result = await resolve(deps)
    old.assert_consumed()
    new.assert_consumed()
    assert result is not None and result[0] == row.id
    assert result[1].model_dump(mode="json", exclude_unset=True) == original.model_dump(
        mode="json", exclude_unset=True
    )
    assert result[1].model_fields_set == original.model_fields_set
    lookup.assert_awaited_once_with(
        deps.sessionmaker,
        tenant_id=TENANT,
        account_id=ACCOUNT,
        platform="discord",
        thread_id="thread",
    )
    write.assert_awaited_once_with(deps.sessionmaker, id=row.id, budget_channel_id=original_stamp)
    warning.assert_not_called()
    assert old.requests == new.requests and len(new.requests) == 1
    assert len(scopes) == 1
    scope, ref = scopes[0]
    assert scope.tenant_id == str(TENANT) and scope.account_id == str(ACCOUNT)
    assert not scope.is_platform and not scope.is_legacy_host_authorized
    assert scope.authorization_id == "session_preparation:_resolve_legacy_budget_stamp"
    assert ref.id == SESSION and ref.kind == "session"
    assert ref.tenant_id == scope.tenant_id and ref.account_id == scope.account_id
    assert row.budget_channel_id == "" and row.watermark_message_id == "progress"


@pytest.mark.parametrize("extra", [{}, {"metadata": None}], ids=["absent", "null"])
async def test_budget_native_read_keeps_legacy_missing_metadata_failure(
    monkeypatch: pytest.MonkeyPatch, extra: dict[str, JsonValue]
) -> None:
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        transport.queue(
            ScriptedReply("GET", PATH, httpx.Response(200, json={"id": SESSION, **extra}))
        )
    async with old.client() as legacy, new.client() as client:
        original = await legacy.beta.sessions.retrieve(SESSION)
        with pytest.raises(AttributeError) as before:
            original.metadata.get(MA_METADATA_KEY_BUDGET_CHANNEL)
        deps, _, write, warning = host(monkeypatch, client, mapping())
        with pytest.raises(AttributeError) as after:
            await resolve(deps)
    old.assert_consumed()
    new.assert_consumed()
    assert type(after.value) is type(before.value) and str(after.value) == str(before.value)
    assert old.requests == new.requests and len(new.requests) == 1
    write.assert_not_awaited()
    warning.assert_not_called()


@pytest.mark.parametrize("status", [400, 404, 409, 429, 500])
async def test_budget_native_read_preserves_sdk_failure_retry_marker_and_notice(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        transport.queue(
            ScriptedReply(
                "GET", PATH, httpx.Response(status, json={"error": {"message": "read failed"}})
            )
        )
    row = mapping()
    async with old.client() as legacy, new.client() as client:
        with pytest.raises(anthropic.APIError) as before:
            await legacy.beta.sessions.retrieve(SESSION)
        deps, _, write, warning = host(monkeypatch, client, row)
        assert await resolve(deps) is None
    old.assert_consumed()
    new.assert_consumed()
    assert old.requests == new.requests and len(new.requests) == 1
    write.assert_not_awaited()
    warning.assert_called_once_with(
        "session_preparation.budget_stamp_unreadable",
        session_id=SESSION,
        error=type(before.value).__name__,
    )
    assert row.budget_channel_id == ""


@pytest.mark.parametrize("stamp", [None, "channel-already-known"])
async def test_budget_native_read_skips_an_already_known_mapping(
    monkeypatch: pytest.MonkeyPatch, stamp: str | None
) -> None:
    transport = ScriptedTransport()
    async with transport.client() as client:
        deps, lookup, write, warning = host(monkeypatch, client, mapping(stamp=stamp))
        assert await resolve(deps) is None
    lookup.assert_awaited_once()
    write.assert_not_awaited()
    warning.assert_not_called()
    transport.assert_consumed()
    assert not transport.requests


async def test_budget_native_read_skips_a_missing_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = ScriptedTransport()
    async with transport.client() as client:
        deps, lookup, write, warning = host(monkeypatch, client, None)
        assert await resolve(deps) is None
    lookup.assert_awaited_once()
    write.assert_not_awaited()
    warning.assert_not_called()
    transport.assert_consumed()
    assert not transport.requests


async def test_budget_native_read_refuses_foreign_metadata_before_stamp_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "GET",
            PATH,
            httpx.Response(
                200,
                json={
                    "id": SESSION,
                    "metadata": {
                        "daimon_tenant": "foreign",
                        MA_METADATA_KEY_BUDGET_CHANNEL: "channel-foreign",
                    },
                },
            ),
        )
    )
    async with transport.client() as client:
        deps, _, write, warning = host(monkeypatch, client, mapping())
        with pytest.raises(ScopeViolation):
            await resolve(deps)
    transport.assert_consumed()
    assert len(transport.requests) == 1
    write.assert_not_awaited()
    warning.assert_not_called()


async def test_budget_native_read_refuses_foreign_account_reference_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_ref = session_preparation.resource_ref

    def foreign_ref(*args, **kwargs):
        return original_ref(*args, **kwargs).model_copy(update={"account_id": "foreign"})

    monkeypatch.setattr(session_preparation, "resource_ref", foreign_ref)
    transport = ScriptedTransport()
    async with transport.client() as client:
        deps, _, write, warning = host(monkeypatch, client, mapping())
        with pytest.raises(ScopeViolation):
            await resolve(deps)
    transport.assert_consumed()
    assert not transport.requests
    write.assert_not_awaited()
    warning.assert_not_called()
