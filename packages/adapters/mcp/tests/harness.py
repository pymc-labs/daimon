"""MCP-adapter-specific test harness: JWTs, identities, tenant seeding.

Domain-row factories (tenants, accounts) live in `daimon.testing.factories`;
MA shape builders in `daimon.testing.ma_models`; the in-process ASGI harness
in `daimon.testing.asgi`. Don't duplicate them here.
"""

from __future__ import annotations

import datetime as dt
import uuid

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.core.mcp_auth import mint_jwt
from daimon.core.stores.domain import Role
from daimon.testing.asgi import mcp_session
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "make_identity",
    "make_jwt",
    "mcp_session",
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
