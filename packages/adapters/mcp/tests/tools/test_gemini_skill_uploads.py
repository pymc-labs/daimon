"""Real Gemini catalog Add-skill replay; NEW18's yes-please stays an honest failure."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import uuid
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import MagicMock

import httpx
import pytest
import pytest_asyncio
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import skill_uploads
from daimon.adapters.mcp.tools._gemini_resources import GeminiSkillCapability, GeminiSkillGrant
from daimon.adapters.mcp.tools._openai_resources import MCP_AUTHORIZATION_ID
from daimon.adapters.mcp.tools.skill_uploads import AddSkillResult
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.channel_backend import channel_ref, set_channel_backend
from daimon.core.config import Settings
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.neutral_resources import NeutralResourceHost
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import Role
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.turn_origins import create_origin
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.core.turn_origin import turn_origin
from daimon.testing.db import build_test_engine, require_owned_sessionmaker
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent
from fastmcp.exceptions import ToolError
from mux.contracts.config import BackendConfig
from mux.contracts.ids import ModelRef, Scope
from mux.contracts.resources import AgentSpec, ToolSpec
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.catalog_transport import SDKAgentCatalogTransport
from mux.drivers.gemini.transport import Object
from mux.state.memory import MemoryStateStore
from mux.testing.drivers.anthropic.ma import FakeMAState, build_fake_anthropic, make_fake_ma_handler
from mux.testing.drivers.gemini.fake import FakeTransport, MemoryStorage
from mux.testing.isolated_sdk import build_gemini_client
from pydantic import TypeAdapter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ROOM = "111111111111111111"
CHAT_THREAD = "555555555555555555"
USER = "444444444444444444"


@dataclass
class World:
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    runtime: McpRuntime
    created: list[str]


async def world_for(sessions: async_sessionmaker[AsyncSession]) -> World:
    async with sessions.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
    state = FakeMAState()
    agent = ma_agent(
        id="agent_helper",
        name="helper",
        tenant_id=tenant.id,
        metadata={"daimon_account": str(account.id)},
    )
    state.agents[agent.id] = agent.model_dump(mode="json")
    upstream = make_fake_ma_handler(state)
    created: list[str] = []

    def anthropic_wire(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/skills":
            created.append(request.url.path)
        return upstream(request)

    settings = MagicMock()
    settings.tool_safety = ToolSafetyPolicy(enabled=False)
    settings.mcp.public_url = None
    runtime = McpRuntime(
        session_factory=sessions,
        client=build_fake_anthropic(anthropic_wire),
        settings=cast(Settings, settings),
        deployment_default=DeploymentDefault(),
    )
    return World(tenant.id, account.id, runtime, created)


MARKDOWN = "---\nname: notes\ndescription: Take meeting notes.\n---\nWrite them down.\n"
WORKSPACE = "gemini-new18-workspace"
GEMINI = BackendConfig(backend="gemini", profile="gemini.inline_reuse", model="gemini-3.8-flash")
OBJECT: TypeAdapter[Object] = TypeAdapter(Object)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def owned_database_preflight(db_schema: str) -> AsyncIterator[None]:
    """Ownership admission before the parent fixture can provision/connect."""
    url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not url:
        pytest.skip("Gemini host proof requires an explicitly configured isolated test database")
    probe = build_test_engine(url, db_schema)
    try:
        try:
            require_owned_sessionmaker(probe)
        except ValueError:
            pytest.skip("Gemini host proof requires owned local or registered isolated CI database")
        yield
    finally:
        await probe.dispose()


class NativeWire:
    """Native Agents SDK retains independent request/readback bytes; no host add fake."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.agents: dict[str, Object] = {}
        self.drift = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST" and request.url.path == "/v1beta/agents":
            body = OBJECT.validate_json(request.content, strict=True)
            id_ = body["id"]
            if not isinstance(id_, str):
                raise ValueError("missing native catalog identity")
            self.agents[id_] = body
            return httpx.Response(200, json=body)
        if request.method == "GET" and request.url.path.startswith("/v1beta/agents/"):
            body = self.agents[request.url.path.rsplit("/", 1)[-1]]
            return httpx.Response(200, json={**body, **({"tools": []} if self.drift else {})})
        raise AssertionError("unexpected Gemini native request")


