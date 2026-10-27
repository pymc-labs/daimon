from __future__ import annotations

import asyncio
import importlib.util
import json
import time
import uuid
from pathlib import Path
from unittest import mock

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.authz import Subject
from daimon.core.context_provider import (
    DETACHED_CALL_TIMEOUT_SECONDS,
    MAX_DETACHED_CALLS,
    ContextProviders,
    ContextRequest,
    ContextResponse,
    LoadedProvider,
    McpProvider,
    ProviderConfig,
)
from daimon.core.stores import context_providers
from daimon.testing.factories import make_tenant
from sqlalchemy import text

from .context_card_stub import Card, FixtureProvider

FIXTURE = Path(__file__).parent / "fixtures/context_provider/cards.json"
CONFIG = ProviderConfig(endpoint="fixture", enabled=True)
TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
REQUEST = ContextRequest(
    tenant_id=TENANT,
    server_id="fixture-server",
    platform="discord",
    channel_id="staff",
    place_type="channel",
    requester_platform_user_id="alice",
    agent_id="ag",
    session_id="s1",
    message_text="Ask Bob about Orbit's plan",
    mention_ids=("bob",),
)


async def loaded(request: ContextRequest) -> LoadedProvider:
    return LoadedProvider(CONFIG, "fixture-server")


async def test_dedupe_only_after_acceptance_and_reset_with_new_session() -> None:
    provider = FixtureProvider(FIXTURE, tenant_id=str(TENANT))
    owner = ContextProviders(provider=provider)
    async with owner.turn(REQUEST, loaded) as first:
        assert [c.id for c in first.response.cards] == [
            "person:alice",
            "channel:staff",
            "client:acme",
            "person:bob",
            "client:orbit",
        ]
        assert first.response.system_fragment
        # A failed send does not acknowledge.
    async with owner.turn(REQUEST, loaded) as retry:
        assert retry.response == first.response
        retry.accepted()
    async with owner.turn(REQUEST, loaded) as second:
        assert second.response.turn_fragment == second.response.system_fragment == ""
        assert provider.requests[-1].system_fragment_received
    changed = provider.snapshot.cards[0].model_copy(update={"text": "Alice: new role"})
    provider.snapshot.cards[0] = changed
    async with owner.turn(REQUEST, loaded) as updated:
        assert [c.id for c in updated.response.cards] == ["person:alice"]
        updated.accepted()
    async with owner.turn(REQUEST.model_copy(update={"session_id": "s2"}), loaded) as reset:
        assert len(reset.response.cards) == 5
        assert reset.response.system_fragment


async def test_cache_isolation_and_provider_gating_never_reuse_authorization() -> None:
    provider = FixtureProvider(FIXTURE, tenant_id=str(TENANT))
    owner = ContextProviders(provider=provider)
    async with owner.turn(REQUEST, loaded) as one:
        one.accepted()
    other = REQUEST.model_copy(update={"tenant_id": uuid.uuid4()})
    async with owner.turn(other, loaded) as refused:
        assert refused.response.cards == ()
        assert provider.requests[-1].sent_hashes == {}
    for patch in (
        {"channel_id": "client-facing"},
        {"requester_platform_user_id": "outsider"},
        {
            "place_type": "thread",
            "channel_id": "thread",
            "thread_id": "other",
            "parent_channel_id": "staff",
        },
    ):
        async with owner.turn(REQUEST.model_copy(update=patch), loaded) as gated:
            assert not gated.response.system_fragment and not gated.response.turn_fragment
    # Removing current placement permission must take effect despite acknowledged state.
    provider.snapshot.internal_channels = ()
    async with owner.turn(REQUEST, loaded) as revoked:
        assert not revoked.response.cards and not revoked.response.system_fragment


async def test_selection_limits_alias_boundaries_ambiguity_and_whole_card_skip() -> None:
    provider = FixtureProvider(FIXTURE, tenant_id=str(TENANT))
    req = REQUEST.model_copy(
        update={
            "mention_ids": ("alice", "missing", "bob", "bob", "carol", "dave", "eve"),
            "message_text": "orbital Shared abc orbit's Nova Third acme",
        }
    )
    response = await provider(CONFIG, req)
    assert [c.id for c in response.cards] == [
        "person:alice",
        "channel:staff",
        "client:acme",
        "person:bob",
        "person:carol",
        "person:dave",
        "client:orbit",
        "client:nova",
    ]
    # Slugs are not implicitly aliases; duplicate/missing mention candidates use no slots.
    assert "client:third" not in [c.id for c in response.cards]
    provider.snapshot.cards[0].text = "X" * 9000
    smaller = await provider(CONFIG, req.model_copy(update={"max_tokens": 2500}))
    assert "person:alice" not in [c.id for c in smaller.cards]
    assert "person:bob" in [c.id for c in smaller.cards]
    provider.snapshot.internal_channels = ("ambiguous",)
    ambiguous = await provider(
        CONFIG, req.model_copy(update={"channel_id": "ambiguous", "message_text": ""})
    )
    assert not any(c.id.startswith(("channel:", "client:")) for c in ambiguous.cards)
    dm = await provider(CONFIG, req.model_copy(update={"place_type": "dm", "channel_id": None}))
    assert not any(c.id.startswith("channel:") for c in dm.cards)


@pytest.mark.parametrize(
    "kind", ["turn", "system", "bytes", "error", "cancelled", "refused", "repeated"]
)
async def test_bad_responses_fail_open_without_acknowledging(kind: str) -> None:
    calls = 0

    async def provider(config: ProviderConfig, request: ContextRequest) -> ContextResponse:
        nonlocal calls
        calls += 1
        if kind == "error":
            raise RuntimeError("private provider detail")
        if kind == "cancelled":
            raise asyncio.CancelledError
        if kind == "turn":
            return ContextResponse(status="ok", turn_fragment="x" * 9000)
        if kind == "system":
            return ContextResponse(status="ok", system_fragment="x" * 4000)
        if kind == "bytes":
            return ContextResponse(status="ok", provenance={"oversize": "x" * 66000})
        if kind == "refused":
            return ContextResponse(status="refused", system_fragment="must never attach")
        return await FixtureProvider(FIXTURE, tenant_id=str(TENANT))(config, REQUEST)

    owner = ContextProviders(provider=provider)
    async with owner.turn(REQUEST, loaded) as first:
        if kind == "repeated":
            first.accepted()
        else:
            assert not first.response.turn_fragment and not first.response.system_fragment
    async with owner.turn(REQUEST, loaded) as second:
        assert not second.response.turn_fragment and not second.response.system_fragment
    assert calls == 2


