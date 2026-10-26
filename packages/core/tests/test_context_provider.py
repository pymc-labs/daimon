from __future__ import annotations

import asyncio
import importlib.util
import json
import time
import uuid
from pathlib import Path

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.authz import Subject
from daimon.core.context_provider import (
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
        return LoadedProvider(CONFIG.model_copy(update={"timeout_ms": 20}), "fixture-server")

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
        await asyncio.Event().wait()
        return await loaded(request)

    started = time.monotonic()
    async with owner.turn(REQUEST, slow_load) as result:
        assert not result.response.turn_fragment
        assert time.monotonic() - started < 0.35


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
        return LoadedProvider(CONFIG.model_copy(update={"timeout_ms": 10}), "fixture-server")

    async with owner.turn(REQUEST, short) as first:
        async with owner.turn(REQUEST, short) as blocked:
            assert blocked.response == ContextResponse()
        first.accepted()
    assert len(fixture.requests) == 1
    assert not next(iter(owner.sessions.values())).lock.locked()


async def test_mcp_reuses_one_client_and_passes_each_call_timeout(monkeypatch) -> None:
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
            assert requests[-1].extensions["timeout"]["read"] == timeout_ms / 1000
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
