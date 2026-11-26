"""Shared MCP connections on `/mcp` and the hub: the feature gate, cards, reads and audit rows."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from cryptography.fernet import Fernet
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.hub.identity import HUB_AUTH_STATE_KEY, HubIdentity
from daimon.adapters.mcp.middleware.host_call_gate import HostCallGate
from daimon.adapters.mcp.middleware.mcp_identity import IdentityMiddleware, narrow_to_agent_key
from daimon.adapters.mcp.search_transform import AgentChatAwareBM25SearchTransform
from daimon.adapters.mcp.shared_connections import (
    OPERATION,
    SharedConnectionProvider,
    register_scopes,
)
from daimon.adapters.mcp.tools.hub_shared import register_hub_shared_tools
from daimon.core import host_calls
from daimon.core import shared_connections as shared
from daimon.core.authz import Subject
from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
from daimon.core.data_audience import DataAudience
from daimon.core.features.service import FeatureActor, change_feature
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.github_credentials import build_multifernet
from daimon.core.hub_identity import HubTenant
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.security_audit import capture_decision
from daimon.core.stores import agent_tool_rules
from daimon.core.stores import mcp_connections as store
from daimon.core.stores.domain import DiscoveredTool, Role
from daimon.core.stores.host_call_cards import list_cards
from daimon.core.stores.security_audit import list_events
from daimon.core.stores.turn_origins import create_origin
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolSafetyPolicy
from daimon.testing.factories import (
    make_account,
    make_ledger_entry,
    make_platform_principal,
    make_tenant,
)
from daimon.testing.membership import FixtureMembershipResolver
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from pydantic import HttpUrl, JsonValue, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import auth_identity

_URL = "https://mcp.example.com/mcp"
_USER = "user-1"
_MA_AGENT = "agent_x"
_CHANNEL = "channel-1"
_GUILD = "guild-1"


@dataclass
class World:
    sessions: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    agent_id: uuid.UUID
    grant: str
    runtime: SimpleNamespace
    middleware: IdentityMiddleware
    upstream: list[tuple[str, str | None, dict[str, Any]]]
    operations: list[str | None]

    def chat(self, **changes: Any) -> AuthIdentity:
        base = auth_identity(
            self.account_id,
            self.tenant_id,
            role=Role.USER,
            platform="discord",
            chat_agent_id=self.agent_id,
            platform_user_id=_USER,
            chat_session_id=self.grant,
            is_chat_execution=True,
            no_confirmation_surface=True,
        )
        return replace(base, **changes)

    def key(self) -> AuthIdentity:
        """An agent key of the agent: an outside client held to that agent's tool rules."""
        return replace(
            self.chat(), agent_id=self.agent_id, chat_agent_id=None, chat_session_id=None
        )

    def server(self, identity: AuthIdentity) -> FastMCP:
        server = FastMCP("test")
        server.add_provider(SharedConnectionProvider(self.runtime))  # pyright: ignore[reportArgumentType]

        @server.tool(name="describe_agent", tags={"agent-chat"})
        def describe_agent() -> str:  # pyright: ignore[reportUnusedFunction]
            return "an agent-chat tool"

        @server.tool(name="list_routines")
        def list_routines() -> str:  # pyright: ignore[reportUnusedFunction]
            return "found only by search"

        server.add_transform(AgentChatAwareBM25SearchTransform(max_results=5))
        server.add_middleware(_Edge(self, identity))
        return server

    async def call(self, identity: AuthIdentity, name: str) -> str:
        async with Client(self.server(identity)) as client:
            result = await client.call_tool(name, {"id": "1"}, raise_on_error=False)
        return "".join(getattr(part, "text", "") for part in result.content)

    async def listed(self, identity: AuthIdentity) -> list[str]:
        async with Client(self.server(identity)) as client:
            return sorted(t.name for t in await client.list_tools())


class _Edge(Middleware):
    """Stands in for the identity edge: one identity, then the middleware's own dispatch."""

    def __init__(self, world: World, identity: AuthIdentity) -> None:
        self._world = world
        self._identity = identity

    async def on_request(self, context: MiddlewareContext, call_next: CallNext) -> object:
        assert context.fastmcp_context is not None
        await context.fastmcp_context.set_state("auth", self._identity, serializable=False)
        if self._identity.agent_id is not None:
            await narrow_to_agent_key(context.fastmcp_context)
        with capture_decision() as decision:
            result = await self._world.middleware._dispatch_for_backend(  # pyright: ignore[reportPrivateUsage]
                context, call_next, self._identity
            )
        self._world.operations.append(decision.operation)
        return result