def test_old_binding_json_defaults_to_400_ms_first_call() -> None:
    config = ProviderConfig.model_validate_json(
        '{"endpoint":"fixture","enabled":true,"timeout_ms":200}'
    )
    assert config.first_call_timeout_ms == 400
    assert config.timeout_ms == 200


def test_first_call_timeout_above_400_ms_is_rejected() -> None:
    with pytest.raises(ValueError, match="less than or equal to 400"):
        ProviderConfig(endpoint="fixture", first_call_timeout_ms=401)


async def test_300_ms_provider_is_injected_until_system_acknowledged() -> None:
    requests: list[ContextRequest] = []

    async def provider(config: ProviderConfig, request: ContextRequest) -> ContextResponse:
        requests.append(request)
        await asyncio.sleep(0.3)
        return ContextResponse(
            status="ok",
            system_fragment="" if request.system_fragment_received else "system",
            turn_fragment="cards",
        )

    owner = ContextProviders(provider=provider)
    # An unaccepted first response must retain the first-call budget on retry.
    for accept in (False, True):
        async with owner.turn(REQUEST, loaded) as result:
            assert result.response.turn_fragment == "cards"
            assert 300 < requests[-1].deadline_ms <= 400
            assert not requests[-1].system_fragment_received
            if accept:
                result.accepted()
    started = time.monotonic()
    async with owner.turn(REQUEST, loaded) as later:
        assert later.response == ContextResponse()
        assert time.monotonic() - started < 0.3
        assert requests[-1].system_fragment_received
        assert 0 < requests[-1].deadline_ms <= 200
    await asyncio.gather(*owner._detached)


async def test_first_call_budget_includes_config_read() -> None:
    requests: list[ContextRequest] = []

    async def load(request: ContextRequest) -> LoadedProvider:
        await asyncio.sleep(0.1)
        return await loaded(request)

    async def provider(config: ProviderConfig, request: ContextRequest) -> ContextResponse:
        requests.append(request)
        await asyncio.sleep(0.35)
        return ContextResponse(status="ok", system_fragment="late")

    owner = ContextProviders(provider=provider)
    started = time.monotonic()
    async with owner.turn(REQUEST, load) as result:
        assert result.response == ContextResponse()
        assert time.monotonic() - started < 0.5
    assert 0 < requests[0].deadline_ms <= 300
    await asyncio.gather(*owner._detached)
    assert not next(iter(owner.sessions.values())).system_received


async def test_first_call_over_400_ms_finishes_without_acknowledging() -> None:
    release = asyncio.Event()
    finished = asyncio.Event()
    fixture = FixtureProvider(FIXTURE, tenant_id=str(TENANT))

    async def provider(config: ProviderConfig, request: ContextRequest) -> ContextResponse:
        await release.wait()
        finished.set()
        return await fixture(config, request)

    owner = ContextProviders(provider=provider)
    started = time.monotonic()
    async with owner.turn(REQUEST, loaded) as result:
        assert 0.35 < time.monotonic() - started < 0.55
        assert result.response == ContextResponse()
        result.accepted()
    assert len(owner._detached) == 1
    release.set()
    await asyncio.gather(*owner._detached)
    assert finished.is_set()
    state = next(iter(owner.sessions.values()))
    assert not state.system_received and not state.hashes


@pytest.mark.parametrize("lock_wait", [0.1, 0.25])
async def test_concurrent_ack_uses_200_ms_from_hook_start(lock_wait: float) -> None:
    requests: list[ContextRequest] = []
    second_loaded = asyncio.Event()
    fixture = FixtureProvider(FIXTURE, tenant_id=str(TENANT))

    async def provider(config: ProviderConfig, request: ContextRequest) -> ContextResponse:
        requests.append(request)
        if request.system_fragment_received:
            await asyncio.sleep(0.15)
            return ContextResponse(status="ok", turn_fragment="late")
        return await fixture(config, request)

    async def second_load(request: ContextRequest) -> LoadedProvider:
        second_loaded.set()
        return await loaded(request)

    owner = ContextProviders(provider=provider)

    async def second() -> None:
        started = time.monotonic()
        async with owner.turn(REQUEST, second_load) as result:
            assert result.response == ContextResponse()
            assert time.monotonic() - started < 0.35

    async with owner.turn(REQUEST, loaded) as first:
        task = asyncio.create_task(second())
        await second_loaded.wait()
        await asyncio.sleep(lock_wait)
        first.accepted()
    await task
    if lock_wait < 0.2:
        assert len(requests) == 2
        assert requests[-1].system_fragment_received
        assert 0 < requests[-1].deadline_ms <= 110
    else:
        # The fallback deadline already elapsed while waiting; do not call the provider.
        assert len(requests) == 1
    await asyncio.gather(*owner._detached)


@pytest.mark.parametrize(
    ("acknowledged", "first_timeout_ms", "expected_budget_ms"),
    [(True, 400, 200), (False, 50, 50)],
)
async def test_lock_wait_uses_current_acknowledgement_budget(
    acknowledged: bool, first_timeout_ms: int, expected_budget_ms: int
) -> None:
    fixture = FixtureProvider(FIXTURE, tenant_id=str(TENANT))
    owner = ContextProviders(provider=fixture)

    async def load(request: ContextRequest) -> LoadedProvider:
        return LoadedProvider(
            CONFIG.model_copy(update={"first_call_timeout_ms": first_timeout_ms}),
            "fixture-server",
        )

    async with owner.turn(REQUEST, load) as holding:
        assert holding.response.system_fragment
        if acknowledged:
            holding.accepted()
        started = time.monotonic()
        async with owner.turn(REQUEST, load) as blocked:
            elapsed = time.monotonic() - started
            assert blocked.response == ContextResponse()
            assert expected_budget_ms / 1000 * 0.75 <= elapsed < expected_budget_ms / 1000 + 0.1
        assert len(fixture.requests) == 1
        assert not owner._detached


async def test_hard_deadline_includes_cancellation_resistant_provider_and_config() -> None:
    release = asyncio.Event()
    finished = asyncio.Event()

    async def slow(config: ProviderConfig, request: ContextRequest) -> ContextResponse:
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        finally:
            finished.set()
        return ContextResponse(status="ok", turn_fragment="late")

    async def short(request: ContextRequest) -> LoadedProvider:
        return LoadedProvider(
            CONFIG.model_copy(update={"timeout_ms": 20, "first_call_timeout_ms": 20}),
            "fixture-server",
        )

    owner = ContextProviders(provider=slow)
    started = time.monotonic()
    async with owner.turn(REQUEST, short) as result:
        elapsed = time.monotonic() - started
        assert not result.response.turn_fragment
        assert elapsed < 0.15
    release.set()
    await asyncio.wait_for(finished.wait(), 1)
    assert not next(iter(owner.sessions.values())).hashes

    async def slow_load(request: ContextRequest) -> LoadedProvider:
        await release.wait()
        return await loaded(request)

    release.clear()
    started = time.monotonic()
    async with owner.turn(REQUEST, slow_load) as result:
        assert not result.response.turn_fragment
        assert time.monotonic() - started < 0.35
    release.set()
    await asyncio.gather(*owner._detached)


