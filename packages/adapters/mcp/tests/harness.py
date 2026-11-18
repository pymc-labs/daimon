"""MCP-adapter-specific test harness: JWTs, identities, tenant seeding.

Domain-row factories (tenants, accounts) live in `daimon.testing.factories`;
MA shape builders in `daimon.testing.ma_models`; the in-process ASGI harness
in `daimon.testing.asgi`. Don't duplicate them here.
"""

from __future__ import annotations

import datetime as dt
import uuid
from contextlib import AbstractAsyncContextManager
from typing import Any

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.middleware.mcp_identity import (
    IdentityMiddleware,
    production_agent_id_resolver,
    production_internal_resolver,
    production_is_admin_resolver,
    production_role_resolver,
    production_subject_resolver,
    production_tenant_resolver,
)
from daimon.core.mcp_auth import mint_jwt, mint_panel_mcp_token
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import AccountRow, Role, TenantRow, TurnOriginRow
from daimon.core.turn_origin import turn_origin
from daimon.testing.asgi import mcp_session
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "auth_identity",
    "discord_turn_origin",
    "make_identity",
    "make_jwt",
    "mcp_session",
    "production_identity_middleware",
    "seed_server_admin",
    "seed_tenant",
    "seed_tenant_and_account",
]


def make_jwt(
    *,
    account_id: uuid.UUID,
    secret: bytes = b"a" * 32,
    now: dt.datetime | None = None,
    is_admin: bool = False,
) -> str:
    return mint_jwt(
        account_id=account_id,
        secret=secret,
        now=now or dt.datetime(2026, 4, 24, tzinfo=dt.UTC),
        is_admin=is_admin,
    )


def make_identity(
    *,
    account_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    role: Role = Role.ADMIN,
) -> AuthIdentity:
    return AuthIdentity(
        account_id=account_id or uuid.uuid4(),
        tenant_id=tenant_id or uuid.uuid4(),
        role=role,
        is_platform_admin=role is Role.ADMIN,
        is_admin=role is Role.ADMIN,
        token_kind="cli",
    )


def auth_identity(
    account_id: uuid.UUID, tenant_id: uuid.UUID, role: Role = Role.ADMIN, **fields: Any
) -> AuthIdentity:
    """An identity for `role`: an admin is also a platform admin unless `fields` say otherwise."""
    admin = role is Role.ADMIN
    defaults: dict[str, Any] = {"is_admin": admin, "is_platform_admin": admin}
    return AuthIdentity(
        account_id=account_id, tenant_id=tenant_id, role=role, **(defaults | fields)
    )


def discord_turn_origin(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant: TenantRow,
    caller: AccountRow,
    *,
    parent_channel_id: str = "C_PARENT",
    thread_id: str = "T_THREAD",
    responder_ma_agent_id: str = "agt_daimon",
    responder_name: str = "daimon",
    role: Role = Role.USER,
    chat_session_id: str | None = "test-session",
    **fields: Any,
) -> AbstractAsyncContextManager[TurnOriginRow]:
    """A committed Discord thread origin for `caller`, removed when the block ends."""
    return turn_origin(
        sessionmaker,
        tenant_id=tenant.id,
        account_id=caller.id,
        platform="discord",
        parent_channel_id=parent_channel_id,
        thread_id=thread_id,
        responder_ma_agent_id=responder_ma_agent_id,
        responder_name=responder_name,
        role=role,
        chat_session_id=chat_session_id,
        sender_foreign=False,
        shared_channel=False,
        **fields,
    )


def production_identity_middleware(
    sessionmaker: async_sessionmaker[AsyncSession], **fields: Any
) -> IdentityMiddleware:
    """The identity middleware with every production resolver."""
    return IdentityMiddleware(
        subject_resolver=production_subject_resolver,
        tenant_resolver=production_tenant_resolver,
        role_resolver=production_role_resolver,
        agent_id_resolver=production_agent_id_resolver,
        is_admin_resolver=production_is_admin_resolver,
        internal_resolver=production_internal_resolver,
        sessionmaker=sessionmaker,
        **fields,
    )


async def seed_tenant(session: AsyncSession, *, workspace_id: str | None = None) -> uuid.UUID:
    """Insert a Tenant row and return its id."""
    tenant = await make_tenant(
        session, platform="discord", workspace_id=workspace_id or str(uuid.uuid4())
    )
    return tenant.id


async def seed_tenant_and_account(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a Tenant + Account and return (tenant_id, account_id)."""
    tenant = await make_tenant(session, platform="discord")
    account = await make_account(session, tenant=tenant)
    return tenant.id, account.id


async def seed_server_admin(
    session: AsyncSession, *, platform_user_id: str | None = "u-admin"
) -> tuple[uuid.UUID, uuid.UUID]:
    """A Discord tenant and an account with a stored admin role: (tenant_id, account_id).

    ``platform_user_id=None`` leaves the account without a platform user.
    """
    tenant = await make_tenant(session, platform="discord")
    account = await make_account(session, tenant=tenant)
    await set_role(session, account.id, Role.ADMIN)
    if platform_user_id is not None:
        await make_platform_principal(
            session,
            platform="discord",
            external_id=platform_user_id,
            tenant=tenant,
            account=account,
        )
    return tenant.id, account.id


async def registered_account_token(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    account_id: uuid.UUID,
    secret: bytes = b"a" * 32,
) -> str:
    """The production registered checkout identity, committed before HTTP admission."""
    async with sessionmaker.begin() as session:
        return await mint_panel_mcp_token(
            session,
            account_id=account_id,
            secret=secret,
            now=dt.datetime.now(dt.UTC),
        )
