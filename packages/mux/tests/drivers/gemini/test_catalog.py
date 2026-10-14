"""Opt-in native managed-agent compiler/readback, never a live cell certificate."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn, cast

import httpx
import pytest
import pytest_asyncio
from daimon.core.neutral_resources import NeutralResourceHost
from daimon.core.skills.ingest import SkillIngestError
from daimon.core.stores.neutral_catalog import CatalogConflict, PostgresNeutralCatalog
from daimon.testing.db import (
    build_test_engine,
    require_owned_sessionmaker,
)
from daimon.testing.db import (
    db_engine as db_engine,
)
from daimon.testing.db import (
    db_schema as db_schema,
)
from daimon.testing.drivers.gemini.fake import FakeTransport, MemoryStorage
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.isolated_sdk import build_gemini_client
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ModelRef, PageRequest, Scope, SkillRef, ThreadRef
from mux.contracts.resources import (
    AgentFilter,
    AgentSpec,
    EnvironmentSpec,
    MCPConnection,
    SessionSpec,
    SkillUpload,
    SkillUploadFile,
    ToolSpec,
)
from mux.drivers.gemini import GeminiCatalogAdapter, GeminiManagedAgents
from mux.drivers.gemini.catalog_transport import SDKAgentCatalogTransport
from mux.drivers.gemini.core import GeminiAgents
from mux.drivers.gemini.transport import Object
from mux.errors import OperationConflict, ProviderError, ScopeViolation, UnsupportedCapability
from mux.state.memory import MemoryStateStore
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from google.genai import Client

SCOPE = Scope(tenant_id="tenant", account_id="account", principal_id="admin", authorization_id="a1")
WORKSPACE = "native-a1-workspace"
_OBJECT: TypeAdapter[Object] = TypeAdapter(Object)
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
    # Refuse present names before values/import/allocation, including TLS keylog.
    if any(
        name.upper() in TLS_NAMES
        or name.upper().startswith(("ANTHROPIC_", "OPENAI_", "GOOGLE_", "GEMINI_"))
        for name in os.environ
    ):
        raise ValueError("A1 replay refuses ambient SDK configuration")
    return build_gemini_client(transport=transport)


def spec(*, name: str = "source", pins: tuple[SkillRef, ...] = ()) -> AgentSpec:
    return AgentSpec(
        name=name,
        description="qa-original",
        model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
        system="Help this workspace.",
        tools=(ToolSpec(name="code_execution", kind="builtin"),),
        skills=pins,
        metadata={"daimon.name": name},
    )


class Wire:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.agents: dict[str, Object] = {}
        self.get_changes: Object = {}
        self.lose_post = False
        self.sparse_ack = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST" and request.url.path == "/v1beta/agents":
            body = _OBJECT.validate_json(request.content, strict=True)
            id_ = body["id"]
            if not isinstance(id_, str):
                raise ValueError("missing authored native identity")
            self.agents[id_] = deepcopy(body)
            if self.lose_post:
                self.lose_post = False
                raise httpx.ReadError("offline accepted POST response lost", request=request)
            response = (
                {"id": id_, "tools": None, "base_environment": None} if self.sparse_ack else body
            )
            return httpx.Response(200, json=response)
        if request.method == "GET" and request.url.path.startswith("/v1beta/agents/"):
            id_ = request.url.path.rsplit("/", 1)[-1]
            body = self.agents.get(id_)
            if body is None:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            return httpx.Response(200, json={**deepcopy(body), **self.get_changes})
        raise ValueError("unexpected native catalog request")


@dataclass
class Case:
    wire: Wire
    storage: MemoryStorage
    interactions: FakeTransport
    native: SDKAgentCatalogTransport
    driver: GeminiManagedAgents


@pytest_asyncio.fixture
async def case() -> AsyncIterator[Case]:
    wire, storage, interactions = Wire(), MemoryStorage(), FakeTransport()
    client = replay_client(httpx.MockTransport(wire.handle))
    try:
        native = SDKAgentCatalogTransport(client)
        driver = GeminiManagedAgents(
            interactions,
            storage=storage,
            state_store=MemoryStateStore(),
            account_scope_id=WORKSPACE,
            resource_catalog_enabled=True,
            resource_catalog_transport=native,
        )
        yield Case(wire, storage, interactions, native, driver)
    finally:
        await client.aio.aclose()
        client.close()


async def test_native_catalog_create_uses_real_sdk_and_independent_get(case: Case) -> None:
    made = await case.driver.agents.create(SCOPE, spec(), key="create:source")
    adapter = case.driver.catalog_adapter
    assert adapter is not None
    expected = await adapter.compile(SCOPE, spec(), id_=made.ref.id)
    assert [r.method for r in case.wire.requests] == ["POST", "GET"]
    assert (
        case.wire.requests[0].content
        == json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode()
    )
    assert expected["tools"] == [{"type": "code_execution"}]
    assert made.revision.native is None  # No fictional provider version.
    assert made.ref.id in case.wire.agents
    assert made.spec.metadata == spec().metadata  # Host-only intent, not provider metadata.
    assert await adapter.verify(SCOPE, made) == made
    assert not case.interactions.requests


async def test_default_false_keeps_existing_inline_port_and_never_calls_native(case: Case) -> None:
    driver = GeminiManagedAgents(
        case.interactions,
        storage=MemoryStorage(),
        state_store=MemoryStateStore(),
        account_scope_id=WORKSPACE,
        resource_catalog_transport=case.native,
    )
    assert driver.catalog_adapter is None and type(driver.agents) is GeminiAgents
    made = await driver.agents.create(SCOPE, spec(), key="default")
    assert made.native is None
    assert not case.wire.requests
    disabled = GeminiCatalogAdapter(case.interactions, case.storage, WORKSPACE, case.native)
    with pytest.raises(UnsupportedCapability, match="gemini_resource_catalog"):
        await disabled.create(SCOPE, spec(), key="disabled")
    assert not case.wire.requests


def test_enabled_requires_explicit_transport_without_allocating_sdk() -> None:
    with pytest.raises(UnsupportedCapability, match="gemini_resource_catalog_transport"):
        GeminiManagedAgents(
            FakeTransport(),
            storage=MemoryStorage(),
            state_store=MemoryStateStore(),
            account_scope_id=WORKSPACE,
            resource_catalog_enabled=True,
        )


@pytest.mark.parametrize("flag", [1, "true", None])
def test_catalog_capability_is_an_explicit_bool(flag: object) -> None:
    with pytest.raises(ValueError, match="explicit bool"):
        GeminiManagedAgents(
            FakeTransport(),
            storage=MemoryStorage(),
            state_store=MemoryStateStore(),
            account_scope_id=WORKSPACE,
            resource_catalog_enabled=cast(bool, flag),
        )


async def test_restart_reconciles_accepted_lost_post_by_get_without_resending(case: Case) -> None:
    case.wire.lose_post = True
    with pytest.raises(ProviderError):
        await case.driver.agents.create(SCOPE, spec(), key="lost-post")
    async with case.storage.transaction() as records:
        assert records.creations and not records.agents
    restarted = GeminiManagedAgents(
        case.interactions,
        storage=case.storage,
        state_store=MemoryStateStore(),
        account_scope_id=WORKSPACE,
        resource_catalog_enabled=True,
        resource_catalog_transport=case.native,
    )
    made = await restarted.agents.create(SCOPE, spec(), key="lost-post")
    assert [r.method for r in case.wire.requests] == ["POST", "GET"]
    assert made.ref.id in case.wire.agents
    with pytest.raises(OperationConflict):
        await restarted.agents.create(SCOPE, spec(name="changed"), key="lost-post")
    assert [r.method for r in case.wire.requests] == ["POST", "GET"]


async def test_unverified_get_keeps_fence_without_fabricating_agent(case: Case) -> None:
    case.wire.lose_post = True
    with pytest.raises(ProviderError):
        await case.driver.agents.create(SCOPE, spec(), key="unknown")
    case.wire.agents.clear()
    with pytest.raises(ProviderError) as error:
        await case.driver.agents.create(SCOPE, spec(), key="unknown")
    assert error.value.native_code == "catalog_create_unverified"
    assert [r.method for r in case.wire.requests] == ["POST", "GET"]
    async with case.storage.transaction() as records:
        assert records.creations and not records.agents


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "base_agent",
        "agent_config",
        "tools",
        "base_environment",
        "description",
        "system_instruction",
    ],
)
async def test_native_readback_drift_refuses_instead_of_echoing_catalog(
    case: Case, field: str
) -> None:
    made = await case.driver.agents.create(SCOPE, spec(), key="drift")
    case.wire.get_changes[field] = None
    with pytest.raises(ProviderError) as error:
        await case.driver.agents.retrieve(SCOPE, made.ref)
    assert error.value.category == "conflict"
    assert len([r for r in case.wire.requests if r.method == "POST"]) == 1


async def test_sparse_null_sdk_ack_and_nullable_metadata_do_not_require_native_refs(
    case: Case,
) -> None:
    case.wire.sparse_ack = True
    requested = spec().model_copy(update={"description": None, "system": None, "tools": ()})
    case.wire.get_changes = {"description": None, "system_instruction": None, "metadata": None}
    made = await case.driver.agents.create(SCOPE, requested, key="nulls")
    assert made.spec == requested
    assert case.wire.agents[made.ref.id]["tools"] == []
    assert await case.driver.agents.retrieve(SCOPE, made.ref) == made


@pytest.mark.parametrize(
    "target", ["tenant_id", "account_id", "account_scope_id", "provider", "kind"]
)
async def test_foreign_native_reference_refuses_before_readback(case: Case, target: str) -> None:
    made = await case.driver.agents.create(SCOPE, spec(), key="scope")
    count = len(case.wire.requests)
    wrong = made.ref.model_copy(update={target: "other"})
    with pytest.raises(ScopeViolation):
        await case.driver.agents.retrieve(SCOPE, wrong)
    assert len(case.wire.requests) == count


@pytest.mark.parametrize("bad", ["model", "pro", "latest", "digest", "credentials", "tool"])
async def test_unsupported_authored_intent_refuses_before_native_post(case: Case, bad: str) -> None:
    requested = spec()
    if bad == "model":
        requested = requested.model_copy(
            update={"model": ModelRef(provider="anthropic", id="claude-haiku-4-5-20251001")}
        )
    elif bad == "pro":
        requested = requested.model_copy(
            update={"model": ModelRef(provider="gemini", id="gemini-3.5-pro")}
        )
    elif bad == "credentials":
        requested = requested.model_copy(
            update={
                "mcp_servers": (
                    MCPConnection(
                        name="mcp", url="https://mcp.invalid", credential_ref="credential:owned"
                    ),
                )
            }
        )
    elif bad == "tool":
        requested = requested.model_copy(update={"tools": (ToolSpec(name="bash", kind="builtin"),)})
    else:
        skill = await case.driver.skills.create(SCOPE, bundle(), key="skill")
        assert skill.latest_version is not None
        pin = skill.latest_version.model_copy(
            update={"version": "latest"} if bad == "latest" else {"digest": "wrong"}
        )
        requested = requested.model_copy(update={"skills": (pin,)})
    with pytest.raises(UnsupportedCapability):
        await case.driver.agents.create(SCOPE, requested, key="refusal")
    assert not case.wire.requests


def bundle(body: bytes = b"# Original\nKeep every byte.\n") -> SkillUpload:
    return SkillUpload(
        files=(
            SkillUploadFile(path="SKILL.md", content=body),
            SkillUploadFile(path="scripts/check.sh", content=b"true\n"),
        )
    )


async def test_copy_preserves_concrete_bundle_bytes_and_old_pin_after_publish(case: Case) -> None:
    source = await case.driver.skills.create(SCOPE, bundle(), key="skill:source")
    assert source.latest_version is not None
    original = source.latest_version
    made = await case.driver.agents.create(SCOPE, spec(pins=(original,)), key="agent:source")
    copied = await case.driver.skills.create(SCOPE, bundle(), key="skill:copy")
    assert copied.latest_version is not None and copied.id != source.id
    forked = await case.driver.agents.create(
        SCOPE, spec(name="fork", pins=(copied.latest_version,)), key="agent:fork"
    )
    await case.driver.skills.publish_version(
        SCOPE, source.id, bundle(b"# Changed\n"), key="skill:v2"
    )
    assert made.spec.skills == (original,)
    assert await case.driver.agents.retrieve(SCOPE, made.ref) == made
    assert await case.driver.agents.retrieve(SCOPE, forked.ref) == forked
    old_sources = _OBJECT.validate_python(case.wire.agents[made.ref.id]["base_environment"])[
        "sources"
    ]
    copied_sources = _OBJECT.validate_python(case.wire.agents[forked.ref.id]["base_environment"])[
        "sources"
    ]
    assert isinstance(old_sources, list) and isinstance(copied_sources, list)
    assert [_OBJECT.validate_python(s)["content"] for s in old_sources] == [
        _OBJECT.validate_python(s)["content"] for s in copied_sources
    ]
    assert all(source.id in str(_OBJECT.validate_python(s)["target"]) for s in old_sources)
    assert all(copied.id in str(_OBJECT.validate_python(s)["target"]) for s in copied_sources)


async def test_local_bundle_drift_refuses_before_native_read(case: Case) -> None:
    skill = await case.driver.skills.create(SCOPE, bundle(), key="bundle-drift")
    assert skill.latest_version is not None and skill.latest_version.version is not None
    made = await case.driver.agents.create(
        SCOPE, spec(pins=(skill.latest_version,)), key="bundle-agent"
    )
    async with case.storage.transaction() as records:
        records.skills[skill.id].bundles[skill.latest_version.version] = bundle(b"# Forged\n")
    before = len(case.wire.requests)
    with pytest.raises(ProviderError, match="catalog_skill_drift"):
        await case.driver.agents.retrieve(SCOPE, made.ref)
    assert len(case.wire.requests) == before


async def test_named_agent_cannot_silently_enter_inline_session_plan(case: Case) -> None:
    made = await case.driver.agents.create(SCOPE, spec(), key="native")
    environment = await case.driver.environments.create(
        SCOPE, EnvironmentSpec(name="inline"), key="env"
    )
    with pytest.raises(UnsupportedCapability, match="named_agent_session_plan"):
        await case.driver.sessions.create(
            SCOPE,
            SessionSpec(
                agent=made.ref,
                agent_revision=made.revision,
                environment=environment.ref,
                config_revision=1,
                extensions={
                    "gemini.session": ExtensionConfig(
                        namespace="gemini.session",
                        version=1,
                        value={
                            "thread": ThreadRef(
                                channel=ChannelRef(
                                    tenant_id=SCOPE.tenant_id,
                                    platform="discord",
                                    channel_id="channel",
                                ),
                                thread_id="thread",
                            ).model_dump(mode="json"),
                            "binding_id": "binding",
                        },
                    )
                },
            ),
            key="session",
        )
    assert not case.interactions.requests


async def test_adapter_lists_only_owned_native_definitions_and_verifies_each(case: Case) -> None:
    made = await case.driver.agents.create(SCOPE, spec(), key="owned")
    inline = GeminiAgents(case.interactions, case.storage, WORKSPACE)
    await inline.create(SCOPE, spec(name="inline"), key="inline")
    page = await case.driver.agents.list(SCOPE, filters=AgentFilter(), page=PageRequest())
    assert page.data == (made,)
    assert [r.method for r in case.wire.requests] == ["POST", "GET", "GET"]


@pytest.fixture
def catalog_engine(request: pytest.FixtureRequest) -> AsyncEngine:
    # The mux CI job is deliberately database-free. Opt in only through the
    # normal test DSN and refuse foreign binds before the engine fixture connects.
    dsn = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not dsn:
        pytest.skip("native catalog proofs require an explicit test database")
    schema = cast(str, request.getfixturevalue("db_schema"))
    guard_engine = build_test_engine(dsn, schema)
    try:
        require_owned_sessionmaker(guard_engine)
    except ValueError:
        pytest.skip("native catalog proofs require an owned test database")
    finally:
        # No connection was opened; disposing this empty pool is synchronous.
        guard_engine.sync_engine.dispose()
    return cast(AsyncEngine, request.getfixturevalue("db_engine"))


@pytest_asyncio.fixture
async def catalog_db(
    catalog_engine: AsyncEngine,
) -> tuple[async_sessionmaker[AsyncSession], Scope, Scope]:
    sessions = async_sessionmaker(catalog_engine, expire_on_commit=False)
    require_owned_sessionmaker(sessions)
    async with sessions() as session, session.begin():
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        other = await make_account(session, tenant=tenant)
        scopes = tuple(
            Scope(
                tenant_id=str(tenant.id),
                account_id=str(owner.id),
                principal_id="authorized-operator",
                authorization_id="a1-native-catalog",
            )
            for owner in (account, other)
        )
    return sessions, scopes[0], scopes[1]


async def test_native_catalog_cas_retains_old_agent_pin_and_independent_readback(
    case: Case, catalog_db: tuple[async_sessionmaker[AsyncSession], Scope, Scope]
) -> None:
    sessions, scope, foreign = catalog_db
    catalog = PostgresNeutralCatalog(sessions, provider="gemini", account_scope_id=WORKSPACE)
    skill = await case.driver.skills.create(scope, bundle(), key="cas:skill")
    assert skill.latest_version is not None
    original = skill.latest_version
    first = await case.driver.agents.create(scope, spec(pins=(original,)), key="cas:first")
    row = await catalog.put_agent(scope, "logical", first, expected_revision=0)
    next_pin = await case.driver.skills.publish_version(
        scope, skill.id, bundle(b"# New\n"), key="cas:skill:v2"
    )
    second = await case.driver.agents.create(
        scope, spec(pins=(next_pin.ref,)), key="cas:replacement"
    )
    updated = await catalog.put_agent(scope, "logical", second, expected_revision=1)
    restarted = PostgresNeutralCatalog(sessions, provider="gemini", account_scope_id=WORKSPACE)
    assert await restarted.get_agent(scope, "logical") == updated
    assert await restarted.get_agent(scope, "logical", local_revision=1) == row
    assert updated.local_revision == 2
    assert first.ref.id != second.ref.id
    assert first.revision.native is second.revision.native is None
    assert row.agent.spec.skills == (original,)
    assert case.driver.catalog_adapter is not None
    assert await case.driver.catalog_adapter.verify(scope, row.agent) == first
    with pytest.raises(CatalogConflict):
        await restarted.put_agent(scope, "logical", first, expected_revision=1)
    with pytest.raises(ScopeViolation):
        await restarted.put_agent(foreign, "logical", first, expected_revision=0)
    assert await restarted.get_agent(scope, "logical") == updated
    assert await restarted.get_agent(foreign, "logical") is None
    authored_drift = first.model_copy(update={"spec": spec(name="tampered")})
    with pytest.raises(ProviderError, match="catalog_authored_drift"):
        await case.driver.catalog_adapter.verify(scope, authored_drift)
    case.wire.get_changes = {"tools": []}
    with pytest.raises(ProviderError, match="catalog_native_definition_drift"):
        await case.driver.catalog_adapter.verify(scope, updated.agent)


async def test_host_guard_still_refuses_gemini_before_io(
    case: Case, catalog_db: tuple[async_sessionmaker[AsyncSession], Scope, Scope]
) -> None:
    sessions, scope, _ = catalog_db
    with pytest.raises(UnsupportedCapability, match="neutral_resource_host_after_g2"):
        NeutralResourceHost(
            case.driver,
            sessions,
            scope=scope,
            account_scope_id=WORKSPACE,
            operation_key="host:guard",
        )
    assert not case.wire.requests


@pytest.mark.parametrize(
    "edge", ["missing", "disabled", "provider", "profile", "workspace", "port", "truthy"]
)
async def test_host_requires_the_explicit_matching_native_adapter_before_io(
    case: Case,
    catalog_db: tuple[async_sessionmaker[AsyncSession], Scope, Scope],
    monkeypatch: pytest.MonkeyPatch,
    edge: str,
) -> None:
    sessions, scope, _ = catalog_db
    adapter = case.driver.catalog_adapter
    assert adapter is not None
    workspace = WORKSPACE
    flag = True
    supplied: GeminiCatalogAdapter | None = adapter
    if edge == "missing":
        supplied = None
    elif edge == "disabled":
        monkeypatch.setattr(adapter, "enabled", False)
    elif edge == "provider":
        monkeypatch.setattr(adapter, "provider", "openai")
    elif edge == "profile":
        monkeypatch.setattr(adapter, "profile_id", "gemini.other")
    elif edge == "workspace":
        workspace = "foreign-workspace"
    elif edge == "truthy":
        flag = cast(bool, 1)
    else:
        supplied = GeminiCatalogAdapter(
            case.interactions, case.storage, WORKSPACE, case.native, enabled=True
        )
    with pytest.raises(UnsupportedCapability, match="neutral_resource_host_after_g2"):
        NeutralResourceHost(
            case.driver,
            sessions,
            scope=scope,
            account_scope_id=workspace,
            operation_key="host:negative",
            resource_catalog_enabled=flag,
            catalog_adapter=supplied,
        )
    assert not case.wire.requests


async def test_enabled_host_publishes_and_reads_native_then_refuses_scope_or_drift(
    case: Case, catalog_db: tuple[async_sessionmaker[AsyncSession], Scope, Scope]
) -> None:
    sessions, scope, foreign = catalog_db
    host = NeutralResourceHost(
        case.driver,
        sessions,
        scope=scope,
        account_scope_id=WORKSPACE,
        operation_key="host:positive",
        resource_catalog_enabled=True,
        catalog_adapter=case.driver.catalog_adapter,
    )
    row = await host.publish("source", spec(), expected_revision=0, key="host:source")
    assert await host.read("source") == row
    assert [r.method for r in case.wire.requests] == ["POST", "GET", "GET"]
    before = len(case.wire.requests)
    foreign_host = NeutralResourceHost(
        case.driver,
        sessions,
        scope=foreign,
        account_scope_id=WORKSPACE,
        operation_key="host:foreign",
        resource_catalog_enabled=True,
        catalog_adapter=case.driver.catalog_adapter,
    )
    with pytest.raises(SkillIngestError):
        await foreign_host.read("source")
    assert len(case.wire.requests) == before
    with pytest.raises(CatalogConflict):
        await host.publish("source", spec(name="stale"), expected_revision=0, key="stale")
    assert len(case.wire.requests) == before
    with pytest.raises(ScopeViolation):
        NeutralResourceHost(
            case.driver,
            sessions,
            scope=scope.model_copy(update={"authorization_id": " "}),
            account_scope_id=WORKSPACE,
            operation_key="host:unbound",
            resource_catalog_enabled=True,
            catalog_adapter=case.driver.catalog_adapter,
        )
    assert len(case.wire.requests) == before
    case.wire.get_changes = {"tools": []}
    with pytest.raises(ProviderError, match="catalog_native_definition_drift"):
        await host.read("source")


class NamesOnly(Mapping[str, str]):
    def __init__(self, name: str) -> None:
        self.name = name

    def __iter__(self) -> Iterator[str]:
        return iter((self.name,))

    def __len__(self) -> int:
        return 1

    def __getitem__(self, key: str) -> str:
        raise AssertionError("read ambient value")


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
def test_sdk_replay_refuses_present_names_before_any_value_or_allocation(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("allocated SDK/HTTP client")

    wire = httpx.MockTransport(lambda _: httpx.Response(500))
    with monkeypatch.context() as patch:
        patch.setattr(os, "environ", NamesOnly(name))
        patch.setattr("google.genai.Client", forbidden)
        patch.setattr(httpx, "Client", forbidden)
        patch.setattr(httpx, "AsyncClient", forbidden)
        with pytest.raises(ValueError, match="ambient SDK"):
            replay_client(wire)
