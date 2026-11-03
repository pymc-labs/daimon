"""Agent tool rule precedence, live reads and revision history."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import cast

import pytest
from daimon.core._models import AgentToolRuleHistoryRecord, AgentToolRuleRecord, Tenant
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Place, Subject, authorize
from daimon.core.confirmation import (
    ConfirmationAnswer,
    ConfirmationPrompt,
    ToolRuleCardReference,
)
from daimon.core.stores import agent_tool_rules as store
from daimon.core.stores.agent_tool_rules import valid_tool_name
from daimon.core.tool_safety import (
    CONFIRMED_DAIMON_TOOLS,
    PUBLISH_TOOLS,
    ResolvedAgentToolRule,
    ToolCall,
    ToolSafetyPolicy,
    decide_tool_call,
    proxy_refusal,
    stricter_agent_tool_rule,
)
from daimon.core.turn.approvals import (
    agent_rule_lookup,
    channel_for_tool_rules,
    daimon_rule_tools_for,
    interactive_decider,
    unattended_decider,
)
from sqlalchemy import Index, Table, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _call(name: str = "append_note", server: str = "remote") -> ToolCall:
    return ToolCall(tool_use_id="one", server_name=server, tool_name=name)


@pytest.mark.parametrize(
    ("rule", "attended", "scope", "expected"),
    [
        ("allow", True, "chat", "allow"),
        ("allow", False, "chat", "allow"),
        ("allow", False, "chat_and_routines", "allow"),
        ("ask", True, "chat", "ask"),
        ("ask", False, "chat_and_routines", "deny"),
        ("deny", True, "chat", "deny"),
        ("deny", False, "chat", "deny"),
    ],
)
def test_rule_decision(rule: str, attended: bool, scope: str, expected: str) -> None:
    policy = ToolSafetyPolicy(enabled=True, agent_rules=True, unattended_writes=("*",))
    resolved = ResolvedAgentToolRule.model_validate({"rule": rule, "scope": scope})
    assert (
        decide_tool_call(policy, _call(), attended=attended, agent_rule=resolved).outcome
        == expected
    )


def test_rule_precedence_and_own_tools() -> None:
    allow = ResolvedAgentToolRule(rule="allow")
    denied = ToolSafetyPolicy(enabled=True, agent_rules=True, denied=("remote/append_note",))
    assert (
        decide_tool_call(denied, _call(), attended=True, agent_rule=allow).reason
        == "denied_by_operator"
    )
    ask = ResolvedAgentToolRule(rule="ask")
    policy = ToolSafetyPolicy(enabled=True, agent_rules=True)
    assert (
        decide_tool_call(policy, _call("list_items"), attended=True, agent_rule=ask).outcome
        == "ask"
    )
    # Daimon's own tools follow the rule too: an agent ask pauses a tool that
    # would run (the preview of add_skill), and never loosens a fixed gate.
    trusted_ask = decide_tool_call(
        policy,
        _call("add_skill", "daimon-mcp"),
        attended=True,
        agent_rule=ask,
        trusted_servers=frozenset({"daimon-mcp"}),
    )
    assert (trusted_ask.outcome, trusted_ask.reason) == ("ask", "agent_rule_ask")
    off = ToolSafetyPolicy(enabled=True)
    assert decide_tool_call(off, _call(), attended=True, agent_rule=allow) == decide_tool_call(
        off, _call(), attended=True
    )
    chat_only = ResolvedAgentToolRule(rule="allow", scope="chat")
    assert decide_tool_call(policy, _call(), attended=False, agent_rule=chat_only).outcome == "deny"
    pinned_write = ToolSafetyPolicy(
        enabled=True, agent_rules=True, effects={"remote/append_note": "write"}
    )
    assert (
        decide_tool_call(pinned_write, _call(), attended=True, agent_rule=allow).outcome == "allow"
    )
    pinned_read = ToolSafetyPolicy(
        enabled=True, agent_rules=True, effects={"remote/append_note": "read"}
    )
    assert decide_tool_call(pinned_read, _call(), attended=True, agent_rule=ask).outcome == "ask"


@pytest.mark.parametrize(
    ("global_rule", "channel_rule", "expected"),
    [
        ("allow", "allow", "allow"),
        ("allow", "ask", "ask"),
        ("allow", "deny", "deny"),
        ("ask", "allow", "ask"),
        ("ask", "deny", "deny"),
        ("deny", "allow", "deny"),
        ("deny", "ask", "deny"),
    ],
)
def test_stricter_rule_wins(global_rule: str, channel_rule: str, expected: str) -> None:
    merged = stricter_agent_tool_rule(
        ResolvedAgentToolRule.model_validate({"rule": global_rule}),
        ResolvedAgentToolRule.model_validate({"rule": channel_rule}),
    )
    assert merged is not None and merged.rule == expected


def test_rule_channel_is_parent_and_dm_is_excluded() -> None:
    assert (
        channel_for_tool_rules(platform="discord", channel_id="parent", private_dm_id=None)
        == "parent"
    )
    assert (
        channel_for_tool_rules(platform="slack", channel_id="C123:1234.5678", private_dm_id=None)
        == "C123"
    )
    assert (
        channel_for_tool_rules(platform="discord", channel_id="source", private_dm_id="dm") is None
    )

    assert (
        channel_for_tool_rules(
            platform="slack", channel_id="C123:1234.5678", private_dm_id="routine:job"
        )
        == "C123"
    )
    assert (
        channel_for_tool_rules(platform="discord", channel_id=None, private_dm_id="routine:job")
        is None
    )


def test_nullable_global_key_has_unique_index() -> None:
    indexes: dict[str, Index] = {
        index.name: index
        for index in cast(Table, AgentToolRuleRecord.__table__).indexes
        if index.name is not None
    }
    assert indexes["uq_agent_tool_rules_global"].unique
    assert str(indexes["uq_agent_tool_rules_global"].dialect_options["postgresql"]["where"]) == (
        "channel_id IS NULL"
    )
    assert indexes["uq_agent_tool_rules_channel"].unique


@pytest.mark.asyncio
async def test_live_lookup_sees_clear_on_next_call() -> None:
    # Replays ToolRulesSnapshot: same decider, rule cleared between calls.
    current: ResolvedAgentToolRule | None = ResolvedAgentToolRule(rule="allow")
    lookups = 0

    async def lookup(_call: ToolCall) -> ResolvedAgentToolRule | None:
        nonlocal lookups
        lookups += 1
        return current

    decider = interactive_decider(
        ToolSafetyPolicy(enabled=True, agent_rules=True),
        requester_platform_user_id="human",
        rules_for=lookup,
    )
    first = await decider(_call())
    assert first.allow
    current = None
    second = await decider(_call())
    assert not second.allow
    assert lookups == 2


@pytest.mark.asyncio
async def test_lookup_error_asks_in_chat_and_refuses_unattended() -> None:
    async def failed(_call: ToolCall) -> ResolvedAgentToolRule | None:
        raise RuntimeError("database down")

    policy = ToolSafetyPolicy(enabled=True, agent_rules=True)
    asked: list[ConfirmationPrompt] = []

    async def confirm(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        asked.append(prompt)
        return "denied"

    chat = interactive_decider(
        policy, requester_platform_user_id="human", rules_for=failed, confirm=confirm
    )
    routine = unattended_decider(policy, rules_for=failed)
    assert not (await chat(_call("list_items"))).allow
    assert len(asked) == 1
    assert not (await routine(_call("list_items"))).allow


async def test_deny_set_while_card_waits_refuses_approved_call() -> None:
    """An explicit deny committed during the card wait wins before release."""
    current: ResolvedAgentToolRule | None = None
    reads = 0

    async def lookup(_call: ToolCall) -> ResolvedAgentToolRule | None:
        nonlocal reads
        reads += 1
        return current

    async def confirm(_prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        nonlocal current
        current = ResolvedAgentToolRule(rule="deny")
        return "approved"

    decide = interactive_decider(
        ToolSafetyPolicy(enabled=True, agent_rules=True),
        requester_platform_user_id="human",
        rules_for=lookup,
        confirm=confirm,
    )
    result = await decide(_call())
    assert not result.allow and "denies remote/append_note" in (result.deny_message or "")
    assert reads == 2


async def test_always_button_only_for_classification_asks() -> None:
    prompts: list[ConfirmationPrompt] = []
    reference = ToolRuleCardReference(
        tenant_id=uuid.uuid4(),
        agent_id="ma-1",
        agent_name="Helper",
        tool_key="remote/append_note",
        server_url="https://example.test/mcp",
    )

    async def confirm(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "denied"

    async def rule(_call: ToolCall) -> ResolvedAgentToolRule | None:
        return ResolvedAgentToolRule(rule="ask")

    def card_for(_tool: ToolCall) -> ToolRuleCardReference:
        return reference

    await interactive_decider(
        ToolSafetyPolicy(enabled=True, agent_rules=True),
        requester_platform_user_id="human",
        confirm=confirm,
        rule_card_for=card_for,
        rules_for=rule,
    )(_call("list_items"))
    assert prompts[-1].tool_rule is None and prompts[-1].tool_rules_enabled

    async def failed_rule(_tool: ToolCall) -> ResolvedAgentToolRule | None:
        raise RuntimeError("database unavailable")

    await interactive_decider(
        ToolSafetyPolicy(enabled=True, agent_rules=True),
        requester_platform_user_id="human",
        confirm=confirm,
        rule_card_for=card_for,
        rules_for=failed_rule,
    )(_call("list_items"))
    assert prompts[-1].tool_rule is None
    await interactive_decider(
        ToolSafetyPolicy(enabled=True, agent_rules=True),
        requester_platform_user_id="human",
        confirm=confirm,
        rule_card_for=card_for,
    )(_call())
    assert prompts[-1].tool_rule == reference
    await interactive_decider(
        ToolSafetyPolicy(enabled=True),
        requester_platform_user_id="human",
        confirm=confirm,
        rule_card_for=card_for,
    )(_call())
    assert prompts[-1].tool_rule is None and not prompts[-1].tool_rules_enabled
    await interactive_decider(
        ToolSafetyPolicy(enabled=True, agent_rules=True),
        trusted_servers=frozenset({"daimon-mcp"}),
        confirmed_tools=frozenset({"add_skill"}),
        requester_platform_user_id="human",
        confirm=confirm,
        rule_card_for=card_for,
    )(_call("add_skill", "daimon-mcp"))
    assert prompts[-1].tool_rule is None


def test_only_human_admin_may_edit() -> None:
    policy = TenantAccessPolicy()
    assert authorize(policy, subject=Subject(is_admin=True), action=Action.SET_AGENT_TOOL_RULE)
    assert not authorize(policy, subject=Subject(is_admin=False), action=Action.SET_AGENT_TOOL_RULE)
    assert not authorize(
        policy,
        subject=Subject(is_admin=True, via_agent_key=True),
        action=Action.SET_AGENT_TOOL_RULE,
    )
    member = Subject(platform_user_id="member")
    assert not authorize(
        policy,
        subject=member,
        action=Action.SET_CHANNEL_AGENT_TOOL_RULE,
        place=Place(channel_id="home"),
    )
    assert not authorize(
        policy,
        subject=member,
        action=Action.SET_CHANNEL_AGENT_TOOL_RULE,
        place=Place(channel_id="other"),
    )
    assert not authorize(
        policy,
        subject=member,
        action=Action.SET_AGENT_TOOL_RULE,
    )
    assert not authorize(
        policy,
        subject=Subject(is_admin=True, via_agent_key=True),
        action=Action.SET_CHANNEL_AGENT_TOOL_RULE,
        place=Place(channel_id="home"),
    )


@pytest.mark.asyncio
async def test_store_refuses_agent_key_before_touching_database() -> None:
    with pytest.raises(PermissionError):
        await store.set_rule(
            cast(AsyncSession, None),
            tenant_id=uuid.uuid4(),
            agent_id="ma-1",
            agent_name="one",
            tool_key="remote/append_note",
            server_url="https://one.example/mcp",
            rule="allow",
            set_by="agent",
            actor=Subject(is_admin=True, via_agent_key=True),
        )
    with pytest.raises(PermissionError):
        await store.set_rule(
            cast(AsyncSession, None),
            tenant_id=uuid.uuid4(),
            agent_id="ma-1",
            agent_name="one",
            tool_key="remote/append_note",
            channel_id="other",
            server_url="https://one.example/mcp",
            rule="allow",
            set_by="member",
            actor=Subject(platform_user_id="member"),
        )
    with pytest.raises(PermissionError):
        await store.set_rule(
            cast(AsyncSession, None),
            tenant_id=uuid.uuid4(),
            agent_id="ma-1",
            agent_name="one",
            tool_key="remote/append_note",
            server_url="https://one.example/mcp",
            rule="allow",
            set_by="member",
            actor=Subject(platform_user_id="member"),
        )


@pytest.mark.asyncio
async def test_store_binding_history_and_clear_cas(db_session: AsyncSession) -> None:
    tenant = Tenant(platform="discord", external_id=str(uuid.uuid4()))
    other = Tenant(platform="discord", external_id=str(uuid.uuid4()))
    db_session.add_all([tenant, other])
    await db_session.flush()
    await db_session.refresh(tenant)
    await db_session.refresh(other)
    key = "remote/append_note"
    row = await store.set_rule(
        db_session,
        tenant_id=tenant.id,
        agent_id="ma-1",
        agent_name="one",
        tool_key=key,
        server_url="https://one.example/mcp",
        rule="allow",
        set_by="admin",
        actor=Subject(is_admin=True),
        expected_revision=0,
    )
    assert row.revision == 1
    assert await store.get_for_call(
        db_session,
        tenant_id=tenant.id,
        agent_id="ma-1",
        tool_key=key,
        server_url="https://one.example/mcp",
    ) == ResolvedAgentToolRule(rule="allow")
    scoped = await store.set_rule(
        db_session,
        tenant_id=tenant.id,
        agent_id="ma-1",
        agent_name="one",
        tool_key=key,
        channel_id="home",
        server_url="https://one.example/mcp",
        rule="deny",
        set_by="admin",
        actor=Subject(is_admin=True),
        expected_revision=0,
    )
    assert scoped.revision == 1 and scoped.channel_id == "home"
    assert await store.get_for_call(
        db_session,
        tenant_id=tenant.id,
        agent_id="ma-1",
        tool_key=key,
        server_url="https://one.example/mcp",
        channel_id=channel_for_tool_rules(
            platform="discord", channel_id="home", private_dm_id=None
        ),
    ) == ResolvedAgentToolRule(rule="deny")
    assert await store.get_for_call(
        db_session,
        tenant_id=tenant.id,
        agent_id="ma-1",
        tool_key=key,
        server_url="https://one.example/mcp",
        channel_id="other",
    ) == ResolvedAgentToolRule(rule="allow")
    for tenant_id, agent_id, url in (
        (other.id, "ma-1", "https://one.example/mcp"),
        (tenant.id, "ma-2", "https://one.example/mcp"),
        (tenant.id, "ma-1", "https://two.example/mcp"),
    ):
        assert (
            await store.get_for_call(
                db_session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                tool_key=key,
                server_url=url,
            )
            is None
        )
    cleared = await store.clear_rule(
        db_session,
        tenant_id=tenant.id,
        agent_id="ma-1",
        agent_name="one",
        tool_key=key,
        server_url="https://one.example/mcp",
        set_by="admin",
        actor=Subject(is_admin=True),
        expected_revision=1,
    )
    assert cleared.revision == 2 and cleared.rule is None
    with pytest.raises(store.StaleAgentToolRule):
        await store.set_rule(
            db_session,
            tenant_id=tenant.id,
            agent_id="ma-1",
            agent_name="one",
            tool_key=key,
            server_url="https://one.example/mcp",
            rule="allow",
            set_by="admin",
            actor=Subject(is_admin=True),
            expected_revision=1,
        )
    history = (
        await db_session.scalars(
            select(AgentToolRuleHistoryRecord).order_by(AgentToolRuleHistoryRecord.id)
        )
    ).all()
    assert [(r.channel_id, r.before_rule, r.after_rule) for r in history] == [
        (None, None, "allow"),
        ("home", None, "deny"),
        (None, "allow", None),
    ]
    assert history[0].before_server_url is None
    assert history[2].before_server_url == "https://one.example/mcp"


@pytest.mark.asyncio
async def test_deny_rules_for_trusted_tool_returns_only_agent_wide_denies_of_the_tenant(
    db_session: AsyncSession,
) -> None:
    tenant = Tenant(platform="discord", external_id=str(uuid.uuid4()))
    other = Tenant(platform="discord", external_id=str(uuid.uuid4()))
    db_session.add_all([tenant, other])
    await db_session.flush()
    key = "daimon-mcp/post_update"
    url = "https://mcp.example/mcp"

    async def put(
        tenant_id: uuid.UUID,
        agent: str,
        rule: store.Rule | None,
        *,
        tool_key: str = key,
        channel_id: str | None = None,
    ) -> None:
        await store.set_rule(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent,
            agent_name=agent,
            tool_key=tool_key,
            channel_id=channel_id,
            server_url=url,
            rule=rule,
            scope="chat_and_routines",
            set_by="admin",
            actor=Subject(is_admin=True),
        )

    await put(tenant.id, "ma-deny", "deny")
    await put(tenant.id, "ma-other-agent", "deny")
    await put(tenant.id, "ma-allow", "allow")
    await put(tenant.id, "ma-ask", "ask")
    await put(tenant.id, "ma-cleared", "deny")
    await put(tenant.id, "ma-cleared", None)
    await put(tenant.id, "ma-channel", "deny", channel_id="home")
    await put(tenant.id, "ma-other-tool", "deny", tool_key="daimon-mcp/list_things")
    await put(other.id, "ma-foreign", "deny")

    rows = await store.deny_rules_for_trusted_tool(db_session, tenant_id=tenant.id, tool_keys=[key])

    assert sorted(row.agent_id for row in rows) == ["ma-deny", "ma-other-agent"]
    assert {(row.rule, row.channel_id, row.server_url, row.scope) for row in rows} == {
        ("deny", None, url, "chat_and_routines")
    }
    foreign = await store.deny_rules_for_trusted_tool(
        db_session, tenant_id=other.id, tool_keys=[key]
    )
    assert [row.agent_id for row in foreign] == ["ma-foreign"]


@pytest.mark.asyncio
async def test_the_wildcard_deny_saves_and_is_read_with_the_tools_own_rows(
    db_session: AsyncSession,
) -> None:
    """`daimon-mcp/*` passes the store's `server/tool` check and the `LIKE '%/%'` constraint."""
    tenant = Tenant(platform="discord", external_id=str(uuid.uuid4()))
    other = Tenant(platform="discord", external_id=str(uuid.uuid4()))
    db_session.add_all([tenant, other])
    await db_session.flush()
    url = "https://mcp.example/mcp"

    async def put(
        tenant_id: uuid.UUID,
        agent: str,
        tool_key: str,
        rule: store.Rule | None,
        channel_id: str | None = None,
    ) -> None:
        await store.set_rule(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent,
            agent_name=agent,
            tool_key=tool_key,
            channel_id=channel_id,
            server_url=url,
            rule=rule,
            set_by="admin",
            actor=Subject(is_admin=True),
        )

    await put(tenant.id, "ma-all", "daimon-mcp/*", "deny")
    await put(tenant.id, "ma-one", "daimon-mcp/post_update", "deny")
    await put(tenant.id, "ma-allow-all", "daimon-mcp/*", "allow")
    await put(tenant.id, "ma-channel", "daimon-mcp/*", "deny", channel_id="home")
    await put(tenant.id, "ma-cleared", "daimon-mcp/*", "deny")
    await put(tenant.id, "ma-cleared", "daimon-mcp/*", None)
    await put(tenant.id, "ma-third-party", "linear/*", "deny")
    await put(other.id, "ma-foreign", "daimon-mcp/*", "deny")

    keys = ["daimon-mcp/post_update", "daimon-mcp/*"]
    rows = await store.deny_rules_for_trusted_tool(db_session, tenant_id=tenant.id, tool_keys=keys)

    assert {(row.agent_id, row.tool_key) for row in rows} == {
        ("ma-all", "daimon-mcp/*"),
        ("ma-one", "daimon-mcp/post_update"),
    }
    only_wildcard = await store.deny_rules_for_trusted_tool(
        db_session, tenant_id=tenant.id, tool_keys=["daimon-mcp/*"]
    )
    assert [row.agent_id for row in only_wildcard] == ["ma-all"]