async def test_late_provider_warms_next_turn_without_acknowledging() -> None:
    release = asyncio.Event()
    finished = asyncio.Event()
    cancelled = False
    fixture = FixtureProvider(FIXTURE, tenant_id=str(TENANT))

    async def provider(config, request):
        nonlocal cancelled
        if not finished.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled = True
                raise
            finished.set()
        return await fixture(config, request)

    async def short(request):
        return LoadedProvider(
            CONFIG.model_copy(update={"timeout_ms": 20, "first_call_timeout_ms": 20}),
            "fixture-server",
        )

    owner = ContextProviders(provider=provider)
    started = time.monotonic()
    async with owner.turn(REQUEST, short) as result:
        assert time.monotonic() - started < 0.2
        assert result.response == ContextResponse()
        result.accepted()
    pending = tuple(owner._detached)
    assert len(pending) == 1
    release.set()
    await asyncio.gather(*pending)
    assert finished.is_set() and not cancelled
    state = next(iter(owner.sessions.values()))
    assert not state.hashes and not state.system_received
    assert not owner._detached
    async with owner.turn(REQUEST, short) as warm:
        assert warm.response.cards and warm.response.system_fragment
        assert not fixture.requests[-1].sent_hashes
        assert not fixture.requests[-1].system_fragment_received
        warm.accepted()
    assert state.hashes and state.system_received


async def test_late_completion_during_next_turn_lock_does_not_acknowledge() -> None:
    release = asyncio.Event()
    fixture = FixtureProvider(FIXTURE, tenant_id=str(TENANT))
    calls = 0

    async def provider(config, request):
        nonlocal calls
        calls += 1
        if calls == 1:
            await release.wait()
        return await fixture(config, request)

    async def short(request):
        return LoadedProvider(
            CONFIG.model_copy(update={"timeout_ms": 20, "first_call_timeout_ms": 20}),
            "fixture-server",
        )

    owner = ContextProviders(provider=provider)
    async with owner.turn(REQUEST, short) as late:
        assert late.response == ContextResponse()
    pending = tuple(owner._detached)
    assert len(pending) == 1
    state = next(iter(owner.sessions.values()))
    async with owner.turn(REQUEST, short) as current:
        assert state.lock.locked()
        assert current.response.cards and current.response.system_fragment
        release.set()
        await asyncio.gather(*pending)
        assert state.lock.locked()
        assert not state.hashes and not state.system_received
        late.accepted()
        assert not state.hashes and not state.system_received
        current.accepted()
        assert state.hashes == {card.id: card.hash for card in current.response.cards}
        assert state.system_received
    assert not state.lock.locked() and not owner._detached


async def test_detached_hung_config_read_expires_and_frees_slot(monkeypatch) -> None:
    from daimon.core import context_provider as context_module

    monkeypatch.setattr(context_module, "DETACHED_CALL_TIMEOUT_SECONDS", 0.05)
    cancelled = asyncio.Event()

    async def hung_loader(request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    owner = ContextProviders(provider=lambda *_: None)
    with pytest.raises(TimeoutError):
        await context_module._bounded(hung_loader(REQUEST), 0.01, owner._detached)
    assert len(owner._detached) == 1
    results = await asyncio.wait_for(asyncio.gather(*owner._detached, return_exceptions=True), 1)
    assert len(results) == 1 and isinstance(results[0], TimeoutError)
    assert cancelled.is_set() and not owner._detached


async def test_detached_cap_cancels_extra_late_provider() -> None:
    release = asyncio.Event()
    cancelled = []
    cancellation = asyncio.Event()
    started_calls = []

    async def provider(config, request):
        started_calls.append(request.session_id)
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancellation.set()
            cancelled.append(request.session_id)
            raise
        return ContextResponse()

    async def short(request):
        return LoadedProvider(
            CONFIG.model_copy(update={"timeout_ms": 100, "first_call_timeout_ms": 100}),
            "fixture-server",
        )

    owner = ContextProviders(provider=provider)

    async def turn(index):
        async with owner.turn(REQUEST.model_copy(update={"session_id": str(index)}), short):
            pass

    try:
        for index in range(MAX_DETACHED_CALLS + 1):
            await turn(index)
        await asyncio.wait_for(cancellation.wait(), 1)
        assert len(started_calls) == MAX_DETACHED_CALLS + 1
        assert len(owner._detached) == MAX_DETACHED_CALLS
        assert cancelled == [str(MAX_DETACHED_CALLS)]
    finally:
        release.set()
        await asyncio.gather(*owner._detached)
    assert not owner._detached


@pytest.mark.parametrize("operation", ["provider", "config"])
async def test_parent_cancellation_cancels_own_pending_work(operation) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    owner = ContextProviders(provider=slow)

    async def turn():
        async with owner.turn(REQUEST, slow if operation == "config" else loaded):
            pytest.fail("cancelled turn must not yield input")

    task = asyncio.create_task(turn())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(cancelled.wait(), 1)
    assert not owner._detached
    assert all(not state.lock.locked() for state in owner.sessions.values())


async def test_late_config_read_finishes_and_closes_session_cleanly(db_nullpool_engine) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(db_nullpool_engine)
    finished = asyncio.Event()
    cancelled = False

    async def slow_load(request):
        nonlocal cancelled
        try:
            async with factory() as session:
                await session.execute(text("SELECT pg_sleep(0.3)"))
                assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
            finished.set()
            return await loaded(request)
        except asyncio.CancelledError:
            cancelled = True
            raise

    async def forbidden(config, request):
        pytest.fail("late config must not start a provider call")

    owner = ContextProviders(provider=forbidden)
    started = time.monotonic()
    async with owner.turn(REQUEST, slow_load) as result:
        assert time.monotonic() - started < 0.3
        assert result.response == ContextResponse()
        result.accepted()
    assert len(owner._detached) == 1
    await asyncio.gather(*owner._detached)
    assert finished.is_set() and not cancelled
    assert not owner.sessions and not owner._detached


async def test_provider_http_has_a_wall_clock_cap(monkeypatch) -> None:
    from daimon.core import context_provider as context_module

    cancelled = asyncio.Event()

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                while True:
                    await asyncio.sleep(0.01)
                    yield b" "
            finally:
                cancelled.set()

    def handle(request):
        return httpx.Response(200, stream=SlowStream())

    monkeypatch.setattr(context_module, "DETACHED_CALL_TIMEOUT_SECONDS", 0.05)
    provider = McpProvider(httpx.MockTransport(handle))
    owner = ContextProviders(provider=provider)

    async def short(request):
        return LoadedProvider(
            CONFIG.model_copy(
                update={
                    "endpoint": "https://provider.invalid/mcp",
                    "timeout_ms": 10,
                    "first_call_timeout_ms": 10,
                }
            ),
            "fixture-server",
        )

    try:
        async with owner.turn(REQUEST, short) as result:
            assert result.response == ContextResponse()
        assert len(owner._detached) == 1
        results = await asyncio.wait_for(
            asyncio.gather(*owner._detached, return_exceptions=True), 1
        )
        assert len(results) == 1 and isinstance(results[0], TimeoutError)
        assert cancelled.is_set()
        assert not owner._detached
    finally:
        await provider.aclose()


async def test_late_exception_is_consumed_without_logging_provider_text() -> None:
    release = asyncio.Event()

    async def slow_load(request):
        await release.wait()
        raise ValueError("private provider text")

    owner = ContextProviders(provider=lambda *_: None)
    with mock.patch("daimon.core.context_provider.log.warning") as warning:
        async with owner.turn(REQUEST, slow_load):
            pass
        pending = tuple(owner._detached)
        release.set()
        await asyncio.wait(pending)
        await asyncio.sleep(0)
    warning.assert_called_once_with(
        "context_provider.dropped", tenant_id=str(TENANT), error_class="TimeoutError"
    )
    assert not owner._detached
    assert all(not task._log_traceback for task in pending)


async def test_concurrent_turns_serialize_hash_acceptance() -> None:
    provider = FixtureProvider(FIXTURE, tenant_id=str(TENANT))
    owner = ContextProviders(provider=provider)
    started = asyncio.Event()
    release = asyncio.Event()

    async def first() -> None:
        async with owner.turn(REQUEST, loaded) as result:
            started.set()
            await release.wait()
            result.accepted()

    async def second() -> None:
        async with owner.turn(REQUEST, loaded) as result:
            assert result.response.turn_fragment == ""

    first_task = asyncio.create_task(first())
    await started.wait()
    second_task = asyncio.create_task(second())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first_task, second_task)
    assert len(provider.requests) == 2
    assert provider.requests[-1].sent_hashes


