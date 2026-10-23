"""MCP gate settings reject delegated identities and stale admin claims."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools import channel_rules
from daimon.core.authz import Subject
from daimon.core.stores.domain import Role
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.parametrize("target,thread", [(None, False), ("123", False), ("456", True)])
@pytest.mark.parametrize(
    "identity", ["member", "stale_admin", "agent_key", "chat_agent", "operator", "legacy"]
)
async def test_unauthorized_mcp_settings_write_nothing(
    monkeypatch: pytest.MonkeyPatch,
    target: str | None,
    thread: bool,
    identity: str,
) -> None:
    runtime = MagicMock()
    runtime.session_factory = MagicMock()
    fresh = AsyncMock(return_value=Subject(platform_user_id="requester"))
    monkeypatch.setattr(channel_rules, "approval_admin", fresh)
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.ADMIN,
        is_admin=identity != "member",
        platform="discord",
        platform_user_id="requester",
        agent_id=uuid.uuid4() if identity == "agent_key" else None,
        chat_agent_id=uuid.uuid4() if identity == "chat_agent" else None,
        token_kind="operator" if identity == "operator" else "cli",
        token_jti=None if identity == "legacy" else uuid.uuid4(),
        scopes=frozenset({"channels:write"}),
    )
    with pytest.raises(ToolError, match="admin"):
        await channel_rules._set_approval_gate_impl(  # pyright: ignore[reportPrivateUsage]
            runtime,
            auth,
            value="off",
            channel_id=target,
            thread=thread,
        )
    runtime.session_factory.assert_not_called()
    if identity in ("member", "stale_admin"):
        assert fresh.await_args.kwargs["tenant_id"] == auth.tenant_id
        assert fresh.await_args.kwargs["user_id"] == "requester"
    else:
        fresh.assert_not_awaited()


@pytest.mark.parametrize(
    "target,thread", [(None, False), ("123456789012345678", False), ("223456789012345678", True)]
)
@pytest.mark.parametrize("enabled", [False, True])
async def test_admin_settings_are_stored_audited_and_report_operator_flag(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    target: str | None,
    thread: bool,
    enabled: bool,
) -> None:
    from daimon.core.stores.access_policy import load_access_policy
    from daimon.core.stores.security_audit import list_events
    from daimon.core.tool_safety import ToolSafetyPolicy
    from daimon.testing.factories import make_tenant

    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
    runtime = MagicMock()
    runtime.session_factory = db_session_factory
    runtime.settings.tool_safety = ToolSafetyPolicy(enabled=True, allow_gate_overrides=enabled)
    monkeypatch.setattr(
        channel_rules,
        "approval_admin",
        AsyncMock(return_value=Subject(is_admin=True, platform_user_id="admin")),
    )
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant.id,
        role=Role.ADMIN,
        platform="discord",
        platform_user_id="admin",
        is_admin=True,
        token_kind="cli",
        token_jti=uuid.uuid4(),
    )
    result = await channel_rules._set_approval_gate_impl(  # pyright: ignore[reportPrivateUsage]
        runtime,
        auth,
        value="off",
        channel_id=target,
        thread=thread,
    )
    assert result["active"] is enabled
    note = result["note"]
    assert isinstance(note, str)
    assert ("ignored" in note) is (not enabled)
    async with db_session_factory() as session:
        policy = await load_access_policy(session, tenant_id=tenant.id)
        events = await list_events(session, tenant_id=tenant.id)
    assert (policy.approvals if target is None else policy.channel_rules[target].approvals) == "off"
    assert len(events) == 1 and events[0].platform_user_id == "admin"
