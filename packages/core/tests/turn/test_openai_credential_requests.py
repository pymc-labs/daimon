"""Actual native SDK/catalog/origin and Discord serialization proofs for A3."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

import discord
import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools import openai_credential_requests as native_mcp
from daimon.adapters.mcp.tools.discord._posted_card import build_card_view
from daimon.core import agent_fork
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.authz import Subject
from daimon.core.channel_rules import set_channel_rule
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.github_connect_cards import ConnectCard, discord_embed_payload
from daimon.core.neutral_resources import NeutralResourceHost
from daimon.core.posted_controls import PostedCard
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.stores.neutral_catalog import CatalogConflict
from daimon.core.stores.turn_origins import create_origin, delete_origin
from daimon.core.turn import native_credential_requests as native_product
from daimon.core.turn.openai_credential_requests import (
    CONTROL_PREFIX,
    NativeCredentialOrigin,
    NativeCredentialRequest,
    NativeOfferReceipt,
    OpenAINativeCredentialAdapter,
)
from daimon.testing import pytest_plugin as database_plugin
from daimon.testing.db import DatabasePreflight, preflight_test_database, require_owned_sessionmaker
from daimon.testing.discord_surface import DiscordMessageCapture, DiscordSurfaceHarness
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from daimon.testing.isolated_sdk import SDK_ENV_VARS, build_async_anthropic, build_async_openai
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import TextContent
from mux.contracts.config import ConfigRevision, ResolvedBackend
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef, Revision, Scope, ThreadRef
from mux.contracts.resources import Agent, AgentSpec, ProviderBinding, ToolSpec
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.transport import SDKTransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ScopeViolation, UnsupportedCapability
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

NOW = datetime(2026, 10, 11, tzinfo=UTC)


db_preflight = database_plugin.db_preflight


@pytest.fixture(scope="session", autouse=True)
def owned_database_preflight(db_preflight: DatabasePreflight) -> None:
    """Admit all binds through the shared fixture before any engine connects."""


@pytest.mark.parametrize(
    ("url", "skipped"),
    [
        ("postgresql+asyncpg://test:test@foreign.invalid/daimon_test_nc_n11", False),
        ("postgresql+asyncpg://test:test@localhost/unowned_test", False),
        ("postgresql+asyncpg://test:test@localhost/daimon_test_nc_n11?host=foreign.invalid", False),
        ("postgresql+asyncpg://test:test@localhost/daimon_test", True),
    ],
)
def test_database_preflight_precedes_connections(tmp_path: Path, url: str, skipped: bool) -> None:
    """Exercise real pytest fixture ordering; every attempted connection is trapped."""
    trap = tmp_path / "connection-attempted"
    plugin = tmp_path / "a3_connection_trap.py"
    plugin.write_text(
        "from pathlib import Path\n"
        "from sqlalchemy import event\n"
        "from sqlalchemy.engine import Engine\n"
        "def pytest_sessionstart(session):\n"
        "    def forbid(*args, **kwargs):\n"
        f"        Path({str(trap)!r}).write_text('connection attempted')\n"
        "        raise RuntimeError('A3 refused URL reached connection setup')\n"
        "    event.listen(Engine, 'do_connect', forbid)\n"
    )
    env = dict(os.environ)
    for name in ("GITHUB_ACTIONS", "CI", "PYTEST_ADDOPTS"):
        env.pop(name, None)
    env["DAIMON_DATABASE__TEST_URL"] = url
    env["PYTHONPATH"] = os.pathsep.join((str(tmp_path), env.get("PYTHONPATH", "")))
    selected = (
        f"{Path(__file__).resolve()}"
        "::test_default_off_or_missing_native_delivery_refuses_before_io[flag]"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            "pyproject.toml",
            "-o",
            "addopts=",
            "-n",
            "0",
            "-p",
            "a3_connection_trap",
            selected,
            "-q",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert not trap.exists(), result.stdout + result.stderr
    if skipped:
        assert result.returncode == 0 and "1 skipped" in result.stdout
    else:
        assert result.returncode == 1, result.stdout + result.stderr
        assert "Test hooks require owned local lane engines" in result.stdout
        assert "skipped" not in result.stdout


@pytest.mark.parametrize("registered", [True, False])
def test_database_preflight_honors_registered_isolated_ci(
    monkeypatch: pytest.MonkeyPatch, registered: bool
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("CI", "true")
    schema = f"test_w{os.getpid()}_1234abcd" if registered else "unowned_schema"
    url = "postgresql+asyncpg://test:test@localhost/daimon_test"
    monkeypatch.setenv("DAIMON_DATABASE__TEST_URL", url)
    if registered:
        preflight_test_database(schema)
    else:
        with pytest.raises(ValueError, match="owned local lane engines"):
            preflight_test_database(schema)


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SDK_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    def no_face(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(agent_fork, "queue_agent_face", no_face)


class NativeWire:
    def __init__(self, scope: Scope, source: AgentSpec) -> None:
        self.scope = scope
        self.requests: list[httpx.Request] = []
        self.agents: dict[str, dict[str, object]] = {
            "native-source": self.raw("native-source", source)
        }

    def raw(self, id_: str, source: AgentSpec) -> dict[str, object]:
        return {
            "id": id_,
            "created_at": 0,
            "name": source.name,
            "model": source.model.id,
            "instructions": source.system,
            "tools": [],
            "metadata": dict(source.metadata or {})
            | {
                "mux_tenant": self.scope.tenant_id,
                "mux_skill_pins": "[]",
            },
        }

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        assert req.url.host == "openai.invalid"
        assert req.headers["OpenAI-Beta"] == "agents=v1"
        if req.method == "POST" and req.url.path == "/v1/agents":
            body = json.loads(req.content)
            self.agents["native-copy"] = dict(body) | {"id": "native-copy", "created_at": 0}
            return httpx.Response(200, json=self.agents["native-copy"])
        if req.method == "GET" and req.url.path.startswith("/v1/agents/"):
            return httpx.Response(200, json=self.agents[req.url.path.rsplit("/", 1)[-1]])
        raise AssertionError("No other native operation belongs in an A3 offer")


class NativeDelivery:
    """Transport-only registry/wire using the real card renderer.

    The button is fixture-owned; offer semantics come from the product card.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.surface = DiscordSurfaceHarness(
            evidence_id="native-credential",
            session_id="native-session",
            root_turn_id="root-offer",
            clock=lambda: 0.0,
            model_id="gpt-6-luna",
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
                    label="Open offer",
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
class Case:
    adapter: OpenAINativeCredentialAdapter
    origin: NativeCredentialOrigin
    wire: NativeWire
    delivery: NativeDelivery
    sessions: async_sessionmaker[AsyncSession]


@pytest.fixture
async def case(db_engine: AsyncEngine, db_clean: None, tmp_path: Path) -> AsyncIterator[Case]:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    require_owned_sessionmaker(sessions)
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
        model=ModelRef(provider="openai", id="gpt-6-luna"),
        tools=(ToolSpec(name="bash", kind="builtin"),),
        skills=(),
        metadata={MA_METADATA_KEY_NAME: "daimon"},
        system="Help.",
    )
    wire = NativeWire(scope, source)
    legacy_calls: list[httpx.Request] = []

    def no_legacy(req: httpx.Request) -> httpx.Response:
        legacy_calls.append(req)
        raise AssertionError("native own-copy or offer must never issue an MA request")

    async with (
        build_async_openai(
            transport=httpx.MockTransport(wire.handle), base_url="https://openai.invalid/v1"
        ) as sdk,
        build_async_anthropic(
            transport=httpx.MockTransport(no_legacy), base_url="https://anthropic.invalid"
        ) as legacy,
    ):
        host = NeutralResourceHost(
            OpenAIDriver(
                SDKTransport(sdk),
                account_scope_id="project",
                authorization=lambda actual, kind, id_: actual == scope,
                journal=MemoryRecoveryJournal(),
                usage_revisions=MemoryUsageRevisions(),
            ),
            sessions,
            scope=scope,
            account_scope_id="project",
            operation_key="own-copy",
        )
        root = Agent(
            ref=ResourceRef(
                id="native-source",
                kind="agent",
                provider="openai",
                account_scope_id="project",
                tenant_id=scope.tenant_id,
                account_id=scope.account_id,
            ),
            spec=source,
            revision=Revision(local=0),
            created_at=NOW,
        )
        await host.catalog.put_agent(scope, "daimon", root, expected_revision=0)
        copied = await set_channel_rule(
            legacy,
            sessions,
            tenant_id=tenant.id,
            platform="discord",
            channel_id="101",
            subject=Subject(is_admin=True),
            default=DeploymentDefault(agent_name="daimon"),
            readers="own",
            writers="own",
            copy=True,
            copy_from="daimon",
            channel_label="Credential Team",
            actor_account_id=resource.id,
            resource_host=host,
        )
        assert copied.agent_name is not None
        row = await host.entry(copied.agent_name)
        thread = ThreadRef(
            channel=ChannelRef(tenant_id=scope.tenant_id, platform="discord", channel_id="101"),
            thread_id="102",
        )
        binding = ProviderBinding(
            id="owned-native-binding",
            provider="openai",
            profile="openai.persistent_workspace",
            generation=1,
            config_revision=1,
            thread=thread,
            legacy_account_id=str(actor.id),
            native_refs={
                "agent": row.agent.ref.id,
                "session": "native-session",
                "account_scope": "project",
            },
        )
        store = PostgresStateStore(sessions)
        await store.put_config_revision(
            ConfigRevision.create(
                thread.channel,
                1,
                ResolvedBackend(
                    backend="openai", profile="openai.persistent_workspace", model="gpt-6-luna"
                ),
            )
        )
        await store.put_binding(binding, expected_generation=0)
        origin = NativeCredentialOrigin(
            place.id,
            actor.id,
            binding,
            ResourceRef(
                id="native-session",
                kind="session",
                provider="openai",
                account_scope_id="project",
                tenant_id=scope.tenant_id,
                account_id=str(actor.id),
            ),
            row.agent.ref,
            row.catalog_id,
            row.local_revision,
            scope,
        )
        delivery = NativeDelivery(tmp_path)
        adapter = OpenAINativeCredentialAdapter(
            sessions,
            host,
            operator_enabled=True,
            delivery=delivery,
            public_base_url="https://mcp.invalid",
            now=lambda: NOW,
        )
        yield Case(adapter, origin, wire, delivery, sessions)
        assert legacy_calls == []
    # The offline Discord gateway has no network session; SDK clients closed above.