async def test_mcp_pool_keeps_idle_connections_between_turns() -> None:
    with mock.patch("httpcore.AsyncConnectionPool") as pool:
        pool.return_value.aclose = mock.AsyncMock()
        provider = McpProvider()
    assert pool.call_args.kwargs["keepalive_expiry"] == 240
    assert pool.call_args.kwargs["max_connections"] == 100
    assert pool.call_args.kwargs["max_keepalive_connections"] == 20
    await provider.aclose()


@pytest.mark.parametrize("sse", [False, True])
async def test_mcp_wire_contract_with_bearer_and_json_or_sse(sse, monkeypatch) -> None:
    bearer_env = f"DAIMON_CONTEXT_{TENANT.hex.upper()}_TEST_BEARER"
    monkeypatch.setenv(bearer_env, "test-bearer")
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer test-bearer"
        assert request.headers["Accept"] == "application/json, text/event-stream"
        body = json.loads(request.content)
        assert body["method"] == "tools/call"
        assert body["params"]["name"] == "context_cards_for_turn"
        assert body["params"]["arguments"]["tenant_id"] == str(TENANT)
        result = {
            "jsonrpc": "2.0",
            "id": "context",
            "result": {
                "structuredContent": ContextResponse(
                    status="ok", turn_fragment="fixture\n"
                ).model_dump(mode="json")
            },
        }
        if sse:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content="event: message\ndata: " + json.dumps(result) + "\n\n",
            )
        return httpx.Response(200, json=result)

    provider = McpProvider(httpx.MockTransport(handle))
    config = ProviderConfig(
        endpoint="https://provider.invalid/mcp", bearer_env=bearer_env, enabled=True
    )
    assert (await provider(config, REQUEST)).turn_fragment == "fixture\n"
    assert len(seen) == 1
    with pytest.raises(ValueError, match="another scope"):
        await provider(config, REQUEST.model_copy(update={"tenant_id": uuid.uuid4()}))
    assert len(seen) == 1


@pytest.mark.parametrize("kind", ["malformed", "is_error", "oversize", "redirect", "timeout"])
async def test_mcp_failures(kind) -> None:
    seen = []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(66):
                yield b"x" * 1024

    def handle(request):
        seen.append(request)
        if kind == "timeout":
            raise httpx.ReadTimeout("private endpoint", request=request)
        if kind == "redirect":
            return httpx.Response(307, headers={"location": "https://elsewhere.invalid"})
        if kind == "oversize":
            return httpx.Response(200, stream=Chunks())
        if kind == "is_error":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "context",
                    "result": {"isError": True, "structuredContent": {}},
                },
            )
        return httpx.Response(200, content=b"not json")

    with pytest.raises((ValueError, httpx.HTTPError)):
        await McpProvider(httpx.MockTransport(handle))(
            ProviderConfig(endpoint="https://provider.invalid/mcp"), REQUEST
        )
    assert len(seen) == 1


async def test_config_isolation_overrides_and_human_admin_only(db_session) -> None:
    one = await make_tenant(db_session, workspace_id="provider-one")
    two = await make_tenant(db_session, workspace_id="provider-two")
    for actor in (
        Subject(),
        Subject(is_admin=False, platform_user_id="owner"),
        Subject(is_admin=True, via_agent_key=True),
    ):
        with pytest.raises(PermissionError):
            await context_providers.set_provider(
                db_session,
                tenant_id=one.id,
                config=CONFIG,
                actor=actor,
                set_by="denied",
            )
    await context_providers.set_provider(
        db_session,
        tenant_id=one.id,
        config=CONFIG,
        actor=Subject(is_admin=True),
        set_by="admin",
    )
    assert (
        await context_providers.load(
            db_session, tenant_id=two.id, channel_id="staff", agent_id="ag"
        )
        is None
    )
    assert (
        await context_providers.load(
            db_session, tenant_id=one.id, channel_id="staff", agent_id="ag"
        )
        == CONFIG
    )
    for channel, agent, endpoint in (
        ("", "ag", "agent"),
        ("staff", "", "channel"),
        ("staff", "ag", "both"),
    ):
        await context_providers.set_provider(
            db_session,
            tenant_id=one.id,
            channel_id=channel,
            agent_id=agent,
            config=CONFIG.model_copy(update={"endpoint": endpoint}),
            actor=Subject(is_admin=True),
            set_by="admin",
        )
        found = await context_providers.load(
            db_session, tenant_id=one.id, channel_id="staff", agent_id="ag"
        )
        assert found.endpoint == endpoint
    await context_providers.set_provider(
        db_session,
        tenant_id=one.id,
        channel_id="staff",
        agent_id="ag",
        config=CONFIG.model_copy(update={"enabled": False}),
        actor=Subject(is_admin=True),
        set_by="admin",
    )
    assert not (
        await context_providers.load(
            db_session, tenant_id=one.id, channel_id="staff", agent_id="ag"
        )
    ).enabled