@dataclass
class Factory:
    sessions: async_sessionmaker[AsyncSession]
    driver: GeminiManagedAgents
    account_scope_id: str = WORKSPACE
    wrong_scope: str | None = None
    calls: list[Scope] = field(default_factory=list[Scope])

    def __call__(
        self,
        *,
        tenant_id: uuid.UUID,
        account_id: uuid.UUID,
        principal_id: str,
        authorization_id: str,
        operation_key: str,
    ) -> NeutralResourceHost:
        scope = Scope(
            tenant_id=str(tenant_id),
            account_id=str(account_id),
            principal_id=principal_id,
            authorization_id=authorization_id,
        )
        self.calls.append(scope)
        if self.wrong_scope is not None:
            scope = scope.model_copy(update={self.wrong_scope: "different"})
        return NeutralResourceHost(
            self.driver,
            self.sessions,
            scope=scope,
            account_scope_id=self.account_scope_id,
            operation_key=operation_key,
            resource_catalog_enabled=True,
            catalog_adapter=self.driver.catalog_adapter,
        )


@dataclass
class Case:
    world: World
    auth: AuthIdentity
    origin: str
    factory: Factory
    wire: NativeWire
    native_id: str
    scope: Scope

    async def call(
        self,
        *,
        origin: str | None = None,
        expected: str | None = "current",
        content_hash: str | None = None,
        markdown: str = MARKDOWN,
    ) -> AddSkillResult:
        return await skill_uploads._add_skill_impl(  # pyright: ignore[reportPrivateUsage]
            self.world.runtime,
            self.auth,
            agent_name="helper",
            expected_ma_agent_id=self.native_id if expected == "current" else expected,
            skill_md=markdown,
            content_hash=content_hash,
            origin_context_id=origin or self.origin,
        )

    @asynccontextmanager
    async def reply(self, message: str) -> AsyncIterator[str]:
        async with turn_origin(
            self.world.runtime.session_factory,
            tenant_id=self.world.tenant_id,
            account_id=self.auth.account_id,
            platform="discord",
            parent_channel_id=ROOM,
            thread_id=CHAT_THREAD,
            responder_ma_agent_id=self.native_id,
            responder_name="helper",
            role=self.auth.role,
            sender_foreign=False,
            shared_channel=False,
            message_text=message,
            chat_session_id="test-session",
        ) as origin:
            yield str(origin.id)

    async def pending(self) -> int:
        async with self.world.runtime.session_factory() as session:
            return int(await session.scalar(text("SELECT count(*) FROM pending_skill_adds")) or 0)


@pytest.mark.parametrize("present", [False, True])
async def test_default_capability_needs_no_runtime_or_origin_access(present: bool) -> None:
    from daimon.adapters.mcp.runtime import McpResourceHostFactory
    from daimon.adapters.mcp.tools._gemini_resources import gemini_skill_host
    from daimon.core.stores.domain import TurnOriginRow

    # Accessing any property of these inert sentinels would fail; default-off
    # admission must return before reading identities or allocating anything.
    capability = (
        GeminiSkillCapability(cast(McpResourceHostFactory, object()), ()) if present else None
    )
    assert (
        await gemini_skill_host(
            cast(McpRuntime, object()),
            cast(AuthIdentity, object()),
            cast(TurnOriginRow, object()),
            capability=capability,
            operation="proof",
        )
        is None
    )


def test_operator_capability_refuses_truthy_non_boolean() -> None:
    from daimon.adapters.mcp.runtime import McpResourceHostFactory

    with pytest.raises(ValueError, match="explicit bool"):
        GeminiSkillCapability(cast(McpResourceHostFactory, object()), (), enabled=cast(bool, 1))