def _assert_product_github_offer(message: DiscordMessageCapture) -> None:
    assert "Connect GitHub" in message.embed_texts, "missing product GitHub title"
    assert any(
        "Tap the button and tick the repos credential-team can use." in text
        for text in message.embed_texts
    ), "missing product GitHub description"


@pytest.mark.parametrize("kind,target", [("github", "GitHub"), ("env", "ACME_API_KEY")])
async def test_actual_native_fork_target_posts_shared_offer(
    case: Case, kind: str, target: str
) -> None:
    before = len(case.wire.requests)
    receipt = await case.adapter.offer(case.origin, kind=kind, target=target)  # type: ignore[arg-type]
    registered = case.delivery.registered[0]
    message = case.delivery.surface.gateway.surviving_messages()[0]
    assert receipt.message_id == message.message_id and receipt.thread == case.origin.binding.thread
    assert receipt.request == registered.ref and receipt.request.provider == "openai"
    assert (
        registered.origin.agent == case.origin.agent and registered.agent_name == "credential-team"
    )
    assert registered.expires_at == NOW + timedelta(minutes=30)
    assert registered.origin.requester_account_id != uuid.UUID(case.adapter.host.scope.account_id)
    assert all(req.method == "GET" for req in case.wire.requests[before:])
    assert len(case.wire.requests[before:]) == 1
    assert CONTROL_PREFIX in message.payload_json and '"ztc:' not in message.payload_json
    assert "gh_connect:" not in message.payload_json
    if kind == "github":
        _assert_product_github_offer(message)
        assert "ask your daimon operator" not in " ".join(message.embed_texts).lower()
    else:
        assert "Enter it privately" in " ".join(message.component_labels)
        assert "ACME_API_KEY" in message.payload_json
    # The routing/offer is observed, not an expiry/OAuth/submit certificate.
    case.adapter.now = lambda: NOW + timedelta(seconds=1920)
    assert case.delivery.surface.gateway.surviving_messages()[0] == message
    assert len(case.delivery.surface.gateway.events) == 1