# --- daimon's own tools, through the real store --------------------------------

_DAIMON = "daimon-mcp"
_DAIMON_URL = "https://mcp.example.test/mcp"
_ON = ToolSafetyPolicy(enabled=True, agent_rules=True)


def _proxy(inner: str, **arguments: object) -> ToolCall:
    return ToolCall(
        tool_use_id="tu_p",
        server_name=_DAIMON,
        tool_name="call_tool",
        input={"name": inner, "arguments": arguments},
    )


async def _tenant(db_session: AsyncSession) -> uuid.UUID:
    tenant = Tenant(platform="discord", external_id=str(uuid.uuid4()))
    db_session.add(tenant)
    await db_session.flush()
    await db_session.refresh(tenant)
    return tenant.id


async def _set(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
    tool: str,
    rule: store.Rule | None,
    *,
    channel_id: str | None = None,
    agent_id: str = "ma-1",
    url: str = _DAIMON_URL,
    key: str | None = None,
) -> None:
    await store.set_rule(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        agent_name="one",
        tool_key=key or f"{_DAIMON}/{tool}",
        channel_id=channel_id,
        server_url=url,
        rule=rule,
        set_by="admin",
        actor=Subject(is_admin=True),
    )


def _lookup(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    channel_id: str | None = None,
    server_url: str = _DAIMON_URL,
    also: str | None = None,
):
    return agent_rule_lookup(
        factory,
        tenant_id=tenant_id,
        agent_id="ma-1",
        server_urls={_DAIMON: server_url},
        channel_id=channel_id,
        also_urls={_DAIMON: also} if also else None,
    )