async def test_capability_for_another_channel_adds_no_configuration_read() -> None:
    from types import SimpleNamespace

    from daimon.adapters.mcp.runtime import McpResourceHostFactory
    from daimon.adapters.mcp.tools._gemini_resources import gemini_skill_host
    from daimon.core.stores.domain import TurnOriginRow

    tenant, account = uuid.uuid4(), uuid.uuid4()
    auth = AuthIdentity(
        tenant_id=tenant,
        account_id=account,
        role=Role.USER,
        platform="discord",
        chat_session_id="test-session",
    )
    origin = cast(
        TurnOriginRow,
        SimpleNamespace(
            tenant_id=tenant, account_id=account, platform="discord", parent_channel_id=ROOM
        ),
    )
    capability = GeminiSkillCapability(
        cast(McpResourceHostFactory, object()),
        (GeminiSkillGrant(tenant, account, "discord", "another", "digest", WORKSPACE),),
        enabled=True,
    )
    assert (
        await gemini_skill_host(
            cast(McpRuntime, object()), auth, origin, capability=capability, operation="proof"
        )
        is None
    )


@pytest_asyncio.fixture
async def case(db_session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[Case]:
    require_owned_sessionmaker(db_session_factory)
    world = await world_for(db_session_factory)
    auth = AuthIdentity(
        account_id=world.account_id,
        tenant_id=world.tenant_id,
        role=Role.USER,
        platform="discord",
        platform_user_id=USER,
        is_admin=False,
        chat_session_id="test-session",
    )
    wire = NativeWire()
    client = build_gemini_client(transport=httpx.MockTransport(wire.handle))
    try:
        driver = GeminiManagedAgents(
            FakeTransport(),
            storage=MemoryStorage(),
            state_store=MemoryStateStore(),
            account_scope_id=WORKSPACE,
            resource_catalog_enabled=True,
            resource_catalog_transport=SDKAgentCatalogTransport(client),
        )
        factory = Factory(db_session_factory, driver)
        host = factory(
            tenant_id=world.tenant_id,
            account_id=world.account_id,
            principal_id=f"mcp:{world.account_id}",
            authorization_id=MCP_AUTHORIZATION_ID,
            operation_key="setup",
        )
        row = await host.publish(
            "helper",
            AgentSpec(
                name="helper",
                model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
                tools=(ToolSpec(name="code_execution", kind="builtin"),),
                metadata={"daimon_name": "helper", "daimon_account": str(world.account_id)},
            ),
            expected_revision=0,
            key="setup-native",
        )
        auth = dataclasses.replace(
            auth,
            role=Role.ADMIN,
            is_admin=True,
            chat_agent_id=derive_agent_uuid(
                tenant_id=world.tenant_id, ma_agent_id=row.agent.ref.id
            ),
        )
        async with db_session_factory.begin() as session:
            await set_access_policy(
                session,
                tenant_id=world.tenant_id,
                policy=TenantAccessPolicy(agent_rules={"helper": AgentRule(runs_in=(ROOM,))}),
            )
            await set_fields(
                session,
                scope=ChannelScopeRef(tenant_id=world.tenant_id, channel_id=ROOM),
                tenant_id=world.tenant_id,
                agent_name="helper",
                mode="agent",
            )
            native_origin = await create_origin(
                session,
                tenant_id=world.tenant_id,
                account_id=world.account_id,
                platform="discord",
                parent_channel_id=ROOM,
                thread_id=CHAT_THREAD,
                responder_ma_agent_id=row.agent.ref.id,
                responder_name="helper",
                configuration_target_ma_agent_id=None,
                configuration_target_name=None,
                role=auth.role,
                sender_foreign=False,
                shared_channel=False,
                now=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
                chat_session_id="test-session",
            )
            origin = str(native_origin.id)
            revision = await set_channel_backend(
                session, channel_ref(world.tenant_id, "discord", ROOM), GEMINI
            )
        capability = GeminiSkillCapability(
            factory,
            (
                GeminiSkillGrant(
                    world.tenant_id, world.account_id, "discord", ROOM, revision.digest, WORKSPACE
                ),
            ),
            enabled=True,
        )
        world.runtime = dataclasses.replace(world.runtime, gemini_skill_capability=capability)
        factory.calls.clear()
        wire.requests.clear()
        yield Case(world, auth, origin, factory, wire, row.agent.ref.id, host.scope)
    finally:
        await client.aio.aclose()
        client.close()
        await world.runtime.client.close()


async def test_canonical_yes_adds_native_concrete_pin_with_real_readback_once(case: Case) -> None:
    preview = await case.call()
    assert preview.status == "preview" and preview.added is None
    assert not case.wire.requests and case.world.created == []
    async with case.reply("yes") as reply:
        added = await case.call(origin=reply, content_hash=preview.preview.content_hash)
        assert added.status == "added" and added.added is not None
        with pytest.raises(ToolError, match="missing or changed"):
            await case.call(origin=reply, content_hash=preview.preview.content_hash)
    host = case.factory(
        tenant_id=case.world.tenant_id,
        account_id=case.world.account_id,
        principal_id=case.scope.principal_id,
        authorization_id=case.scope.authorization_id,
        operation_key="inspect",
    )
    row = await host.read("helper")
    assert row.local_revision == 2 and row.agent.ref.id != case.native_id
    assert row.agent.spec.skills is not None and len(row.agent.spec.skills) == 1
    pin = row.agent.spec.skills[0]
    assert added.added is not None and pin.id == added.added.skill_id
    assert pin.version not in (None, "latest") and pin.version == added.added.version
    retained = await host.retained(pin)
    with zipfile.ZipFile(io.BytesIO(retained.bundle.zip_bytes)) as archive:
        assert archive.read("notes/SKILL.md") == MARKDOWN.encode()
    posts = [r for r in case.wire.requests if r.method == "POST"]
    assert len(posts) == 1 and case.native_id in case.wire.agents
    assert (
        posts[0].content
        == json.dumps(
            case.wire.agents[row.agent.ref.id], separators=(",", ":"), ensure_ascii=False
        ).encode()
    )
    assert case.wire.agents[row.agent.ref.id]["tools"] == [{"type": "code_execution"}]
    assert case.wire.agents[row.agent.ref.id]["base_environment"] == {
        "type": "remote",
        "sources": [
            {"type": "inline", "target": f".agents/skills/{pin.id}/SKILL.md", "content": MARKDOWN}
        ],
    }
    async with case.world.runtime.session_factory() as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM pending_skill_adds WHERE consumed_at IS NOT NULL")
            )
            == 1
        )
    assert case.world.created == [], "the Anthropic upload path was not entered"