async def _none(_: MiddlewareContext) -> str | None:
    return None


def _tool(name: str, *, writes: bool) -> DiscoveredTool:
    schema: dict[str, JsonValue] = {"type": "object", "properties": {"id": {"type": "string"}}}
    return DiscoveredTool(name=name, description=f"{name}.", input_schema=schema, writes=writes)


async def _admin(_: object) -> bool:
    return True


async def _feature(world: World, value: str, key: str = "shared_connections") -> None:
    await change_feature(
        world.sessions,
        world.runtime.settings,
        FeatureActor(world.tenant_id, None, "web", None, _admin),
        key=key,
        tier="workspace",
        value=value,
        place=FeaturePlace(world.tenant_id, "discord"),
    )


@pytest.fixture
async def world(
    committing_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> World:
    now = dt.datetime.now(dt.UTC)
    grant = f"grant-{uuid.uuid4()}"
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id=_GUILD)
        account = await make_account(session, tenant=tenant)
        await make_ledger_entry(session, tenant=tenant)  # the hub's spend gate
        await make_platform_principal(
            session, platform="discord", external_id=_USER, tenant=tenant, account=account
        )
        await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=account.id,
            platform="discord",
            parent_channel_id=_CHANNEL,
            thread_id="thread-1",
            responder_ma_agent_id=_MA_AGENT,
            responder_name="Researcher",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.USER,
            expires_at=now + dt.timedelta(hours=1),
            now=now,
            sender_foreign=False,
            shared_channel=False,
            chat_session_id=grant,
        )
        row = await store.create_connection(
            session,
            tenant_id=tenant.id,
            name="crm",
            url=_URL,
            encrypted_auth=shared.encrypt_auth(fernet, "secret-token"),
            scope=None,
        )
        await store.sync_tools(
            session,
            tenant_id=tenant.id,
            connection_id=row.id,
            discovered=[_tool("lookup", writes=False), _tool("send", writes=True)],
        )
        for name in ("lookup", "send"):
            stored = next(
                t for t in await store.list_tools(session, tenant_id=tenant.id) if t.name == name
            )
            await shared.approve_tool(
                session,
                tenant_id=tenant.id,
                connection_id=row.id,
                name=name,
                seen=shared.seen_digest(stored),
            )
    settings = Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(jwt_secret=SecretStr("s" * 32), public_url=HttpUrl(_URL)),
    )
    runtime = SimpleNamespace(
        session_factory=committing_sessionmaker,
        settings=settings,
        fernet=fernet,
        membership_resolver=FixtureMembershipResolver(_USER),
        resolver_cache=new_resolver_cache(),
    )
    middleware = IdentityMiddleware(
        subject_resolver=_none,
        tenant_resolver=_none,
        role_resolver=_none,
        agent_id_resolver=_none,
        is_admin_resolver=_none,
        internal_resolver=_none,
        sessionmaker=committing_sessionmaker,
        tool_safety=ToolSafetyPolicy(enabled=True, agent_rules=True),
        public_url=_URL,
    )
    middleware.attach_host_call_gate(HostCallGate(runtime, public_url=_URL))  # pyright: ignore[reportArgumentType]
    made = World(
        committing_sessionmaker,
        tenant.id,
        account.id,
        derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_MA_AGENT),
        grant,
        runtime,
        middleware,
        [],
        [],
    )

    class _Upstream:
        async def call_tool_result(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            made.upstream.append((name, self.token, arguments))
            return {"content": [{"type": "text", "text": f"{name} ran"}]}

        def __init__(self, token: str | None) -> None:
            self.token = token

    @asynccontextmanager
    async def open_upstream(url: str, token: str | None) -> AsyncIterator[Any]:
        assert url == _URL, f"the stored address is used: {url}"
        yield _Upstream(token)

    posted: list[object] = []

    async def post_switch_card(*_: object, card: object, **__: object) -> str:
        posted.append(card)
        return "msg-1"

    monkeypatch.setattr("daimon.core.shared_connections.open_upstream", open_upstream)
    monkeypatch.setattr(
        "daimon.adapters.mcp.tools.discord._switch_card.post_switch_card", post_switch_card
    )
    return made  # `shared_connections` is on by default


async def test_a_read_runs_with_the_connections_own_token(world: World) -> None:
    """A read runs for a chat caller, upstream sees the stored token, and the audit names it."""
    text = await world.call(world.chat(), "crm_lookup")

    assert text == "lookup ran", f"the upstream's answer comes back: {text}"
    assert world.upstream == [("lookup", "secret-token", {"id": "1"})], "the connection's token"
    assert OPERATION in world.operations, f"the audit row names the operation: {world.operations}"


async def test_a_write_waits_on_a_card_and_does_not_run(world: World) -> None:
    """A write tool of a shared connection is carded for a chat caller, never run unasked."""
    text = await world.call(world.chat(), "crm_send")

    async with world.sessions() as session:
        cards = await list_cards(session, tenant_id=world.tenant_id)
    assert text == host_calls.waiting_text("the person who asked"), f"waiting result: {text}"
    assert [c.tool_name for c in cards] == ["crm_send"], "one card names the served tool"
    assert world.upstream == [], "nothing reached the upstream before the press"


async def test_after_a_data_read_a_read_waits_on_a_card(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the session read data, even a read asks first: its arguments leave Daimon."""

    async def read_data(*_: object) -> bool:
        return True

    monkeypatch.setattr(HostCallGate, "_read_data", read_data)

    text = await world.call(world.chat(), "crm_lookup")

    assert text == host_calls.waiting_text("the person who asked"), f"waiting result: {text}"
    assert world.upstream == [], "nothing reached the upstream before the press"


async def test_an_outside_client_is_told_to_ask_and_may_write(world: World) -> None:
    """An outside client can't answer a card: a write's description says to ask its user,
    and the write runs."""
    outside = world.chat(chat_agent_id=None, chat_session_id=None, no_confirmation_surface=False)
    async with Client(world.server(outside)) as client:
        described = {t.name: t.description or "" for t in await client.list_tools()}

    assert shared.WRITE_NOTE in described["crm_send"], f"the write says to ask: {described}"
    assert shared.WRITE_NOTE not in described["crm_lookup"], "a read says nothing more"
    assert await world.call(outside, "crm_send") == "send ran", "the write runs"


async def test_a_chat_write_runs_unasked_with_approval_skipped(world: World) -> None:
    """`shared_write_approval` Skip runs a chat write without a card."""
    await _feature(world, "skip", "shared_write_approval")

    assert await world.call(world.chat(), "crm_send") == "send ran", "no card, it ran"


async def test_nothing_is_served_while_the_feature_is_off(world: World) -> None:
    """The tools exist for a workspace only while the feature is on there. They are listed by
    name, beside search, so the agent page's tool rules can name each one."""
    assert await world.listed(world.chat()) == [
        "call_tool", "crm_lookup", "crm_send", "search_tools",
    ], "listed by name while on"  # fmt: skip

    await _feature(world, "off")

    assert await world.listed(world.chat()) == ["call_tool", "search_tools"], "none once off"


async def test_an_agent_key_gets_shared_tools_under_its_agents_rules(world: World) -> None:
    """An agent key sees the shared tools beside agent chat and may write, but its agent's
    deny rule refuses the tool."""
    assert await world.listed(world.key()) == ["crm_lookup", "crm_send", "describe_agent"]
    assert await world.call(world.key(), "crm_send") == "send ran", "the write runs"

    rules: tuple[tuple[Literal["ask", "deny"], str], ...] = (
        ("ask", "needs approval"),
        ("deny", "denies crm_send"),
    )
    for rule, refused in rules:
        async with world.sessions.begin() as session:
            await agent_tool_rules.set_rule(
                session,
                tenant_id=world.tenant_id,
                agent_id=_MA_AGENT,
                agent_name="Researcher",
                tool_key=f"{DAIMON_SERVER_NAME}/crm_send",
                server_url=_URL,
                rule=rule,
                set_by="admin",
                actor=Subject(is_admin=True),
            )
        assert refused in await world.call(world.key(), "crm_send"), f"{rule}: a key has no card"
    assert [u[0] for u in world.upstream] == ["send"], "the refused calls never left"


def _hub(world: World) -> FastMCP:
    hub = FastMCP("hub")
    register_hub_shared_tools(hub, world.runtime)  # pyright: ignore[reportArgumentType]
    caller = HubIdentity(
        platform="discord",
        platform_user_id=_USER,
        tenants=(
            HubTenant(
                tenant_id=world.tenant_id,
                account_id=world.account_id,
                workspace_id=_GUILD,
                workspace_name="Guild",
            ),
        ),
    )

    class _Hub(Middleware):
        async def on_request(self, context: MiddlewareContext, call_next: CallNext) -> object:
            assert context.fastmcp_context is not None
            await context.fastmcp_context.set_state(HUB_AUTH_STATE_KEY, caller, serializable=False)
            return await call_next(context)

    hub.add_middleware(_Hub())
    return hub


async def test_the_hub_audits_each_call(world: World) -> None:
    """A hub call leaves a security audit row, allowed for a read and for a write."""
    async with Client(_hub(world)) as client:
        read = await client.call_tool(
            "call_shared_tool", {"workspace_id": _GUILD, "name": "crm_lookup"}
        )
        await client.call_tool("call_shared_tool", {"workspace_id": _GUILD, "name": "crm_send"})

    async with world.sessions() as session:
        rows = await list_events(session, tenant_id=world.tenant_id)
    assert "lookup ran" in str(read.content), f"the read answered: {read}"
    assert [(r.tool_name, r.outcome, r.operation) for r in rows] == [
        ("crm_lookup", "allowed", OPERATION),
        ("crm_send", "allowed", OPERATION),
    ], f"one row per call: {rows}"


def _people(*people: uuid.UUID, public_only: bool = False) -> DataAudience:
    return DataAudience(frozenset(people), frozenset(), public_only, list_rows=False, redact=False)


@pytest.fixture
async def scoped(world: World, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    """The connection is scoped `sales`, which only the world's person holds, alone."""
    async with world.sessions.begin() as session:
        row = await store.get_connection_by_name(session, tenant_id=world.tenant_id, name="crm")
        assert row is not None
        await store.update_connection(
            session, tenant_id=world.tenant_id, connection_id=row.id, values={"scope": "sales"}
        )

    async def holds(_: object, __: object, audience: DataAudience, scope: str) -> bool:
        return (
            scope == "sales"
            and audience.people == {world.account_id}
            and not audience.guests
            and not audience.public_only
        )

    monkeypatch.setattr("daimon.adapters.mcp.shared_connections.holds_scope", holds)
    register_scopes(world.runtime)  # pyright: ignore[reportArgumentType]
    yield world
    shared.register_scope_check(None)


_AUDIENCES: list[tuple[str, Callable[[World], DataAudience] | Exception, bool]] = [
    ("the holder in chat", lambda w: _people(w.account_id), True),
    ("Teams outsiders or unknown viewers", ToolError("Data can't be read here."), False),
    ("Teams without consent", RuntimeError("no consent"), False),
    ("viewers capped at public", lambda w: _people(w.account_id, public_only=True), False),
    ("someone else in the room", lambda w: _people(w.account_id, uuid.uuid4()), False),
]


@pytest.mark.parametrize(("case", "audience", "shown"), _AUDIENCES, ids=[a[0] for a in _AUDIENCES])
async def test_a_scoped_tool_follows_the_data_reads_audience(
    scoped: World,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    audience: Callable[[World], DataAudience] | Exception,
    shown: bool,
) -> None:
    """A scoped tool is served only where data reads answer for people who all hold its
    scope; any failure to read the audience hides it."""

    async def audience_for(*_: object) -> DataAudience:
        if isinstance(audience, Exception):
            raise audience
        return audience(scoped)

    monkeypatch.setattr("daimon.adapters.mcp.shared_connections.data_audience_for", audience_for)

    listed = await scoped.listed(scoped.chat())

    assert ("crm_lookup" in listed) is shown, f"{case}: {listed}"
    if shown:
        await _feature(scoped, "off", key="data_sources")
        assert "crm_lookup" not in await scoped.listed(scoped.chat()), "none with data off"


async def test_a_scoped_tool_by_origin(scoped: World) -> None:
    """With the real audience: a key and the hub answer for their person; an outside client
    and an operator token get none. The agent page's listing shows what its viewer holds,
    but its credential can't call it."""
    key = scoped.key()
    page = scoped.chat(chat_session_id=None)
    outside = replace(key, agent_id=None, foreign_home_id="org")
    operator = replace(key, agent_id=None, platform_user_id=None)

    assert "crm_lookup" in await scoped.listed(key), "a key's person holds it"
    assert "crm_lookup" in await scoped.listed(page), "the agent page lists it"
    await scoped.call(page, "crm_lookup")
    assert scoped.upstream == [], "but can't call it"
    for caller in (outside, operator):
        assert "crm_lookup" not in await scoped.listed(caller), caller
    async with Client(_hub(scoped)) as client:
        hub = await client.call_tool("list_shared_tools", {"workspace_id": _GUILD})
    assert "crm_lookup" in str(hub.content), "the hub's person holds it"