async def test_warmup_configs_returns_both_live_tenants_and_excludes_archived(db_session) -> None:
    tenants = [
        await make_tenant(db_session, workspace_id=name)
        for name in ("warm-one", "warm-two", "warm-archived")
    ]
    configs = [
        ProviderConfig(endpoint=f"https://provider-{index}.invalid/mcp", enabled=True)
        for index in range(3)
    ]
    for tenant, config in zip(tenants, configs, strict=True):
        await context_providers.set_provider(
            db_session,
            tenant_id=tenant.id,
            config=config,
            actor=Subject(is_admin=True),
            set_by="admin",
        )
    await db_session.execute(
        text("UPDATE tenants SET archived_at = now() WHERE id = :id"),
        {"id": tenants[2].id},
    )
    loaded = await context_providers.warmup_configs(db_session)
    assert {config.endpoint for config in loaded} == {config.endpoint for config in configs[:2]}


@pytest.mark.fresh_schema
async def test_migration_rls_isolation_and_with_check_for_non_bypass_role(db_engine) -> None:
    # Exercise the actual migration, since fixture metadata creation does not install policies.
    migration_path = (
        Path(__file__).parents[1] / "alembic/versions/0149_context_provider_bindings.py"
    )
    spec = importlib.util.spec_from_file_location("context_binding_migration", migration_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    role = "context_probe_" + uuid.uuid4().hex
    tenant_one, tenant_two = uuid.uuid4(), uuid.uuid4()
    async with db_engine.begin() as connection:
        await connection.execute(text("DROP TABLE context_provider_bindings"))

        def migrate(sync):
            with Operations.context(MigrationContext.configure(sync)):
                module.upgrade()

        await connection.run_sync(migrate)
        await connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN NOBYPASSRLS'))
        schema = await connection.scalar(text("SELECT current_schema()"))
        await connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"'))
        await connection.execute(
            text(f'GRANT SELECT, INSERT, UPDATE ON context_provider_bindings TO "{role}"')
        )
        for tenant in (tenant_one, tenant_two):
            await connection.execute(
                text(
                    "INSERT INTO tenants (id, platform, external_id) VALUES (:id, 'discord', :ext)"
                ),
                {"id": tenant, "ext": str(tenant)},
            )
            await connection.execute(
                text(
                    "INSERT INTO context_provider_bindings (tenant_id, config, updated_by) VALUES (:id, '{}'::jsonb, 'test')"
                ),
                {"id": tenant},
            )
    try:
        async with db_engine.begin() as connection:
            await connection.execute(text(f'SET LOCAL ROLE "{role}"'))
            assert (
                await connection.scalar(text("SELECT count(*) FROM context_provider_bindings")) == 0
            )
            await connection.execute(
                text("SELECT set_config('daimon.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_one)},
            )
            rows = (
                (await connection.execute(text("SELECT tenant_id FROM context_provider_bindings")))
                .scalars()
                .all()
            )
            assert rows == [tenant_one]
            with pytest.raises(Exception, match="row-level security"):
                async with connection.begin_nested():
                    await connection.execute(
                        text("UPDATE context_provider_bindings SET tenant_id=:other"),
                        {"other": tenant_two},
                    )
            with pytest.raises(Exception, match="row-level security"):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            "INSERT INTO context_provider_bindings (tenant_id, channel_id, config, updated_by) VALUES (:other, 'foreign', '{}'::jsonb, 'probe')"
                        ),
                        {"other": tenant_two},
                    )
        async with db_engine.begin() as connection:
            await connection.execute(text(f'SET LOCAL ROLE "{role}"'))
            assert (
                await connection.scalar(text("SELECT count(*) FROM context_provider_bindings")) == 0
            )
    finally:
        async with db_engine.begin() as connection:
            await connection.execute(text(f'DROP OWNED BY "{role}"'))
            await connection.execute(text(f'DROP ROLE "{role}"'))


def test_card_hash_changes_only_with_rendered_content() -> None:
    card = Card(kind="person", key="a", source="people/a.md", as_of="2026-10-01", text="A & B")
    assert "A &amp; B" in card.render()
    assert card.render() != card.model_copy(update={"as_of": "2026-10-02"}).render()


async def test_late_initial_load_retries_system_until_accepted() -> None:
    fixture = FixtureProvider(FIXTURE, tenant_id=str(TENANT))
    calls = 0

    async def provider(config: ProviderConfig, request: ContextRequest) -> ContextResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ContextResponse(status="empty")
        return await fixture(config, request)

    owner = ContextProviders(provider=provider)
    async with owner.turn(REQUEST, loaded) as unloaded:
        unloaded.accepted()
        assert not unloaded.response.system_fragment
    async with owner.turn(REQUEST, loaded) as now_loaded:
        assert now_loaded.response.system_fragment
        now_loaded.accepted()
    async with owner.turn(REQUEST, loaded) as after_ack:
        assert not after_ack.response.system_fragment
        assert not after_ack.response.turn_fragment


async def test_no_binding_never_calls_provider() -> None:
    async def absent(request):
        return None

    async def forbidden(config, request):
        pytest.fail("unbound turn called provider")

    owner = ContextProviders(provider=forbidden)
    async with owner.turn(REQUEST, absent) as result:
        assert result.response == ContextResponse()
    assert not owner.sessions


async def test_failure_logs_only_tenant_and_exception_class() -> None:
    from structlog.testing import capture_logs

    async def fail(config, request):
        raise RuntimeError("SECRET card-id source credential text")

    with capture_logs() as logs:
        async with ContextProviders(provider=fail).turn(REQUEST, loaded) as result:
            assert result.response == ContextResponse()
    assert logs == [
        {
            "tenant_id": str(TENANT),
            "error_class": "RuntimeError",
            "event": "context_provider.dropped",
            "log_level": "warning",
        }
    ]


