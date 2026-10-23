"""Agent-wide `deny` rules on Daimon's own tools, enforced by the MCP server.

The driver skips rules for the trusted `daimon-mcp` server, so `IdentityMiddleware`
refuses the call instead. These tests drive the real middleware, the real
`call_tool` proxy transform and a real rule store.
"""

from __future__ import annotations

import uuid
from typing import Literal

import pytest
from daimon.adapters.mcp.auth.verifier import SCOPES_CLAIM, TOKEN_KIND_CLAIM
from daimon.adapters.mcp.middleware.mcp_identity import (
    IdentityMiddleware,
    production_agent_id_resolver,
    production_internal_resolver,
    production_is_admin_resolver,
    production_role_resolver,
    production_subject_resolver,
    production_tenant_resolver,
)
from daimon.adapters.mcp.search_transform import AgentChatAwareBM25SearchTransform
from daimon.core.authz import Subject
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.operator_tokens import scope_tag
from daimon.core.stores import agent_tool_rules as store
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.testing.factories import make_tenant
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PUBLIC_URL = "https://mcp.example.com/mcp"
ENTRY_URL = "https://mcp.example.com/mcp/"  # an agent's entry: a trailing slash is the same server
ON = ToolSafetyPolicy(enabled=True, agent_rules=True)
REFUSED = "a tool rule set on this agent blocks post_update"

Rule = Literal["allow", "ask", "deny"]


class _World:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker
        self.ran: list[str] = []
        self.tenant_id = uuid.uuid4()
        self.other_tenant_id = uuid.uuid4()

    async def seed(self) -> None:
        async with self.sessionmaker() as session, session.begin():
            self.tenant_id = (await make_tenant(session)).id
            self.other_tenant_id = (await make_tenant(session)).id

    async def rule(
        self,
        agent: str,
        rule: Rule | None,
        *,
        tool: str = "post_update",
        channel_id: str | None = None,
        url: str = ENTRY_URL,
        tenant_id: uuid.UUID | None = None,
        server: str = "daimon-mcp",
        scope: Literal["chat", "chat_and_routines"] = "chat",
    ) -> None:
        async with self.sessionmaker() as session, session.begin():
            await store.set_rule(
                session,
                tenant_id=tenant_id or self.tenant_id,
                agent_id=agent,
                agent_name=agent,
                tool_key=f"{server}/{tool}",
                channel_id=channel_id,
                server_url=url,
                rule=rule,
                scope=scope,
                set_by="admin",
                actor=Subject(is_admin=True),
            )

    def agent_uuid(self, ma_agent_id: str, tenant_id: uuid.UUID | None = None) -> uuid.UUID:
        return derive_agent_uuid(tenant_id=tenant_id or self.tenant_id, ma_agent_id=ma_agent_id)

    def app(
        self, *, policy: ToolSafetyPolicy | None = ON, public_url: str | None = PUBLIC_URL
    ) -> FastMCP:
        mcp = FastMCP(name="test")
        mcp.add_middleware(
            IdentityMiddleware(
                subject_resolver=production_subject_resolver,
                tenant_resolver=production_tenant_resolver,
                role_resolver=production_role_resolver,
                agent_id_resolver=production_agent_id_resolver,
                is_admin_resolver=production_is_admin_resolver,
                internal_resolver=production_internal_resolver,
                sessionmaker=self.sessionmaker,
                tool_safety=policy,
                public_url=public_url,
            )
        )
        tags = {"agent-chat", scope_tag("tenant:read")}

        @mcp.tool(tags=tags)
        async def post_update() -> str:  # pyright: ignore[reportUnusedFunction]
            self.ran.append("post_update")
            return "posted"

        @mcp.tool(tags=tags)
        async def list_things() -> str:  # pyright: ignore[reportUnusedFunction]
            self.ran.append("list_things")
            return "listed"

        @mcp.tool(tags=tags)
        async def publish_report() -> str:  # pyright: ignore[reportUnusedFunction]
            self.ran.append("publish_report")
            return "published"

        mcp.add_transform(AgentChatAwareBM25SearchTransform(max_results=5))
        return mcp