@pytest.mark.parametrize("mutation", ["title", "description", "both"])
async def test_product_github_offer_mutation_is_detected(
    case: Case, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    build_card = native_product.build_connect_card

    def mutated_card(
        *,
        agent_name: str | None,
        identity_enabled: bool,
        avatar_url: str | None,
        public_base_url: str,
        variant: Literal["A", "B", "B_NO_FOOTER"] | None = None,
    ) -> ConnectCard:
        card = build_card(
            agent_name=agent_name,
            identity_enabled=identity_enabled,
            avatar_url=avatar_url,
            public_base_url=public_base_url,
            variant=variant,
        )
        return replace(
            card,
            title="Something else" if mutation in ("title", "both") else card.title,
            description="x" if mutation in ("description", "both") else card.description,
        )

    monkeypatch.setattr(native_product, "build_connect_card", mutated_card)
    await case.adapter.offer(case.origin, kind="github", target="GitHub")
    message = case.delivery.surface.gateway.surviving_messages()[0]
    with pytest.raises(AssertionError, match="missing product GitHub"):
        _assert_product_github_offer(message)


@pytest.mark.parametrize("missing", ["flag", "delivery"])
async def test_default_off_or_missing_native_delivery_refuses_before_io(
    case: Case, missing: str
) -> None:
    if missing == "flag":
        adapter = OpenAINativeCredentialAdapter(
            case.sessions,
            case.adapter.host,
            delivery=case.delivery,
            public_base_url="https://mcp.invalid",
        )
    else:
        adapter = OpenAINativeCredentialAdapter(
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
    ],
)
async def test_foreign_or_stale_target_refuses_before_native_io(case: Case, bad: str) -> None:
    origin = case.origin
    if bad == "provider":
        origin = replace(origin, agent=origin.agent.model_copy(update={"provider": "anthropic"}))
    elif bad == "workspace":
        origin = replace(
            origin, agent=origin.agent.model_copy(update={"account_scope_id": "foreign"})
        )
    elif bad == "resource_account":
        origin = replace(
            origin, agent=origin.agent.model_copy(update={"account_id": str(uuid.uuid4())})
        )
    elif bad == "session_account":
        origin = replace(
            origin, session=origin.session.model_copy(update={"account_id": str(uuid.uuid4())})
        )
    elif bad == "generation":
        origin = replace(origin, binding=origin.binding.model_copy(update={"generation": 2}))
    elif bad == "origin":
        origin = replace(origin, origin_id=uuid.uuid4())
    elif bad == "catalog":
        origin = replace(origin, catalog_revision=2)
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
    with pytest.raises((ScopeViolation, CatalogConflict)):
        await case.adapter.offer(origin, kind="github")
    assert len(case.wire.requests) == before and case.delivery.registered == []