async def test_lru_idle_expiry_and_hash_cap(monkeypatch) -> None:
    provider = FixtureProvider(FIXTURE, tenant_id=str(TENANT))
    owner = ContextProviders(provider=provider, max_sessions=1)
    async with owner.turn(REQUEST, loaded) as result:
        result.accepted()
    old = next(iter(owner.sessions.values()))
    old.touched -= 86401
    async with owner.turn(REQUEST, loaded) as result:
        assert result.response.system_fragment
        result.accepted()
    assert next(iter(owner.sessions.values())) is not old
    async with owner.turn(REQUEST.model_copy(update={"session_id": "s2"}), loaded):
        pass
    assert len(owner.sessions) == 1
    state = next(iter(owner.sessions.values()))
    state.hashes = {str(i): "a" * 64 for i in range(512)}
    async with owner.turn(REQUEST.model_copy(update={"session_id": "s2"}), loaded) as result:
        assert result.response == ContextResponse()
    assert len(state.hashes) == 512


def test_v1_fields_and_config_limits() -> None:
    assert set(ContextRequest.model_fields) == {
        "version",
        "tenant_id",
        "server_id",
        "platform",
        "channel_id",
        "parent_channel_id",
        "thread_id",
        "place_type",
        "requester_platform_user_id",
        "agent_id",
        "caller_identity",
        "trust_placement",
        "message_text",
        "mention_ids",
        "session_id",
        "system_fragment_received",
        "sent_hashes",
        "deadline_ms",
        "max_tokens",
        "system_max_tokens",
    }
    assert set(ContextResponse.model_fields) == {
        "system_fragment",
        "turn_fragment",
        "cards",
        "estimated_tokens",
        "token_method",
        "provenance",
        "cache_hint",
        "status",
    }
    assert CONFIG.system_max_tokens == 1000
    for update in (
        {"max_tokens": 2501},
        {"system_max_tokens": 2001},
        {"timeout_ms": 201},
        {"transport": "http"},
        {"transport": "stub"},
        {"bearer_key": "secret"},
    ):
        with pytest.raises(ValueError):
            ProviderConfig.model_validate(CONFIG.model_dump() | update)


async def test_lock_wait_is_bounded_and_never_calls_provider_twice() -> None:
    fixture = FixtureProvider(FIXTURE, tenant_id=str(TENANT))
    owner = ContextProviders(provider=fixture)

    async def short(request):
        return LoadedProvider(
            CONFIG.model_copy(update={"timeout_ms": 10, "first_call_timeout_ms": 10}),
            "fixture-server",
        )

    async with owner.turn(REQUEST, short) as first:
        async with owner.turn(REQUEST, short) as blocked:
            assert blocked.response == ContextResponse()
        first.accepted()
    assert len(fixture.requests) == 1
    assert not next(iter(owner.sessions.values())).lock.locked()


async def test_mcp_reuses_one_client_and_passes_independent_http_timeout(monkeypatch) -> None:
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "context",
                "result": {"structuredContent": {"status": "empty"}},
            },
        )

    provider = McpProvider(httpx.MockTransport(handle))
    client = provider.client

    def forbidden_constructor(*args, **kwargs):
        pytest.fail("a provider turn constructed a client")

    from daimon.core import context_provider as context_module

    monkeypatch.setattr(context_module, "AsyncClient", forbidden_constructor)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden_constructor)
    try:
        for timeout_ms in (20, 80):
            await provider(
                CONFIG.model_copy(
                    update={"endpoint": "https://provider.invalid/mcp", "timeout_ms": timeout_ms}
                ),
                REQUEST,
            )
            assert provider.client is client
            assert requests[-1].extensions["timeout"]["read"] == DETACHED_CALL_TIMEOUT_SECONDS
        assert len(requests) == 2
        assert not client.follow_redirects
    finally:
        await provider.aclose()
    assert client.is_closed


def test_unknown_place_is_not_a_wire_request() -> None:
    with pytest.raises(ValueError):
        ContextRequest.model_validate(REQUEST.model_dump() | {"place_type": "unknown"})


async def test_context_client_constructor_is_independent_of_other_transport_overrides(
    monkeypatch,
) -> None:
    constructor = httpx.AsyncClient

    def other_transport_client(*, verify, proxy, timeout, trust_env):
        pytest.fail("context startup used another transport owner's client factory")

    monkeypatch.setattr(httpx, "AsyncClient", other_transport_client)
    provider = McpProvider(httpx.MockTransport(lambda request: httpx.Response(200)))
    try:
        assert isinstance(provider.client, constructor)
    finally:
        await provider.aclose()


async def test_mcp_never_shares_response_cookies_or_bearers_between_tenants(monkeypatch) -> None:
    other = uuid.uuid4()
    one_env = f"DAIMON_CONTEXT_{TENANT.hex.upper()}_BRIDGE"
    two_env = f"DAIMON_CONTEXT_{other.hex.upper()}_BRIDGE"
    monkeypatch.setenv(one_env, "tenant-one-bearer")
    monkeypatch.setenv(two_env, "tenant-two-bearer")
    seen = []

    def handle(request):
        seen.append(request)
        assert "cookie" not in request.headers
        return httpx.Response(
            200,
            headers={"set-cookie": "session=tenant-one; Path=/; Secure; HttpOnly"},
            json={
                "jsonrpc": "2.0",
                "id": "context",
                "result": {"structuredContent": {"status": "empty"}},
            },
        )

    provider = McpProvider(httpx.MockTransport(handle))
    try:
        for tenant, bearer_env in ((TENANT, one_env), (other, two_env), (TENANT, None)):
            await provider(
                ProviderConfig(endpoint="https://provider.invalid/mcp", bearer_env=bearer_env),
                REQUEST.model_copy(update={"tenant_id": tenant}),
            )
        assert [r.headers.get("authorization") for r in seen] == [
            "Bearer tenant-one-bearer",
            "Bearer tenant-two-bearer",
            None,
        ]
        assert not provider.client.cookies
        assert "authorization" not in provider.client.headers
        assert "cookie" not in provider.client.headers
    finally:
        await provider.aclose()