async def test_the_agents_daimon_rule_tools_are_the_ask_and_deny_rows_only(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _tenant(db_session)
    other_tenant = await _tenant(db_session)
    await _set(db_session, tenant_id, "hubspot_update_objects", "ask")
    await _set(db_session, tenant_id, "discord_send_message", "deny", channel_id="home")
    await _set(db_session, tenant_id, "get_session", "allow")
    await _set(db_session, tenant_id, "routine_list", "deny")
    await _set(db_session, tenant_id, "routine_list", None)
    await _set(db_session, tenant_id, "", "deny", key="linear/create_issue")
    await _set(db_session, tenant_id, "list_events", "deny", agent_id="ma-2")
    await _set(db_session, other_tenant, "list_events", "deny")
    assert await store.daimon_rule_tools(db_session, tenant_id=tenant_id, agent_id="ma-1") == {
        "hubspot_update_objects",
        "discord_send_message",
    }
    assert await daimon_rule_tools_for(
        db_session_factory, _ON, tenant_id=tenant_id, agent_id="ma-1"
    ) == {"hubspot_update_objects", "discord_send_message"}
    # A rule is inert until tool safety and the rules switch are both on: no read at all.
    for policy in (ToolSafetyPolicy(), ToolSafetyPolicy(enabled=True)):
        assert (
            await daimon_rule_tools_for(
                db_session_factory, policy, tenant_id=tenant_id, agent_id="ma-1"
            )
            == frozenset()
        )


async def test_an_ask_rule_on_a_proxied_daimon_tool_shows_a_card_naming_that_tool(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _tenant(db_session)
    await _set(db_session, tenant_id, "hubspot_update_objects", "ask")
    prompts: list[ConfirmationPrompt] = []
    reference = ToolRuleCardReference(
        tenant_id=tenant_id,
        agent_id="ma-1",
        agent_name="one",
        tool_key=f"{_DAIMON}/hubspot_update_objects",
        server_url=_DAIMON_URL,
    )

    async def confirm(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "approved"

    decide = interactive_decider(
        _ON,
        requester_platform_user_id="human",
        confirm=confirm,
        trusted_servers=frozenset({_DAIMON}),
        daimon_rules=True,
        rules_for=_lookup(db_session_factory, tenant_id),
        rule_card_for=lambda _call: reference,
    )
    result = await decide(_proxy("hubspot_update_objects", title="Renewal"))
    assert result.allow
    assert len(prompts) == 1
    assert prompts[0].title == 'Hubspot update objects "Renewal"?'
    assert "Call tool" not in prompts[0].title
    assert prompts[0].tool_rule is None, "no Always allow on daimon's own tools"
    # An unruled tool through the same proxy runs without a card.
    free = await decide(_proxy("get_session"))
    assert free.allow and len(prompts) == 1


async def test_a_deny_rule_on_a_proxied_daimon_tool_refuses_without_a_card(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _tenant(db_session)
    await _set(db_session, tenant_id, "hubspot_update_objects", "deny")
    prompts: list[ConfirmationPrompt] = []

    async def confirm(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "approved"

    for decide in (
        interactive_decider(
            _ON,
            requester_platform_user_id="human",
            confirm=confirm,
            trusted_servers=frozenset({_DAIMON}),
            daimon_rules=True,
            rules_for=_lookup(db_session_factory, tenant_id),
        ),
        unattended_decider(
            _ON,
            trusted_servers=frozenset({_DAIMON}),
            daimon_rules=True,
            rules_for=_lookup(db_session_factory, tenant_id),
        ),
    ):
        result = await decide(_proxy("hubspot_update_objects"))
        assert not result.allow
        assert "denies daimon-mcp/hubspot_update_objects" in (result.deny_message or "")
    assert prompts == []


async def test_a_deny_set_while_a_daimon_card_waits_refuses_the_approved_call(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _tenant(db_session)
    await _set(db_session, tenant_id, "hubspot_update_objects", "ask")

    async def confirm(_prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        await _set(db_session, tenant_id, "hubspot_update_objects", "deny")
        return "approved"

    decide = interactive_decider(
        _ON,
        requester_platform_user_id="human",
        confirm=confirm,
        trusted_servers=frozenset({_DAIMON}),
        daimon_rules=True,
        rules_for=_lookup(db_session_factory, tenant_id),
    )
    result = await decide(_proxy("hubspot_update_objects"))
    assert not result.allow
    assert "denies daimon-mcp/hubspot_update_objects" in (result.deny_message or "")


async def test_an_unattended_ask_rule_on_a_daimon_tool_is_refused(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _tenant(db_session)
    await _set(db_session, tenant_id, "hubspot_update_objects", "ask")
    decide = unattended_decider(
        _ON,
        trusted_servers=frozenset({_DAIMON}),
        daimon_rules=True,
        rules_for=_lookup(db_session_factory, tenant_id),
    )
    result = await decide(_proxy("hubspot_update_objects"))
    assert not result.allow and "requires approval" in (result.deny_message or "")
    assert (await decide(_proxy("get_session"))).allow


async def test_a_channel_rule_on_a_daimon_tool_applies_only_in_its_channel(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _tenant(db_session)
    await _set(db_session, tenant_id, "discord_send_message", "deny", channel_id="home")
    for channel, allowed in (("home", False), ("elsewhere", True), (None, True)):
        decide = unattended_decider(
            _ON,
            trusted_servers=frozenset({_DAIMON}),
            daimon_rules=True,
            rules_for=_lookup(db_session_factory, tenant_id, channel_id=channel),
        )
        assert (await decide(_proxy("discord_send_message"))).allow is allowed, channel


async def test_a_daimon_rule_applies_through_either_endpoint_the_session_could_use(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A foreign URL under the reserved name is healed to the deployment's own."""
    tenant_id = await _tenant(db_session)
    await _set(db_session, tenant_id, "hubspot_update_objects", "deny")
    decide = unattended_decider(
        _ON,
        trusted_servers=frozenset({_DAIMON}),
        daimon_rules=True,
        rules_for=_lookup(
            db_session_factory,
            tenant_id,
            server_url="https://foreign.example/mcp",
            also=_DAIMON_URL,
        ),
    )
    assert not (await decide(_proxy("hubspot_update_objects"))).allow
    unbound = unattended_decider(
        _ON,
        trusted_servers=frozenset({_DAIMON}),
        daimon_rules=True,
        rules_for=_lookup(db_session_factory, tenant_id, server_url="https://foreign.example/mcp"),
    )
    assert (await unbound(_proxy("hubspot_update_objects"))).allow


async def test_a_daimon_publish_card_still_asks_when_the_agent_allows_it(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _tenant(db_session)
    await _set(db_session, tenant_id, "publish_report", "allow")
    prompts: list[ConfirmationPrompt] = []

    async def confirm(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "denied"

    decide = interactive_decider(
        _ON,
        requester_platform_user_id="human",
        confirm=confirm,
        trusted_servers=frozenset({_DAIMON}),
        daimon_rules=True,
        rules_for=_lookup(db_session_factory, tenant_id),
    )
    result = await decide(
        ToolCall(tool_use_id="p", server_name=_DAIMON, tool_name="publish_report", input={})
    )
    assert not result.allow and len(prompts) == 1


# --- the `daimon-mcp/*` wildcard in the driver ---------------------------------


async def test_only_an_agent_wide_wildcard_deny_counts_as_a_daimon_rule_tool(
    db_session: AsyncSession,
) -> None:
    tenant_id = await _tenant(db_session)
    await _set(db_session, tenant_id, "*", "ask")
    await _set(db_session, tenant_id, "*", "deny", channel_id="home")
    await _set(db_session, tenant_id, "*", "allow", agent_id="ma-3")
    assert await store.daimon_rule_tools(db_session, tenant_id=tenant_id, agent_id="ma-1") == set()
    await _set(db_session, tenant_id, "*", "deny", agent_id="ma-2")
    await _set(db_session, tenant_id, "get_session", "ask", agent_id="ma-2")
    assert await store.daimon_rule_tools(db_session, tenant_id=tenant_id, agent_id="ma-2") == {
        "*",
        "get_session",
    }


async def test_a_wildcard_deny_refuses_every_proxied_daimon_call_in_the_driver(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _tenant(db_session)
    await _set(db_session, tenant_id, "*", "deny")
    lookup = agent_rule_lookup(
        db_session_factory,
        tenant_id=tenant_id,
        agent_id="ma-1",
        server_urls={_DAIMON: _DAIMON_URL},
        also_urls={_DAIMON: _DAIMON_URL},
    )
    for decide in (
        interactive_decider(
            _ON,
            requester_platform_user_id="human",
            trusted_servers=frozenset({_DAIMON}),
            daimon_rules=True,
            rules_for=lookup,
        ),
        unattended_decider(
            _ON, trusted_servers=frozenset({_DAIMON}), daimon_rules=True, rules_for=lookup
        ),
    ):
        for inner in ("get_session", "hubspot_update_objects"):
            result = await decide(_proxy(inner))
            assert not result.allow
            assert f"denies daimon-mcp/{inner}" in (result.deny_message or "")


async def test_a_wildcard_row_does_nothing_for_other_servers_or_without_the_daimon_endpoint(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _tenant(db_session)
    await _set(db_session, tenant_id, "*", "deny")
    # No `also_urls`: the deployment named no daimon endpoint, so there is no built-in server.
    bare = unattended_decider(
        _ON,
        trusted_servers=frozenset({_DAIMON}),
        daimon_rules=True,
        rules_for=_lookup(db_session_factory, tenant_id),
    )
    assert (await bare(_proxy("get_session"))).allow
    # A `*` row on another server is not a wildcard either.
    await _set(db_session, tenant_id, "*", "deny", key="linear/*", url="https://linear.example/mcp")
    linear = agent_rule_lookup(
        db_session_factory,
        tenant_id=tenant_id,
        agent_id="ma-1",
        server_urls={"linear": "https://linear.example/mcp"},
    )
    assert (
        await linear(ToolCall(tool_use_id="x", server_name="linear", tool_name="list_issues"))
        is None
    )
    # An ask or channel wildcard is ignored too.
    other = await _tenant(db_session)
    await _set(db_session, other, "*", "ask")
    await _set(db_session, other, "*", "deny", channel_id="home")
    scoped = agent_rule_lookup(
        db_session_factory,
        tenant_id=other,
        agent_id="ma-1",
        server_urls={_DAIMON: _DAIMON_URL},
        also_urls={_DAIMON: _DAIMON_URL},
        channel_id="home",
    )
    assert (
        await scoped(_proxy("get_session").model_copy(update={"tool_name": "get_session"})) is None
    )


# --- review follow-ups -----------------------------------------------------------


async def _never(_call: ToolCall) -> ResolvedAgentToolRule | None:
    raise AssertionError("this agent has no rule on daimon's tools: nothing should be read")


@pytest.mark.parametrize(
    "inner",
    sorted({*CONFIRMED_DAIMON_TOOLS, *PUBLISH_TOOLS}),
)
async def test_an_approval_tool_sent_through_call_tool_is_refused_at_once(inner: str) -> None:
    prompts: list[ConfirmationPrompt] = []

    async def confirm(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "approved"

    expected = (
        f"Call {inner} directly, not through call_tool: it is listed under its own "
        "name so the person can approve it. Nothing was done."
    )
    assert proxy_refusal(inner) == expected
    for decide in (
        interactive_decider(
            _ON,
            requester_platform_user_id="human",
            confirm=confirm,
            trusted_servers=frozenset({_DAIMON}),
            rules_for=_never,
            daimon_rules=True,
        ),
        unattended_decider(
            _ON, trusted_servers=frozenset({_DAIMON}), rules_for=_never, daimon_rules=True
        ),
    ):
        result = await decide(_proxy(inner, content_hash="abc"))
        assert not result.allow and result.deny_message == expected
    assert prompts == [], "a card here would approve nothing"
    assert proxy_refusal("get_session") is None


async def test_a_direct_call_of_an_approval_tool_still_gets_its_card() -> None:
    prompts: list[ConfirmationPrompt] = []

    async def confirm(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "denied"

    decide = interactive_decider(
        _ON,
        requester_platform_user_id="human",
        confirm=confirm,
        trusted_servers=frozenset({_DAIMON}),
    )
    await decide(ToolCall(tool_use_id="p", server_name=_DAIMON, tool_name="publish_report"))
    assert len(prompts) == 1


async def test_an_agent_without_daimon_rules_costs_no_lookup_and_keeps_every_card() -> None:
    prompts: list[ConfirmationPrompt] = []

    async def confirm(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "approved"

    decide = interactive_decider(
        _ON,
        requester_platform_user_id="human",
        confirm=confirm,
        trusted_servers=frozenset({_DAIMON}),
        confirmed_tools=frozenset({"add_skill"}),
        rules_for=_never,
    )
    assert (await decide(_proxy("get_session"))).allow
    assert (
        await decide(
            ToolCall(
                tool_use_id="a",
                server_name=_DAIMON,
                tool_name="add_skill",
                input={"content_hash": "abc"},
            )
        )
    ).allow
    assert (
        await decide(ToolCall(tool_use_id="p", server_name=_DAIMON, tool_name="publish_report"))
    ).allow
    assert len(prompts) == 2, "add_skill and publish still card; the proxied read does not"
    unattended = unattended_decider(_ON, trusted_servers=frozenset({_DAIMON}), rules_for=_never)
    assert (await unattended(_proxy("get_session"))).allow
    # A third-party server is still looked up.
    looked_up: list[str] = []

    async def lookup(call: ToolCall) -> ResolvedAgentToolRule | None:
        looked_up.append(call.key)
        return None

    await interactive_decider(
        _ON,
        requester_platform_user_id="human",
        trusted_servers=frozenset({_DAIMON}),
        rules_for=lookup,
    )(ToolCall(tool_use_id="x", server_name="linear", tool_name="get_issue"))
    assert looked_up == ["linear/get_issue"]


async def test_a_failed_rule_read_on_a_daimon_call_asks_in_chat_and_refuses_unattended() -> None:
    async def failed(_call: ToolCall) -> ResolvedAgentToolRule | None:
        raise RuntimeError("database down")

    prompts: list[ConfirmationPrompt] = []

    async def confirm(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "denied"

    chat = interactive_decider(
        _ON,
        requester_platform_user_id="human",
        confirm=confirm,
        trusted_servers=frozenset({_DAIMON}),
        rules_for=failed,
        daimon_rules=True,
    )
    routine = unattended_decider(
        _ON, trusted_servers=frozenset({_DAIMON}), rules_for=failed, daimon_rules=True
    )
    assert not (await chat(_proxy("get_session"))).allow
    assert len(prompts) == 1 and prompts[0].tool_rule is None
    assert not (await routine(_proxy("get_session"))).allow


def test_rule_tool_names_must_be_plain_names_or_the_wildcard() -> None:
    assert all(valid_tool_name(n) for n in ("get_session", "a.b-c_1", "*", "x" * 128))
    assert not any(valid_tool_name(n) for n in ("", "x" * 129, "a b", "a/b", "a:b", "é", "a\n"))


async def test_a_rule_on_an_invalid_tool_name_is_refused_by_the_store(
    db_session: AsyncSession,
) -> None:
    tenant_id = await _tenant(db_session)
    for key in ("daimon-mcp/" + "x" * 129, "daimon-mcp/a b", "daimon-mcp/a:b"):
        with pytest.raises(ValueError, match="tool_key must be exactly"):
            await _set(db_session, tenant_id, "", "deny", key=key)
    # A third party's names keep today's shape-only check.
    for key in ("linear/a:b", "linear/a b", "linear/" + "x" * 200):
        await _set(db_session, tenant_id, "", "allow", key=key, url="https://linear.example/mcp")
    await _set(db_session, tenant_id, "x" * 128, "deny")
    await _set(db_session, tenant_id, "*", "deny", agent_id="ma-9")


async def test_a_stored_row_with_a_bad_name_is_skipped_and_logged_not_sent_to_ma(
    db_session: AsyncSession,
) -> None:
    from structlog.testing import capture_logs

    tenant_id = await _tenant(db_session)
    await _set(db_session, tenant_id, "get_session", "deny")
    for bad in ("x" * 129, "a b", "é"):
        db_session.add(
            AgentToolRuleRecord(
                tenant_id=tenant_id,
                agent_id="ma-1",
                agent_name="one",
                tool_key=f"{_DAIMON}/{bad}",
                channel_id=None,
                server_url=_DAIMON_URL,
                rule="deny",
                scope="chat",
                revision=1,
                set_by="legacy",
                set_at=datetime.now(UTC),
            )
        )
    await db_session.flush()
    with capture_logs() as logs:
        ruled = await store.daimon_rule_set(db_session, tenant_id=tenant_id, agent_id="ma-1")
    assert ruled.tools == {"get_session"} and not ruled.needs_driver
    assert len([row for row in logs if row["event"] == "tool_safety.rule_name_skipped"]) == 3


async def test_which_daimon_rules_only_the_driver_can_apply(db_session: AsyncSession) -> None:
    tenant_id = await _tenant(db_session)

    async def needs(agent_id: str) -> bool:
        ruled = await store.daimon_rule_set(db_session, tenant_id=tenant_id, agent_id=agent_id)
        assert ruled.tools
        return ruled.needs_driver

    await _set(db_session, tenant_id, "get_session", "deny", agent_id="wide")
    await _set(db_session, tenant_id, "*", "deny", agent_id="wide")
    assert not await needs("wide"), "the server enforces an agent-wide deny, specific or *"
    await _set(db_session, tenant_id, "get_session", "ask", agent_id="asks")
    assert await needs("asks")
    await _set(db_session, tenant_id, "get_session", "deny", agent_id="chan", channel_id="home")
    assert await needs("chan"), "the server does not know the channel"


async def test_a_channel_rule_on_a_publish_tool_cards_everywhere_and_denies_in_its_channel(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Documented behaviour: a rule on a publish tool turns the tool's own card on session-wide."""
    tenant_id = await _tenant(db_session)
    await _set(db_session, tenant_id, "publish_report", "deny", channel_id="home")
    publish = ToolCall(tool_use_id="p", server_name=_DAIMON, tool_name="publish_report")
    prompts: list[ConfirmationPrompt] = []

    async def confirm(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "approved"

    def decider(channel: str):
        return interactive_decider(
            _ON,
            requester_platform_user_id="human",
            confirm=confirm,
            trusted_servers=frozenset({_DAIMON}),
            rules_for=_lookup(db_session_factory, tenant_id, channel_id=channel),
            daimon_rules=True,
        )

    refused = await decider("home")(publish)
    assert not refused.allow and "denies daimon-mcp/publish_report" in (refused.deny_message or "")
    assert prompts == []
    elsewhere = await decider("elsewhere")(publish)
    assert elsewhere.allow and len(prompts) == 1, "no rule here, but the publish card still shows"
    routine = unattended_decider(
        _ON,
        trusted_servers=frozenset({_DAIMON}),
        rules_for=_lookup(db_session_factory, tenant_id, channel_id="elsewhere"),
        daimon_rules=True,
    )
    assert not (await routine(publish)).allow, "and a routine is refused, as for any publish"