@pytest.fixture
async def world(sessionmaker: async_sessionmaker[AsyncSession]) -> _World:
    world = _World(sessionmaker)
    await world.seed()
    return world


def _as(monkeypatch: pytest.MonkeyPatch, **claims: object) -> None:
    claims = {"sub": str(uuid.uuid4()), "role": "user", **claims}
    token = AccessToken(token="t", client_id="c", scopes=[], claims=claims)
    monkeypatch.setattr(
        "daimon.adapters.mcp.middleware.mcp_identity.get_access_token", lambda: token
    )


def _chat(monkeypatch: pytest.MonkeyPatch, world: _World, agent: str = "agent-a") -> None:
    _as(
        monkeypatch,
        tenant_id=str(world.tenant_id),
        chat_agent_id=str(world.agent_uuid(agent)),
    )


async def _direct(world: _World, tool: str = "post_update", **kw: object) -> object:
    async with Client(world.app(**kw)) as client:  # type: ignore[arg-type]
        return (await client.call_tool(tool, {})).data


async def _proxied(world: _World, tool: str = "post_update", **kw: object) -> object:
    async with Client(world.app(**kw)) as client:  # type: ignore[arg-type]
        return (await client.call_tool("call_tool", {"name": tool, "arguments": {}})).data


async def test_a_deny_refuses_a_direct_call_and_the_tool_does_not_run(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.rule("agent-a", "deny")
    _chat(monkeypatch, world)
    with pytest.raises(ToolError, match=REFUSED):
        await _direct(world)
    assert world.ran == []


async def test_a_deny_refuses_the_same_call_through_the_call_tool_proxy(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.rule("agent-a", "deny")
    _chat(monkeypatch, world)
    with pytest.raises(ToolError, match=REFUSED):
        await _proxied(world)
    assert world.ran == []


async def test_a_deny_on_one_tool_leaves_the_others_callable_directly_and_proxied(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.rule("agent-a", "deny")
    _chat(monkeypatch, world)
    assert await _direct(world, "list_things") == "listed"
    assert await _proxied(world, "list_things") == "listed"


@pytest.mark.parametrize("scope", ["chat", "chat_and_routines"])
async def test_a_deny_holds_for_either_scope(
    world: _World, monkeypatch: pytest.MonkeyPatch, scope: Literal["chat", "chat_and_routines"]
) -> None:
    """The driver's deny ignores scope, and one chat credential serves chat and routines."""
    await world.rule("agent-a", "deny", scope=scope)
    _chat(monkeypatch, world)
    with pytest.raises(ToolError, match=REFUSED):
        await _direct(world)


async def test_the_refusal_is_audited_as_a_denial(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.rule("agent-a", "deny")
    _chat(monkeypatch, world)
    middleware_ran: list[tuple[str, str]] = []

    from daimon.core.stores.security_audit import SecurityAuditEntry

    original = IdentityMiddleware._queue_audit  # pyright: ignore[reportPrivateUsage]

    def spy(self: IdentityMiddleware, event: SecurityAuditEntry) -> None:
        middleware_ran.append((event.outcome, event.reason))
        original(self, event)

    monkeypatch.setattr(IdentityMiddleware, "_queue_audit", spy)
    with pytest.raises(ToolError, match=REFUSED):
        await _direct(world)
    assert ("denied", "agent_tool_rule_deny") in middleware_ran


@pytest.mark.parametrize(
    "rule",
    [
        pytest.param(None, id="no-rule"),
        pytest.param("allow", id="allow"),
        pytest.param("ask", id="ask"),
    ],
)
async def test_no_rule_allow_ask_or_a_cleared_deny_is_not_refused_by_the_server(
    world: _World, monkeypatch: pytest.MonkeyPatch, rule: Rule | None
) -> None:
    if rule is not None:
        await world.rule("agent-a", rule)
    _chat(monkeypatch, world)
    assert await _direct(world) == "posted"
    assert await _proxied(world) == "posted"


async def test_a_cleared_deny_is_lifted_on_the_next_call(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.rule("agent-a", "deny")
    _chat(monkeypatch, world)
    with pytest.raises(ToolError, match=REFUSED):
        await _direct(world)
    await world.rule("agent-a", None)
    assert await _direct(world) == "posted"


async def test_a_channel_scoped_deny_is_not_applied_by_the_server(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server does not know the channel; channel rules wait for the driver."""
    await world.rule("agent-a", "deny", channel_id="C1")
    _chat(monkeypatch, world)
    assert await _direct(world) == "posted"


async def test_a_channel_allow_does_not_lift_an_agent_wide_deny(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deny is the stricter rule when both match, as in the driver."""
    await world.rule("agent-a", "deny")
    await world.rule("agent-a", "allow", channel_id="C1")
    _chat(monkeypatch, world)
    with pytest.raises(ToolError, match=REFUSED):
        await _direct(world)


async def test_another_agents_deny_does_not_reach_this_agent(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.rule("agent-b", "deny")
    _chat(monkeypatch, world, "agent-a")
    assert await _direct(world) == "posted"
    _chat(monkeypatch, world, "agent-b")
    with pytest.raises(ToolError, match=REFUSED):
        await _direct(world)


async def test_a_deny_in_another_tenant_does_not_leak(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same Managed Agents id, other tenant: a different derived uuid and a tenant-keyed read."""
    await world.rule("agent-a", "deny", tenant_id=world.other_tenant_id)
    _chat(monkeypatch, world, "agent-a")
    assert await _direct(world) == "posted"
    _as(
        monkeypatch,
        tenant_id=str(world.other_tenant_id),
        chat_agent_id=str(world.agent_uuid("agent-a", world.other_tenant_id)),
    )
    with pytest.raises(ToolError, match=REFUSED):
        await _direct(world)


async def test_a_deny_on_another_server_url_named_daimon_mcp_is_not_applied(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A third-party server wearing the reserved name is not this server."""
    await world.rule("agent-a", "deny", url="https://elsewhere.example.com/mcp")
    _chat(monkeypatch, world)
    assert await _direct(world) == "posted"


async def test_a_deny_on_a_differently_named_server_is_not_applied(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.rule("agent-a", "deny", server="linear", url=PUBLIC_URL)
    _chat(monkeypatch, world)
    assert await _direct(world) == "posted"


@pytest.mark.parametrize(
    "entry", ["https://MCP.example.com/mcp", "https://mcp.example.com:443/mcp/"]
)
async def test_the_entry_url_matches_the_public_url_in_canonical_form(
    world: _World, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    await world.rule("agent-a", "deny", url=entry)
    _chat(monkeypatch, world)
    with pytest.raises(ToolError, match=REFUSED):
        await _direct(world)


async def test_an_operator_token_is_unaffected(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.rule("agent-a", "deny")
    _as(
        monkeypatch,
        tenant_id=str(world.tenant_id),
        chat_agent_id=str(world.agent_uuid("agent-a")),
        **{TOKEN_KIND_CLAIM: "operator", SCOPES_CLAIM: ["tenant:read"]},
    )
    assert await _direct(world) == "posted"


async def test_an_agent_key_is_unaffected(world: _World, monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent key carries `agent_id`, not the chat identity; the rule is for chat sessions."""
    await world.rule("agent-a", "deny")
    _as(
        monkeypatch,
        tenant_id=str(world.tenant_id),
        agent_id=str(world.agent_uuid("agent-a")),
        chat_agent_id=str(world.agent_uuid("agent-a")),
    )
    assert await _direct(world) == "posted"
    _as(monkeypatch, tenant_id=str(world.tenant_id), agent_id=str(world.agent_uuid("agent-a")))
    assert await _direct(world) == "posted"


async def test_a_token_with_no_agent_is_unaffected(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.rule("agent-a", "deny")
    _as(monkeypatch, tenant_id=str(world.tenant_id))
    assert await _direct(world) == "posted"


@pytest.mark.parametrize(
    "policy",
    [
        pytest.param(None, id="no-policy"),
        pytest.param(ToolSafetyPolicy(), id="safety-off"),
        pytest.param(ToolSafetyPolicy(enabled=True), id="agent-rules-off"),
        pytest.param(ToolSafetyPolicy(agent_rules=True), id="enabled-off"),
    ],
)
async def test_nothing_is_enforced_while_the_driver_would_not_apply_rules(
    world: _World, monkeypatch: pytest.MonkeyPatch, policy: ToolSafetyPolicy | None
) -> None:
    await world.rule("agent-a", "deny")
    _chat(monkeypatch, world)
    assert await _direct(world, policy=policy) == "posted"
    assert await _proxied(world, policy=policy) == "posted"


async def test_nothing_is_enforced_without_a_public_url(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The driver has no trusted server without one, and the rule row names no server to match."""
    await world.rule("agent-a", "deny")
    _chat(monkeypatch, world)
    assert await _direct(world, public_url=None) == "posted"


@pytest.mark.parametrize("proxy", ["call_tool", "search_tools"])
async def test_the_search_proxies_are_never_refused_by_name(
    world: _World, monkeypatch: pytest.MonkeyPatch, proxy: str
) -> None:
    await world.rule("agent-a", "deny", tool=proxy)
    await world.rule("agent-a", "deny")
    _chat(monkeypatch, world)
    async with Client(world.app()) as client:
        await client.call_tool("search_tools", {"query": "list things"})
        result = await client.call_tool("call_tool", {"name": "list_things", "arguments": {}})
        assert result.data == "listed"


async def test_a_failed_rule_read_refuses_the_call(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_a: object, **_k: object) -> list[store.AgentToolRule]:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "deny_rules_for_trusted_tool", broken)
    _chat(monkeypatch, world)
    with pytest.raises(ToolError, match="could not check this agent's tool rules"):
        await _direct(world)
    assert world.ran == []


# The `daimon-mcp/*` wildcard: a default-deny for the agent's whole Daimon surface.

ALL_REFUSED = "a tool rule set on this agent blocks all Daimon tools"


async def test_a_wildcard_deny_refuses_every_call_including_the_search_proxies(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.rule("agent-a", "deny", tool="*")
    _chat(monkeypatch, world)
    async with Client(world.app()) as client:
        with pytest.raises(ToolError, match=ALL_REFUSED):
            await client.call_tool("search_tools", {"query": "list things"})
        with pytest.raises(ToolError, match=ALL_REFUSED):
            await client.call_tool("call_tool", {"name": "list_things", "arguments": {}})
        with pytest.raises(ToolError, match=ALL_REFUSED):
            await client.call_tool("list_things", {})
        # A pinned approval tool: listed under its own name, called directly.
        with pytest.raises(ToolError, match=ALL_REFUSED):
            await client.call_tool("publish_report", {})
    assert world.ran == []


async def test_a_wildcard_deny_refuses_the_inner_call_even_when_the_proxy_is_not_the_refuser(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both the proxy and the inner call run the middleware, and each is refused."""
    await world.rule("agent-a", "deny", tool="*")
    _chat(monkeypatch, world)
    with pytest.raises(ToolError, match=ALL_REFUSED):
        await _proxied(world)
    with pytest.raises(ToolError, match=ALL_REFUSED):
        await _direct(world)
    assert world.ran == []


async def test_a_wildcard_deny_for_another_agent_or_tenant_does_not_reach_this_agent(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.rule("agent-b", "deny", tool="*")
    await world.rule("agent-a", "deny", tool="*", tenant_id=world.other_tenant_id)
    _chat(monkeypatch, world, "agent-a")
    assert await _direct(world) == "posted"
    assert await _proxied(world) == "posted"
    _chat(monkeypatch, world, "agent-b")
    with pytest.raises(ToolError, match=ALL_REFUSED):
        await _direct(world)


async def test_a_channel_scoped_wildcard_and_non_deny_wildcards_are_ignored(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.rule("agent-a", "deny", tool="*", channel_id="C1")
    _chat(monkeypatch, world)
    assert await _direct(world) == "posted"
    for rule in ("allow", "ask", None):
        await world.rule("agent-a", rule, tool="*")
        assert await _proxied(world) == "posted"


async def test_a_wildcard_on_another_server_or_url_is_not_applied(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wildcards are recognised only for `daimon-mcp` at this deployment's URL."""
    await world.rule("agent-a", "deny", tool="*", server="linear", url=PUBLIC_URL)
    await world.rule("agent-a", "deny", tool="*", url="https://elsewhere.example.com/mcp")
    _chat(monkeypatch, world)
    assert await _direct(world) == "posted"
    assert await _proxied(world) == "posted"


@pytest.mark.parametrize("kind", ["operator", "agent-key", "no-agent"])
async def test_a_wildcard_deny_leaves_operator_agent_key_and_agentless_tokens_alone(
    world: _World, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    await world.rule("agent-a", "deny", tool="*")
    agent = str(world.agent_uuid("agent-a"))
    claims: dict[str, object] = {"tenant_id": str(world.tenant_id)}
    if kind == "operator":
        claims |= {
            "chat_agent_id": agent,
            TOKEN_KIND_CLAIM: "operator",
            SCOPES_CLAIM: ["tenant:read"],
        }
    elif kind == "agent-key":
        claims |= {"agent_id": agent, "chat_agent_id": agent}
    _as(monkeypatch, **claims)
    assert await _direct(world) == "posted"


async def test_a_wildcard_deny_is_not_enforced_while_rules_are_off_and_fails_closed_on_read_error(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.rule("agent-a", "deny", tool="*")
    _chat(monkeypatch, world)
    assert await _direct(world, policy=ToolSafetyPolicy(enabled=True)) == "posted"
    assert await _direct(world, public_url=None) == "posted"

    async def broken(*_a: object, **_k: object) -> list[store.AgentToolRule]:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "deny_rules_for_trusted_tool", broken)
    async with Client(world.app()) as client:
        with pytest.raises(ToolError, match="could not check this agent's tool rules"):
            await client.call_tool("search_tools", {"query": "x"})


async def test_a_specific_deny_still_exempts_the_proxies_and_refuses_the_inner_call(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.rule("agent-a", "deny")
    _chat(monkeypatch, world)
    async with Client(world.app()) as client:
        await client.call_tool("search_tools", {"query": "list things"})
        with pytest.raises(ToolError, match=REFUSED):
            await client.call_tool("call_tool", {"name": "post_update", "arguments": {}})
    assert world.ran == []


def test_the_set_agent_tool_rule_tool_resolves_the_wildcard_key_to_the_agents_daimon_url() -> None:
    from types import SimpleNamespace
    from typing import cast

    from daimon.adapters.mcp.tools.agent_tool_rules import (
        _server_url,  # pyright: ignore[reportPrivateUsage]
    )

    agent = SimpleNamespace(mcp_servers=[SimpleNamespace(name="daimon-mcp", url=ENTRY_URL)])
    assert _server_url(cast("object", agent), "daimon-mcp/*") == ENTRY_URL  # type: ignore[arg-type]
    with pytest.raises(ToolError, match="exactly server/tool"):
        _server_url(cast("object", agent), "daimon-mcp/a/*")  # type: ignore[arg-type]
