"""What the holder of an agent owner's coding key can reach, and whom they act as.

The key's subject is the owner who made it. The holder sees only the agent-chat
tools, and a session the key starts runs on the owner's own chat credential: the
owner's ordinary member powers, never an admin's.
"""

from __future__ import annotations

import datetime as dt
import uuid

from daimon.adapters.mcp.auth.verifier import DaimonJWTVerifier
from daimon.core.mcp_auth import mint_agent_mcp_token, mint_chat_mcp_token
from daimon.core.stores.accounts import set_role
from daimon.core.stores.agent_owners import set_owner
from daimon.core.stores.domain import Role
from daimon.core.stores.tenants import get_tenant
from daimon.testing.asgi import mcp_session
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_chat_session_tool_reachability import SECRET, _build_client, _make_app

AGENT_CHAT_TOOLS = {
    "ask",
    "deliver_turn_charts",
    "describe_agent",
    "list_my_sessions",
    "start_turn",
    "continue_turn",
    "get_my_session",
    "list_events",
    "archive_my_session",
    "cancel_turn",
    "get_turn_cost",
    "self_write_file",
    "self_read_file",
    "self_list_files",
    "self_delete_file",
    "list_credentials",
}


async def _owner_key(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[str, uuid.UUID, uuid.UUID, uuid.UUID]:
    """An owner's registered coding key: (token, tenant, owner account, agent)."""
    agent_id = uuid.uuid4()
    async with sessionmaker.begin() as s:
        tenant = await make_tenant(s, platform="discord", workspace_id="owner-coding-key")
        owner = await make_account(s, tenant=tenant)
        await make_platform_principal(
            s, platform="discord", external_id="u-owner", tenant=tenant, account=owner
        )
        await set_owner(
            s,
            tenant_id=tenant.id,
            agent_id=agent_id,
            owner_account_id=owner.id,
            source="admin_assigned",
        )
        token = await mint_agent_mcp_token(
            s,
            account_id=owner.id,
            tenant_id=tenant.id,
            agent_id=agent_id,
            label="agent",
            secret=SECRET.encode(),
            now=dt.datetime.now(dt.UTC),
            ttl_days=30,
        )
    return token, tenant.id, owner.id, agent_id


async def test_the_holder_of_an_owners_key_sees_only_the_agent_chat_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    token, *_ = await _owner_key(sessionmaker)

    result = await mcp_session(
        _make_app(sessionmaker, _build_client()), token=token, method="tools/list"
    )

    payload = result.get("result", result)
    assert {t["name"] for t in payload.get("tools", [])} == AGENT_CHAT_TOOLS  # type: ignore[union-attr]


async def test_an_owners_key_acts_as_the_owner_who_is_not_an_admin(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Subject, role and billing identity are the owner's own: a member, never an admin."""
    token, tenant_id, owner_id, _ = await _owner_key(sessionmaker)

    access = await DaimonJWTVerifier(
        secret=SECRET.encode(), sessionmaker=sessionmaker
    ).verify_token(token)

    assert access is not None
    assert access.claims["sub"] == str(owner_id), "the key's account is the owner, who made it"
    assert access.claims["tenant_id"] == str(tenant_id)
    assert access.claims["role"] == "user", "an owner is a member, not an admin"
    assert access.claims["platform_user_id"] == "u-owner", (
        "turns are billed and capped against the owner's platform user"
    )


async def _discovered(
    sessionmaker: async_sessionmaker[AsyncSession], *, account_id: uuid.UUID, agent_id: uuid.UUID
) -> str:
    """What `search_tools` lists to an in-session chat credential for this account."""
    async with sessionmaker.begin() as s:
        chat_token, _, _ = await mint_chat_mcp_token(
            s,
            account_id=account_id,
            agent_id=agent_id,
            secret=SECRET.encode(),
            now=dt.datetime.now(dt.UTC),
        )
    result = await mcp_session(
        _make_app(sessionmaker, _build_client()),
        token=chat_token,
        method="tools/call",
        params={"name": "search_tools", "arguments": {"query": "sync skills archive agent"}},
    )
    return " ".join(
        item.get("text", "") for item in result.get("result", result).get("content", [])
    )


async def test_the_session_a_key_starts_runs_on_the_owners_member_credential(
    sessionmaker: async_sessionmaker[AsyncSession], platform_admin_edge: None
) -> None:
    """The in-session credential is the owner's chat credential: no admin-only tool."""
    _token, tenant_id, owner_id, agent_id = await _owner_key(sessionmaker)
    async with sessionmaker.begin() as s:
        tenant = await get_tenant(s, tenant_id)
        admin = await make_account(s, tenant=tenant)
        await make_platform_principal(
            s, platform="discord", external_id="u-admin", tenant=tenant, account=admin
        )
        await set_role(s, admin.id, Role.ADMIN)

    as_owner = await _discovered(sessionmaker, account_id=owner_id, agent_id=agent_id)
    as_admin = await _discovered(sessionmaker, account_id=admin.id, agent_id=agent_id)

    assert "### sync_skills" in as_admin, "the search does surface admin tools to an admin"
    assert "### sync_skills" not in as_owner, "and not to the owner"
    assert "### archive_agent" not in as_owner