@pytest.mark.parametrize("text,adds", [("yes please", True), ("yes please don't", False)])
async def test_a_polite_reply_is_read_by_the_shared_matcher(
    case: Case, text: str, adds: bool
) -> None:
    """A whole polite yes adds through the Gemini host; a qualified one cancels."""
    preview = await case.call()
    async with case.reply(text) as reply:
        if adds:
            added = await case.call(origin=reply, content_hash=preview.preview.content_hash)
            assert added.status == "added" and added.added is not None
        else:
            with pytest.raises(ToolError, match="is not the person replying"):
                await case.call(origin=reply, content_hash=preview.preview.content_hash)
            assert not case.wire.requests
    assert case.world.created == [], "the Anthropic upload path was not entered"


@pytest.mark.parametrize("expected", [None, "different-native-agent"])
async def test_chat_identity_refuses_before_bundle_preview_or_native_write(
    case: Case, expected: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_bundle(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("identity check must precede source loading")

    monkeypatch.setattr(skill_uploads, "_load_bundle", forbidden_bundle)
    with pytest.raises(ToolError, match="Pass expected_ma_agent_id|missing or changed"):
        await case.call(expected=expected)
    assert not case.wire.requests and await case.pending() == 0


@pytest.mark.parametrize("field", ["tenant_id", "account_id", "principal_id", "authorization_id"])
async def test_returned_foreign_host_refuses_before_catalog_read_or_write(
    case: Case, field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    case.factory.wrong_scope = field

    async def forbidden_find(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("foreign returned host must be refused before catalog read")

    monkeypatch.setattr(NeutralResourceHost, "find", forbidden_find)
    with pytest.raises(ToolError, match="belong to someone else"):
        await case.call()
    assert not case.wire.requests and await case.pending() == 0


@pytest.mark.parametrize("setting", ["absent", "disabled"])
async def test_default_off_reuses_the_existing_path_without_gemini_allocation(
    case: Case, setting: str
) -> None:
    capability = case.world.runtime.gemini_skill_capability
    assert capability is not None
    case.world.runtime = dataclasses.replace(
        case.world.runtime,
        gemini_skill_capability=None
        if setting == "absent"
        else dataclasses.replace(capability, enabled=False),
    )
    preview = await case.call(expected="agent_helper")
    assert preview.status == "preview" and not case.factory.calls and not case.wire.requests
    assert case.world.created == []


async def test_native_drift_during_confirm_keeps_retained_revision_and_refuses(case: Case) -> None:
    preview = await case.call()
    case.wire.drift = True
    async with case.reply("yes") as reply:
        with pytest.raises(ToolError, match="failed upstream"):
            await case.call(origin=reply, content_hash=preview.preview.content_hash)
    assert all(r.method == "GET" for r in case.wire.requests) and case.world.created == []
    async with case.world.runtime.session_factory() as session:
        assert (
            await session.scalar(text("SELECT max(local_revision) FROM neutral_agent_revisions"))
            == 1
        )


async def test_actual_own_reader_policy_hides_unruled_helper_before_preview(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    case.auth = dataclasses.replace(case.auth, role=Role.USER, is_admin=False)
    async with case.world.runtime.session_factory.begin() as session:
        await set_access_policy(
            session,
            tenant_id=case.world.tenant_id,
            policy=TenantAccessPolicy(
                channel_rules={ROOM: ChannelRule(readers="own", writers="own")},
                agent_rules={"other": AgentRule(runs_in=(ROOM,))},
            ),
        )

    def forbidden_bundle(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("caller isolation must precede source loading")

    monkeypatch.setattr(skill_uploads, "_load_bundle", forbidden_bundle)
    with pytest.raises(ToolError, match="missing or changed"):
        await case.call()
    assert not case.wire.requests and await case.pending() == 0


@pytest.mark.parametrize(
    "change", ["tenant", "account", "channel", "digest", "workspace", "duplicate"]
)
async def test_stale_or_foreign_grant_refuses_before_factory_allocation(
    case: Case, change: str
) -> None:
    capability = case.world.runtime.gemini_skill_capability
    assert capability is not None
    grant = capability.grants[0]
    if change == "duplicate":
        grants = (grant, grant)
    elif change == "tenant":
        grants = (dataclasses.replace(grant, tenant_id=uuid.uuid4()),)
    elif change == "account":
        grants = (dataclasses.replace(grant, account_id=uuid.uuid4()),)
    elif change == "channel":
        grants = (dataclasses.replace(grant, channel_id="different"),)
    elif change == "workspace":
        grants = (dataclasses.replace(grant, account_scope_id="different"),)
    else:
        grants = (dataclasses.replace(grant, config_digest="different"),)
    case.world.runtime = dataclasses.replace(
        case.world.runtime, gemini_skill_capability=dataclasses.replace(capability, grants=grants)
    )
    with pytest.raises(ToolError, match="not authorized|missing or changed"):
        await case.call()
    assert not case.factory.calls and not case.wire.requests and await case.pending() == 0