async def test_multiple_provider_owners_load_tls_roots_once(monkeypatch) -> None:
    import ssl

    from daimon.core import context_provider as context_module

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    calls = 0

    def create_context(*, trust_env):
        nonlocal calls
        assert trust_env is False
        calls += 1
        return context

    context_module._ssl_context.cache_clear()
    monkeypatch.setattr(httpx, "create_ssl_context", create_context)
    providers = []
    try:
        providers.extend([McpProvider(), McpProvider()])
        assert calls == 1
        assert providers[0].client is not providers[1].client
    finally:
        for provider in providers:
            await provider.aclose()
        context_module._ssl_context.cache_clear()


def test_context_place_type_is_required() -> None:
    payload = REQUEST.model_dump()
    payload.pop("place_type")
    with pytest.raises(ValueError):
        ContextRequest.model_validate(payload)


class WarmClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.ticks: asyncio.Queue[None] = asyncio.Queue()
        self.waiting: asyncio.Queue[None] = asyncio.Queue()

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        assert seconds == 20
        self.waiting.put_nowait(None)
        await self.ticks.get()

    async def ready(self) -> None:
        await asyncio.wait_for(self.waiting.get(), 1)

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.ticks.put_nowait(None)

    async def tick(self, count: int = 1) -> None:
        for _ in range(count):
            self.advance(20)
            await self.ready()


@pytest.fixture
def warm_clock(monkeypatch):
    from daimon.core import context_provider as module

    clock = WarmClock()
    monkeypatch.setattr(module, "_keepwarm_now", clock.monotonic)
    monkeypatch.setattr(module, "_keepwarm_sleep", clock.sleep)
    return clock


async def test_keepwarm_preconnect_refresh_idle_and_same_pool(warm_clock, monkeypatch) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "context",
                    "result": {"structuredContent": {"status": "empty"}},
                },
            )
        return httpx.Response(503, content=b"unavailable", headers={"set-cookie": "secret=bad"})

    provider = McpProvider(httpx.MockTransport(handle))
    original_client = provider.client
    config = ProviderConfig(
        endpoint="https://provider.invalid:8443/mcp?secret=hidden",
        enabled=True,
        bearer_env="DAIMON_CONTEXT_00000000000000000000000000000001_KEY",
    )
    configs = [
        config,
        config,
        ProviderConfig(endpoint="http://skip.invalid", enabled=True),
        ProviderConfig(endpoint="https://disabled.invalid", enabled=False),
    ]
    loads = 0

    async def load():
        nonlocal loads
        loads += 1
        return list(configs)

    owner = ContextProviders(provider=provider)
    with mock.patch.dict("os.environ", {}, clear=True):
        owner.start(load)
        owner.start(load)  # Exactly one task per owner.
        await warm_clock.ready()
    assert [(r.method, str(r.url)) for r in requests] == [
        ("GET", "https://provider.invalid:8443/health")
    ]
    assert provider.client is original_client
    assert "authorization" not in requests[0].headers
    assert "cookie" not in requests[0].headers
    assert requests[0].content == b""
    monkeypatch.setenv(config.bearer_env, "test-key")
    await warm_clock.tick(9)
    assert len(requests) == 1
    assert loads == 1  # Idle ticks do not re-read the database.
    await provider(config, REQUEST)
    assert requests[-1].headers["authorization"] == "Bearer test-key"
    assert "cookie" not in requests[-1].headers
    await warm_clock.tick(9)
    assert len(requests) == 2  # Recent turn suppresses the ping.
    assert loads == 2
    await warm_clock.tick()
    assert requests[-1].method == "GET"
    assert "authorization" not in requests[-1].headers
    configs[:] = [ProviderConfig(endpoint="https://new.invalid/other", enabled=True)]
    await warm_clock.tick()
    assert str(requests[-1].url) == "https://new.invalid/health"
    configs.clear()
    count = len(requests)
    await warm_clock.tick(10)
    assert loads == 4
    assert len(requests) == count
    await owner.aclose()
    assert provider._keepwarm_task.done()
    assert provider.client.is_closed


async def test_keepwarm_failures_logged_and_retried(warm_clock) -> None:
    attempts = 0
    loads = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("private server details")

    async def load():
        nonlocal loads
        loads += 1
        if loads == 1:
            raise RuntimeError("private database details")
        return [ProviderConfig(endpoint="https://provider.invalid/mcp", enabled=True)]

    provider = McpProvider(httpx.MockTransport(handle))
    with mock.patch("daimon.core.context_provider.log") as logger:
        provider.start(load)
        await warm_clock.ready()
        warm_clock.advance(20)
        await warm_clock.ready()
        await warm_clock.tick(10)
        assert attempts == 2
        assert logger.warning.call_count == 3
        assert "private" not in repr(logger.mock_calls)
    await provider.aclose()


async def test_keepwarm_close_cancels_inflight_ping() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handle(request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return httpx.Response(200)

    async def load():
        return [ProviderConfig(endpoint="https://provider.invalid/mcp", enabled=True)]

    provider = McpProvider(httpx.MockTransport(handle))
    provider.start(load)
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.wait_for(provider.aclose(), 1)
    assert cancelled.is_set()
    assert provider.client.is_closed


@pytest.mark.parametrize("phase", ["load", "ping"])
async def test_keepwarm_timeout_is_bounded_and_swallowed(monkeypatch, warm_clock, phase) -> None:
    from daimon.core import context_provider as module

    monkeypatch.setattr(module, "KEEPWARM_TIMEOUT_SECONDS", 0.01)

    async def handle(request):
        await asyncio.Event().wait()

    async def load():
        if phase == "load":
            await asyncio.Event().wait()
        return [ProviderConfig(endpoint="https://provider.invalid/mcp", enabled=True)]

    provider = McpProvider(httpx.MockTransport(handle))
    with mock.patch.object(module, "log") as logger:
        provider.start(load)
        await warm_clock.ready()
        logger.warning.assert_called_once_with(
            "context_provider.keepwarm_failed", error_class="TimeoutError"
        )
    await provider.aclose()


async def test_provider_trace_logs_one_safe_timing_record(monkeypatch) -> None:
    from types import SimpleNamespace

    from daimon.core import context_provider as module

    monotonic = mock.Mock(side_effect=[0, 0, 0.002, 0.002, 0.009, 0.009, 0.020, 0.020])
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=monotonic))
    monkeypatch.setattr(module, "_keepwarm_now", monotonic)

    async def handle(request):
        trace = request.extensions["trace"]
        for phase in [
            "connection.connect_tcp",
            "connection.start_tls",
            "http11.receive_response_headers",
        ]:
            await trace(phase + ".started", {"headers": "private bearer"})
            await trace(phase + ".complete", {"return_value": "private text"})
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "context",
                "result": {"structuredContent": {"status": "empty"}},
            },
        )

    provider = McpProvider(httpx.MockTransport(handle))
    with mock.patch.object(module, "log") as logger:
        await provider(
            ProviderConfig(endpoint="https://provider.invalid/mcp?private=secret"), REQUEST
        )
        logger.info.assert_called_once()
        args, fields = logger.info.call_args
        assert args == ("context_provider.timing",)
        assert fields["new_connection"] is True
        assert fields["connect_ms"] == 2
        assert fields["tls_ms"] == 7
        assert fields["headers_ms"] == 11
        assert fields["dns_ms"] is None
        assert fields["connect_includes_dns"] is True
        assert "private" not in repr(logger.mock_calls)
    await provider.aclose()


