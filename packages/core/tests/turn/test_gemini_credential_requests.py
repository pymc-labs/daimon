"""Actual native SDK/catalog/origin and Discord serialization proofs for A3."""

from __future__ import annotations

import os
import uuid
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterator,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, cast

import discord
import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools import native_credential_requests as native_mcp
from daimon.adapters.mcp.tools.discord._posted_card import build_card_view
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.github_connect_cards import ConnectCard, discord_embed_payload
from daimon.core.neutral_resources import NeutralResourceHost
from daimon.core.posted_controls import PostedCard
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.stores.neutral_catalog import AgentRevision, CatalogConflict
from daimon.core.stores.turn_origins import create_origin, delete_origin
from daimon.core.turn.gemini_credential_requests import (
    GeminiNativeCredentialAdapter,
    NativeCredentialOrigin,
    NativeCredentialRequest,
    NativeOfferReceipt,
)
from daimon.testing.db import build_test_engine, require_owned_sessionmaker
from daimon.testing.discord_surface import DiscordSurfaceHarness
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import TextContent
from mux.contracts.config import ConfigRevision, ResolvedBackend
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import AgentSpec, ProviderBinding, ToolSpec
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.catalog_transport import SDKAgentCatalogTransport
from mux.drivers.gemini.transport import Object
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
from mux.state.memory import MemoryStateStore
from mux.state.store import binding_slot
from mux.testing.drivers.gemini.fake import FakeTransport, MemoryStorage
from mux.testing.isolated_sdk import build_async_openai, build_gemini_client
from pydantic import TypeAdapter
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tests.turn import test_openai_credential_requests as openai_tests
from tests.turn.test_openai_credential_requests import Case as OpenAICase

if TYPE_CHECKING:
    from google.genai import Client


