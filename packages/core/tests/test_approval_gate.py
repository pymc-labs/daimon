"""Authorization boundaries for the optional attended third-party write gate."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
import structlog.testing
from daimon.core.access_policy import Approvals, ChannelRule, TenantAccessPolicy
from daimon.core.approval_gate import ApprovalGate, resolve_approval_gate, set_approval_gate
from daimon.core.authz import Action, Subject, authorize
from daimon.core.errors import DaimonError
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.security_audit import list_events
from daimon.core.tool_safety import (
    CONFIRMED_DAIMON_TOOLS,
    PUBLISH_TOOLS,
    ResolvedAgentToolRule,
    ToolCall,
    ToolSafetyPolicy,
)
from daimon.core.turn.approvals import chat_tool_confirmation, interactive_decider
from daimon.core.turn.posture import PolicyApproval
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ON = ToolSafetyPolicy(enabled=True, allow_gate_overrides=True)
OFF = ApprovalGate("off", "thread:123", "admin-1")
TRUSTED = frozenset({"daimon-mcp"})
WRITE = ToolCall(tool_use_id="t1", server_name="third-party", tool_name="create_issue")


@pytest.mark.parametrize("scope", ["server", "channel:C1", "thread:C1:1.2"])
async def test_off_allows_third_party_write_without_card_and_logs_setter(scope: str) -> None:
    card = AsyncMock(return_value="denied")
    tenant_id = uuid.uuid4()
    with structlog.testing.capture_logs() as logs:
        result = await interactive_decider(
            ON,
            requester_platform_user_id="requester",
            confirm=card,
            approval_gate=ApprovalGate("off", scope, "admin-1", tenant_id),
        )(WRITE)
    assert result.allow
    card.assert_not_awaited()
    event = next(log for log in logs if log["event"] == "tool_safety.gate_override_allowed")
    assert event["set_by"] == "admin-1"
    assert event["tenant_id"] == str(tenant_id)
    assert event["scope"] == scope
    assert event["tool"] == WRITE.key


@pytest.mark.parametrize("tool", sorted(set(CONFIRMED_DAIMON_TOOLS) | set(PUBLISH_TOOLS)))
async def test_every_fixed_daimon_gate_still_asks(tool: str) -> None:
    card = AsyncMock(return_value="denied")
    result = await interactive_decider(
        ON,
        requester_platform_user_id="requester",
        confirm=card,
        trusted_servers=TRUSTED,
        confirmed_tools=frozenset(CONFIRMED_DAIMON_TOOLS),
        approval_gate=OFF,
    )(
        ToolCall(
            tool_use_id="t1",
            server_name="daimon-mcp",
            tool_name=tool,
            input={
                **{CONFIRMED_DAIMON_TOOLS.get(tool, "unused"): "value"},
                **(
                    {
                        "agent_name": "agent",
                        "target_backend": "anthropic",
                        "expected_revision": 1,
                        "preview_digest": "a" * 64,
                    }
                    if tool == "request_agent_backend_switch"
                    else {}
                ),
            },
        )
    )
    assert not result.allow
    card.assert_awaited_once()


async def test_admin_prompt_still_asks() -> None:
    card = AsyncMock(return_value="denied")
    result = await interactive_decider(
        ON,
        requester_platform_user_id="requester",
        confirm=card,
        trusted_servers=TRUSTED,
        confirmed_tools=frozenset({"request_agent_backend_switch"}),
        approval_gate=OFF,
    )(
        ToolCall(
            tool_use_id="t1",
            server_name="daimon-mcp",
            tool_name="request_agent_backend_switch",
            input={
                "agent_name": "agent",
                "target_backend": "anthropic",
                "expected_revision": 1,
                "preview_digest": "a" * 64,
            },
        )
    )
    assert not result.allow
    card.assert_awaited_once()
    assert card.await_args.args[0].admin is True


@pytest.mark.parametrize("value", ["on", "inherit"])
async def test_only_off_bypasses_card(value: Approvals) -> None:
    card = AsyncMock(return_value="denied")
    assert not (
        await interactive_decider(
            ON,
            requester_platform_user_id="requester",
            confirm=card,
            approval_gate=ApprovalGate(value, "server", "admin"),
        )(WRITE)
    ).allow
    card.assert_awaited_once()


async def test_operator_disabled_ignores_stored_off() -> None:
    card = AsyncMock(return_value="denied")
    assert not (
        await interactive_decider(
            ToolSafetyPolicy(enabled=True),
            requester_platform_user_id="requester",
            confirm=card,
            approval_gate=OFF,
        )(WRITE)
    ).allow
    card.assert_awaited_once()


@pytest.mark.parametrize("rule", ["ask", "deny"])
async def test_agent_rule_never_loosened(rule: str) -> None:
    card = AsyncMock(return_value="denied")
    lookup = AsyncMock(return_value=ResolvedAgentToolRule.model_validate({"rule": rule}))
    result = await interactive_decider(
        ON.model_copy(update={"agent_rules": True}),
        requester_platform_user_id="requester",
        confirm=card,
        rules_for=lookup,
        approval_gate=OFF,
    )(WRITE)
    assert not result.allow
    assert card.await_count == (1 if rule == "ask" else 0)


async def test_operator_deny_still_denies_without_a_card() -> None:
    card = AsyncMock(return_value="approved")
    result = await interactive_decider(
        ON.model_copy(update={"denied": (WRITE.key,)}),
        requester_platform_user_id="requester",
        confirm=card,
        approval_gate=OFF,
    )(WRITE)
    assert not result.allow
    card.assert_not_awaited()


async def test_failed_rule_lookup_still_asks() -> None:
    card = AsyncMock(return_value="denied")
    result = await interactive_decider(
        ON.model_copy(update={"agent_rules": True}),
        requester_platform_user_id="requester",
        confirm=card,
        rules_for=AsyncMock(side_effect=RuntimeError("unavailable")),
        approval_gate=OFF,
    )(WRITE)
    assert not result.allow
    card.assert_awaited_once()


@pytest.mark.parametrize("server", [None, "aliased/server"])
async def test_ambiguous_server_does_not_bypass(server: str | None) -> None:
    card = AsyncMock(return_value="denied")
    result = await interactive_decider(
        ON,
        requester_platform_user_id="requester",
        confirm=card,
        approval_gate=OFF,
    )(WRITE.model_copy(update={"server_name": server}))
    if server is not None:
        assert not result.allow
        card.assert_awaited_once()
    else:
        # Nonattached builtins are governed by the existing policy, never an override log.
        assert result.allow
        card.assert_not_awaited()


async def test_off_without_provenance_fails_closed() -> None:
    card = AsyncMock(return_value="denied")
    assert not (
        await interactive_decider(
            ON,
            requester_platform_user_id="requester",
            confirm=card,
            approval_gate=ApprovalGate("off", "server", None),
        )(WRITE)
    ).allow
    card.assert_awaited_once()


@pytest.mark.parametrize("allow", [False, True])
async def test_unattended_still_follows_unattended_writes(allow: bool) -> None:
    card = AsyncMock(return_value="approved")
    posture = chat_tool_confirmation(
        ON.model_copy(update={"unattended_writes": (WRITE.key,) if allow else ()}),
        requester_platform_user_id="requester",
        attended=False,
        confirm=card,
        approval_gate=OFF,
    )
    assert isinstance(posture, PolicyApproval)
    assert (await posture.decide(WRITE)).allow is allow
    card.assert_not_awaited()


@pytest.mark.parametrize("parent,child", [("off", "on"), ("on", "off")])
@pytest.mark.parametrize(
    "platform,key", [("discord", "thread"), ("slack", "C1:1.2"), ("teams", "thread")]
)
def test_most_specific_wins_in_both_directions(
    parent: Approvals, child: Approvals, platform: str, key: str
) -> None:
    thread_id = "1.2" if platform == "slack" else "thread"
    policy = TenantAccessPolicy(
        approvals=parent,
        approvals_set_by="server-admin",
        channel_rules={"C1": ChannelRule(approvals=child, approvals_set_by="channel-admin")},
    )
    assert resolve_approval_gate(
        policy, platform=platform, channel_id="C1", thread_id=thread_id
    ) == ApprovalGate(child, "channel:C1", "channel-admin")
    policy = policy.model_copy(
        update={
            "channel_rules": {
                **policy.channel_rules,
                key: ChannelRule(approvals=parent, approvals_set_by="thread-admin"),
            }
        }
    )
    assert resolve_approval_gate(
        policy, platform=platform, channel_id="C1", thread_id=thread_id
    ) == ApprovalGate(parent, f"thread:{key}", "thread-admin")
    assert (
        resolve_approval_gate(policy, platform=platform, channel_id="other", thread_id=None).value
        == parent
    )


@pytest.mark.parametrize(
    "subject",
    [Subject(), Subject(platform_user_id="requester"), Subject(is_admin=True, via_agent_key=True)],
)
@pytest.mark.parametrize("target", [None, "C1", "C1:1.2"])
async def test_nonadmin_setter_refused_before_io(subject: Subject, target: str | None) -> None:
    factory = AsyncMock()
    assert not authorize(TenantAccessPolicy(), subject=subject, action=Action.SET_APPROVAL_GATE)
    with pytest.raises(DaimonError, match="admin"):
        await set_approval_gate(
            factory,
            tenant_id=uuid.uuid4(),
            platform="slack",
            target=target,
            value="off",
            subject=subject,
        )
    factory.assert_not_called()


async def test_stored_settings_audited_and_tenant_scoped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory.begin() as session:
        first = await make_tenant(session)
        second = await make_tenant(session)
    for target in (None, "C1", "C1:1.2"):
        await set_approval_gate(
            db_session_factory,
            tenant_id=first.id,
            platform="discord",
            target=target,
            value="off",
            subject=Subject(is_admin=True, platform_user_id="admin"),
        )
    async with db_session_factory() as session:
        policy = await load_access_policy(session, tenant_id=first.id)
        other = await load_access_policy(session, tenant_id=second.id)
        events = await list_events(session, tenant_id=first.id)
    assert policy.approvals == "off"
    assert policy.channel_rules["C1"].approvals == "off"
    assert other == TenantAccessPolicy()
    assert len(events) == 3
    assert all(
        event.platform_user_id == "admin" and event.operation == Action.SET_APPROVAL_GATE
        for event in events
    )
    await set_approval_gate(
        db_session_factory,
        tenant_id=first.id,
        platform="discord",
        target="C1",
        value="inherit",
        subject=Subject(is_admin=True, platform_user_id="admin"),
    )
    async with db_session_factory() as session:
        cleared = await load_access_policy(session, tenant_id=first.id)
        events = await list_events(session, tenant_id=first.id)
    assert "C1" not in cleared.channel_rules
    assert len(events) == 4


@pytest.mark.parametrize("tool", sorted(set(CONFIRMED_DAIMON_TOOLS) | set(PUBLISH_TOOLS)))
async def test_proxy_cannot_use_gate_override_to_bypass_fixed_tools(tool: str) -> None:
    card = AsyncMock(return_value="denied")
    result = await interactive_decider(
        ON,
        requester_platform_user_id="requester",
        confirm=card,
        trusted_servers=TRUSTED,
        confirmed_tools=frozenset(CONFIRMED_DAIMON_TOOLS),
        approval_gate=OFF,
    )(
        ToolCall(
            tool_use_id="t1",
            server_name="daimon-mcp",
            tool_name="call_tool",
            input={"name": tool, "arguments": {"source": "test", "content_hash": "test"}},
        )
    )
    # Every fixed-gate tool refuses the proxy before it can reach an override.
    assert not result.allow
    card.assert_not_awaited()


def test_channel_access_edit_keeps_approval_choice() -> None:
    from daimon.core.permissions import with_channel_rule

    policy = TenantAccessPolicy(
        channel_rules={"C1:1.2": ChannelRule(approvals="on", approvals_set_by="admin")}
    )
    from daimon.core.channel_rules import resolve_rule

    changed = with_channel_rule(
        policy,
        "C1:1.2",
        resolve_rule(policy.channel_rules["C1:1.2"], readers="inside", writers=None),
    )
    assert changed.channel_rules["C1:1.2"].approvals == "on"
    assert changed.channel_rules["C1:1.2"].readers == "inside"