async def test_runtime_start_loads_bindings_in_owned_transaction(warm_clock) -> None:
    from types import SimpleNamespace

    from daimon.core.turn.deps import TurnDeps

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(204)

    session = object()
    transaction = mock.MagicMock()
    transaction.__aenter__ = mock.AsyncMock(return_value=session)
    transaction.__aexit__ = mock.AsyncMock()
    sessionmaker = mock.Mock()
    sessionmaker.begin.return_value = transaction
    owner = ContextProviders(provider=McpProvider(httpx.MockTransport(handle)))
    deps = SimpleNamespace(sessionmaker=sessionmaker, context_providers=owner)
    with mock.patch.object(
        context_providers, "warmup_configs", new_callable=mock.AsyncMock
    ) as load:
        load.return_value = [ProviderConfig(endpoint="https://provider.invalid/mcp", enabled=True)]
        TurnDeps.start_context_providers(deps)
        # Synchronous startup only schedules work.
        load.assert_not_awaited()
        await warm_clock.ready()
        load.assert_awaited_once_with(session)
        transaction.__aexit__.assert_awaited_once()
        assert str(requests[0].url) == "https://provider.invalid/health"
    await owner.aclose()


@pytest.mark.parametrize("fails", [False, True])
async def test_provider_trace_reused_connection_or_failure_logs_once(fails) -> None:
    from daimon.core import context_provider as module

    def handle(request):
        if fails:
            raise httpx.ConnectError("secret error")
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "context",
                "result": {"structuredContent": {"status": "empty"}},
            },
        )

    provider = McpProvider(httpx.MockTransport(handle))
    config = ProviderConfig(endpoint="https://provider.invalid/mcp")
    with mock.patch.object(module, "log") as logger:
        if fails:
            with pytest.raises(httpx.ConnectError):
                await provider(config, REQUEST)
        else:
            await provider(config, REQUEST)
        logger.info.assert_called_once()
        assert logger.info.call_args.kwargs["new_connection"] is False
        assert logger.info.call_args.kwargs["connect_ms"] == 0
        assert logger.info.call_args.kwargs["tls_ms"] == 0
        assert "secret" not in repr(logger.mock_calls)
    await provider.aclose()


@pytest.mark.parametrize("status", [302, 500])
async def test_keepwarm_drains_health_body_without_following_redirects(warm_clock, status) -> None:
    consumed = []
    closed = asyncio.Event()
    requests = []

    class HealthBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in (b"one", b"two"):
                consumed.append(chunk)
                yield chunk

        async def aclose(self):
            closed.set()

    def handle(request):
        requests.append(request)
        return httpx.Response(
            status, headers={"location": "https://other.invalid"}, stream=HealthBody()
        )

    async def load():
        return [ProviderConfig(endpoint="https://provider.invalid/mcp", enabled=True)]

    provider = McpProvider(httpx.MockTransport(handle))
    provider.start(load)
    await warm_clock.ready()
    assert consumed == [b"one", b"two"]
    assert closed.is_set()
    assert len(requests) == 1
    assert str(requests[0].url) == "https://provider.invalid/health"
    await provider.aclose()


async def test_invalid_provider_endpoint_still_logs_one_timing_record() -> None:
    from daimon.core import context_provider as module

    provider = McpProvider(httpx.MockTransport(lambda _: httpx.Response(200)))
    with mock.patch.object(module, "log") as logger:
        with pytest.raises(httpx.InvalidURL):
            await provider(ProviderConfig(endpoint="https://provider.invalid:invalid/mcp"), REQUEST)
        logger.info.assert_called_once()
        assert logger.info.call_args.args == ("context_provider.timing",)
    await provider.aclose()


async def test_keepwarm_bad_rows_and_endpoints_do_not_block_good_origin(warm_clock) -> None:
    from daimon.core import context_provider as module

    good = ProviderConfig(endpoint="https://good.invalid/mcp", enabled=True)
    bad_endpoint = good.model_copy(update={"endpoint": "https://bad.invalid:abc/mcp"})
    session = mock.AsyncMock()
    session.scalars.side_effect = [
        [TENANT],
        [{"enabled": True, "endpoint": 123}, bad_endpoint.model_dump(), good.model_dump()],
    ]
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(204)

    async def load():
        return await context_providers.warmup_configs(session)

    provider = McpProvider(httpx.MockTransport(handle))
    logger = mock.Mock()
    with (
        mock.patch.object(module, "log", logger),
        mock.patch.object(context_providers.structlog, "get_logger", return_value=logger),
    ):
        provider.start(load)
        await warm_clock.ready()
        assert [str(request.url) for request in requests] == ["https://good.invalid/health"]
        assert logger.warning.call_args_list == [
            mock.call("context_provider.keepwarm_skipped", error_class="ValidationError"),
            mock.call("context_provider.keepwarm_skipped", error_class="InvalidURL"),
        ]
    await provider.aclose()


async def test_keepwarm_empty_successful_load_is_cached(warm_clock) -> None:
    load = mock.AsyncMock(return_value=[])
    provider = McpProvider(httpx.MockTransport(lambda _: pytest.fail("no origins to ping")))
    provider.start(load)
    await warm_clock.ready()
    await warm_clock.tick(9)
    assert load.await_count == 1
    await warm_clock.tick()
    assert load.await_count == 2
    await provider.aclose()


async def test_keepwarm_caps_health_body_and_closes_stream(warm_clock) -> None:
    consumed = 0
    closed = asyncio.Event()

    class LargeBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal consumed
            for _ in range(10000):
                consumed += 1024
                yield b"x" * 1024

        async def aclose(self):
            closed.set()

    async def load():
        return [ProviderConfig(endpoint="https://provider.invalid/mcp", enabled=True)]

    provider = McpProvider(httpx.MockTransport(lambda _: httpx.Response(200, stream=LargeBody())))
    provider.start(load)
    await warm_clock.ready()
    assert consumed == 4096
    assert closed.is_set()
    await provider.aclose()