TLS_NAMES = frozenset(
    {
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "SSLKEYLOGFILE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    }
)


def replay_client(transport: httpx.MockTransport) -> Client:
    if any(
        name.upper() in TLS_NAMES
        or name.upper().startswith(("ANTHROPIC_", "OPENAI_", "GOOGLE_", "GEMINI_"))
        for name in os.environ
    ):
        raise ValueError("A3 replay refuses ambient SDK configuration")
    return build_gemini_client(transport=transport)


NOW = datetime(2026, 10, 11, tzinfo=UTC)


_OBJECT: TypeAdapter[Object] = TypeAdapter(Object)


class NativeWire:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.agents: dict[str, Object] = {}
        self.get_changes: Object = {}

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.method == "POST" and req.url.path == "/v1beta/agents":
            body = _OBJECT.validate_json(req.content, strict=True)
            id_ = body["id"]
            if not isinstance(id_, str):
                raise ValueError("missing native identity")
            self.agents[id_] = body
            return httpx.Response(200, json={"id": id_, "tools": None})
        if req.method == "GET" and req.url.path.startswith("/v1beta/agents/"):
            return httpx.Response(
                200,
                json={
                    **self.agents[req.url.path.rsplit("/", 1)[-1]],
                    **self.get_changes,
                },
            )
        raise AssertionError("No other native operation belongs in an A3 offer")


class NativeDelivery:
    """Owned durable file registry, real shared card renderer and Discord wire."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.surface = DiscordSurfaceHarness(
            evidence_id="native-credential",
            session_id="host-session-scoped",
            root_turn_id="root-offer",
            clock=lambda: 0.0,
            model_id="gemini-3.8-flash",
        )
        self.registered: list[NativeCredentialRequest] = []
        self.on_register: Callable[[], Awaitable[None]] | None = None
        self.bad_receipt = False

    async def register(self, request: NativeCredentialRequest) -> None:
        # Independent registry receipt; neither legacy request store is touched.
        (self.path / request.ref.id).write_bytes(
            TypeAdapter(NativeCredentialRequest).dump_json(request)
        )
        self.registered.append(request)
        if self.on_register is not None:
            await self.on_register()

    async def post(
        self, request: NativeCredentialRequest, card: ConnectCard | PostedCard
    ) -> NativeOfferReceipt:
        stored = TypeAdapter(NativeCredentialRequest).validate_json(
            (self.path / request.ref.id).read_bytes()
        )
        assert stored == request and request in self.registered
        if isinstance(card, ConnectCard):
            view = discord.ui.View(timeout=None)
            view.add_item(
                discord.ui.Button(
                    label=card.title,
                    custom_id=request.custom_id,
                    style=discord.ButtonStyle.primary,
                )
            )
            message = await self.surface.thread.send(
                embed=discord.Embed.from_dict(discord_embed_payload(card)),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            message = await self.surface.thread.send(
                view=build_card_view(card),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return NativeOfferReceipt(
            request.ref,
            request.thread.model_copy(update={"thread_id": "foreign"})
            if self.bad_receipt
            else request.thread,
            str(message.id),
        )


@dataclass
class GeminiCase:
    adapter: GeminiNativeCredentialAdapter
    origin: NativeCredentialOrigin
    wire: NativeWire
    delivery: NativeDelivery
    sessions: async_sessionmaker[AsyncSession]


@pytest.fixture
def owned_engine(request: pytest.FixtureRequest) -> AsyncEngine:
    # Guard all binds before asking the DB fixture to allocate/connect.
    dsn = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if dsn is None:
        pytest.skip("native A3 requires an explicitly owned or isolated-CI database")
    schema = cast(str, request.getfixturevalue("db_schema"))
    guard = build_test_engine(dsn, schema)
    try:
        require_owned_sessionmaker(async_sessionmaker(guard))
    except ValueError:
        pytest.skip("native A3 requires an owned or isolated-CI database")
    finally:
        guard.sync_engine.dispose()
    engine = cast(AsyncEngine, request.getfixturevalue("db_engine"))
    require_owned_sessionmaker(async_sessionmaker(engine))
    return engine


@pytest.fixture
def owned_clean(request: pytest.FixtureRequest, owned_engine: AsyncEngine) -> None:
    # Resolve cleanup only after the owned bind guard. A direct db_clean
    # dependency would allocate its session-scoped engine ahead of this guard.
    request.getfixturevalue("db_clean")


@pytest.fixture
async def gemini_case(
    owned_engine: AsyncEngine, owned_clean: None, tmp_path: Path
) -> AsyncIterator[GeminiCase]:
    sessions = async_sessionmaker(owned_engine, expire_on_commit=False)
    async with sessions.begin() as db:
        tenant = await make_tenant(db, workspace_id="100")
        actor = await make_account(db, tenant=tenant)
        resource = await make_account(db, tenant=tenant)
        await make_platform_principal(
            db, tenant=tenant, account=actor, platform="discord", external_id="103"
        )
        await set_role(db, actor.id, Role.ADMIN)
        place = await create_origin(
            db,
            tenant_id=tenant.id,
            account_id=actor.id,
            platform="discord",
            parent_channel_id="101",
            thread_id="102",
            responder_ma_agent_id="not-a-native-authority",
            responder_name="Legacy field never used",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.ADMIN,
            sender_foreign=False,
            shared_channel=False,
            now=NOW,
            expires_at=NOW + timedelta(hours=2),
        )
    scope = Scope(
        tenant_id=str(tenant.id),
        account_id=str(resource.id),
        principal_id=f"mcp:{actor.id}",
        authorization_id="native-execution",
    )
    source = AgentSpec(
        name="daimon",
        model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
        tools=(ToolSpec(name="code_execution", kind="builtin"),),
        skills=(),
        metadata={MA_METADATA_KEY_NAME: "daimon"},
        system="Help.",
    )
    wire = NativeWire()
    sdk = replay_client(httpx.MockTransport(wire.handle))
    interactions = FakeTransport()
    try:
        driver = GeminiManagedAgents(
            interactions,
            storage=MemoryStorage(),
            state_store=MemoryStateStore(),
            account_scope_id="gemini-workspace",
            resource_catalog_enabled=True,
            resource_catalog_transport=SDKAgentCatalogTransport(sdk),
        )
        host = NeutralResourceHost(
            driver,
            sessions,
            scope=scope,
            account_scope_id="gemini-workspace",
            operation_key="credential-target",
            resource_catalog_enabled=True,
            catalog_adapter=driver.catalog_adapter,
        )
        # Actual authored native target POST, independent SDK GET and catalog CAS.
        # This is a component proof, not the still-unbound channel-copy route.
        target = source.model_copy(
            update={
                "name": "credential-team",
                "metadata": {MA_METADATA_KEY_NAME: "credential-team"},
            }
        )
        row = await host.publish("credential-team", target, expected_revision=0, key="target:1")
        thread = ThreadRef(
            channel=ChannelRef(tenant_id=scope.tenant_id, platform="discord", channel_id="101"),
            thread_id="102",
        )
        binding = ProviderBinding(
            id="owned-native-binding",
            provider="gemini",
            profile="gemini.inline_reuse",
            generation=1,
            config_revision=1,
            thread=thread,
            legacy_account_id=str(actor.id),
            native_refs={
                "agent": row.agent.ref.id,
                "session": "host-session-scoped",
                "account_scope": "gemini-workspace",
            },
        )
        store = PostgresStateStore(sessions)
        await store.put_config_revision(
            ConfigRevision.create(
                thread.channel,
                1,
                ResolvedBackend(
                    backend="gemini",
                    profile="gemini.inline_reuse",
                    model="gemini-3.8-flash",
                ),
            )
        )
        await store.put_binding(binding, expected_generation=0)
        origin = NativeCredentialOrigin(
            place.id,
            actor.id,
            binding,
            ResourceRef(
                id="host-session-scoped",
                kind="session",
                provider="gemini",
                account_scope_id="gemini-workspace",
                tenant_id=scope.tenant_id,
                account_id=str(actor.id),
            ),
            row.agent.ref,
            row.catalog_id,
            row.local_revision,
            scope,
        )
        delivery = NativeDelivery(tmp_path)
        adapter = GeminiNativeCredentialAdapter(
            sessions,
            host,
            operator_enabled=True,
            delivery=delivery,
            public_base_url="https://mcp.invalid",
            now=lambda: NOW,
        )
        yield GeminiCase(adapter, origin, wire, delivery, sessions)
        assert interactions.requests == []
    finally:
        await sdk.aio.aclose()
        sdk.close()
    # A3 never constructs a legacy client or reads an MA identity authority.


openai_case = openai_tests.case
isolated = openai_tests.isolated
owned_database_preflight = openai_tests.owned_database_preflight
db_preflight = openai_tests.db_preflight

type Case = GeminiCase | OpenAICase


@pytest.fixture(params=("gemini_case", "openai_case"), ids=("gemini", "openai"))
def case(request: pytest.FixtureRequest) -> Case:
    return cast(Case, request.getfixturevalue(request.param))


def control_prefix(case: Case) -> str:
    return f"nc_{case.origin.agent.provider}_input:"


@pytest.mark.parametrize("kind,target", [("github", "GitHub"), ("env", "ACME_API_KEY")])
async def test_actual_native_target_posts_shared_offer(case: Case, kind: str, target: str) -> None:
    before = len(case.wire.requests)
    receipt = await case.adapter.offer(case.origin, kind=kind, target=target)  # type: ignore[arg-type]
    registered = case.delivery.registered[0]
    message = case.delivery.surface.gateway.surviving_messages()[0]
    assert receipt.message_id == message.message_id and receipt.thread == case.origin.binding.thread
    assert (
        receipt.request == registered.ref and receipt.request.provider == case.origin.agent.provider
    )
    assert (
        registered.origin.agent == case.origin.agent and registered.agent_name == "credential-team"
    )
    assert registered.expires_at == NOW + timedelta(minutes=30)
    assert registered.origin.requester_account_id != uuid.UUID(case.adapter.host.scope.account_id)
    assert all(req.method == "GET" for req in case.wire.requests[before:])
    assert len(case.wire.requests[before:]) == 1
    assert control_prefix(case) in message.payload_json and '"ztc:' not in message.payload_json
    assert "gh_connect:" not in message.payload_json
    if kind == "github":
        assert message.component_labels == (
            ("Connect GitHub",) if isinstance(case, GeminiCase) else ("Open offer",)
        )
        payload = _OBJECT.validate_json(message.payload_json)
        embeds = payload["embeds"]
        assert isinstance(embeds, list) and len(embeds) == 1
        embed = _OBJECT.validate_python(embeds[0])
        assert embed["title"] == "Connect GitHub"
        assert embed["description"] == (
            "Nothing is connected yet.\n\nTap the button and tick the repos credential-team can use."
        )
        assert embed["author"] == {
            "name": "Daimon",
            "icon_url": "https://mcp.invalid/web/daimon-face.png",
        }
        assert embed["thumbnail"] == {"url": "https://mcp.invalid/web/github-mark.png"}
        assert "ask your daimon operator" not in " ".join(message.embed_texts).lower()
    else:
        assert "Enter it privately" in " ".join(message.component_labels)
        assert "ACME_API_KEY" in message.payload_json
    # The routing/offer is observed, not an expiry/OAuth/submit certificate.
    case.adapter.now = lambda: NOW + timedelta(seconds=1920)
    assert case.delivery.surface.gateway.surviving_messages()[0] == message
    assert len(case.delivery.surface.gateway.events) == 1


@pytest.mark.parametrize("missing", ["flag", "delivery"])
async def test_default_off_or_missing_native_delivery_refuses_before_io(
    case: Case, missing: str
) -> None:
    if missing == "flag":
        adapter = type(case.adapter)(
            case.sessions,
            case.adapter.host,
            delivery=case.delivery,
            public_base_url="https://mcp.invalid",
        )
    else:
        adapter = type(case.adapter)(
            case.sessions,
            case.adapter.host,
            operator_enabled=True,
            public_base_url="https://mcp.invalid",
        )
    before = len(case.wire.requests)
    with pytest.raises(UnsupportedCapability):
        await adapter.offer(case.origin, kind="github")
    assert len(case.wire.requests) == before and case.delivery.registered == []


@pytest.mark.parametrize(
    "bad",
    [
        "provider",
        "workspace",
        "resource_account",
        "session_account",
        "generation",
        "origin",
        "catalog",
        "binding_workspace",
        "shared_binding",
        "foreign_binding_account",
    ],
)
async def test_foreign_or_stale_target_refuses_before_native_io(case: Case, bad: str) -> None:
    origin = case.origin
    if bad == "provider":
        origin = replace(origin, agent=origin.agent.model_copy(update={"provider": "anthropic"}))
    elif bad == "workspace":
        origin = replace(
            origin,
            agent=origin.agent.model_copy(update={"account_scope_id": "foreign"}),
        )
    elif bad == "resource_account":
        origin = replace(
            origin,
            agent=origin.agent.model_copy(update={"account_id": str(uuid.uuid4())}),
        )
    elif bad == "session_account":
        origin = replace(
            origin,
            session=origin.session.model_copy(update={"account_id": str(uuid.uuid4())}),
        )
    elif bad == "generation":
        origin = replace(origin, binding=origin.binding.model_copy(update={"generation": 2}))
    elif bad == "origin":
        origin = replace(origin, origin_id=uuid.uuid4())
    elif bad == "catalog":
        origin = replace(origin, catalog_revision=2)
    elif bad in ("shared_binding", "foreign_binding_account"):
        session = origin.session.model_copy(update={"id": str(uuid.uuid4())})
        binding = await case.adapter.store.put_binding(
            origin.binding.model_copy(
                update={
                    "id": str(uuid.uuid4()),
                    "legacy_account_id": None if bad == "shared_binding" else str(uuid.uuid4()),
                    "native_refs": dict(origin.binding.native_refs) | {"session": session.id},
                }
            ),
            expected_generation=0,
        )
        assert await case.adapter.store.get_binding(binding_slot(binding)) == binding
        origin = replace(
            origin,
            binding=binding,
            session=session,
        )
    else:
        origin = replace(
            origin,
            binding=origin.binding.model_copy(
                update={
                    "native_refs": dict(origin.binding.native_refs) | {"account_scope": "foreign"}
                }
            ),
        )
    before = len(case.wire.requests)
    with pytest.raises(
        (ScopeViolation, CatalogConflict),
        match="outside its grant" if bad in ("shared_binding", "foreign_binding_account") else None,
    ):
        await case.adapter.offer(origin, kind="github")
    assert len(case.wire.requests) == before and case.delivery.registered == []


@pytest.mark.parametrize("changed", ["origin", "binding", "role", "writers", "catalog"])
async def test_fresh_refusal_after_native_registration_posts_nothing(
    case: Case, changed: str
) -> None:
    async def mutate() -> None:
        if changed == "catalog":
            row = await case.adapter.host.entry(case.origin.catalog_id)
            await case.adapter.host.publish(
                row.catalog_id,
                row.agent.spec.model_copy(update={"system": "Changed after registration."}),
                expected_revision=row.local_revision,
                key="target:changed",
            )
        elif changed == "binding":
            await case.adapter.store.put_binding(
                case.origin.binding.model_copy(update={"generation": 2}),
                expected_generation=1,
            )
        else:
            async with case.sessions.begin() as db:
                if changed == "origin":
                    await delete_origin(db, origin_id=case.origin.origin_id)
                elif changed == "role":
                    await set_role(db, case.origin.requester_account_id, Role.USER)
                else:
                    await set_access_policy(
                        db,
                        tenant_id=uuid.UUID(case.adapter.host.scope.tenant_id),
                        policy=TenantAccessPolicy(
                            channel_rules={"101": ChannelRule(writers="none")}
                        ),
                    )

    case.delivery.on_register = mutate
    with pytest.raises((ScopeViolation, CatalogConflict)):
        await case.adapter.offer(case.origin, kind="github")
    assert len(case.delivery.registered) == 1
    assert case.delivery.surface.gateway.events == []


async def test_false_delivery_receipt_is_refused(case: Case) -> None:
    case.delivery.bad_receipt = True
    with pytest.raises(ScopeViolation, match="receipt mismatched"):
        await case.adapter.offer(case.origin, kind="github")
    assert len(case.delivery.surface.gateway.events) == 1


async def test_reserved_env_key_refuses_before_native_readback(case: Case) -> None:
    before = len(case.wire.requests)
    with pytest.raises(ValueError, match="key name"):
        await case.adapter.offer(case.origin, kind="env", target="SSLKEYLOGFILE")
    assert len(case.wire.requests) == before and not case.delivery.registered


@pytest.mark.parametrize("kind", ["github", "env"])
async def test_native_mcp_dispatch_posts_actual_verified_target_offer(
    case: Case, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    server = FastMCP("explicit-native")
    auth = AuthIdentity(
        tenant_id=uuid.UUID(case.adapter.host.scope.tenant_id),
        account_id=case.origin.requester_account_id,
        role=Role.ADMIN,
    )

    async def authenticated(ctx: Context) -> AuthIdentity:
        return auth

    async def granted(identity: AuthIdentity, origin_id: str) -> native_mcp.NativeCredentialGrant:
        assert identity == auth and origin_id == str(case.origin.origin_id)
        return native_mcp.NativeCredentialGrant(case.adapter, case.origin)

    await native_mcp.register_native_credential_tools(
        server,
        profile=case.adapter.profile,
        auth_resolver=authenticated,
        operator_enabled=True,
        grants=granted,
    )
    before = len(case.wire.requests)
    arguments = {"origin_context_id": str(case.origin.origin_id)}
    if kind == "env":
        arguments["key"] = "ACME_API_KEY"
    result = await server.call_tool(
        "github_connect" if kind == "github" else "request_agent_key", arguments
    )
    content = cast(Sequence[object], result.content)
    assert isinstance(content[0], TextContent)
    receipt = TypeAdapter(NativeOfferReceipt).validate_json(content[0].text)
    message = case.delivery.surface.gateway.surviving_messages()[0]
    assert receipt.message_id == message.message_id
    assert receipt.request == case.delivery.registered[0].ref
    assert control_prefix(case) in message.payload_json
    assert len(case.wire.requests[before:]) == 1 and case.wire.requests[-1].method == "GET"
    if kind == "github":
        assert message.component_labels == (
            ("Connect GitHub",) if isinstance(case, GeminiCase) else ("Open offer",)
        )
        payload = _OBJECT.validate_json(message.payload_json)
        embeds = payload["embeds"]
        assert isinstance(embeds, list) and len(embeds) == 1
        embed = _OBJECT.validate_python(embeds[0])
        assert embed["title"] == "Connect GitHub"
        assert embed["description"] == (
            "Nothing is connected yet.\n\nTap the button and tick the repos credential-team can use."
        )
        assert embed["author"] == {
            "name": "Daimon",
            "icon_url": "https://mcp.invalid/web/daimon-face.png",
        }
        assert embed["thumbnail"] == {"url": "https://mcp.invalid/web/github-mark.png"}
    else:
        assert "ACME_API_KEY" in message.payload_json


@pytest.mark.parametrize(
    "bad",
    [
        "tenant",
        "requester",
        "origin",
        "host_account",
        "host_tenant",
        "principal",
        "authorization",
        "profile",
    ],
)
async def test_native_mcp_factory_mismatch_refuses_before_io(
    case: Case, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    server = FastMCP("explicit-native")
    auth = AuthIdentity(
        tenant_id=uuid.uuid4() if bad == "tenant" else uuid.UUID(case.adapter.host.scope.tenant_id),
        account_id=uuid.uuid4() if bad == "requester" else case.origin.requester_account_id,
        role=Role.ADMIN,
    )

    async def authenticated(ctx: Context) -> AuthIdentity:
        return auth

    async def granted(identity: AuthIdentity, origin_id: str) -> native_mcp.NativeCredentialGrant:
        origin = case.origin
        if bad in ("principal", "authorization"):
            origin = replace(
                origin,
                resource_scope=origin.resource_scope.model_copy(update={bad + "_id": "foreign"}),
            )
        return native_mcp.NativeCredentialGrant(case.adapter, origin)

    if bad in ("host_account", "host_tenant"):
        host = case.adapter.host
        field = "account_id" if bad == "host_account" else "tenant_id"
        case.adapter.host = NeutralResourceHost(
            host.backend,
            case.sessions,
            scope=host.scope.model_copy(update={field: str(uuid.uuid4())}),
            account_scope_id=host.workspace,
            operation_key="foreign-factory-return",
            resource_catalog_enabled=isinstance(case, GeminiCase),
            catalog_adapter=cast(GeminiManagedAgents, host.backend).catalog_adapter
            if isinstance(case, GeminiCase)
            else None,
        )
    from mux.profiles import get_profile

    profile = (
        case.adapter.profile
        if bad != "profile"
        else get_profile(
            "openai.persistent_workspace" if isinstance(case, GeminiCase) else "gemini.inline_reuse"
        )
    )
    await native_mcp.register_native_credential_tools(
        server,
        profile=profile,
        auth_resolver=authenticated,
        operator_enabled=True,
        grants=granted,
    )
    before = len(case.wire.requests)
    with pytest.raises(ToolError, match="native credential grant mismatched"):
        await server.call_tool(
            "github_connect",
            {"origin_context_id": str(uuid.uuid4() if bad == "origin" else case.origin.origin_id)},
        )
    assert len(case.wire.requests) == before and not case.delivery.registered
    assert not case.delivery.surface.gateway.events


@pytest.mark.parametrize("axis", ["tenant", "account", "principal", "authorization", "workspace"])
async def test_returned_host_scope_is_verified_before_database_or_native_io(
    case: Case, owned_engine: AsyncEngine, axis: str
) -> None:
    host = case.adapter.host
    expected = case.origin.resource_scope
    if axis == "workspace":
        if isinstance(case, GeminiCase):
            before = len(case.wire.requests)
            with pytest.raises(UnsupportedCapability):
                NeutralResourceHost(
                    host.backend,
                    case.sessions,
                    scope=expected,
                    account_scope_id="foreign-workspace",
                    operation_key="foreign-return",
                    resource_catalog_enabled=True,
                    catalog_adapter=cast(GeminiManagedAgents, host.backend).catalog_adapter,
                )
            assert len(case.wire.requests) == before
            return
        returned = NeutralResourceHost(
            host.backend,
            case.sessions,
            scope=expected,
            account_scope_id="foreign-workspace",
            operation_key="foreign-return",
        )
    else:
        field = {
            "tenant": "tenant_id",
            "account": "account_id",
            "principal": "principal_id",
            "authorization": "authorization_id",
        }[axis]
        returned = NeutralResourceHost(
            host.backend,
            case.sessions,
            scope=expected.model_copy(update={field: str(uuid.uuid4())}),
            account_scope_id=host.workspace,
            operation_key="foreign-return",
            resource_catalog_enabled=isinstance(case, GeminiCase),
            catalog_adapter=cast(GeminiManagedAgents, host.backend).catalog_adapter
            if isinstance(case, GeminiCase)
            else None,
        )
    # A legitimately constructed host can still be the wrong factory result.
    case.adapter.host = returned
    sql: list[object] = []

    def observed(*args: object, **kwargs: object) -> None:
        sql.append(None)

    event.listen(owned_engine.sync_engine, "before_cursor_execute", observed)
    before = len(case.wire.requests)
    try:
        with pytest.raises(ScopeViolation, match="outside its grant"):
            await case.adapter.offer(case.origin, kind="github")
    finally:
        event.remove(owned_engine.sync_engine, "before_cursor_execute", observed)
    assert sql == [] and len(case.wire.requests) == before
    assert not case.delivery.registered and not case.delivery.surface.gateway.events


async def test_native_readback_drift_refuses_before_register_or_post(
    case: Case,
) -> None:
    if isinstance(case, GeminiCase):
        case.wire.get_changes = {"tools": []}
    else:
        case.wire.agents[case.origin.agent.id]["model"] = "different-model"
    with pytest.raises((ProviderError, CatalogConflict)):
        await case.adapter.offer(case.origin, kind="github")
    assert not case.delivery.registered and not case.delivery.surface.gateway.events


async def test_expired_origin_refuses_before_provider_readback(case: Case) -> None:
    case.adapter.now = lambda: NOW + timedelta(hours=3)
    before = len(case.wire.requests)
    with pytest.raises(ScopeViolation, match="expired"):
        await case.adapter.offer(case.origin, kind="github")
    assert len(case.wire.requests) == before and not case.delivery.registered


class NamesOnly(Mapping[str, str]):
    def __init__(self, name: str) -> None:
        self.name = name

    def __iter__(self) -> Iterator[str]:
        yield self.name

    def __len__(self) -> int:
        return 1

    def __getitem__(self, key: str) -> str:
        raise AssertionError("read ambient value")


@pytest.mark.parametrize("provider", ["gemini", "openai"])
@pytest.mark.parametrize(
    "name",
    [
        "GOOGLE_API_KEY",
        "GEMINI_FUTURE_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "SSLKEYLOGFILE",
        "HTTPS_PROXY",
    ],
)
def test_replay_sdk_refuses_present_value_traps_before_allocation(
    provider: str, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("allocated SDK/HTTP client")

    wire = httpx.MockTransport(lambda _: httpx.Response(500))
    with monkeypatch.context() as patch:
        patch.setattr(os, "environ", NamesOnly(name))
        patch.setattr("google.genai.Client", forbidden)
        patch.setattr(httpx, "Client", forbidden)
        patch.setattr(httpx, "AsyncClient", forbidden)
        with pytest.raises(ValueError):
            if provider == "gemini":
                build_gemini_client(transport=wire)
            else:
                build_async_openai(transport=wire)


@pytest.mark.parametrize(
    "field,value",
    [("backend", "anthropic"), ("profile", "anthropic.managed_agents"), ("thread_mode", "shared")],
)
async def test_retained_revision_must_match_native_route(
    case: Case, monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    revision = await case.adapter.store.get_config_revision(case.origin.binding.thread.channel, 1)
    assert revision is not None

    async def changed(*args: object) -> ConfigRevision:
        return revision.model_copy(update={field: value})

    monkeypatch.setattr(case.adapter.store, "get_config_revision", changed)
    before = len(case.wire.requests)
    with pytest.raises(ScopeViolation, match="configuration unavailable"):
        await case.adapter.offer(case.origin, kind="github")
    assert len(case.wire.requests) == before and not case.delivery.registered


@pytest.mark.parametrize("field,value", [("provider", "anthropic"), ("id", "different-model")])
async def test_catalog_model_must_match_retained_revision(
    case: Case, monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    entry = await case.adapter.host.entry(case.origin.catalog_id)
    changed = replace(
        entry,
        agent=entry.agent.model_copy(
            update={
                "spec": entry.agent.spec.model_copy(
                    update={"model": entry.agent.spec.model.model_copy(update={field: value})}
                )
            }
        ),
    )

    async def foreign_entry(*args: object) -> AgentRevision:
        return changed

    monkeypatch.setattr(case.adapter.host, "entry", foreign_entry)
    before = len(case.wire.requests)
    with pytest.raises(CatalogConflict, match="target changed"):
        await case.adapter.offer(case.origin, kind="github")
    assert len(case.wire.requests) == before and not case.delivery.registered


@pytest.mark.parametrize(
    "axis",
    [
        "tenant",
        "account",
        "principal",
        "authorization",
        "workspace",
        "provider",
        "profile",
        "catalog_type",
        "catalog_enabled",
        "catalog_account",
        "catalog_provider",
        "catalog_profile",
    ],
)
async def test_shared_resource_binder_refuses_every_foreign_axis(
    case: Case, monkeypatch: pytest.MonkeyPatch, axis: str
) -> None:
    from daimon.adapters.mcp.tools._gemini_resources import bind_gemini_host
    from daimon.adapters.mcp.tools._native_resources import MCP_AUTHORIZATION_ID
    from daimon.adapters.mcp.tools._openai_resources import bind_host

    auth = AuthIdentity(
        tenant_id=uuid.UUID(case.adapter.host.scope.tenant_id),
        account_id=uuid.UUID(case.adapter.host.scope.account_id),
        role=Role.ADMIN,
    )
    host = case.adapter.host
    scope = host.scope.model_copy(
        update={"principal_id": f"mcp:{auth.account_id}", "authorization_id": MCP_AUTHORIZATION_ID}
    )
    monkeypatch.setattr(host, "scope", scope)
    binder = bind_gemini_host if isinstance(case, GeminiCase) else bind_host
    assert binder(host, auth=auth, workspace=host.workspace) is host
    workspace = host.workspace
    if axis in ("tenant", "account", "principal", "authorization"):
        monkeypatch.setattr(host, "scope", scope.model_copy(update={axis + "_id": "foreign"}))
    elif axis == "workspace":
        workspace = "foreign"
    elif axis in ("provider", "profile"):
        capabilities = host.backend.capabilities().model_copy(
            update={
                "provider" if axis == "provider" else "profile_id": "anthropic"
                if axis == "provider"
                else "anthropic.managed_agents"
            }
        )
        monkeypatch.setattr(host.backend, "capabilities", lambda: capabilities)
    elif axis == "catalog_type":
        monkeypatch.setattr(host.backend, "agents", object())
    else:
        port = host.backend.agents
        field = {
            "catalog_enabled": "enabled",
            "catalog_account": "account_scope_id",
            "catalog_provider": "provider",
            "catalog_profile": "profile_id",
        }[axis]
        value = False if axis == "catalog_enabled" else "foreign"
        monkeypatch.setattr(type(port), field, property(lambda _: value), raising=False)
    before = len(case.wire.requests)
    with pytest.raises(ToolError, match="belong to someone else"):
        binder(host, auth=auth, workspace=workspace)
    assert len(case.wire.requests) == before and not case.delivery.registered


@pytest.mark.parametrize(
    "field,value",
    [("backend", "anthropic"), ("model", "unreviewed-model"), ("thread_mode", "shared")],
)
async def test_shared_skill_admission_refuses_invalid_revision(
    case: Case, field: str, value: str
) -> None:
    from daimon.adapters.mcp.tools._native_resources import check_skill_revision

    revision = await case.adapter.store.get_config_revision(case.origin.binding.thread.channel, 1)
    assert revision is not None
    check_skill_revision(revision, case.adapter.profile)
    with pytest.raises(ToolError, match="not authorized"):
        check_skill_revision(revision.model_copy(update={field: value}), case.adapter.profile)
    assert not case.delivery.registered


@pytest.mark.parametrize(
    "field,value",
    [("backend", "anthropic"), ("model", "unreviewed-model"), ("thread_mode", "shared")],
)
async def test_skill_routes_reject_invalid_configuration_before_factory(
    case: Case, monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    from types import SimpleNamespace

    from daimon.adapters.mcp.runtime import McpResourceHostFactory, McpRuntime
    from daimon.adapters.mcp.tools import _gemini_resources, _openai_resources
    from daimon.core.stores.domain import TurnOriginRow
    from daimon.core.turn import runtimes

    revision = await case.adapter.store.get_config_revision(case.origin.binding.thread.channel, 1)
    assert revision is not None
    revision = revision.model_copy(update={field: value})
    auth = AuthIdentity(
        tenant_id=uuid.UUID(case.origin.resource_scope.tenant_id),
        account_id=case.origin.requester_account_id,
        role=Role.ADMIN,
        platform="discord",
    )
    origin = cast(
        TurnOriginRow,
        SimpleNamespace(
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            platform=auth.platform,
            parent_channel_id="101",
        ),
    )
    calls: list[object] = []

    class Factory:
        account_scope_id = case.adapter.host.workspace

        def __call__(self, **kwargs: object) -> NeutralResourceHost:
            calls.append(None)
            raise AssertionError("invalid revision allocated a native host")

    factory = cast(McpResourceHostFactory, Factory())
    runtime = cast(
        McpRuntime, SimpleNamespace(session_factory=case.sessions, resource_hosts=factory)
    )

    async def current(*args: object) -> ConfigRevision:
        return revision

    module = _gemini_resources if isinstance(case, GeminiCase) else _openai_resources
    monkeypatch.setattr(module, "current_backend", current)
    with pytest.raises(ToolError, match="not authorized"):
        if isinstance(case, GeminiCase):
            cap = _gemini_resources.GeminiSkillCapability(
                factory,
                (
                    _gemini_resources.GeminiSkillGrant(
                        auth.tenant_id,
                        auth.account_id,
                        "discord",
                        "101",
                        revision.digest,
                        factory.account_scope_id,
                    ),
                ),
                enabled=True,
            )
            await module.gemini_skill_host(runtime, auth, origin, capability=cap, operation="proof")
        else:
            deployment = SimpleNamespace(
                platform="discord",
                channel_id="101",
                config_digest=revision.digest,
                account_id=str(auth.account_id),
                project=factory.account_scope_id,
            )
            monkeypatch.setattr(runtimes, "_RUNTIME_FACTORIES", {})
            runtimes.register_channel_runtime(
                module.OPENAI_PROFILE,
                lambda _: (_ for _ in ()).throw(AssertionError("authorization built a runtime")),
                authorizations=lambda _: (deployment,),
            )
            await module.openai_skill_host(runtime, auth, origin, operation="proof")
    assert calls == []