@pytest.mark.parametrize("changed", ["origin", "binding", "role", "writers"])
async def test_fresh_refusal_after_native_registration_posts_nothing(
    case: Case, changed: str
) -> None:
    async def mutate() -> None:
        if changed == "binding":
            await case.adapter.store.put_binding(
                case.origin.binding.model_copy(update={"generation": 2}), expected_generation=1
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
    with pytest.raises(ScopeViolation):
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

    async def granted(identity: AuthIdentity, origin_id: str) -> native_mcp.OpenAICredentialGrant:
        assert identity == auth and origin_id == str(case.origin.origin_id)
        return native_mcp.OpenAICredentialGrant(case.adapter, case.origin)

    monkeypatch.setattr(native_mcp, "_auth", authenticated)
    await native_mcp.register_openai_credential_tools(server, operator_enabled=True, grants=granted)
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
    assert CONTROL_PREFIX in message.payload_json
    assert len(case.wire.requests[before:]) == 1 and case.wire.requests[-1].method == "GET"
    if kind == "github":
        _assert_product_github_offer(message)
    else:
        assert "ACME_API_KEY" in message.payload_json


@pytest.mark.parametrize("bad", ["tenant", "requester", "origin"])
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

    async def granted(identity: AuthIdentity, origin_id: str) -> native_mcp.OpenAICredentialGrant:
        return native_mcp.OpenAICredentialGrant(case.adapter, case.origin)

    monkeypatch.setattr(native_mcp, "_auth", authenticated)
    await native_mcp.register_openai_credential_tools(server, operator_enabled=True, grants=granted)
    before = len(case.wire.requests)
    with pytest.raises(ToolError, match="native credential grant mismatched"):
        await server.call_tool(
            "github_connect",
            {"origin_context_id": str(uuid.uuid4() if bad == "origin" else case.origin.origin_id)},
        )
    assert len(case.wire.requests) == before and not case.delivery.registered
    assert not case.delivery.surface.gateway.events
