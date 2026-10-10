"""Seal migration keeps DB-first publication and exact native request bodies."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import anthropic
import httpx
import pytest
from daimon.core.scope import ResolvedConfig
from daimon.core.session_seal import origin_stamp
from daimon.core.turn import prepare
from daimon.core.turn.admission import Admission, AdmissionGrant
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import SessionBusyError
from daimon.testing.ma import MARouter
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.ma_transport import ScriptedTransport
from mux.errors import ScopeViolation
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

TENANT, ACCOUNT = UUID(int=701), UUID(int=702)
NOW = datetime(2026, 10, 10, tzinfo=UTC)


def _admission() -> Admission:
    return Admission(
        account_id=ACCOUNT,
        agent=ma_agent(id="agent"),
        environment=ma_environment(id="environment"),
        config=ResolvedConfig(agent_name="agent", environment_name="environment"),
        origin_channel_id="channel",
        origin_thread_id="thread",
        origin_seal_ids=frozenset({"channel"}),
    )


def _deps(
    client: anthropic.AsyncAnthropic, trace: list[str], monkeypatch: pytest.MonkeyPatch
) -> TurnDeps:
    @asynccontextmanager
    async def begin() -> AsyncIterator[AsyncSession]:
        trace.append("begin")
        yield cast(AsyncSession, SimpleNamespace())
        trace.append("commit")

    async def publish(
        db: AsyncSession, *, tenant_id: UUID, ma_session_id: str, seals: frozenset[str]
    ):
        assert (
            tenant_id == TENANT and ma_session_id == "session" and seals == frozenset({"channel"})
        )
        trace.append("publish")

    monkeypatch.setattr(prepare, "record_session_seals", publish)
    return cast(
        TurnDeps, SimpleNamespace(anthropic=client, sessionmaker=SimpleNamespace(begin=begin))
    )


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        {"daimon_channel": None, "daimon_thread": None},
        {"daimon_sealed": "narrow", "extra": "keep-wire"},
        {"daimon_sealed": "channel,narrow"},
    ],
)
async def test_metadata_only_update_matches_original_wire_and_publishes_first(
    monkeypatch: pytest.MonkeyPatch, metadata: dict[str, str | None] | None
) -> None:
    results: list[tuple[list[dict[str, object]], list[str], list[bytes]]] = []
    for migrated in (False, True):
        trace: list[str] = []

        def handler(
            request: httpx.Request,
            _match: object,
            *,
            is_migrated: bool = migrated,
            trace_events: list[str] = trace,
        ) -> httpx.Response:
            if is_migrated:
                assert trace_events[:3] == ["begin", "publish", "commit"]
            trace_events.append(request.method)
            return httpx.Response(200, json={"id": "session", "metadata": metadata})

        router = MARouter()
        router.add("GET", r"/v1/sessions/session$", handler)
        router.add("POST", r"/v1/sessions/session$", handler)
        transport = ScriptedTransport(router=router)
        async with transport.client() as client:
            # Use a real SDK response parser on both sides; accepted null/partial
            # native fields cannot become a new generic-binding validation gate.
            if migrated:
                await prepare.stamp_session_seal(
                    _deps(client, trace, monkeypatch),
                    "session",
                    _admission(),
                    tenant_id=TENANT,
                    now=lambda: NOW,
                )
            else:
                current = await client.beta.sessions.retrieve("session")
                from daimon.core.session_seal import seal_ids

                if not {"channel"} <= seal_ids(current.metadata):
                    await client.beta.sessions.update(
                        "session",
                        metadata=cast(
                            dict[str, str | None],
                            origin_stamp(
                                channel_id="channel",
                                thread_id="thread",
                                seal=seal_ids(current.metadata) | {"channel"},
                            ),
                        ),
                    )
        results.append(
            (
                [request.to_dict() for request in transport.requests],
                trace[3:] if migrated else trace,
                [request.body for request in transport.requests],
            )
        )
    assert results[0] == results[1]
    expected = (
        ["GET"]
        if metadata and metadata.get("daimon_sealed") == "channel,narrow"
        else ["GET", "POST"]
    )
    assert results[0][1] == expected


@pytest.mark.parametrize("status", [404, 409, 400, 500])
async def test_native_errors_keep_their_existing_seal_policy(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    trace: list[str] = []
    transport = ScriptedTransport()
    from daimon.testing.ma_transport import ScriptedReply

    transport.queue(
        ScriptedReply(
            "GET",
            "/v1/sessions/session",
            httpx.Response(status, json={"error": {"type": "error", "message": "failure"}}),
        )
    )
    async with transport.client() as client:
        deps = _deps(client, trace, monkeypatch)
        if status == 404:
            await prepare.stamp_session_seal(
                deps, "session", _admission(), tenant_id=TENANT, now=lambda: NOW
            )
        elif status == 409:
            with pytest.raises(SessionBusyError) as error:
                await prepare.stamp_session_seal(
                    deps, "session", _admission(), tenant_id=TENANT, now=lambda: NOW
                )
            assert error.value.retry_after == NOW + timedelta(seconds=5)
        else:
            with pytest.raises(anthropic.APIStatusError) as error:
                await prepare.stamp_session_seal(
                    deps, "session", _admission(), tenant_id=TENANT, now=lambda: NOW
                )
            assert error.value.status_code == status
    assert trace == ["begin", "publish", "commit"]
    transport.assert_consumed()


async def test_wrong_admission_tenant_refuses_before_publish_or_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    admission = replace(
        _admission(), grant=cast(AdmissionGrant, SimpleNamespace(tenant_id=UUID(int=703)))
    )
    trace: list[str] = []
    transport = ScriptedTransport()
    async with transport.client() as client:
        with pytest.raises(ScopeViolation):
            await prepare.stamp_session_seal(
                _deps(client, trace, monkeypatch),
                "session",
                admission,
                tenant_id=TENANT,
                now=lambda: NOW,
            )
    assert trace == [] and transport.requests == []


async def test_native_read_preserves_partial_null_and_unknown_response_fields() -> None:
    from daimon.core.mux_backend import managed_agents, resource_ref, resource_scope
    from daimon.testing.ma_transport import ScriptedReply
    from mux.drivers.anthropic.sessions_lifecycle import SessionReads

    body: dict[str, JsonValue] = {
        "id": "session",
        "metadata": {"daimon_channel": None, "daimon_thread": None},
        "future_field": {"items": [None, "kept"]},
    }
    transport = ScriptedTransport()
    transport.queue(ScriptedReply("GET", "/v1/sessions/session", httpx.Response(200, json=body)))
    scope = resource_scope(tenant_id=str(TENANT), account_id=str(ACCOUNT))
    async with transport.client() as client:
        backend = managed_agents(client, scope=scope, resources=frozenset({("session", "session")}))
        reads = backend.extension(SessionReads, namespace="anthropic.session_reads", version=1)
        native = await reads.read_native(
            scope, resource_ref(backend, "session", "session", scope=scope)
        )
    assert native == body
    assert len(transport.requests) == 1
    transport.assert_consumed()


@pytest.mark.parametrize("foreign", ["reference", "scope", "record"])
async def test_native_read_refuses_foreign_identity(foreign: str) -> None:
    from daimon.core.mux_backend import managed_agents, resource_ref, resource_scope
    from daimon.testing.ma_transport import ScriptedReply
    from mux.drivers.anthropic.sessions_lifecycle import SessionReads

    transport = ScriptedTransport()
    scope = resource_scope(tenant_id=str(TENANT), account_id=str(ACCOUNT))
    if foreign == "record":
        transport.queue(
            ScriptedReply(
                "GET",
                "/v1/sessions/session",
                httpx.Response(
                    200,
                    json={
                        "id": "session",
                        "metadata": {"daimon_tenant": "foreign"},
                    },
                ),
            )
        )
    async with transport.client() as client:
        backend = managed_agents(client, scope=scope, resources=frozenset({("session", "session")}))
        ref = resource_ref(backend, "session", "session", scope=scope)
        reads = backend.extension(SessionReads, namespace="anthropic.session_reads", version=1)
        if foreign == "reference":
            ref = ref.model_copy(update={"account_id": "foreign"})
        elif foreign == "scope":
            scope = scope.model_copy(update={"tenant_id": "foreign"})
        with pytest.raises(ScopeViolation):
            await reads.read_native(scope, ref)
    assert len(transport.requests) == (1 if foreign == "record" else 0)
    transport.assert_consumed()


async def test_running_update_retains_five_second_busy_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.testing.ma_transport import ScriptedReply

    trace: list[str] = []
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply("GET", "/v1/sessions/session", httpx.Response(200, json={"id": "session"})),
        ScriptedReply(
            "POST",
            "/v1/sessions/session",
            httpx.Response(
                400,
                json={
                    "error": {
                        "type": "invalid_request_error",
                        "message": "cannot update while session is running",
                    }
                },
            ),
        ),
    )
    async with transport.client() as client:
        with pytest.raises(SessionBusyError) as error:
            await prepare.stamp_session_seal(
                _deps(client, trace, monkeypatch),
                "session",
                _admission(),
                tenant_id=TENANT,
                now=lambda: NOW,
            )
    assert error.value.pending_reasons == ("seal",)
    assert error.value.retry_after == NOW + timedelta(seconds=5)
    assert isinstance(error.value.__cause__, anthropic.BadRequestError)
    assert trace == ["begin", "publish", "commit"]
    transport.assert_consumed()
